# Deploying `sdi_workcenter_concurrency` 19.0.1.0.4 to production

Target: SDI Wire & Cable Odoo.sh project. Odoo **19.0 Enterprise**.
Production: `sdi-wire.odoo.com`
Staging: `sdi-wire-main-staging-41165534.dev.odoo.com`

Read `README.md` for what the module does and its known risks, and
`CHANGELOG.md` for what changed in each version.

---

## Situation (verified against both databases on 2026-11-05)

| | Production | Staging |
|---|---|---|
| Installed version | **19.0.1.0.1** | 19.0.1.0.4 |
| Work-center calendar | **already correct** | corrected |
| Concurrent Operators configured | **yes** | yes |
| Weekend placements (not-started WOs) | **119** | 0 |
| Out-of-hours placements | **168** | 0 |
| Smeared work orders | **73** | 0 |

Production is on 1.0.1, so the `KeyError` that broke opening and editing
manufacturing orders is **already fixed there**. What production is missing is
1.0.2, 1.0.3 and 1.0.4 — all three of which are scheduling-correctness fixes,
and all three ship together in 1.0.4.

What production still has wrong, and which version fixes it:

- **Work scheduled on nights and weekends.** 1.0.1 carries a stale copy of
  core's `_get_first_available_slot` that is missing core's `min(..., stop)`
  clamp, so `start` could be taken from a leave interval and escape the working
  day. Fixed in **1.0.3**.
- **A past over-capacity block dragging a slot backwards** into the weekend.
  Fixed in **1.0.4**.
- **Work orders smeared across weeks.** `_calculate_date_finished` counted
  parallel operators as station downtime, which compounds into a feedback loop.
  Worst case on production today: `WH/MO/02313` ASSEMBLY, **36 minutes of work
  holding a 65.9-day window**. Fixed in **1.0.2**.

Constraints:

- No data migration, no reinstall, no uninstall required.
- Existing "Concurrent Operators" values are preserved.
- Python-only changes. The manifest version bump (`19.0.1.0.1` →
  `19.0.1.0.4`) makes Odoo.sh run a module update on the next build.

### Two things that are NOT needed on production

Both were required on staging and are easy to carry over by mistake:

1. **No calendar change.** Production's `Production - Standard - Assembly`
   (calendar id 1) is already Mon–Fri 08:00–12:00 / 13:00–17:00, non-flexible,
   40 h/week, US/Central. A separate flexible calendar (`Assembly Work Week`,
   id 3, `flexible_hours = true`, 40 h/day) exists but **no work center uses
   it** — leave it alone.
2. **No Concurrent Operators data entry.** Production already has
   ASSEMBLY 8, TERMINATION 7, INSERTION 6, SPLICING 4, BRAIDING 3, KOMAX 3,
   TESTING 3, STRIP 2, and CABLE / HAND SEAL / LOOM CUTTING / QUALITY /
   REWORK / SOLDER at 1.

## Repository layout

The module folder goes at the **root of the Odoo.sh project repository** —
one top-level folder per addon:

```
<repo root>/
└── sdi_workcenter_concurrency/
    ├── __manifest__.py
    ├── models/
    ├── views/
    ├── tests/
    └── tools/
```

Sanity check before pushing: an earlier deployed traceback showed the module
resolving under a doubled path — `/home/odoo/src/user/src/user/sdi_workcenter_concurrency/`.
Match wherever the addon currently lives rather than introducing a second copy
at a different depth. Two copies on the addons path cause confusing behavior.

Do not commit `tools/intervals.py` if it is present — it is a cached copy of
Odoo's own file that the verification harnesses download on first run.

## Step 1 — Deploy the code

1. **Development branch first.** Commit to an Odoo.sh *development* branch.
   Odoo.sh runs a module's test suite on development-branch builds but **not**
   on staging or production builds, so this is the only point where the 9 tests
   in `tests/` execute. They have never been run against a real database (see
   README "Testing status") — treat a green run as new information.

2. **Read the build log.** Confirm all 9 tests pass, and specifically confirm
   README risk #2: whether Enterprise `mrp_workorder` also overrides
   `_get_first_available_slot` and is being bypassed on concurrency-enabled
   work centers. This could not be checked without Enterprise source.

3. **Merge to production.** Staging is already running 1.0.4 and has been
   verified (see "What was verified on staging" below), so a separate staging
   soak is optional.

4. **Confirm the version landed** before doing anything else:

   Manufacturing → Apps → `sdi_workcenter_concurrency` must read
   **19.0.1.0.4**. Running the repair below against 1.0.1 will simply recreate
   the bad dates.

## Step 2 — Repair the existing bad dates

Deploying stops *new* work orders from being mis-scheduled. It does not fix the
ones already carrying bad dates. On production that is **180 not-started work
orders across 60 manufacturing orders**.

