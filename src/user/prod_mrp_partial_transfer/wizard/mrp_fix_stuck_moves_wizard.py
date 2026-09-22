# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class MrpFixStuckMovesWizard(models.TransientModel):
    _name = 'mrp.fix.stuck.moves.wizard'
    _description = 'Fix Stuck Raw Material Moves on Done Manufacturing Orders'

    stuck_move_count = fields.Integer(
        string='Stuck Moves Found',
        readonly=True,
        default=0,
    )
    affected_mo_count = fields.Integer(
        string='Affected MOs',
        readonly=True,
        default=0,
    )
    state = fields.Selection([
        ('preview', 'Preview'),
        ('done', 'Complete'),
    ], default='preview', readonly=True)
    result_summary = fields.Text(
        string='Result',
        readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        stuck = self._get_stuck_moves()
        res['stuck_move_count'] = len(stuck)
        res['affected_mo_count'] = len(stuck.mapped('raw_material_production_id'))
        return res

    def _get_stuck_moves(self):
        return self.env['stock.move'].search([
            ('raw_material_production_id', '!=', False),
            ('state', '=', 'assigned'),
            ('raw_material_production_id.state', '=', 'done'),
        ])

    def action_fix(self):
        self.ensure_one()
        stuck_moves = self._get_stuck_moves()

        if not stuck_moves:
            self.write({
                'state': 'done',
                'result_summary': 'No stuck moves found — nothing to fix.',
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

                move._action_done()
                fixed += 1
                _logger.info('Fixed stuck move %s on MO %s', move.id, mo.name)

            except Exception as e:
                msg = f"{mo.name} / {move.product_id.display_name}: {e}"
                errors.append(msg)
                _logger.error('Could not fix stuck move %s: %s', move.id, e)

        remaining = len(self._get_stuck_moves())

        summary_lines = [f"Fixed: {fixed} moves."]
        if errors:
            summary_lines.append(f"Errors ({len(errors)}):")
            summary_lines.extend(f"  • {e}" for e in errors)
        if remaining:
            summary_lines.append(
                f"\nWarning: {remaining} move(s) could not be fixed automatically. "
                "Manual review required."
            )
        else:
            summary_lines.append("All stuck moves resolved successfully.")

        self.write({
            'state': 'done',
            'stuck_move_count': fixed,
            'result_summary': '\n'.join(summary_lines),
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
