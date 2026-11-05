# Changelog

## 19.0.1.0.3 — hotfix (not yet deployed)

### FIX (critical): work scheduled on nights and weekends

`models/mrp_workcenter.py` — `_get_first_available_slot`

**Symptom.** Replanned work orders on multi-operator stations landed outside
working hours: BRAIDING and TESTING jobs chained back-to-back through Sunday
night (11:26 → 17:33 → 22:40 → 23:12 → 01:19), each one's elapsed span exactly
equal to its duration with no calendar gaps. Proven to be this module and not
core by A/B on one work order: at `concurrent_capacity` 3 it was placed Sun
08 Nov 22:40; at `concurrent_capacity` 1 (which delegates to `super()`) core
placed the same work order Tue 17 Nov 09:35.

**Cause.** This method is a full copy of core's, and **core changed underneath
it**. Current core detects conflicts with `workorder_intervals.conflicting(interval)`
and advances with `start = min(max(conflict_stop ...), stop)`. The copy in this
module was taken from an earlier revision that used `interval & workorder_intervals`
and `start = conflict._items[0]`.

The missing piece is the `min(..., stop)` clamp. `stop` is the end of the
current *working-hours* interval. Without it, `start` is assigned a timestamp
taken from a leave interval, and leaves routinely span nights and weekends, so
`start` escaped the working day and the returned slot went with it. Core's
clamp keeps `start` inside the available interval.

**Fix.** Both the forward and backward branches now match core's current
implementation exactly. A re-diff confirms the only remaining differences are
the three intended ones: the `concurrent_capacity <= 1` guard, the substitution
of `_get_overcapacity_intervals` for core's leave lookup, and the records
payload on `extra_leaves_slots_intervals`.

**Also added:** `tools/check_core_drift.py`, which diffs this override against
core's current source and exits non-zero on unexpected drift. This class of bug
has now occurred once; run the tool after every Odoo upgrade. It is what found
this one.

### Data impact

Any work order planned by `_get_first_available_slot` while 19.0.1.0.2 was
installed may carry night or weekend dates and should be replanned once this
version is live. On staging that is the 104 work orders replanned in the first
batch of the not-started backfill.

## 19.0.1.0.2 — superseded by 19.0.1.0.3

Fixes work orders being scheduled across weeks or months instead of days.
Diagnosed from production data on 2026-11-05 (MO/02316 and 79 others).

### FIX (critical): started work orders smeared across weeks

`models/mrp_workorder.py` — new override of `_calculate_date_finished`

**Symptom.** Work orders occupying absurd windows on Work Center Planning.
Measured on production: 80 of 652 open work orders spanned more than 5 days.
Worst case `WH/MO/02290` ASSEMBLY — **54.67 minutes of work holding a 65-day
window**. Every one of the ten worst cases was in state `progress`, i.e. had
been started. Work centers left at 1 operator were essentially unaffected,
which is what localised the cause.

**Cause.** Core's `_calculate_date_finished` answers "start at T, finish when?"
by walking the work center calendar and skipping unavailable time, with
`domain = [('time_type', 'in', ['leave', 'other'])]` and
`resource=workcenter.resource_id`. Every *other* work order's calendar block is
`time_type='other'` on that resource, so core treats a colleague's job as
station downtime. That is right for a single-lane machine and wrong for a
multi-operator bench — and this module deliberately creates up to N overlapping
blocks per work center, which core never expected.

It then compounds. `button_start` calls `_calculate_date_finished`, so each
started job skips over every concurrent job's window, lands far in the future,
and writes its own leave that wide. The next job skips over *that*, and so on.
Once N such windows overlap, `_get_first_available_slot` also sees the station
as full and pushes new work out too. A runaway feedback loop, which is why the
spans cluster around 50-65 days rather than being merely somewhat long.

**Fix.** For work centers with concurrent operators, exclude work-order
occupancy leaves from the unavailable-time domain. Genuine closures
(`time_type='leave'`) and hard blocks such as maintenance downtime still count.
Whether a free lane exists at all is a scheduling question and stays with
`_get_first_available_slot` — deciding it inside a duration calculation is what
caused the smearing. Work centers at the default of 1 operator fall through to
`super()` and keep core behavior exactly.

`_calculate_duration_expected` was checked and is **not** affected: it calls
`get_work_duration_data` without a `resource`, so it only ever sees
company-wide leaves.

