# Changelog

## 19.0.1.0.1 — hotfix (not yet deployed)

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
