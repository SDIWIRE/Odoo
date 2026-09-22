# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
from odoo.tools import float_compare
import logging

_logger = logging.getLogger(__name__)


class MrpPartialTransferWizard(models.TransientModel):
    _name = 'mrp.partial.transfer.wizard'
    _description = 'Transfer Partial Finished Goods to Stock'

    production_id = fields.Many2one('mrp.production', string='Manufacturing Order',
                                     required=True, readonly=True)
    product_id = fields.Many2one('product.product', string='Finished Product', readonly=True)
    product_uom_id = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    qty_to_transfer = fields.Float(string='Quantity to Transfer', required=True,
                                    digits='Product Unit of Measure')
    qty_remaining = fields.Float(string='Remaining to Produce', readonly=True,
                                  digits='Product Unit of Measure')
    location_dest_id = fields.Many2one('stock.location', string='Destination Location',
                                        required=True, domain=[('usage', '=', 'internal')])
    lot_id = fields.Many2one('stock.lot', string='Lot/Serial Number',
                              domain="[('product_id', '=', product_id)]")

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
        finished_move = production.move_finished_ids.filtered(
            lambda m: m.product_id == production.product_id
            and m.state not in ('done', 'cancel')
        )[:1]
        if not finished_move:
            raise UserError(_('No open finished-goods move found on this Manufacturing Order.'))
        production_location = finished_move.location_id

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

        # ── Step 7: Auto-close or keep open ──────────────────────────────────
        rounding = production.product_uom_id.rounding
        remaining_after = production.product_qty - new_total

        if float_compare(remaining_after, 0, precision_rounding=rounding) <= 0:
            # ── Fully transferred — close via button_mark_done ────────────────
            # This is critical: using button_mark_done (not a direct state write)
            # ensures Odoo's standard costing flow runs, which:
            #   - Validates all remaining raw material moves (clears ASSIGNED status)
            #   - Creates Stock Valuation Layer (SVL) entries
            #   - Writes unit_cost onto stock.move records
            #   - Sets MO state to done with date_finished properly
            _logger.info(
                'MO %s fully transferred (%s %s). Closing via button_mark_done.',
                production.name, new_total, production.product_uom_id.name,
            )

            # Set qty_producing to full demand so Odoo knows all is accounted for
            try:
                production.write({'qty_producing': production.product_qty})
            except Exception as e:
                _logger.warning('Could not set qty_producing on MO %s: %s', production.name, e)

            try:
                result = production.button_mark_done()

                # button_mark_done can return a wizard action in two cases:
                # 1. mrp.immediate.production  — qty not set, needs confirmation
                # 2. mrp.production.backorder  — partial qty, asks about backorder
                # We handle both to ensure clean closure with no backorder.
                if isinstance(result, dict) and result.get('res_model'):
                    wizard_model = result['res_model']
                    _logger.info('button_mark_done returned wizard: %s', wizard_model)

                    if wizard_model == 'mrp.immediate.production':
                        wizard = self.env[wizard_model].with_context(
                            result.get('context', {})
                        ).create({'production_ids': [(4, production.id)]})
                        wizard.process()

                    elif wizard_model == 'mrp.production.backorder':
                        # Tell Odoo: do NOT create a backorder
                        wizard = self.env[wizard_model].with_context(
                            result.get('context', {})
                        ).create({
                            'mrp_production_backorder_line_ids': [(0, 0, {
                                'mrp_production_id': production.id,
                                'to_backorder': False,
                            })]
                        })
                        wizard.action_close_production()

            except Exception as e:
                # Fallback: direct write if button_mark_done fails unexpectedly.
                # Log clearly so the admin knows costing may need manual review.
                _logger.error(
                    'button_mark_done failed on MO %s: %s. '
                    'Falling back to direct state write — unit costs may need '
                    'manual review in stock.move.',
                    production.name, e,
                )
                production.write({
                    'state': 'done',
                    'date_finished': fields.Datetime.now(),
                })

            production.message_post(body=_(
                '<b>Partial Transfer to Stock — Order Complete</b><br/>'
                'Final transfer: <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                'to <b>%(location)s</b> via picking <b>%(picking)s</b>.<br/>'
                'Total transferred: <b>%(total)s of %(demand)s %(uom)s</b>.<br/>'
                'Manufacturing Order closed. Stock moves and unit costs '
                'processed via standard Odoo costing flow.',
                qty=self.qty_to_transfer,
                uom=self.product_uom_id.name,
                product=self.product_id.display_name,
                location=self.location_dest_id.complete_name,
                picking=picking.name,
                total=new_total,
                demand=production.product_qty,
            ))

        else:
            # ── Still more to produce — keep MO open ──────────────────────────
            if production.state == 'done':
                production.write({'state': 'progress'})
                _logger.info(
                    'MO %s forced back to progress. Remaining: %s %s',
                    production.name, remaining_after, production.product_uom_id.name,
                )

            production.message_post(body=_(
                '<b>Partial Transfer to Stock</b><br/>'
                'Transferred <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                'to <b>%(location)s</b> via picking <b>%(picking)s</b>.<br/>'
                'Total so far: <b>%(total)s</b> of <b>%(demand)s %(uom)s</b>. '
                'Remaining: <b>%(remaining)s %(uom)s</b>.',
                qty=self.qty_to_transfer,
                uom=self.product_uom_id.name,
                product=self.product_id.display_name,
                location=self.location_dest_id.complete_name,
                picking=picking.name,
                total=new_total,
                demand=production.product_qty,
                remaining=remaining_after,
            ))

        return {'type': 'ir.actions.act_window_close'}
