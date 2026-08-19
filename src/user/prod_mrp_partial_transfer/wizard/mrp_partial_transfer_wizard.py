# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_compare
import logging

_logger = logging.getLogger(__name__)


class MrpPartialTransferWizard(models.TransientModel):
    _name = 'mrp.partial.transfer.wizard'
    _description = 'Transfer Partial Finished Goods to Stock'

    production_id = fields.Many2one('mrp.production', string='Manufacturing Order', required=True, readonly=True)
    product_id = fields.Many2one('product.product', string='Finished Product', readonly=True)
    product_uom_id = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    qty_to_transfer = fields.Float(string='Quantity to Transfer', required=True, digits='Product Unit of Measure')
    qty_remaining = fields.Float(string='Remaining to Produce', readonly=True, digits='Product Unit of Measure')
    location_dest_id = fields.Many2one('stock.location', string='Destination Location', required=True,
                                        domain=[('usage', '=', 'internal')])
    lot_id = fields.Many2one('stock.lot', string='Lot/Serial Number', domain="[('product_id', '=', product_id)]")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        production_id = self.env.context.get('default_production_id')
        if production_id:
            production = self.env['mrp.production'].browse(production_id)
            finished_move = production.move_finished_ids.filtered(
                lambda m: m.product_id == production.product_id and m.state not in ('done', 'cancel')
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
                    'You cannot transfer more than the remaining quantity (%(remaining)s %(uom)s).',
                    remaining=wizard.qty_remaining, uom=wizard.product_uom_id.name,
                ))

    def action_confirm_transfer(self):
        self.ensure_one()
        production = self.production_id

        if self.qty_to_transfer <= 0:
            raise UserError(_('Quantity to transfer must be greater than zero.'))
        if self.qty_to_transfer > self.qty_remaining:
            raise UserError(_(
                'Cannot transfer %(requested)s — only %(remaining)s %(uom)s remaining.',
                requested=self.qty_to_transfer, remaining=self.qty_remaining, uom=self.product_uom_id.name,
            ))

        # ── Step 1: Find the production virtual location ─────────────────────
        finished_move = production.move_finished_ids.filtered(
            lambda m: m.product_id == production.product_id and m.state not in ('done', 'cancel')
        )[:1]
        if finished_move:
            production_location = finished_move.location_id
        else:
            raise UserError(_('No open finished-goods move found on this Manufacturing Order.'))

        # ── Step 2: Find an internal picking type ────────────────────────────
        picking_type = self.env['stock.picking.type'].search([
            ('code', '=', 'internal'),
            ('company_id', '=', production.company_id.id),
            ('warehouse_id', '!=', False),
        ], limit=1)
        if not picking_type:
            raise UserError(_(
                'No internal transfer operation type found for company %s.',
                production.company_id.name,
            ))

        # ── Step 3: Create internal picking ──────────────────────────────────
        picking = self.env['stock.picking'].create({
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
        })
        picking.action_confirm()
        picking.action_assign()

        # ── Step 4: Set done quantity on move lines ───────────────────────────
        for move in picking.move_ids:
            if not move.move_line_ids:
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

        # ── Step 5: Validate picking → goods in inventory immediately ─────────
        picking.with_context(skip_backorder=True).button_validate()

        # ── Step 6: Update qty_transferred_to_stock on the MO ────────────────
        already_produced = production.qty_transferred_to_stock
        new_total = already_produced + self.qty_to_transfer
        production.write({'qty_transferred_to_stock': new_total})

        try:
            production.write({'qty_producing': new_total})
        except Exception as e:
            _logger.warning('Could not update qty_producing on MO %s: %s', production.name, e)

        # ── Step 7: Proportionally consume raw material components ────────────
        fraction = self.qty_to_transfer / production.product_qty
        for raw_move in production.move_raw_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            qty_to_consume = round(raw_move.product_uom_qty * fraction, 10)
            if qty_to_consume <= 0:
                continue
            qty_to_consume = min(qty_to_consume, raw_move.product_uom_qty)
            if not raw_move.move_line_ids:
                raw_move._action_assign()
            if raw_move.move_line_ids:
                for ml in raw_move.move_line_ids:
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

        # ── Step 8: Auto-close or keep open ──────────────────────────────────
        rounding = production.product_uom_id.rounding
        remaining_after = production.product_qty - new_total

        if float_compare(remaining_after, 0, precision_rounding=rounding) <= 0:
            # Fully transferred — close the MO
            _logger.info('MO %s fully transferred. Auto-closing.', production.name)
            production.write({
                'state': 'done',
                'date_finished': fields.Datetime.now(),
            })
            production.message_post(body=_(
                '<b>Partial Transfer to Stock — Order Complete</b><br/>'
                'Final transfer: <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                'to <b>%(location)s</b> via picking <b>%(picking)s</b>.<br/>'
                'Total transferred: <b>%(total)s of %(demand)s %(uom)s</b>.<br/>'
                'Manufacturing Order automatically closed.',
                qty=self.qty_to_transfer, uom=self.product_uom_id.name,
                product=self.product_id.display_name,
                location=self.location_dest_id.complete_name,
                picking=picking.name, total=new_total, demand=production.product_qty,
            ))
        else:
            # Still more to produce — keep open
            if production.state == 'done':
                production.write({'state': 'progress'})
            production.message_post(body=_(
                '<b>Partial Transfer to Stock</b><br/>'
                'Transferred <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                'to <b>%(location)s</b> via picking <b>%(picking)s</b>.<br/>'
                'Total so far: <b>%(total)s</b> of <b>%(demand)s %(uom)s</b>. '
                'Remaining: <b>%(remaining)s %(uom)s</b>.',
                qty=self.qty_to_transfer, uom=self.product_uom_id.name,
                product=self.product_id.display_name,
                location=self.location_dest_id.complete_name,
                picking=picking.name, total=new_total,
                demand=production.product_qty, remaining=remaining_after,
            ))

        return {'type': 'ir.actions.act_window_close'}
