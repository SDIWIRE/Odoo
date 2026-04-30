# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError


class MrpProduction(models.Model):
    _inherit = 'mrp.production'

    qty_transferred_to_stock = fields.Float(
        string='Qty Transferred to Stock',
        compute='_compute_qty_transferred_to_stock',
        store=True,
        help='Total quantity of finished goods already transferred to stock '
             'via partial transfers while this MO was still open.',
    )

    qty_remaining_to_produce = fields.Float(
        string='Qty Remaining to Produce',
        compute='_compute_qty_transferred_to_stock',
        store=True,
        help='Remaining quantity still to be produced and transferred.',
    )

    show_partial_transfer_button = fields.Boolean(
        string='Show Partial Transfer Button',
        compute='_compute_show_partial_transfer_button',
    )

    @api.depends('move_finished_ids', 'move_finished_ids.state',
                 'move_finished_ids.quantity', 'product_qty')
    def _compute_qty_transferred_to_stock(self):
        for production in self:
            # In Odoo 17+, 'quantity' replaces 'quantity_done' on stock.move.
            # For done moves, 'quantity' holds the validated amount.
            done_moves = production.move_finished_ids.filtered(
                lambda m: m.state == 'done'
                and m.product_id == production.product_id
            )
            transferred = sum(done_moves.mapped('quantity'))
            production.qty_transferred_to_stock = transferred
            production.qty_remaining_to_produce = max(
                0.0, production.product_qty - transferred
            )

    @api.depends('state', 'qty_remaining_to_produce', 'product_qty')
    def _compute_show_partial_transfer_button(self):
        for production in self:
            production.show_partial_transfer_button = (
                production.state in ('confirmed', 'progress')
                and production.qty_remaining_to_produce > 0
            )

    def action_partial_transfer_to_stock(self):
        """Open the wizard to transfer a partial quantity to stock."""
        self.ensure_one()
        if self.state not in ('confirmed', 'progress'):
            raise UserError(_(
                'You can only do a partial transfer on a confirmed or '
                'in-progress Manufacturing Order.'
            ))
        if self.qty_remaining_to_produce <= 0:
            raise UserError(_(
                'There is no remaining quantity to transfer. '
                'Please validate the Manufacturing Order to close it.'
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
