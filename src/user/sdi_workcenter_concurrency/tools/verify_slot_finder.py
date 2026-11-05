#!/usr/bin/env python3
"""End-to-end check of _get_first_available_slot without an Odoo runtime.

Loads the REAL method from models/mrp_workcenter.py, stubs Odoo, and drives it
with a faithful Mon-Fri 08:00-12:00 / 13:00-17:00 America/Chicago calendar and
synthetic occupancy. Complements verify_sweepline.py, which covers only the
occupancy maths.

Case 1 is the regression that caused production work to be scheduled on a
Saturday night. `interval` starts at the accumulated `start_interval`, which can
be days earlier than the interval being examined, so `conflicting()` can return a
block that ended before this interval began. Without clamping forward
(`max(..., start)`) the walk sets `start` to that past block's end and returns a
slot there. Core gets away with no forward clamp because its conflict set (every
leave) is dense; our over-capacity set is sparse.

    python3 tools/verify_slot_finder.py

Requires pytz, and network on first run to fetch Odoo's intervals.py (cached).
"""
import importlib.util
import os
import sys
import types
import urllib.request
from datetime import datetime, timedelta

try:
    from pytz import UTC, timezone
except ImportError:
    sys.exit("pytz is required:  python3 -m pip install pytz")

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, os.pardir, 'models', 'mrp_workcenter.py')
INTERVALS = os.path.join(HERE, 'intervals.py')
INTERVALS_URL = 'https://raw.githubusercontent.com/odoo/odoo/19.0/odoo/tools/intervals.py'
CHI = timezone('America/Chicago')


def load_intervals():
    if not os.path.exists(INTERVALS):
        try:
            with urllib.request.urlopen(INTERVALS_URL, timeout=30) as r:
                data = r.read()
        except Exception as exc:
            sys.exit("could not fetch intervals.py (%s)" % exc)
        if b'class Intervals' not in data:
            sys.exit("unexpected content from %s (rate limited?)" % INTERVALS_URL)
        open(INTERVALS, 'wb').write(data)
    spec = importlib.util.spec_from_file_location("ri", INTERVALS)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.Intervals


def stub_odoo(Intervals):
    fields_mod = types.ModuleType("odoo.fields")
    fields_mod.Integer = lambda *a, **k: None
    models_mod = types.ModuleType("odoo.models")
    models_mod.Model = type("Model", (), {})
    models_mod.AbstractModel = models_mod.Model
    models_mod.Constraint = type("Constraint", (), {"__init__": lambda s, *a, **k: None})
    odoo = types.ModuleType("odoo")
    odoo.fields, odoo.models = fields_mod, models_mod
    odoo._ = lambda s, *a: s
    du = types.ModuleType("odoo.tools.date_utils")
    du.localized = lambda dt: dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    du.to_timezone = lambda tz: ((lambda d: d.astimezone(UTC).replace(tzinfo=None))
                                 if tz is None else (lambda d: d.astimezone(tz)))
    fu = types.ModuleType("odoo.tools.float_utils")
    fu.float_compare = lambda a, b, precision_digits=3: (a > b) - (a < b)
    iv = types.ModuleType("odoo.tools.intervals")
    iv.Intervals = Intervals
    tools = types.ModuleType("odoo.tools")
    tools.date_utils, tools.float_utils, tools.intervals = du, fu, iv
    odoo.tools = tools
    for n, m in [("odoo", odoo), ("odoo.fields", fields_mod), ("odoo.models", models_mod),
                 ("odoo.tools", tools), ("odoo.tools.date_utils", du),
                 ("odoo.tools.float_utils", fu), ("odoo.tools.intervals", iv)]:
        sys.modules[n] = m


