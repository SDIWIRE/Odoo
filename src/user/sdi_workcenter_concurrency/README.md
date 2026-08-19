# SDI Work Center Concurrency — developer handoff

Custom Odoo 19.0 Enterprise addon for SDI Wire & Cable.

> ## ⚠️ Read this first
>
> **This module is already installed on PRODUCTION (`sdi-wire.odoo.com`) and the
> deployed build is currently throwing an unhandled error.** What you have here
> is a **hotfix**, not a first-time install.
>
> The deployed version crashes with `KeyError` whenever a user opens or edits a
> manufacturing order that has two or more overlapping work orders on the same
> work center — which is the normal state this module is designed to create.
> Reported twice from production: 2026-08-14 and 2026-08-15.
>
> Both source fixes are in this package. See `CHANGELOG.md` for exactly what
> changed and `DEPLOY.md` for how to get it out.
>
> There is **no data-level workaround** — lowering "Concurrent Operators" back
> to 1 does not avoid the crash, because the failure happens before that value
> is ever read. Either the code fix ships, or the module has to be uninstalled.

## What the module does

Odoo's `mrp.workcenter` has one **Capacity** field (Product Capacities tab) that
does two unrelated jobs: it drives the duration formula
(`cycles = ROUNDUP(qty / capacity)`, so `duration_expected = setup + cleanup +
cycles × time_cycle × 100/efficiency`) *and* it is what planners reach for when
they want more than one job running at a station.

For SDI's manual stations that conflict is a real problem. At ASSEMBLY, five
people each build their **own** unit, one at a time. Nobody batch-processes.
Setting Capacity = 5 to get concurrency divides the computed duration of any
order over 5 units by up to 5× — a silent, compounding scheduling error on the
20–135 unit MOs SDI typically runs.

This module adds a **separate** integer field, `concurrent_capacity`
("Concurrent Operators", default 1), and teaches the scheduler to respect it.
Duration math is left completely untouched.

- `concurrent_capacity = 1` (default, every work center unless changed):
  behavior is identical to stock Odoo — the override early-returns to `super()`.
- `concurrent_capacity = N`: up to N work orders may be scheduled in overlapping
  windows on that one work center record; the (N+1)th is pushed to the next
  opening.

Deliberately unchanged: `_get_duration_expected`, `capacity_ids` / the Product
Capacities tab, clock-in/out (employees still clock into one named station), and
the duplicate-workcenter + Alternative Workcenters pattern used by KOMAX /
GAMMA / SD ARTOS.

## Files

| Path | What it is |
|---|---|
| `models/mrp_workcenter.py` | The `concurrent_capacity` field, a DB CHECK constraint (>= 1), the `_get_overcapacity_intervals` sweep-line helper, and a **full override** of `_get_first_available_slot`. The high-risk file. |
| `models/mrp_workorder.py` | Small post-filter on `_get_conflicted_workorder_ids` — the Gantt warning triangle only. Not a scheduling gate. This is where the production `KeyError` was. |
| `views/mrp_workcenter_views.xml` | Adds the field to the Work Center form, after `oee_target`. Inherits `mrp.mrp_workcenter_view`. |
| `tests/test_workcenter_concurrency.py` | 7 `TransactionCase` tests, tagged `post_install`. **See "Testing status" below — these have never been executed.** |
| `tools/verify_sweepline.py` | Standalone harness (no Odoo runtime needed) that runs the real occupancy math against Odoo's real `Intervals` class. 13/13 passing. |

## How the scheduling override works

The single thing that actually prevents work orders overlapping in core is
`mrp.workcenter._get_first_available_slot()`
(`addons/mrp/models/mrp_workcenter.py`, ~lines 339-408 in 19.0). It walks the
work center's `resource.calendar` and treats **any** overlapping
`resource.calendar.leaves` record with `time_type='other'` — one is created per
work order by `_plan_workorder` / `button_start` — as fully blocking. Binary
busy/free, no notion of depth.

Callers: `mrp_workorder._plan_workorder` (~line 610) and
`mrp_production._calculate_expected_finished_date` (~line 813). Note the second
one runs inside the `_compute_date_finished` compute, so **this override
executes during onchange on `mrp.production`**, not only on explicit planning.