**Tests added.** `test_date_finished_ignores_parallel_operators` (a 10-day
parallel block must not move a 1-hour job's finish date) and
`test_date_finished_single_lane_unchanged` (at capacity 1 the work order must
still queue behind a blocking job).

### Refactor

`_workorder_leave_ids` / `_get_lane_occupancy_leave_ids` factored onto
`mrp.workcenter` so the "is this leave an occupied lane or a hard block"
classification has one definition shared by the scheduler and the date
calculation. `tools/verify_sweepline.py` now exercises the real helper;
still 13/13.

### Data repair required after deploying

The fix corrects new calculations; it does not retroactively fix the 80 work
orders already carrying bad dates. Those need re-planning — see README
"Repairing existing smeared work orders".

## 19.0.1.0.1 — hotfix (superseded by 19.0.1.0.2, not deployed separately)

Fixes the production errors reported 2026-08-14 and 2026-08-15. Two source files
changed; no data migration, no reinstall needed, existing "Concurrent Operators"
values are preserved. The manifest version is bumped so Odoo.sh performs a module
update on the next build (the changes are Python-only, so a server restart alone
would also pick them up).

### 1. FIX (critical): `KeyError` broke opening/editing manufacturing orders

`models/mrp_workorder.py` — `_get_conflicted_workorder_ids`

**Symptom.** `RPC_ERROR` / `KeyError: <id>` on `mrp.production`, raised through
`mrp_workorder._compute_json_popover`. Users could not add work orders to
existing MOs. Two production occurrences, different work order ids (10904,
15505), one on form load and one after an edit.

**Cause.** The override built its capacity lookup keyed on `wo.id`. Inside an
onchange, records are virtual: `wo.id` is a transient `NewId` object while
`recordset.ids` — which is what core's SQL uses and therefore what core's result
dict is keyed by — resolves to the real database id. So the lookup contained only
`NewId` keys, matched nothing, and raised on the first real id core returned.

**Trigger condition.** Any work center with two or more overlapping ready/blocked
work orders. That is the normal state once Concurrent Operators > 1, so the crash
was effectively guaranteed as soon as the feature was used. Note it fires even
when the cluster is *within* capacity, because the dict comprehension evaluates
the lookup for every key core returns before any filtering happens.

**Fix.** Key the map on the real id (`wo._origin.id or wo.id`), and read it with
`.get(wo_id, 1)` rather than a bare subscript, so an unmappable id degrades to
core behavior (show the warning) instead of raising. `KeyError` from this line is
now structurally impossible. Also early-returns when core reports no conflicts.

**Test added.** `test_conflicted_workorder_ids_with_virtual_onchange_records`
reproduces the real/virtual id split using `.new(origin=...)` records and asserts
both no-raise and correct filtering.

### 2. FIX: occupancy classification ignored record rules

`models/mrp_workcenter.py` — `_get_overcapacity_intervals`

The query deciding whether a calendar leave represents a running work order (one
occupied lane) or a hard block ran with the current user's permissions. A work
order the user could not read — record rules, or a different company in a
multi-company database — had its leave misclassified as a hard block, silently
making a capacity-N station schedule like capacity-1 for that window. Wrong
dates, no error, difficult to notice. The lookup now runs with `sudo()`, since
"is this leave a work-order leave" is a factual classification rather than
user-scoped data.

### Also in this version

- `tools/verify_sweepline.py` added: runs the real occupancy math against Odoo's
  real `Intervals` class with no Odoo runtime required. 13/13 passing.
- `README.md` / `DEPLOY.md` rewritten for hotfix deployment and to state the
  testing situation honestly.

## 19.0.1.0.0 — initial

- `concurrent_capacity` ("Concurrent Operators") integer field on
  `mrp.workcenter`, default 1, DB CHECK constraint `>= 1`.
- Full override of `mrp.workcenter._get_first_available_slot` implementing
  sweep-line occupancy counting, guarded by an early `return super()` when
  `concurrent_capacity <= 1`. Forward and backward scheduling both supported.
- Post-filter on `mrp.workorder._get_conflicted_workorder_ids` (Gantt warning
  triangle only).
- Work Center form view inheritance.
- 6 `TransactionCase` tests — **never executed against a real database.**

Pre-release corrections made during review, listed so they are not rediscovered:

- Tests originally set `time_cycle` on BOM operations. That field is a readonly
  compute; Odoo silently discards writes to it and falls back to
  `time_cycle_manual` (default 60), which would have made the duration
  assertion compare 800 against 4800. Now sets `time_cycle_manual`.
- The leave query originally filtered on resource only. Core also filters by
  calendar and includes calendar-wide (resource-less) leaves; the domain now
  mirrors core's `_leave_intervals_batch` filters.
- Non-work-order leaves were being counted as one lane. They now block fully, so
  Maintenance-style downtime cannot be scheduled into.
- `_get_conflicted_workorder_ids` returned a plain `dict` where core returns a
  `defaultdict(list)`; restored to preserve the API contract for other callers.
