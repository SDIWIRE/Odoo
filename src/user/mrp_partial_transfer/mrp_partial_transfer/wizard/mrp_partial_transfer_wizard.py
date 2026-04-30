# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class MrpPartialTransferWizard(models.TransientModel):
    _name = 'mrp.partial.transfer.wizard'
    _description = 'Transfer Partial Finished Goods to Stock'

    production_id = fields.Many2one(
        'mrp.production',
        string='Manufacturing Order',
        required=True,
        readonly=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string='Finished Product',
        readonly=True,
    )
    product_uom_id = fields.Many2one(
        'uom.uom',
        string='Unit of Measure',
        readonly=True,
    )
    qty_to_transfer = fields.Float(
        string='Quantity to Transfer',
        required=True,
        digits='Product Unit of Measure',
        help='Enter the quantity of finished goods to move into stock now. '
             'The Manufacturing Order will remain open for the remainder.',
    )
    qty_remaining = fields.Float(
        string='Remaining to Produce',
        readonly=True,
        digits='Product Unit of Measure',
    )
    location_dest_id = fields.Many2one(
        'stock.location',
        string='Destination Location',
        required=True,
        domain=[('usage', '=', 'internal')],
        help="Where to put the finished goods. Defaults to the MO's "
             "finished product destination.",
    )
    lot_id = fields.Many2one(
        'stock.lot',
        string='Lot/Serial Number',
        domain="[('product_id', '=', product_id)]",
        help='Optional: assign a lot or serial number to this partial transfer.',
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        production_id = self.env.context.get('default_production_id')
        if production_id:
            production = self.env['mrp.production'].browse(production_id)
            finished_move = production.move_finished_ids.filtered(
                lambda m: m.product_id == production.product_id
                and m.state not in ('done', 'cancel')
            )[:1]
            if finished_move:
                res['location_dest_id'] = finished_move.location_dest_id.id
        return res

    @api.constrains('qty_to_transfer', 'qty_remaining')
    def _check_qty(self):
        for wizard in self:
            if wizard.qty_to_transfer <= 0:
                raise UserError(_('Quantity to transfer must be greater than zero.'))
            if wizard.qty_to_transfer > wizard.qty_remaining:
                raise UserError(_(
                    'You cannot transfer more than the remaining quantity '
                    '(%(remaining)s %(uom)s).',
                    remaining=wizard.qty_remaining,
                    uom=wizard.product_uom_id.name,
                ))

    def action_confirm_transfer(self):
        """
        Odoo 19 compatible approach: instead of calling _split() on the MO's
        own stock moves (API changed in v17 and is fragile), we create a brand
        new stock move directly from the production virtual location to the
        destination. This is clean, does not touch the MO's internal moves,
        and does not trigger MO closure.
        """
        self.ensure_one()
        production = self.production_id

        if self.qty_to_transfer <= 0:
            raise UserError(_('Quantity to transfer must be greater than zero.'))
        if self.qty_to_transfer > self.qty_remaining:
            raise UserError(_(
                'Cannot transfer %(requested)s — only %(remaining)s %(uom)s remaining.',
                requested=self.qty_to_transfer,
                remaining=self.qty_remaining,
                uom=self.product_uom_id.name,
            ))

        # ── Step 1: Determine the production virtual location ────────────────
        # Try the product property first, then fall back to the MO's move
        production_location = None
        try:
            production_location = production.product_id.with_company(
                production.company_id
            ).property_stock_production
        except Exception:
            pass

        if not production_location:
            finished_move = production.move_finished_ids.filtered(
                lambda m: m.product_id == production.product_id
                and m.state not in ('done', 'cancel')
            )[:1]
            if finished_move:
                production_location = finished_move.location_id
            else:
                raise UserError(_(
                    'Cannot determine the production virtual location. '
                    'Please check the product and company configuration.'
                ))

        # ── Step 2: Create a new stock move: Virtual Production → Destination ─
        move_vals = {
            'name': _('Partial Transfer: %s') % production.name,
            'product_id': self.product_id.id,
            'product_uom': self.product_uom_id.id,
            'product_uom_qty': self.qty_to_transfer,
            'location_id': production_location.id,
            'location_dest_id': self.location_dest_id.id,
            'origin': production.name,
            'company_id': production.company_id.id,
            'state': 'draft',
        }
        partial_move = self.env['stock.move'].create(move_vals)
        partial_move._action_confirm()
        partial_move._action_assign()

        # ── Step 3: Set done quantity and lot on the move line ───────────────
        if not partial_move.move_line_ids:
            ml_vals = {
                'move_id': partial_move.id,
                'product_id': self.product_id.id,
                'product_uom_id': self.product_uom_id.id,
                'quantity': self.qty_to_transfer,
                'location_id': production_location.id,
                'location_dest_id': self.location_dest_id.id,
                'company_id': production.company_id.id,
            }
            if self.lot_id:
                ml_vals['lot_id'] = self.lot_id.id
            self.env['stock.move.line'].create(ml_vals)
        else:
            for ml in partial_move.move_line_ids:
                ml.quantity = self.qty_to_transfer
                if self.lot_id:
                    ml.lot_id = self.lot_id.id

        # ── Step 4: Validate → goods land in inventory immediately ───────────
        partial_move._action_done()

        # ── Step 5: Update MO qty_producing for internal progress tracking ───
        already_produced = production.qty_transferred_to_stock
        production.write({
            'qty_producing': already_produced + self.qty_to_transfer,
        })

        # ── Step 6: Proportionally consume raw material components ───────────
        fraction = self.qty_to_transfer / production.product_qty

        for raw_move in production.move_raw_ids.filtered(
            lambda m: m.state not in ('done', 'cancel')
        ):
            qty_to_consume = round(raw_move.product_uom_qty * fraction, 10)
            if qty_to_consume <= 0:
                continue
            qty_to_consume = min(qty_to_consume, raw_move.product_uom_qty)

            if not raw_move.move_line_ids:
                raw_move._action_assign()

            if raw_move.move_line_ids:
                for ml in raw_move.move_line_ids:
                    ml.quantity = ml.reserved_uom_qty or qty_to_consume
                raw_move._action_done()
            else:
                self.env['stock.move.line'].create({
                    'move_id': raw_move.id,
                    'product_id': raw_move.product_id.id,
                    'product_uom_id': raw_move.product_uom.id,
                    'quantity': qty_to_consume,
                    'location_id': raw_move.location_id.id,
                    'location_dest_id': raw_move.location_dest_id.id,
                    'company_id': production.company_id.id,
                })
                raw_move._action_done()

        # ── Step 7: Ensure MO stays open if there is remaining qty ───────────
        if production.state == 'done':
            remaining = production.qty_remaining_to_produce
            if remaining > 0:
                production.write({'state': 'progress'})
                _logger.info(
                    'MO %s forced back to progress after partial transfer. '
                    'Remaining: %s %s',
                    production.name, remaining, production.product_uom_id.name,
                )

        # ── Step 8: Log chatter note ─────────────────────────────────────────
        total_so_far = already_produced + self.qty_to_transfer
        production.message_post(
            body=_(
                '<b>Partial Transfer to Stock</b><br/>'
                'Transferred <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                'to <b>%(location)s</b>.<br/>'
                'Total produced so far: <b>%(total)s</b> of '
                '<b>%(demand)s %(uom)s</b> demanded.',
                qty=self.qty_to_transfer,
                uom=self.product_uom_id.name,
                product=self.product_id.display_name,
                location=self.location_dest_id.complete_name,
                total=total_so_far,
                demand=production.product_qty,
            )
        )

        return {'type': 'ir.actions.act_window_close'}
