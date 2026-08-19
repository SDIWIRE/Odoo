#!/usr/bin/env python3
"""Verify the concurrency occupancy math without needing an Odoo runtime.

This does NOT reimplement the algorithm. It stubs out the small part of the odoo
package that models/mrp_workcenter.py imports, loads the REAL
_get_overcapacity_intervals from it, and runs that against Odoo's REAL
(dependency-free) Intervals class. A logic error in the shipped code shows up here.

Usage:
    python3 tools/verify_sweepline.py

Requires: pytz, plus network access on first run to fetch odoo/tools/intervals.py
from the public Odoo repo (cached next to this script afterwards).
"""
import importlib.util
import os
import sys
import types
import urllib.request
from datetime import datetime, timedelta

try:
    from pytz import UTC
except ImportError:
    sys.exit("pytz is required:  python3 -m pip install pytz")

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.join(HERE, os.pardir, 'models', 'mrp_workcenter.py')
INTERVALS = os.path.join(HERE, 'intervals.py')
INTERVALS_URL = 'https://raw.githubusercontent.com/odoo/odoo/19.0/odoo/tools/intervals.py'


def load_real_intervals():
    if not os.path.exists(INTERVALS):
        print(f"fetching {INTERVALS_URL}")
        try:
            with urllib.request.urlopen(INTERVALS_URL, timeout=30) as r:
                data = r.read()
        except Exception as exc:
            sys.exit(f"could not fetch intervals.py ({exc}).\n"
                     f"Download it manually to {INTERVALS} and re-run.")
        if b'class Intervals' not in data:
            sys.exit(f"unexpected content from {INTERVALS_URL} (rate limited?). Retry.")
        with open(INTERVALS, 'wb') as fh:
            fh.write(data)
    spec = importlib.util.spec_from_file_location("real_intervals", INTERVALS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Intervals


def install_odoo_stubs(Intervals):
    """Minimal fake odoo package: just enough for the module to import."""
    class _Field:
        def __init__(self, *a, **k):
            pass

    fields_mod = types.ModuleType("odoo.fields")
    fields_mod.Integer = lambda *a, **k: _Field()

    models_mod = types.ModuleType("odoo.models")
    models_mod.Model = type("Model", (), {})
    models_mod.AbstractModel = models_mod.Model
    models_mod.Constraint = type("Constraint", (), {"__init__": lambda self, *a, **k: None})

    odoo = types.ModuleType("odoo")
    odoo.fields = fields_mod
    odoo.models = models_mod
    odoo._ = lambda s, *a: s

    date_utils = types.ModuleType("odoo.tools.date_utils")
    date_utils.localized = lambda dt: dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    date_utils.to_timezone = lambda tz: (
        (lambda dt: dt.astimezone(UTC).replace(tzinfo=None)) if tz is None
        else (lambda dt: dt.astimezone(tz))
    )
    float_utils = types.ModuleType("odoo.tools.float_utils")
    float_utils.float_compare = lambda a, b, precision_digits=3: (a > b) - (a < b)
    intervals_mod = types.ModuleType("odoo.tools.intervals")
    intervals_mod.Intervals = Intervals
    tools = types.ModuleType("odoo.tools")
    tools.date_utils, tools.float_utils, tools.intervals = date_utils, float_utils, intervals_mod
    odoo.tools = tools

    for name, mod in [("odoo", odoo), ("odoo.fields", fields_mod),
                      ("odoo.models", models_mod), ("odoo.tools", tools),
                      ("odoo.tools.date_utils", date_utils),
                      ("odoo.tools.float_utils", float_utils),
                      ("odoo.tools.intervals", intervals_mod)]:
        sys.modules[name] = mod


def load_method_under_test():
    spec = importlib.util.spec_from_file_location("mod_under_test", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MrpWorkcenter._get_overcapacity_intervals


# ---------------------------------------------------------------- fake ORM ---
class FakeLeave:
    _next = 100

    def __init__(self, date_from, date_to, resource_id=1, is_workorder=True):
        FakeLeave._next += 1
        self.id = FakeLeave._next
        self.date_from = date_from   # naive UTC, as Odoo stores it
        self.date_to = date_to
        self.resource_id = resource_id
        self.is_workorder = is_workorder


class FakeRecordset(list):
    @property
    def ids(self):
        return [x.id for x in self]


class FakeEnv:
    def __init__(self, leaves):
        self._leaves = leaves

    def __getitem__(self, model):
        env = self

        class _M:
            def search(self, domain):
                if model == 'resource.calendar.leaves':
                    return FakeRecordset(env._leaves)
                if model == 'mrp.workorder':
                    wos = FakeRecordset([l for l in env._leaves if l.is_workorder])
                    return types.SimpleNamespace(leave_id=FakeRecordset(wos))
                raise KeyError(model)

            def sudo(self):
                return self

            def __bool__(self):
                return False  # empty recordset -> Intervals payload
        return _M()


class FakeWorkcenter:
    def __init__(self, capacity, leaves):
        self.concurrent_capacity = capacity
        self.resource_id = types.SimpleNamespace(id=1)
        self.resource_calendar_id = types.SimpleNamespace(id=7, tz='UTC')
        self.env = FakeEnv(leaves)

    def ensure_one(self):
        return self


# -------------------------------------------------------------------- cases ---
def main():
    Intervals = load_real_intervals()
    install_odoo_stubs(Intervals)
    method = load_method_under_test()
    print(f"loaded real method: {method.__qualname__}\n")

    D = lambda h, m=0: datetime(2035, 1, 1, h, m)
    win_start = datetime(2035, 1, 1, 0, 0, tzinfo=UTC)
    win_stop = datetime(2035, 1, 1, 23, 59, tzinfo=UTC)

    results = []

    def run(name, capacity, leaves, expect):
        out = method(FakeWorkcenter(capacity, leaves), win_start, win_stop)
        got = [(s.strftime('%H:%M'), e.strftime('%H:%M')) for s, e, _ in out[1]]
        ok = got == expect
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print(f"      expected {expect}\n      got      {got}")
        results.append(ok)

    # room left under capacity -> nothing blocked
    run("cap5, 3 overlapping -> no block", 5,
        [FakeLeave(D(8), D(10)) for _ in range(3)], [])
    # exactly at capacity -> window is full
    run("cap5, 5 overlapping -> blocked 08-10", 5,
        [FakeLeave(D(8), D(10)) for _ in range(5)], [('08:00', '10:00')])
    # staggered chain: 09-10 has {8-10,9-11}, 10-11 has {9-11,10-12} -> both full
    run("cap2, staggered chain -> blocked 09-11", 2,
        [FakeLeave(D(8), D(10)), FakeLeave(D(9), D(11)), FakeLeave(D(10), D(12))],
        [('09:00', '11:00')])
    run("cap3, same chain -> no block", 3,
        [FakeLeave(D(8), D(10)), FakeLeave(D(9), D(11)), FakeLeave(D(10), D(12))], [])
    # the running count must come back down once a cluster ends
    run("cap3, count drops after cluster ends", 3,
        [FakeLeave(D(8), D(9)), FakeLeave(D(8), D(9)), FakeLeave(D(8), D(11))],
        [('08:00', '09:00')])
    run("cap2, two disjoint clusters stay separate", 2,
        [FakeLeave(D(8), D(9)), FakeLeave(D(8), D(9)),
         FakeLeave(D(14), D(15)), FakeLeave(D(14), D(15))],
        [('08:00', '09:00'), ('14:00', '15:00')])
    run("cap2, abutting pairs -> merged 08-10", 2,
        [FakeLeave(D(8), D(9)), FakeLeave(D(8), D(9)),
         FakeLeave(D(9), D(10)), FakeLeave(D(9), D(10))],
        [('08:00', '10:00')])
    # a non-work-order leave (e.g. maintenance downtime) blocks fully at any capacity
    run("cap5, 1 non-workorder leave -> full block", 5,
        [FakeLeave(D(13), D(14), is_workorder=False)], [('13:00', '14:00')])
    run("cap2, full block + at-capacity cluster", 2,
        [FakeLeave(D(6), D(7), is_workorder=False),
         FakeLeave(D(8), D(9)), FakeLeave(D(8), D(9))],
        [('06:00', '07:00'), ('08:00', '09:00')])
    # leaves starting before the window are clipped but still counted inside it
    run("cap2, leaves clipped to window", 2,
        [FakeLeave(D(0) - timedelta(hours=5), D(12)),
         FakeLeave(D(0) - timedelta(hours=5), D(12))], [('00:00', '12:00')])
    run("cap3, nested -> blocked 10-10:30", 3,
        [FakeLeave(D(8), D(12)), FakeLeave(D(9), D(11)), FakeLeave(D(10), D(10, 30))],
        [('10:00', '10:30')])
    run("no leaves -> empty", 5, [], [])
    run("zero-length leaves ignored", 2,
        [FakeLeave(D(9), D(9)), FakeLeave(D(9), D(9))], [])

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
