# Deploying `sdi_workcenter_concurrency` to Odoo.sh

**What this is:** a custom Odoo 19 addon for SDI Wire & Cable. It adds a
"Concurrent Operators" field to Manufacturing Work Centers so up to N work
orders can be scheduled in overlapping time windows on one work center
(for multi-person manual stations like ASSEMBLY), without touching the
existing per-product Capacity duration math. Work centers left at the
default (1) behave exactly as stock Odoo.

**Target:** the SDI Odoo.sh project (staging DB
`sdi-wire-main-staging-34748396.dev.odoo.com`). Odoo 19.0 Enterprise.

## Steps

1. Place the `sdi_workcenter_concurrency/` folder at the **root of the
   project's GitHub repository** (Odoo.sh convention: one top-level folder
   per addon).

2. **Commit to a development branch first — not directly to staging.**
   Odoo.sh runs module test suites automatically on development-branch
   builds (it does not on staging builds). This module ships with 6 tests
   (`tests/test_workcenter_concurrency.py`) that are the safety gate — they
   verify duration math is untouched, N-way overlap scheduling works, the
   (N+1)th work order queues instead of overlapping, and default work
   centers are unchanged. Check the dev build's log: all tests must pass.

3. Once green, merge/drag the development branch into the **staging**
   branch in the Odoo.sh Branches view. Do NOT deploy to production until
   staging has been verified by SDI.

4. On the staging database: enable developer mode → Apps → **Update Apps
   List** → search "SDI Work Center Concurrency" → Install.

5. Post-install data setup (manual, by SDI): open each multi-operator work
   center (Manufacturing → Configuration → Work Centers) and set
   **Concurrent Operators** to the station's headcount (e.g. ASSEMBLY = 5).
   Leave "Product Capacities" untouched. Do not change KOMAX / GAMMA /
   SD ARTOS — they stay at the default of 1 and keep their existing
   Alternative Workcenters setup.

## Technical notes for the reviewer

- Two overrides: `mrp.workcenter._get_first_available_slot` (the real
  scheduling change; full method override guarded by an early
  `return super()` when Concurrent Operators <= 1) and
  `mrp.workorder._get_conflicted_workorder_ids` (cosmetic Gantt-warning
  filter only).
- The slot override mirrors core 19.0's structure verbatim and swaps the
  binary "any overlapping work-order leave blocks" check for a sweep-line
  occupancy count. Only leaves actually linked to a work order count
  toward occupancy; anything else (e.g. maintenance downtime leaves)
  still blocks fully, as in core.
- Known review point: for Concurrent Operators > 1 the override does not
  call super(), so if Enterprise `mrp_workorder` also overrides
  `_get_first_available_slot`, that override would be bypassed on those
  work centers. The tests are tagged `post_install` so the dev-branch CI
  run exercises them with all Enterprise modules loaded — a green run is
  the check for this.
- On future Odoo version upgrades, re-diff the override against core's
  `addons/mrp/models/mrp_workcenter.py::_get_first_available_slot`.