The override keeps core's structure verbatim (same 14-day chunked
forward/backward walk, same `Intervals` intersections, same tz handling, same
`leaves_to_ignore` / `extra_leaves_slots` threading) and substitutes only the
definition of "busy": `_get_overcapacity_intervals` builds a sweep-line
(`+1` at each leave start, `-1` at each end, clipped to the search window) and
marks time unavailable only where the running count is already `>=
concurrent_capacity`.

Two deliberate carve-outs, both erring toward under-scheduling rather than
overbooking:

- Only leaves **actually linked to a work order** count as lane occupancy. Any
  other `time_type='other'` leave (calendar-wide, or created by another module —
  e.g. Maintenance downtime) still blocks fully, as in core.
- `extra_leaves_slots` stay full blocks. Nothing in community `mrp` passes them
  and their meaning elsewhere is unknown.

### Maintenance notes / known risks

1. **It is a full method override, not a hook.** Core inlines its busy-interval
   computation, so there was no smaller seam. **Re-diff against core's
   `_get_first_available_slot` on every Odoo version upgrade.**
2. **For `concurrent_capacity > 1` the override does not call `super()`.** If
   Enterprise `mrp_workorder` also overrides `_get_first_available_slot`, that
   override is bypassed on concurrency-enabled work centers. This could not be
   verified — Enterprise source was not available (private repo). The test suite
   is tagged `post_install` specifically so an Odoo.sh CI run exercises it with
   Enterprise loaded. **Please confirm this on the dev-branch build.**
3. **The conflict-warning filter over-approximates.** It counts pairwise
   overlaps, not instantaneous concurrency, so a chain A–B–C can flag B even
   when no single moment exceeds capacity. One-sided (never a false negative),
   cosmetic-only, and only reachable via manual Gantt drags.

## Testing status — please read

Be skeptical of the test suite. It was written without access to a running Odoo
instance, so **the 7 tests in `tests/` have never been executed against a real
database.** "Compiles cleanly" was the only verification performed on them. The
production `KeyError` is a direct consequence: no test covered the onchange /
virtual-record path, so the bug shipped. Test 6
(`..._with_virtual_onchange_records`) now covers that specific path.

What *has* been verified locally, and can be re-run by you in seconds without
Odoo:

```bash
python3 tools/verify_sweepline.py
```

This stubs out Odoo, loads the **real** `_get_overcapacity_intervals` from
`models/mrp_workcenter.py`, and runs it against Odoo's real dependency-free
`Intervals` class with concrete datetimes — staggered chains, nested intervals,
count-drops after a cluster ends, disjoint clusters, window clipping, abutting
intervals, non-work-order leaves. 13/13 pass. It verifies the interval math, not
the ORM integration around it.

**Recommended: push to a development branch first so Odoo.sh runs the suite**
before this reaches production again.

## Post-deploy configuration (SDI, via UI — no code)

`Manufacturing → Configuration → Work Centers` → open a station → General
Information tab → **Concurrent Operators**.

- ASSEMBLY: 5 (per original spec). Other multi-person manual stations — the ones
  tagged `ASSEMBLY`: TERMINATION, SPLICING, INSERTION, HAND SEAL, SOLDER — set
  to their actual headcount.
- **Leave KOMAX, GAMMA, SD ARTOS at 1.** They use the Alternative Workcenters
  pattern; do not modify them or that relationship.
- **Do not touch the Product Capacities tab** as part of staffing changes. It is
  not a headcount field.
- Minimum value is 1, enforced by a DB constraint.
- Changing the number affects **future** planning only; re-plan existing orders
  (Unplan / Plan) to apply a new headcount to them.

Rick has a separate end-user work-instructions document for shop floor staff.

## Provenance

Written by Claude (Anthropic) working with Rick Regole. All core behavior above
was verified by reading the real Odoo 19.0 community source
(`github.com/odoo/odoo` @ `19.0`), not from memory. Enterprise-only code
(`mrp_workorder`, Shop Floor) could not be read — see risk #2.
