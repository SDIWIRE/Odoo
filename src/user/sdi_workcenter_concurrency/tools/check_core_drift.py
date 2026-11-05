#!/usr/bin/env python3
"""Detect drift between our copied override and the current Odoo core method.

models/mrp_workcenter.py contains a FULL OVERRIDE of core's
mrp.workcenter._get_first_available_slot, copied from core with only the
busy-interval definition changed. When Odoo changes that method upstream our
copy silently keeps the old behaviour.

That has already caused a production-visible bug once: core moved from
`interval & workorder_intervals` to `workorder_intervals.conflicting(interval)`
and started clamping with `min(max(conflict_stop ...), stop)`. Our stale copy
had no clamp, so `start` could be taken from a leave interval and escape the
working day, scheduling work on nights and weekends.

Run this after every Odoo version/point-release upgrade.

    python3 tools/check_core_drift.py            # compares against branch 19.0
    python3 tools/check_core_drift.py --ref 20.0

Exit code 0 = only the expected differences. 1 = unexpected drift, read the diff.
"""
import argparse
import difflib
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OURS = os.path.join(HERE, os.pardir, 'models', 'mrp_workcenter.py')
CORE_URL = ("https://raw.githubusercontent.com/odoo/odoo/{ref}/"
            "addons/mrp/models/mrp_workcenter.py")
METHOD = '_get_first_available_slot'

# Lines that are SUPPOSED to differ. Anything else is drift worth a human look.
EXPECTED = (
    'concurrent_capacity',
    'super()._get_first_available_slot',
    'start_datetime, duration, forward=forward',
    'leaves_to_ignore=leaves_to_ignore, extra_leaves_slots=extra_leaves_slots)',
    'get_overcapacity_intervals',
    'get_workorder_intervals',
    'workorder_intervals_leaves_domain',
    # core builds a leaves domain here; we handle leaves_to_ignore inside
    # _get_overcapacity_intervals instead
    'if leaves_to_ignore:',
    'extra_leaves_slots_intervals = Intervals(',
    # core's docstring, which we replace with our own
    'The available interval is disjoinct',
    'can overlap the time-off',
    'Return the first available interval',
    'if there is none before 700 days',
)


def extract(text, name):
    out, on, indent = [], False, None
    for line in text.split('\n'):
        if re.match(r'\s*def %s\(' % name, line):
            on, indent = True, len(line) - len(line.lstrip())
            out.append(line)
            continue
        if on:
            stripped = line.strip()
            if stripped and (len(line) - len(line.lstrip())) <= indent \
                    and not stripped.startswith(('#', '"')):
                break
            out.append(line)
    return [l.strip() for l in out
            if l.strip() and not l.strip().startswith(('#', '"""', ':'))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ref', default='19.0', help='Odoo git branch or tag')
    args = ap.parse_args()

    url = CORE_URL.format(ref=args.ref)
    try:
        core_src = urllib.request.urlopen(url, timeout=30).read().decode()
    except Exception as exc:
        sys.exit("could not fetch core from %s (%s)" % (url, exc))
    if 'class MrpWorkcenter' not in core_src:
        sys.exit("unexpected content from %s (rate limited?) - retry" % url)

    core = extract(core_src, METHOD)
    ours = extract(open(OURS).read(), METHOD)
    if not core or not ours:
        sys.exit("could not locate %s in core and/or our module" % METHOD)

    changed = [l for l in difflib.unified_diff(core, ours, lineterm='', n=0)
               if l.startswith(('+', '-')) and not l.startswith(('+++', '---'))]
    unexpected = [l for l in changed
                  if not any(tok in l for tok in EXPECTED)]

    print("core (%s): %d lines   ours: %d lines" % (args.ref, len(core), len(ours)))
    if not unexpected:
        print("OK - only the expected differences.")
        return 0
    print("\nUNEXPECTED DRIFT - core changed underneath this override:\n")
    print('\n'.join(unexpected))
    print("\nReview core's current %s and port the change across." % METHOD)
    return 1


if __name__ == '__main__':
    sys.exit(main())
