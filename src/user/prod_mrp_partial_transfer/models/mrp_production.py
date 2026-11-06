# -*- coding: utf-8 -*-
"""Partial transfers posted on the MO itself ("option A" of the Nov 2026 fix brief).

Each partial transfer goes through Odoo's own production posting
(``_set_qty_producing`` + ``_post_inventory``), so a single stock movement per
unit drives on-hand, forecast, component consumption and cost. The final
transfer closes the MO with the standard ``button_mark_done``, which only posts
what has not been posted yet. The chained storage step (Store Finished Product)
is validated for exactly the posted quantity.

Rules kept from the brief:
* never write ``state`` on mrp.production or stock.move;
* never post a quantity twice;
* never match MOs by origin text for anything that drives stock (origin is only
  used to find receipts made by the old app, see mrp_production_legacy.py);
* if something blocks, raise (everything rolls back) and tell the user why.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_is_zero

OPEN_MOVE_STATES = ('draft', 'waiting', 'confirmed', 'partially_available', 'assigned')


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    pt_used = fields.Boolean(
        string='Partial Transfers Used',
        copy=False,
        readonly=True,
        help="Set once a partial transfer has been posted on this MO. When the MO "
             "is closed, its storage step is validated automatically.",
    )
    # Kept for compatibility with existing views and reports. These are plain
    # aliases of Odoo's own produced quantity, never a separate counter, so a
    # backorder can never inherit a value from its parent.
    qty_transferred_to_stock = fields.Float(
        string='Transferred to Stock', compute='_compute_pt_progress',
        digits='Product Unit of Measure')
    qty_remaining_to_produce = fields.Float(
        string='Remaining to Produce', compute='_compute_pt_progress',
        digits='Product Unit of Measure')
    qty_progress_pct = fields.Float(string='% Complete', compute='_compute_pt_progress')
    show_partial_transfer_button = fields.Boolean(compute='_compute_pt_progress')
    show_pt_close_button = fields.Boolean(compute='_compute_pt_progress')

    @api.depends('qty_produced', 'product_qty', 'state', 'pt_used')
    def _compute_pt_progress(self):
        for mo in self:
            rounding = mo.product_uom_id.rounding or 0.0001
            produced = mo.qty_produced
            remaining = max(mo.product_qty - produced, 0.0)
            has_remaining = float_compare(remaining, 0.0, precision_rounding=rounding) > 0
            is_open = mo.state in ('confirmed', 'progress', 'to_close')
            mo.qty_transferred_to_stock = produced
            mo.qty_remaining_to_produce = remaining
            # The percentage widget expects a fraction (0.62 shows as 62%).
            mo.qty_progress_pct = produced / mo.product_qty if mo.product_qty else 0.0
            mo.show_partial_transfer_button = is_open and has_remaining
            mo.show_pt_close_button = (
                is_open and has_remaining and mo.pt_used
                and not float_is_zero(produced, precision_rounding=rounding))

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def action_partial_transfer_to_stock(self):
        self.ensure_one()
        self._pt_check_can_transfer()
        return {
            'name': _('Transfer Partial Quantity to Stock'),
            'type': 'ir.actions.act_window',
            'res_model': 'mrp.partial.transfer.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_production_id': self.id},
        }

    def action_pt_close_at_produced(self):
        """Short close: close the MO at the quantity already posted, through
        Odoo's standard backorder flow (the user chooses backorder or not)."""
        self.ensure_one()
        self._pt_require('_set_qty_producing')
        if float_is_zero(self.qty_produced, precision_rounding=self.product_uom_id.rounding):
            raise UserError(_('Nothing has been transferred on %s yet.', self.name))
        self.qty_producing = self.qty_produced
        self._set_qty_producing()
        return self.button_mark_done()

    # ------------------------------------------------------------------
    # Standard close hook
    # ------------------------------------------------------------------
    def button_mark_done(self):
        """After Odoo closes an MO that used partial transfers, put away the
        quantity this close posted. Runs on every path that ends in
        button_mark_done (our wizard, the standard button, the backorder and
        consumption-warning dialogs)."""
        done_before = {
            mo.id: mo.move_finished_ids.filtered(lambda m: m.state == 'done')
            for mo in self
        }
        res = super().button_mark_done()
        for mo in self.filtered(lambda p: p.pt_used and p.state == 'done'):
            new_done = mo.move_finished_ids.filtered(
                lambda m: m.state == 'done' and m.product_id == mo.product_id
            ) - done_before.get(mo.id, self.env['stock.move'])
            if new_done:
                mo._pt_store_finished(new_done)
        return res

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    def _pt_require(self, method_name):
        if not hasattr(self, method_name):
            raise UserError(_(
                "This Odoo version has no mrp.production.%s, which the partial "
                "transfer app relies on. Nothing has been posted; the app needs "
                "to be updated for this Odoo version.", method_name))

    def _pt_open_finished_move(self):
        self.ensure_one()
        return self.move_finished_ids.filtered(
            lambda m: m.product_id == self.product_id and m.state not in ('done', 'cancel'))

    def _pt_check_can_transfer(self):
        self.ensure_one()
        if self.state not in ('confirmed', 'progress', 'to_close'):
            raise UserError(_(
                'Partial transfers are only possible on confirmed or in-progress '
                'Manufacturing Orders (%(mo)s is %(state)s).',
                mo=self.name, state=self.state))
        if self.product_id.tracking != 'none':
            raise UserError(_(
                '%s is tracked by lot or serial number. Partial transfers do not '
                'support tracked products yet; use the standard Produce flow.',
                self.product_id.display_name))
        mode, legacy_qty = self._pt_legacy_eval(
            self._pt_legacy_index([self.name]), self._pt_cleanup_reversed_names())
        if mode:
            raise UserError(_(
                '%(mo)s has %(qty)s %(uom)s received by the old version of this app '
                '(a separate WH/QC receipt not linked to the MO). A Manufacturing '
                'administrator must click "Convert Old Partial Transfers" on it first.',
                mo=self.name, qty=legacy_qty, uom=self.product_uom_id.name))
        if len(self._pt_open_finished_move()) != 1:
            raise UserError(_(
                '%s does not have exactly one open finished-product move, so the '
                'app cannot tell what to post. Check the MO before continuing.',
                self.name))

    def _pt_component_shortages(self):
        """Components that would go negative in their source location if the
        quantities currently set on the MO were posted."""
        self.ensure_one()
        shortages = []
        for move in self.move_raw_ids.filtered(
                lambda m: m.state not in ('done', 'cancel') and m.picked):
            product = move.product_id
            if not getattr(product, 'is_storable', product.type == 'product'):
                continue
            rounding = product.uom_id.rounding
            need = move.product_uom._compute_quantity(move.quantity, product.uom_id)
            if float_is_zero(need, precision_rounding=rounding):
                continue
            available = product.with_context(location=move.location_id.id).qty_available
            if float_compare(available, need, precision_rounding=rounding) < 0:
                shortages.append(_(
                    '%(product)s: need %(need)s %(uom)s, %(avail)s on hand in %(loc)s',
                    product=product.display_name, need=round(need, 4),
                    uom=product.uom_id.name, avail=round(available, 4),
                    loc=move.location_id.display_name))
        return shortages

    # ------------------------------------------------------------------
    # Storage step (e.g. WH/SFP: Post-Production -> Stock)
    # ------------------------------------------------------------------
    def _pt_store_type(self):
        self.ensure_one()
        warehouse = self.picking_type_id.warehouse_id
        return getattr(warehouse, 'sam_type_id', False) or self.env['stock.picking.type']

    def _pt_store_finished(self, finished_moves):
        """Validate the chained storage transfer for exactly the quantity posted
        by ``finished_moves``. If the route has no storage step the goods are
        already in stock and nothing is done. Raises UserError (rolling back the
        whole transfer) if storage cannot be completed without user input."""
        self.ensure_one()
        store_type = self._pt_store_type()
        for fmove in finished_moves:
            store_moves = fmove.move_dest_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')
                and m.picking_id
                and m.product_id == fmove.product_id
                and m.location_id == fmove.location_dest_id
                and (m.picking_type_id == store_type if store_type else m.picking_type_id.code == 'internal')
            )
            if not store_moves:
                continue
            store_move = store_moves[0]
            qty = fmove.product_uom._compute_quantity(fmove.quantity, store_move.product_uom)
            if float_compare(qty, store_move.product_uom_qty,
                             precision_rounding=store_move.product_uom.rounding) > 0:
                qty = store_move.product_uom_qty
            self._pt_validate_store_move(store_move, qty)

    def _pt_validate_store_move(self, store_move, qty):
        picking = store_move.picking_id
        rounding = store_move.product_uom.rounding
        if store_move.state not in ('assigned', 'partially_available'):
            store_move._action_assign()
        store_move.quantity = qty
        if float_compare(store_move.quantity, qty, precision_rounding=rounding) < 0:
            raise UserError(_(
                'Only %(avail)s %(uom)s of %(product)s could be reserved in %(loc)s '
                'for storage transfer %(picking)s. Nothing has been posted.',
                avail=store_move.quantity, uom=store_move.product_uom.name,
                product=store_move.product_id.display_name,
                loc=store_move.location_id.display_name, picking=picking.name))
        self._pt_fill_negative_first(store_move)
        store_move.picked = True
        res = picking.with_context(skip_backorder=True, skip_sms=True).button_validate()
        if store_move.state != 'done':
            reason = isinstance(res, dict) and (res.get('name') or res.get('res_model')) or _('unknown reason')
            raise UserError(_(
                'Storage transfer %(picking)s could not be validated automatically '
                '(%(reason)s). Nothing has been posted. Check the operation type '
                'for quality checks or other required steps, then try again.',
                picking=picking.name, reason=reason))

    def _pt_fill_negative_first(self, store_move):
        """If the storage destination itself (e.g. WH/Stock, not a child bin) is
        negative for the product, send units there first: Odoo does not offset
        a negative quant on WH/Stock with stock sitting in a child bin."""
        dest = store_move.location_dest_id
        product = store_move.product_id
        rounding = product.uom_id.rounding
        quants = self.env['stock.quant'].sudo().search([
            ('product_id', '=', product.id), ('location_id', '=', dest.id)])
        to_fill = -sum(quants.mapped('quantity'))
        if float_compare(to_fill, 0.0, precision_rounding=rounding) <= 0:
            return
        for line in store_move.move_line_ids.sorted('id'):
            if float_compare(to_fill, 0.0, precision_rounding=rounding) <= 0:
                break
            line_qty = line.quantity_product_uom
            if line.location_dest_id == dest:
                to_fill -= line_qty
                continue
            if float_compare(line_qty, to_fill, precision_rounding=rounding) <= 0:
                line.location_dest_id = dest
                to_fill -= line_qty
                continue
            fill_in_line_uom = product.uom_id._compute_quantity(to_fill, line.product_uom_id)
            line.quantity = line.quantity - fill_in_line_uom
            self.env['stock.move.line'].create({
                'move_id': store_move.id,
                'picking_id': store_move.picking_id.id,
                'product_id': product.id,
                'product_uom_id': line.product_uom_id.id,
                'quantity': fill_in_line_uom,
                'location_id': line.location_id.id,
                'location_dest_id': dest.id,
                'package_id': line.package_id.id,
                'company_id': store_move.company_id.id,
            })
            to_fill = 0.0

    def _pt_log(self, title, detail):
        self.ensure_one()
        self.message_post(body=Markup('<b>%s</b><br/>%s') % (title, detail))
