# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    # Stored float — written directly by the wizard each time a partial
    # transfer is confirmed. Not computed, because our transfers go through
    # a separate picking and don't touch move_finished_ids.
    qty_transferred_to_stock = fields.Float(
        string='Transferred to Stock',
        store=True,
        default=0.0,
        digits='Product Unit of Measure',
        help='Cumulative quantity of finished goods transferred to stock '
             'via partial transfers while this MO was still open.',
    )

    qty_remaining_to_produce = fields.Float(
        string='Remaining to Produce',
        compute='_compute_partial_transfer_fields',
        store=True,
        digits='Product Unit of Measure',
        help='Remaining quantity still to be produced and transferred.',
    )

    qty_progress_pct = fields.Float(
        string='% Complete',
        compute='_compute_partial_transfer_fields',
        store=True,
        digits=(5, 1),
        help='Percentage of total demand already transferred to stock.',
    )

    show_partial_transfer_button = fields.Boolean(
        string='Show Partial Transfer Button',
        compute='_compute_partial_transfer_fields',
    )

    @api.depends('qty_transferred_to_stock', 'product_qty', 'state')
    def _compute_partial_transfer_fields(self):
        for production in self:
            transferred = production.qty_transferred_to_stock
            demand = production.product_qty or 0.0
            remaining = max(0.0, demand - transferred)
            production.qty_remaining_to_produce = remaining
            production.qty_progress_pct = min(100.0, (transferred / demand * 100.0)) if demand else 0.0
            production.show_partial_transfer_button = (
                production.state in ('confirmed', 'progress')
                and remaining > 0
            )

    def action_partial_transfer_to_stock(self):
        self.ensure_one()
        if self.state not in ('confirmed', 'progress'):
            raise UserError(_(
                'You can only do a partial transfer on a confirmed or '
                'in-progress Manufacturing Order.'
            ))
        if self.qty_remaining_to_produce <= 0:
            raise UserError(_(
                'There is no remaining quantity to transfer. '
                'The Manufacturing Order is already fully transferred.'
            ))
        return {
            'name': _('Transfer Partial Quantity to Stock'),
            'type': 'ir.actions.act_window',
            'res_model': 'mrp.partial.transfer.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_production_id': self.id,
                'default_qty_to_transfer': self.qty_remaining_to_produce,
                'default_product_id': self.product_id.id,
                'default_product_uom_id': self.product_uom_id.id,
                'default_qty_remaining': self.qty_remaining_to_produce,
            },
        }
