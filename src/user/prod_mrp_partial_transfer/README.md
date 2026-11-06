# prod_mrp_partial_transfer 19.0.2.1.1

Replaces 19.0.1.0.0 (the Sept 24, 2026 build installed in production). Implements
option A of the Nov 5, 2026 fix brief: every partial transfer is posted on the MO
itself, the way Odoo's own production posting works.

The folder name must stay `prod_mrp_partial_transfer` so Odoo upgrades the
installed module instead of installing a second one.

## Fixed in 19.0.2.1.1 (found on staging-41414302, Nov 6)

* Partial posting posted 0 finished units: the code marked the finished move as picked
  before posting, and in Odoo 19 a picked finished move already counts as produced, so
  `_post_inventory` posted nothing. Odoo now fills in the finished quantity itself. Checked
  on MO 02160 with a rolled-back test: 99 posted, components consumed, MO still open with 1
  expected, customer Pick reserved.
* Duplicate conversion now skips consumable products (no stock effect) and refuses when the
  units received twice are no longer on hand (CR 16441, MOs 02244 and 02273): those need a
  count, not a return that drives stock negative.

## New in 19.0.2.1.0: converting the old app's receipts

The old app's receipts (WH/QC, origin = MO name, out of Virtual Locations/Production,
not linked to the MO) are converted with standard operations only:

* **Open MO** with old receipts: its old storage (SFP) transfers are cancelled, the old
  receipts are returned (a return of the original moves, so reversed at their own value),
  then the same quantity is posted on the MO with components for it. Components already
  consumed on the MO are counted. The MO stays open and continues with the new app.
  Any reduction of the expected quantity made by cleanup step 7 is undone first.
* **Done MO received twice** (old receipt plus the MO's own posted receipt): old storage
  transfers cancelled and the duplicate quantity returned.
* Receipts already compensated by the cleanup script ("Cleanup: reverse duplicate MO
  receipt ...") are left alone; a receipt is never returned twice.

Where: Manufacturing > Configuration > MOs with Old Partial Transfers (administrators).
Select all, then Actions > Convert Old Partial Transfers, or use the button on one MO.
Each MO is converted on its own; a failure is reported and leaves that MO unchanged.
A transfer on an MO that still has old receipts is refused until it is converted.

Found on staging-41414302 (copy of production, Nov 6) before deployment: 10 open MOs
(01812, 01824, 02178, 02203, 02189, 02160, 01551, 01667-002, 02220, 02315) and 3 Done
MOs received twice on Nov 6 (02290 +10, 02313 +3, 02316 +20). The 7 open SFPs
(WH/SFP/02714 to 02720) all belong to these and are cancelled by the conversion.

## How finished goods flow at SDI (checked on staging)

The warehouse is 3-step (Pick Components, produce, Store Finished Product), and
Post-Production and Pre-Production are child locations of WH/Stock. Most MOs are make
to order: 150 of 156 open MOs have their finished move chained straight to the customer
Pick (or to a Pick Components for subassemblies), not to a Store Finished Product
transfer. For those, posted units land in Post-Production and are reserved by the chained
Pick right away; no storage transfer is involved, and that is standard Odoo behaviour.
The automatic storage step applies to make-to-stock MOs, whose finished move feeds a
Store Finished Product transfer. Test case 1's "0 left in Post-Production" applies to
those; for a make-to-order test MO, check that the units are reserved on the Pick instead.

## What changed

| Brief item | 19.0.1.0.0 (old) | 19.0.2.0.0 (this) |
| --- | --- | --- |
| Partial transfer | Separate WH/QC receipt, Production to Post-Production, not linked to the MO | `qty_producing` = produced + N, `_set_qty_producing()`, exactly N on the open finished move, `_post_inventory(cancel_backorder=False)`. Finished move done for N with the MO set; remainder stays open; components for N consumed at FIFO |
| Storage | WH/SFP created, never validated | Chained Store Finished Product move validated for exactly N (backorder for the rest). Fills a negative WH/Stock quant before applying putaway |
| Final transfer | Direct `state` write (to Sept 23), then `button_mark_done` on top of the app's receipt (double receipt, from Sept 24) | Standard `button_mark_done` for the remainder only. Already-posted units are never posted again |
| Blocked close | Only two wizards handled, the rest swallowed, fallback state write | Any returned dialog or error raises a message and rolls everything back. Consumption differences can be accepted with a tick box |
| Short close | Not available | "Close at Transferred Qty" button: standard backorder dialog (backorder or no backorder) |
| Counter | Stored float, copied into backorders | `qty_transferred_to_stock` is an alias of `qty_produced` (computed, not stored) |
| Fix Stuck Moves wizard | Swept open moves to done | Removed. Replaced by a read-only list: Manufacturing > Configuration > Done MOs with Open Moves |
| Operation type | Borrowed Quality Control | Not used any more |

## Decisions taken (change if the business decides otherwise)

* Transfers are allowed while work orders are open; work orders are left alone.
  Note: Odoo's `_post_inventory` fills in a duration on work orders whose
  duration is still 0. Check this in test case 7.
* The 3-step route is kept; storage is auto-validated rather than sent straight to WH/Stock.
* Lot/serial-tracked products are refused with a message (none are affected today).
* The quantity field starts empty, so the final close is never one accidental click away.
* Component shortage check: by default a transfer is refused if a component would
  go negative in its source location (usually Stock/Pre-Production). Users can
  tick "Post even if components are short". If Pre-Production is routinely
  unpicked, decide whether that should be the default.
* MOs that still carry receipts from the old app are refused with a message;
  they must be closed at the transferred quantity first (see release order).

## Release order

The history cleanup already ran on production (Nov 5) and Rick resolved the open
component moves (Nov 6). Remaining release steps:

1. Staging (copy of production): replace the module folder in the staging branch with
   this one, deploy, then Apps > MRP Partial Transfer to Stock > Upgrade. Version must
   read 19.0.2.1.1. If the view `mrp.production.form.partial.transfer` was archived to
   hide the old button, unarchive it.
2. Run the conversion (MOs with Old Partial Transfers > select all > Convert). Then the
   list must be empty, Done MOs with Open Moves empty, no open SFP left from WH/QC receipts.
3. Pass the brief's test plan on fresh test MOs (see the flow note above for case 1).
4. Production: stop use of the old app, take a backup, deploy and upgrade, run the
   conversion, run the same checks.
5. Tell the users: transfers now post on the MO, storage is automatic for make-to-stock,
   short closes ask about backorders, and component shortages are reported.

## Watch items for the test plan

* Quality control points on the Store Finished Product operation type will block automatic storage (the transfer is refused and rolled back). Check before release.
* This code relies on Odoo's own `_set_qty_producing`, `_post_inventory` and `button_mark_done`. It was written against Odoo 17/18 behaviour and has not been run on Odoo 19 yet; the staging test plan is the proof.
* Case 9 (valuation): compare the finished-goods value of each partial post with the components consumed for it.
