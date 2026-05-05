# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_compare
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
        Odoo 19 approach: use stock.picking to move finished goods from
        the production virtual location to the destination. stock.picking
        handles all the stock.move field requirements internally, so we
        don't need to know which fields stock.move requires — we just
        create a picking with move_ids_without_package.
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

        # ── Step 1: Find the production virtual location ─────────────────────
        # Get it from the existing finished move — most reliable source
        finished_move = production.move_finished_ids.filtered(
            lambda m: m.product_id == production.product_id
            and m.state not in ('done', 'cancel')
        )[:1]

        if finished_move:
            production_location = finished_move.location_id
        else:
            raise UserError(_(
                'No open finished-goods move found on this Manufacturing Order. '
                'It may already be fully transferred or cancelled.'
            ))

        # ── Step 2: Find an internal picking type for this company ────────────
        # We need a picking_type to create the picking. Use 'internal transfer'.
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('company_id', '=', production.company_id.id),
            ('warehouse_id', '!=', False),
        ], limit=1)

        if not picking_type:
            raise UserError(_(
                'No internal transfer operation type found for company %s. '
                'Please configure one in Inventory > Configuration > Operation Types.',
                production.company_id.name,
            ))

        # ── Step 3: Create a stock.picking (internal transfer) ────────────────
        # Let Odoo handle all the stock.move field defaults via the picking.
        # This avoids us needing to know every required field on stock.move.
        picking_vals = {
            'picking_type_id': picking_type.id,
            'location_id': production_location.id,
            'location_dest_id': self.location_dest_id.id,
            'origin': production.name,
            'company_id': production.company_id.id,
            'move_ids': [(0, 0, {
                'product_id': self.product_id.id,
                'product_uom_qty': self.qty_to_transfer,
                'product_uom': self.product_uom_id.id,
                'location_id': production_location.id,
                'location_dest_id': self.location_dest_id.id,
            })],
        }
        picking = self.env['stock.picking'].create(picking_vals)
        picking.action_confirm()
        picking.action_assign()

        # ── Step 4: Set done quantity and lot on the move lines ───────────────
        for move in picking.move_ids:
            if not move.move_line_ids:
                # Force create a move line if auto-assign didn't make one
                # (production virtual location has no tracked quants)
                self.env['stock.move.line'].create({
                    'move_id': move.id,
                    'picking_id': picking.id,
                    'product_id': self.product_id.id,
                    'product_uom_id': self.product_uom_id.id,
                    'quantity': self.qty_to_transfer,
                    'location_id': production_location.id,
                    'location_dest_id': self.location_dest_id.id,
                    'company_id': production.company_id.id,
                })
            else:
                for ml in move.move_line_ids:
                    ml.quantity = self.qty_to_transfer
                    if self.lot_id:
                        ml.lot_id = self.lot_id.id

        # ── Step 5: Validate picking → goods land in inventory immediately ────
        picking.with_context(skip_backorder=True).button_validate()

        # ── Step 6: Update transferred qty on the MO (drives running total) ───
        already_produced = production.qty_transferred_to_stock
        new_total = already_produced + self.qty_to_transfer
        production.write({'qty_transferred_to_stock': new_total})

        # Also update qty_producing for Odoo's internal progress tracking
        try:
            production.write({'qty_producing': new_total})
        except Exception as e:
            _logger.warning('Could not update qty_producing on MO %s: %s', production.name, e)

        # ── Step 7: Proportionally consume raw material components ────────────
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
                    # quantity_product_uom is the reserved qty field in Odoo 17+
                    ml.quantity = ml.quantity_product_uom or qty_to_consume
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

        # ── Step 8: Auto-close or keep open depending on remaining qty ──────────
        total_so_far = already_produced + self.qty_to_transfer
        remaining_after = production.product_qty - total_so_far

        rounding = production.product_uom_id.rounding

        if float_compare(remaining_after, 0, precision_rounding=rounding) <= 0:
            # ── All demand has been transferred — close the MO ────────────────
            _logger.info(
                'MO %s fully transferred (%s %s). Auto-closing.',
                production.name, total_so_far, production.product_uom_id.name,
            )
            # Cancel any remaining open stock moves on the MO so it can close
            open_finished = production.move_finished_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')
            )
            if open_finished:
                open_finished.write({'state': 'cancel'})

            open_raw = production.move_raw_ids.filtered(
                lambda m: m.state not in ('done', 'cancel')
            )
            if open_raw:
                open_raw.write({'state': 'cancel'})

            production.write({
                'state': 'done',
                'date_finished': fields.Datetime.now(),
            })

            production.message_post(
                body=_(
                    '<b>Partial Transfer to Stock — Order Complete</b><br/>'
                    'Final transfer: <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                    'to <b>%(location)s</b> via picking <b>%(picking)s</b>.<br/>'
                    'Total transferred: <b>%(total)s of %(demand)s %(uom)s</b>.<br/>'
                    'Manufacturing Order has been automatically closed.',
                    qty=self.qty_to_transfer,
                    uom=self.product_uom_id.name,
                    product=self.product_id.display_name,
                    location=self.location_dest_id.complete_name,
                    picking=picking.name,
                    total=total_so_far,
                    demand=production.product_qty,
                )
            )
        else:
            # ── Still more to produce — keep the MO open ──────────────────────
            if production.state == 'done':
                production.write({'state': 'progress'})
                _logger.info(
                    'MO %s forced back to progress. Remaining: %s %s',
                    production.name, remaining_after, production.product_uom_id.name,
                )

            production.message_post(
                body=_(
                    '<b>Partial Transfer to Stock</b><br/>'
                    'Transferred <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                    'to <b>%(location)s</b> via picking <b>%(picking)s</b>.<br/>'
                    'Total produced so far: <b>%(total)s</b> of '
                    '<b>%(demand)s %(uom)s</b> demanded. '
                    'Remaining: <b>%(remaining)s %(uom)s</b>.',
                    qty=self.qty_to_transfer,
                    uom=self.product_uom_id.name,
                    product=self.product_id.display_name,
                    location=self.location_dest_id.complete_name,
                    picking=picking.name,
                    total=total_so_far,
                    demand=production.product_qty,
                    remaining=remaining_after,
                )
            )

        return {'type': 'ir.actions.act_window_close'}
