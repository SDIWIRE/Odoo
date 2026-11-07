# -*- coding: utf-8 -*-
"""Conversion of partial transfers made by the old 19.0.1.0.0 app.

The old app received finished goods with a separate receipt (WH/QC, origin =
MO name) out of Virtual Locations/Production, not linked to the MO. This file
finds those receipts and converts them with standard Odoo operations only:

* Open MO: cancel the old storage (SFP) transfers, return the old receipts
  (a return of the original moves, so they are reversed at their own value),
  then post the same quantity on the MO itself with components for it. The MO
  stays open and continues with the new app.
* Done MO received twice (old app receipt + the MO's own posted receipt):
  cancel the old storage transfers and return the duplicate quantity.

Receipts already compensated by the Nov 2026 cleanup script (pickings with
origin "Cleanup: reverse duplicate MO receipt ...") are left alone, and a
receipt is never returned twice (returned quantities are tracked through
origin_returned_move_id).
"""
import re
from collections import defaultdict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare, float_is_zero

OPEN_MO_STATES = ('confirmed', 'progress', 'to_close')
CLEANUP_REVERSAL_PREFIX = 'Cleanup: reverse duplicate MO receipt'
MO_NAME_RE = re.compile(r'[A-Za-z0-9]+/MO/\d+(?:-\d+)?')


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    pt_legacy_mode = fields.Selection(
        [('open', 'Open MO with old receipts'), ('duplicate', 'Received twice')],
        string='Old Partial Transfers', compute='_compute_pt_legacy')
    pt_legacy_qty = fields.Float(
        string='Old Receipts to Convert', compute='_compute_pt_legacy',
        digits='Product Unit of Measure')
    pt_legacy_pending = fields.Boolean(
        string='Old Partial Transfers to Convert', compute='_compute_pt_legacy',
        search='_search_pt_legacy_pending')

    # ------------------------------------------------------------------
    # Finding old receipts
    # ------------------------------------------------------------------
    @api.model
    def _pt_legacy_index(self, names=None):
        """origin (MO name) -> done moves received by the old app."""
        domain = [
            ('state', '=', 'done'),
            ('production_id', '=', False),
            ('raw_material_production_id', '=', False),
            ('location_id.usage', '=', 'production'),
            ('location_dest_id.usage', '=', 'internal'),
            ('picking_id', '!=', False),
        ]
        if names is not None:
            domain.append(('picking_id.origin', 'in', list(names)))
        index = defaultdict(lambda: self.env['stock.move'])
        for move in self.env['stock.move'].sudo().search(domain):
            index[move.picking_id.origin] |= move
        return index

    @api.model
    def _pt_cleanup_reversed_names(self):
        pickings = self.env['stock.picking'].sudo().search(
            [('origin', '=like', CLEANUP_REVERSAL_PREFIX + '%')])
        names = set()
        for picking in pickings:
            names.update(MO_NAME_RE.findall(picking.origin or ''))
        return names

    def _pt_legacy_moves(self, index=None):
        self.ensure_one()
        index = index if index is not None else self._pt_legacy_index([self.name])
        return index.get(self.name, self.env['stock.move']).filtered(
            lambda m: m.product_id == self.product_id)

    @staticmethod
    def _pt_returned_qty(move):
        return sum(move.returned_move_ids.filtered(lambda r: r.state == 'done').mapped('product_qty'))

    def _pt_legacy_eval(self, index, reversed_names):
        """Return (mode, qty in MO unit) for this MO, or (False, 0.0)."""
        self.ensure_one()
        legacy = self._pt_legacy_moves(index)
        if not legacy:
            return False, 0.0
        uom = self.product_id.uom_id
        pending = sum(m.product_qty - self._pt_returned_qty(m) for m in legacy)
        pending = uom._compute_quantity(pending, self.product_uom_id)
        rounding = self.product_uom_id.rounding
        if float_compare(pending, 0.0, precision_rounding=rounding) <= 0:
            return False, 0.0
        if self.state in OPEN_MO_STATES:
            return 'open', pending
        if self.state == 'done' and self.name not in reversed_names and self.product_id.is_storable:
            # Consumables carry no stock, so a duplicate receipt changes nothing.
            posted = sum(
                m.product_uom._compute_quantity(m.quantity, self.product_uom_id)
                for m in self.move_finished_ids
                if m.state == 'done' and m.product_id == self.product_id)
            dup = min(pending, posted)
            if float_compare(dup, 0.0, precision_rounding=rounding) > 0:
                return 'duplicate', dup
        return False, 0.0

    @api.depends('state', 'name', 'product_id')
    def _compute_pt_legacy(self):
        named = self.filtered('name')
        index = self._pt_legacy_index(named.mapped('name')) if named else {}
        reversed_names = self._pt_cleanup_reversed_names() if named else set()
        for mo in self:
            mode, qty = mo._pt_legacy_eval(index, reversed_names) if mo.name else (False, 0.0)
            mo.pt_legacy_mode = mode
            mo.pt_legacy_qty = qty
            mo.pt_legacy_pending = bool(mode)

    def _search_pt_legacy_pending(self, operator, value):
        if operator not in ('=', '!='):
            raise UserError(_('Unsupported search on old partial transfers.'))
        index = self._pt_legacy_index()
        reversed_names = self._pt_cleanup_reversed_names()
        candidates = self.search([('name', 'in', list(index.keys()))])
        ids = [mo.id for mo in candidates if mo._pt_legacy_eval(index, reversed_names)[0]]
        positive = (operator == '=') == bool(value)
        return [('id', 'in' if positive else 'not in', ids)]

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------
    def action_pt_convert_legacy(self):
        if not self.env.user.has_group('mrp.group_mrp_manager'):
            raise AccessError(_('Only Manufacturing administrators can convert old partial transfers.'))
        index = self._pt_legacy_index(self.mapped('name'))
        reversed_names = self._pt_cleanup_reversed_names()
        done_lines, error_lines = [], []
        for mo in self:
            mode, qty = mo._pt_legacy_eval(index, reversed_names)
            if not mode:
                continue
            try:
                with self.env.cr.savepoint():
                    if mode == 'open':
                        mo._pt_convert_open(qty, index)
                    else:
                        mo._pt_convert_duplicate(qty, index)
                done_lines.append('%s: %s' % (mo.name, mode))
            except (UserError, AccessError) as exc:
                self.env.invalidate_all()
                error_lines.append('%s: %s' % (mo.name, exc.args[0] if exc.args else exc))
        if not done_lines and not error_lines:
            message = _('Nothing to convert on the selected Manufacturing Orders.')
        else:
            message = _('Converted: %(ok)s. Not converted: %(ko)s.',
                        ok=', '.join(done_lines) or _('none'),
                        ko='; '.join(error_lines) or _('none'))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Old partial transfers'),
                'message': message,
                'type': 'warning' if error_lines else 'success',
                'sticky': bool(error_lines),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _pt_convert_open(self, qty, index):
        self.ensure_one()
        self._pt_require('_post_inventory')
        rounding = self.product_uom_id.rounding
        if self.product_id.tracking != 'none':
            raise UserError(_('Tracked products are not supported.'))
        if not float_is_zero(self.qty_produced, precision_rounding=rounding):
            raise UserError(_(
                'The MO already has %s posted on it as well as old receipts; convert it by hand.',
                self.qty_produced))
        if float_compare(qty, self.product_qty, precision_rounding=rounding) > 0:
            raise UserError(_(
                'Old receipts (%(qty)s) exceed the MO quantity (%(total)s); convert it by hand.',
                qty=qty, total=self.product_qty))
        finished = self._pt_open_finished_move()
        if len(finished) != 1:
            raise UserError(_('Expected exactly one open finished-product move.'))
        legacy = self._pt_legacy_moves(index)

        # 1. Undo any reduction of the expected quantity (cleanup step 7).
        finished.product_uom_qty = self.product_uom_id._compute_quantity(
            self.product_qty - self.qty_produced, finished.product_uom)
        # 2. Put away what the old receipts left in Post-Production (filling a
        #    negative WH/Stock first, as cleanup step 1 did) before returning them,
        #    so those physical units end up in stock rather than reserved again
        #    by the customer pick chained to the MO.
        cancelled = self._pt_store_legacy_storage(legacy)
        # 3. Return the old receipts first, so the customer pick chained to the
        #    MO does not reserve the units posted in step 4.
        returned = self._pt_return_legacy(legacy, qty)
        # 4. Post the same quantity on the MO, with components for it.
        remaining = self.product_qty - self.qty_produced
        self.qty_producing = self.qty_produced + qty
        self._pt_set_components_for(qty)
        # The finished quantity is filled in by Odoo (qty_producing - qty_produced).
        if float_compare(qty, remaining, precision_rounding=rounding) < 0:
            done_before = self.move_finished_ids.filtered(lambda m: m.state == 'done')
            self.with_context(pt_posting=True)._post_inventory(cancel_backorder=False)
            self.env.invalidate_all()
            new_done = self.move_finished_ids.filtered(
                lambda m: m.state == 'done' and m.product_id == self.product_id) - done_before
            posted = sum(m.product_uom._compute_quantity(m.quantity, self.product_uom_id) for m in new_done)
            if float_compare(posted, qty, precision_rounding=rounding) != 0:
                raise UserError(_('Odoo posted %(p)s instead of %(q)s.', p=posted, q=qty))
        else:
            self.pt_used = True
            self.with_context(skip_consumption=True, pt_posting=True).button_mark_done()
            if self.state != 'done':
                raise UserError(_('The MO could not be closed; close it with Mark as Done.'))
        self.pt_used = True
        self._pt_log(_('Old partial transfers converted'), _(
            '%(qty)s %(uom)s received by the old app were returned (%(ret)s) and posted on '
            'this MO with their components. Old storage transfers put away or cancelled: %(sfp)s.',
            qty=qty, uom=self.product_uom_id.name, ret=returned.name,
            sfp=', '.join(cancelled) or _('none')))

    def _pt_convert_duplicate(self, qty, index):
        self.ensure_one()
        stock_root = self.picking_type_id.warehouse_id.lot_stock_id
        on_hand = self.product_id.with_context(location=stock_root.id).qty_available
        need = self.product_uom_id._compute_quantity(qty, self.product_id.uom_id)
        if float_compare(on_hand, need, precision_rounding=self.product_id.uom_id.rounding) < 0:
            raise UserError(_(
                'only %(on_hand)s %(uom)s on hand, so the %(need)s received twice cannot be '
                'returned without going negative. These units are already gone: correct '
                'the product with a count instead.',
                on_hand=on_hand, need=need, uom=self.product_id.uom_id.name))
        legacy = self._pt_legacy_moves(index)
        cancelled = self._pt_cancel_legacy_storage(legacy)
        returned = self._pt_return_legacy(legacy, qty)
        self._pt_log(_('Duplicate receipt returned'), _(
            '%(qty)s %(uom)s were received twice (old app receipt and the MO\'s own receipt). '
            'The old receipt was returned (%(ret)s). Old storage transfers cancelled: %(sfp)s.',
            qty=qty, uom=self.product_uom_id.name, ret=returned.name,
            sfp=', '.join(cancelled) or _('none')))

    # ------------------------------------------------------------------
    def _pt_store_legacy_storage(self, legacy):
        """Validate the old storage transfers for what they have reserved (with
        the negative fill), cancel the unreserved rest."""
        dest = legacy.move_dest_ids.filtered(lambda m: m.state not in ('done', 'cancel'))
        names = []
        for picking in dest.picking_id:
            ours = picking.move_ids & dest
            to_store = ours.filtered(lambda m: float_compare(
                m.quantity, 0.0, precision_rounding=m.product_uom.rounding) > 0)
            (ours - to_store)._action_cancel()
            if to_store:
                for move in to_store:
                    self._pt_fill_negative_first(move)
                    move.picked = True
                picking.with_context(skip_backorder=True, skip_sms=True,
                                     picking_ids_not_to_backorder=picking.ids).button_validate()
                if to_store.filtered(lambda m: m.state != 'done'):
                    raise UserError(_('Old storage transfer %s could not be validated.', picking.name))
                names.append(picking.name + _(' (put away)'))
            else:
                names.append(picking.name + _(' (cancelled)'))
        return names

    def _pt_cancel_legacy_storage(self, legacy):
        dest = legacy.move_dest_ids.filtered(lambda m: m.state not in ('done', 'cancel'))
        names = []
        for picking in dest.picking_id:
            if picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')) <= dest:
                picking.action_cancel()
            else:
                (picking.move_ids & dest)._action_cancel()
            names.append(picking.name)
        loose = dest.filtered(lambda m: not m.picking_id)
        if loose:
            loose._action_cancel()
        return names

    def _pt_return_legacy(self, legacy, qty):
        """Return up to ``qty`` (MO unit) of the old receipts, newest first, as a
        return of the original moves (origin_returned_move_id)."""
        self.ensure_one()
        uom = self.product_id.uom_id
        to_return = self.product_uom_id._compute_quantity(qty, uom)
        rounding = uom.rounding
        lines = []
        for move in legacy.sorted(lambda m: (m.date, m.id), reverse=True):
            if float_compare(to_return, 0.0, precision_rounding=rounding) <= 0:
                break
            if move.returned_move_ids.filtered(lambda r: r.state not in ('done', 'cancel')):
                raise UserError(_('An earlier return of %s is still open.', move.picking_id.name))
            available = move.product_qty - self._pt_returned_qty(move)
            take = min(available, to_return)
            if float_compare(take, 0.0, precision_rounding=rounding) > 0:
                lines.append((move, take))
                to_return -= take
        if float_compare(to_return, 0.0, precision_rounding=rounding) > 0 or not lines:
            raise UserError(_('Not enough old receipts left to return.'))
        sources = {m.location_dest_id for m, _q in lines}
        targets = {m.location_id for m, _q in lines}
        if len(sources) != 1 or len(targets) != 1:
            raise UserError(_('Old receipts went to different locations; convert by hand.'))
        src, dst = sources.pop(), targets.pop()
        qc_type = lines[0][0].picking_id.picking_type_id
        picking_type = qc_type.return_picking_type_id or self.picking_type_id.warehouse_id.int_type_id
        if not picking_type:
            raise UserError(_('No operation type found for the return.'))
        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'location_id': src.id,
            'location_dest_id': dst.id,
            'origin': _('Partial transfer conversion: return old receipts for %s', self.name),
            'company_id': self.company_id.id,
            'move_ids': [(0, 0, {
                'product_id': move.product_id.id,
                'product_uom_qty': uom._compute_quantity(take, move.product_uom),
                'product_uom': move.product_uom.id,
                'location_id': src.id,
                'location_dest_id': dst.id,
                'origin_returned_move_id': move.id,
                'company_id': self.company_id.id,
            }) for move, take in lines],
        })
        picking.action_confirm()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.with_context(skip_backorder=True, skip_sms=True).button_validate()
        if picking.state != 'done':
            raise UserError(_('Return %s could not be validated automatically.', picking.name))
        return picking

    def _pt_set_components_for(self, qty):
        """Set component quantities for ``qty`` finished units, counting what
        is already consumed on this MO (the old app consumed components in
        some builds). Every open component move is marked picked, so nothing
        is cancelled when posting."""
        self.ensure_one()
        groups = defaultdict(lambda: self.env['stock.move'])
        for move in self.move_raw_ids.filtered(lambda m: m.state != 'cancel'):
            groups[move.product_id] |= move
        for product, moves in groups.items():
            uom = product.uom_id
            done = moves.filtered(lambda m: m.state == 'done')
            open_moves = moves - done
            consumed = sum(m.product_uom._compute_quantity(m.quantity, uom) for m in done)
            planned = consumed + sum(open_moves.mapped('product_qty'))
            per_unit = planned / self.product_qty if self.product_qty else 0.0
            # per_unit is per MO unit (product_qty is in the MO's unit), qty too.
            need = max(per_unit * qty - consumed, 0.0)
            for move in open_moves.sorted('id'):
                take = min(need, move.product_qty)
                move.quantity = uom._compute_quantity(take, move.product_uom)
                move.picked = True
                need -= take