### Use `action_replan`, not Unplan/Plan

Earlier revisions of this document said to Unplan then Plan the manufacturing
order. **That does not work for most of them.** `button_unplan` raises if any
work order on the MO is done or started:

> Some work orders are already done, so you cannot unplan this manufacturing order.

On production, **101 of the 166 open MOs refuse to unplan** for that reason.
Only **4** of the 180 bad work orders are reachable by Unplan/Plan; the other
**176** are not.

The right tool is `mrp.workorder.action_replan()`, which is public and calls
`mrp.production._plan_workorders(replan=True)` on the linked MO. Core's
`_plan_workorder` begins with:

```python
if self.state not in ['blocked', 'ready']:
    return
```

so done and in-progress work orders are never touched, and it reuses
`self.duration_expected` for the same work center, so **durations are
preserved**. It re-chains each work order from its predecessor's real finish
time.

Calling it on **any one** not-started work order replans **every** ready or
blocked work order on that MO, so one call per manufacturing order is enough.

### Running it

From a logged-in browser session on production, with one not-started work order
id per MO:

```js
await rpc('mrp.workorder', 'action_replan', [[workorderId]]);
```

Do this one MO at a time rather than in a single call, so each gets its own
transaction and one failure cannot roll back the rest. Allow roughly 0.4 s per
MO. Work in date order — earlier MOs should claim earlier slots.

Replanning will not schedule into the past: `_plan_workorder` uses
`max(production.date_start, datetime.now())`, so anything whose window has
already passed moves forward. That is correct, but it means **dates will move**,
including for orders that were not previously wrong. Tell production planning
before running it.

## Step 3 — Verify

After the repair, over all not-started work orders, all four of these must be
zero:

- placements on a Saturday or Sunday (`date_start` and `date_finished`)
- placements outside 08:00–17:00 America/Chicago
- spans over 5 days that exceed 2.5× the working days the duration needs
- changed `duration_expected` values (snapshot them before, diff after)

Check `date_finished` only for in-progress work orders — their `date_start` is
the historical moment an operator pressed Start, so it is legitimately
"out of hours" and must not be treated as a defect.

A long span is not automatically a defect. ASSEMBLY's calendar gives a maximum
unbroken block of 240 minutes, so any job longer than that legitimately spans
multiple days. Compare span against `duration_expected / 480` working days
rather than against a flat day count.

## What was verified on staging (19.0.1.0.4, 2026-11-05)

- The BRAID order that reproducibly landed on a Saturday night now plans
  Mon 23 Nov 16:03 → Tue 24 Nov 13:23, with `duration_expected` unchanged.
- Full backfill over **805 not-started work orders**: 0 weekend placements,
  0 out-of-hours placements, 0 excessive spans, 0 duration changes.
- `action_replan` verified against an MO that refused to unplan: done and
  in-progress work orders came back byte-identical; only ready/blocked ones
  moved.

## Known remaining issue, not caused by this module

`QUALITY` is at `concurrent_capacity` 1, so this module delegates entirely to
core there. Core treats every other QC leave on the resource as downtime, which
produces a self-blocking queue — on staging, one QC work order held a 56-day
window for 83 minutes of work, pushing downstream QC into January.

If more than one person inspects at a time, set QUALITY's Concurrent Operators
to the real headcount and the problem disappears the same way it did at
ASSEMBLY. If QUALITY genuinely is one inspector, the long window is correct
queueing and should be left alone. The same question applies to CABLE,
HAND SEAL, LOOM CUTTING, SOLDER and REWORK, all currently at 1.

## If production needs unblocking

Uninstall the module from Apps. That removes the overrides and restores stock
Odoo scheduling immediately. Cost: the `concurrent_capacity` column is dropped,
so per-station operator counts must be re-entered after reinstalling. No other
data is affected — the module never writes to product, BOM, routing, or work
order data.

## Verifying the maths without an Odoo runtime

```bash
python3 tools/verify_sweepline.py      # occupancy maths, 13/13
```

```bash
python3 tools/verify_slot_finder.py    # real _get_first_available_slot, 4/4
```

```bash
python3 tools/check_core_drift.py      # diff vs core, exit 0 = expected only
```

All three need `pytz` and network access on first run (they fetch and cache
Odoo's `intervals.py` and core's `mrp_workcenter.py`).

## On future Odoo upgrades

`_get_first_available_slot` in `models/mrp_workcenter.py` is a **full override**
of core, copied from `addons/mrp/models/mrp_workcenter.py` with only the
busy-interval definition changed, plus a deliberate forward/backward clamp that
core does not have. Core changing that method underneath this copy has already
caused one production bug. **Run `tools/check_core_drift.py` after every
version or point-release upgrade** — it is what caught that bug.
