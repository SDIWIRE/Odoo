# Deploying `sdi_workcenter_concurrency`

Target: SDI Wire & Cable Odoo.sh project. Odoo **19.0 Enterprise**.
Production: `sdi-wire.odoo.com`. Staging: `sdi-wire-main-staging-34748396.dev.odoo.com`.

Read `README.md` first for what the module does and its known risks, and
`CHANGELOG.md` for what changed.

---

## Situation

An earlier version of this module is **already installed on production and is
currently erroring** (unhandled `KeyError` when opening or editing manufacturing
orders — see `CHANGELOG.md` §1). This is a hotfix deployment to a live, partly
broken system.

Constraints:

- No data migration, no reinstall, no uninstall required.
- Existing "Concurrent Operators" values on work centers are preserved.
- Only two Python files changed. The manifest version is bumped
  (`19.0.1.0.0` → `19.0.1.0.1`) so Odoo.sh runs a module update on build.
- **Lowering Concurrent Operators to 1 does NOT work around the bug** — the crash
  happens before that value is read. Only the code fix (or uninstalling the
  module) resolves it.

## Repository layout

The module folder goes at the **root of the Odoo.sh project repository** — that
is the Odoo.sh convention, one top-level folder per addon:

```
<repo root>/
└── sdi_workcenter_concurrency/
    ├── __manifest__.py
    ├── models/
    ├── views/
    ├── tests/
    └── tools/
```

Sanity check before pushing: the deployed traceback showed the module resolving
under a doubled path — `/home/odoo/src/user/src/user/sdi_workcenter_concurrency/`.
If the existing checkout has the addon nested a level deeper than the repo root,
match wherever it currently lives rather than introducing a second copy at a
different depth. Two copies on the addons path will cause confusing behavior.

## Deployment

1. **Development branch first.** Commit the module to an Odoo.sh *development*
   branch. Odoo.sh runs a module's test suite automatically on development-branch
   builds but **not** on staging or production builds — so this is the only point
   where the tests execute.

2. **Read the build log.** The suite is tagged `post_install`, so it runs with all
   Enterprise modules loaded. Two things to look for:
   - All 7 tests in `tests/test_workcenter_concurrency.py` pass. They have never
     been run against a real database before (see README "Testing status") — treat
     a green run as new information, not a formality.
   - Specifically confirms README risk #2: whether Enterprise `mrp_workorder`
     also overrides `_get_first_available_slot` and is being bypassed on
     concurrency-enabled work centers. This could not be checked without
     Enterprise source.

3. **Staging.** Merge the development branch into the staging branch via the
   Odoo.sh Branches view. Staging rebuilds with a copy of production data plus
   the module. Verify by hand there:
   - Open a manufacturing order that has overlapping work orders at ASSEMBLY —
     this is the exact action that crashes today. It must load cleanly.
   - Add a work order to an existing MO and save.
   - Plan an MO for qty 80 at a 10-minute cycle time on ASSEMBLY:
     `duration_expected` must be **800 minutes**, not 160.
   - Plan five single-unit MOs at ASSEMBLY (Concurrent Operators = 5): all five
     should land in the same window. A sixth should be pushed to the next opening.
   - Regression: confirm KOMAX / GAMMA / SD ARTOS schedule exactly as before.

4. **Production.** Merge staging → production once the above passes. Confirm the
   originally-failing action (open/edit an MO with overlapping work orders) now
   works on `sdi-wire.odoo.com`.

## If production needs unblocking before the fix can ship

Uninstall the module from Apps. That removes the overrides and restores stock
Odoo scheduling immediately. Cost: the `concurrent_capacity` column is dropped,
so per-station operator counts must be re-entered after reinstalling. No other
data is affected — the module never writes to product, BOM, routing, or work
order data.

## Post-deploy configuration

Done by SDI through the UI, no code involved:
`Manufacturing → Configuration → Work Centers` → station → General Information →
**Concurrent Operators**. See README "Post-deploy configuration" for the
station-by-station rules and the constraints that must not be changed
(KOMAX/GAMMA/SD ARTOS stay at 1; Product Capacities tab is not a headcount field).

## Verifying the interval math without an Odoo runtime

```bash
python3 tools/verify_sweepline.py
```

Loads the real `_get_overcapacity_intervals` from `models/mrp_workcenter.py`,
stubs out Odoo, and runs it against Odoo's real `Intervals` class. Needs `pytz`
and network access on first run (it fetches `intervals.py` from the public Odoo
repo and caches it next to the script). 13/13 expected.

## On future Odoo upgrades

`_get_first_available_slot` in `models/mrp_workcenter.py` is a **full override**
of core, copied from `addons/mrp/models/mrp_workcenter.py` with only the
busy-interval definition changed. Re-diff it against core on every version
upgrade — core changes there will not merge themselves and will silently diverge.
