# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

BATCH_SIZE = 50  # Process this many moves per RPC call to avoid timeout


class MrpFixStuckMovesWizard(models.TransientModel):
    _name = 'mrp.fix.stuck.moves.wizard'
    _description = 'Fix Stuck Raw Material Moves on Done Manufacturing Orders'

    stuck_move_count = fields.Integer(string='Stuck Moves Found', readonly=True, default=0)
    affected_mo_count = fields.Integer(string='Affected MOs', readonly=True, default=0)
    fixed_count = fields.Integer(string='Fixed So Far', readonly=True, default=0)
    error_count = fields.Integer(string='Errors', readonly=True, default=0)
    state = fields.Selection([
        ('preview', 'Preview'),
        ('in_progress', 'In Progress'),
        ('done', 'Complete'),
    ], default='preview', readonly=True)
    result_summary = fields.Text(string='Result', readonly=True)
    has_more = fields.Boolean(string='More to Process', readonly=True, default=False)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        stuck = self._get_stuck_moves()
        res['stuck_move_count'] = len(stuck)
        res['affected_mo_count'] = len(stuck.mapped('raw_material_production_id'))
        res['has_more'] = len(stuck) > 0
        return res

    def _get_stuck_moves(self):
        return self.env['stock.move'].search([
            ('raw_material_production_id', '!=', False),
            ('state', '=', 'assigned'),
            ('raw_material_production_id.state', '=', 'done'),
        ], limit=BATCH_SIZE)

    def _get_total_remaining(self):
        return self.env['stock.move'].search_count([
            ('raw_material_production_id', '!=', False),
            ('state', '=', 'assigned'),
            ('raw_material_production_id.state', '=', 'done'),
        ])

    def action_fix_batch(self):
        """Process one batch of BATCH_SIZE moves. Called repeatedly until done."""
        self.ensure_one()
        stuck_moves = self._get_stuck_moves()

        if not stuck_moves:
            self.write({
                'state': 'done',
                'has_more': False,
                'result_summary': f'Complete! Fixed {self.fixed_count} moves. Errors: {self.error_count}.',
            })
            return self._reload()

        fixed = 0
        errors = []

        for move in stuck_moves:
            mo = move.raw_material_production_id
            try:
                if not move.move_line_ids:
                    move._action_assign()

                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        ml.quantity = ml.quantity_product_uom or move.product_uom_qty
                else:
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': move.product_id.id,
                        'product_uom_id': move.product_uom.id,
                        'quantity': move.product_uom_qty,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        'company_id': move.company_id.id,
                    })

                # In Odoo 18/19 a move must be marked 'picked' for _action_done()
                # to validate it — without this the move is silently skipped and
                # stays in 'assigned' state.
                move.picked = True

                move._action_done()
                fixed += 1

            except Exception as e:
                msg = f"{mo.name} / {move.product_id.display_name}: {e}"
                errors.append(msg)
                _logger.error('Could not fix stuck move %s: %s', move.id, e)

        remaining = self._get_total_remaining()
        new_fixed = self.fixed_count + fixed
        new_errors = self.error_count + len(errors)

        self.write({
            'state': 'in_progress' if remaining > 0 else 'done',
            'fixed_count': new_fixed,
            'error_count': new_errors,
            'has_more': remaining > 0,
            'stuck_move_count': remaining,
            'result_summary': (
                f"Fixed {new_fixed} moves so far. "
                f"{remaining} remaining. "
                f"Errors: {new_errors}."
                + (f"\nLast batch errors:\n" + '\n'.join(f'  • {e}' for e in errors) if errors else '')
            ) if remaining > 0 else (
                f"Complete! Fixed {new_fixed} moves across all affected MOs. "
                f"Errors: {new_errors}."
                + (f"\nErrors:\n" + '\n'.join(f'  • {e}' for e in errors) if errors else '')
            ),
        })

        return self._reload()

    def _reload(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
