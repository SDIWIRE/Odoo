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
        help='Where to put the finished goods. Defaults to the MO\'s '
             'finished product destination.',
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
            # Default destination = the MO's finished product move destination
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
                    'You cannot transfer more than the remaining quantity to produce '
                    '(%(remaining)s %(uom)s).',
                    remaining=wizard.qty_remaining,
                    uom=wizard.product_uom_id.name,
                ))

    def action_confirm_transfer(self):
        """
        Core logic: create a done stock move from the Production virtual
        location to the chosen destination, proportionally consume components,
        and leave the MO open.
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

        # ── Step 1: Find or prepare the finished-goods stock move ────────────
        # We look for an existing pending (not done/cancel) finished move for
        # the main product. If the move covers more than our partial qty we
        # split it so only our portion is marked done; the remainder stays open.
        finished_moves = production.move_finished_ids.filtered(
            lambda m: m.product_id == production.product_id
            and m.state not in ('done', 'cancel')
        )

        if not finished_moves:
            raise UserError(_(
                'No open finished-goods move found on this Manufacturing Order. '
                'The order may already be fully transferred or cancelled.'
            ))

        finished_move = finished_moves[0]

        # ── Step 2: Split the move if we're doing a partial ──────────────────
        # stock.move._split(qty) creates a new move for `qty` and reduces
        # the original by that amount. We then only validate the new split move.
        if self.qty_to_transfer < finished_move.product_uom_qty:
            # _split returns the ID of the new (smaller) move
            new_move_id = finished_move._split(self.qty_to_transfer)
            move_to_validate = self.env['stock.move'].browse(new_move_id)
            # Update destination location in case user changed it in wizard
            move_to_validate.location_dest_id = self.location_dest_id
        else:
            # Transferring the full remaining — use move as-is
            move_to_validate = finished_move
            move_to_validate.location_dest_id = self.location_dest_id

        # ── Step 3: Set quantity_done and lot if provided ────────────────────
        move_to_validate.quantity_done = self.qty_to_transfer

        # Handle lot/serial tracking
        if self.lot_id or production.product_id.tracking != 'none':
            # Ensure a move line exists
            if not move_to_validate.move_line_ids:
                move_to_validate._action_assign()

            for ml in move_to_validate.move_line_ids:
                ml.qty_done = self.qty_to_transfer
                if self.lot_id:
                    ml.lot_id = self.lot_id
        else:
            # No tracking — just set qty_done directly on the move line
            if not move_to_validate.move_line_ids:
                move_to_validate._action_assign()
                if not move_to_validate.move_line_ids:
                    # Create a move line manually if assign didn't produce one
                    self.env['stock.move.line'].create({
                        'move_id': move_to_validate.id,
                        'product_id': move_to_validate.product_id.id,
                        'product_uom_id': move_to_validate.product_uom.id,
                        'qty_done': self.qty_to_transfer,
                        'location_id': move_to_validate.location_id.id,
                        'location_dest_id': self.location_dest_id.id,
                    })
            else:
                move_to_validate.move_line_ids[0].qty_done = self.qty_to_transfer

        # ── Step 4: Validate ONLY this move (not the whole MO) ───────────────
        move_to_validate._action_done()

        # ── Step 5: Proportionally consume raw material components ───────────
        # Calculate what fraction of the total MO qty this transfer represents
        fraction = self.qty_to_transfer / production.product_qty

        for raw_move in production.move_raw_ids.filtered(
            lambda m: m.state not in ('done', 'cancel')
        ):
            qty_to_consume = raw_move.product_uom_qty * fraction

            if qty_to_consume <= 0:
                continue

            # Clamp to what's actually available on the move
            qty_to_consume = min(qty_to_consume, raw_move.product_uom_qty)

            if qty_to_consume < raw_move.product_uom_qty:
                new_raw_id = raw_move._split(qty_to_consume)
                raw_move_to_validate = self.env['stock.move'].browse(new_raw_id)
            else:
                raw_move_to_validate = raw_move

            raw_move_to_validate.quantity_done = qty_to_consume
            if not raw_move_to_validate.move_line_ids:
                raw_move_to_validate._action_assign()
            for ml in raw_move_to_validate.move_line_ids:
                ml.qty_done = ml.reserved_uom_qty or qty_to_consume

            raw_move_to_validate._action_done()

        # ── Step 6: Force MO back to 'progress' (not 'done') ─────────────────
        # _action_done on moves can trigger the MO to auto-close if all moves
        # are done. We prevent that by writing state back if needed.
        if production.state == 'done':
            # Check if there's genuinely remaining qty — if so reopen
            remaining = production.qty_remaining_to_produce
            if remaining > 0:
                production.write({'state': 'progress'})
                _logger.info(
                    'MO %s set back to progress after partial transfer. '
                    'Remaining: %s %s',
                    production.name,
                    remaining,
                    production.product_uom_id.name,
                )

        # ── Step 7: Log a note on the MO chatter ────────────────────────────
        production.message_post(
            body=_(
                '<b>Partial Transfer to Stock</b><br/>'
                'Transferred <b>%(qty)s %(uom)s</b> of <b>%(product)s</b> '
                'to <b>%(location)s</b>.<br/>'
                'Remaining to produce: <b>%(remaining)s %(uom)s</b>.',
                qty=self.qty_to_transfer,
                uom=self.product_uom_id.name,
                product=self.product_id.display_name,
                location=self.location_dest_id.complete_name,
                remaining=production.qty_remaining_to_produce,
            )
        )

        return {'type': 'ir.actions.act_window_close'}