def load_module():
    spec = importlib.util.spec_from_file_location("mut", MODULE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.MrpWorkcenter


class Leave:
    _n = 0

    def __init__(self, a, b):
        Leave._n += 1
        self.id = Leave._n
        self.date_from = datetime.strptime(a, "%Y-%m-%d %H:%M")
        self.date_to = datetime.strptime(b, "%Y-%m-%d %H:%M")
        self.resource_id = 10


class RS(list):
    @property
    def ids(self):
        return [x.id for x in self]


def build(Intervals, MWC, leaves, capacity):
    class Calendar:
        id, tz = 1, 'America/Chicago'

        def _work_intervals_batch(self, start_dt, end_dt, resources=None, domain=None, tz=None):
            out = []
            day = start_dt.astimezone(CHI).replace(hour=0, minute=0, second=0, microsecond=0)
            end = end_dt.astimezone(CHI)
            while day <= end:
                if day.weekday() < 5:
                    for h1, h2 in ((8, 12), (13, 17)):
                        s = CHI.localize(datetime(day.year, day.month, day.day, h1))
                        e = CHI.localize(datetime(day.year, day.month, day.day, h2))
                        s = max(s, start_dt.astimezone(CHI))
                        e = min(e, end_dt.astimezone(CHI))
                        if s < e:
                            out.append((s, e, RS()))
                day += timedelta(days=1)
            return {10: Intervals(out)}

    class Env:
        def __getitem__(self, model):
            class M:
                def search(inner, domain):
                    if model == 'resource.calendar.leaves':
                        return RS(leaves)
                    if model == 'mrp.workorder':
                        return types.SimpleNamespace(leave_id=RS(leaves))
                    raise KeyError(model)

                def sudo(inner):
                    return inner

                def get_param(inner, *a):
                    return '50'

                def __bool__(inner):
                    return False
            return M()

    class WC:
        concurrent_capacity = capacity

        def __init__(self):
            self.resource_id = types.SimpleNamespace(id=10)
            self.resource_calendar_id = Calendar()
            self.env = Env()

        def ensure_one(self):
            return self
        _workorder_leave_ids = MWC._workorder_leave_ids
        _get_overcapacity_intervals = MWC._get_overcapacity_intervals
        _get_first_available_slot = MWC._get_first_available_slot
    return WC()


def main():
    Intervals = load_intervals()
    stub_odoo(Intervals)
    MWC = load_module()
    chi = lambda d: d.astimezone(CHI).strftime('%a %d %b %H:%M')
    results = []

    def case(name, leaves, capacity, start, minutes, expect_start, expect_gaps):
        wc = build(Intervals, MWC, leaves, capacity)
        got = wc._get_first_available_slot(start, float(minutes))
        if not got[0]:
            print("FAIL  %s -> no slot (%s)" % (name, got[1]))
            results.append(False)
            return
        span = (got[1] - got[0]).total_seconds() / 60
        has_gaps = abs(span - minutes) > 2
        s = chi(got[0])
        ok = s == expect_start and has_gaps == expect_gaps
        print("%s  %s" % ('PASS' if ok else 'FAIL', name))
        print("        start %s   span %.0f min for %.0f min of work" % (s, span, minutes))
        if not ok:
            print("        expected start %s, gaps=%s" % (expect_start, expect_gaps))
        results.append(ok)

    # 1. REGRESSION: a past over-capacity block must not drag the slot backwards.
    #    Three overlapping jobs saturate capacity 3 on Sat 21 Nov 16:22-21:42.
    #    Searching from Wed 18 Nov, the answer must be Mon 23 Nov, never Saturday.
    sat = [Leave("2026-11-21 22:22", "2026-11-22 03:42") for _ in range(3)]
    busy = [Leave("2026-11-18 14:00", "2026-11-21 00:00") for _ in range(3)]
    case("past over-capacity block must not pull the slot into the weekend",
         sat + busy, 3, UTC.localize(datetime(2026, 11, 18, 17, 38)), 1000,
         "Mon 23 Nov 08:00", True)

    # 2. empty station: must USE the rest of the current morning (11:38-12:00),
    #    then carry on after lunch - so 90 min of work spans 150 min of clock.
    case("empty station, 90 min -> starts immediately, spans the lunch break",
         [], 3, UTC.localize(datetime(2026, 11, 18, 17, 38)), 90,
         "Wed 18 Nov 11:38", True)

    # 3. morning saturated -> first free working time is 13:00, not 12:00 (lunch)
    block = [Leave("2026-11-19 14:00", "2026-11-19 18:00") for _ in range(3)]
    case("saturated Thu morning -> pushed to Thu 13:00, after lunch",
         block, 3, UTC.localize(datetime(2026, 11, 19, 14, 0)), 60,
         "Thu 19 Nov 13:00", False)

    # 4. under capacity -> overlap allowed, no delay
    case("2 jobs at capacity 3 -> may overlap, no delay",
         [Leave("2026-11-19 14:00", "2026-11-19 18:00") for _ in range(2)], 3,
         UTC.localize(datetime(2026, 11, 19, 14, 0)), 60,
         "Thu 19 Nov 08:00", False)

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
