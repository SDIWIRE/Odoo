# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round


class MrpPartialTransferWizard(models.TransientModel):
    _name = 'mrp.partial.transfer.wizard'
    _description = 'Transfer Partial Finished Goods to Stock'

    production_id = fields.Many2one('mrp.production', string='Manufacturing Order',
                                    required=True, readonly=True)
    product_id = fields.Many2one(related='production_id.product_id')
    product_uom_id = fields.Many2one(related='production_id.product_uom_id')
    qty_ordered = fields.Float(related='production_id.product_qty', string='Ordered')
    qty_done = fields.Float(related='production_id.qty_produced', string='Already Transferred')
    qty_remaining = fields.Float(string='Remaining', compute='_compute_qty_remaining',
                                 digits='Product Unit of Measure')
    qty_to_transfer = fields.Float(string='Quantity to Transfer Now', required=True,
                                   digits='Product Unit of Measure')
    is_final = fields.Boolean(compute='_compute_qty_remaining')
    allow_component_shortage = fields.Boolean(
        string='Post even if components are short',
        help='Components missing from their source location will go negative.')
    accept_consumption_differences = fields.Boolean(
        string='Accept consumption differences',
        help='Close the MO even if component consumption differs from the BoM.')

    @api.depends('production_id.product_qty', 'production_id.qty_produced', 'qty_to_transfer')
    def _compute_qty_remaining(self):
        for wiz in self:
            mo = wiz.production_id
            rounding = mo.product_uom_id.rounding or 0.0001
            wiz.qty_remaining = max(mo.product_qty - mo.qty_produced, 0.0)
            wiz.is_final = bool(mo) and float_compare(
                wiz.qty_to_transfer, wiz.qty_remaining, precision_rounding=rounding) >= 0

    # ------------------------------------------------------------------
    def action_confirm_transfer(self):
        self.ensure_one()
        mo = self.production_id
        mo._pt_check_can_transfer()
        mo._pt_require('_set_qty_producing')
        mo._pt_require('_post_inventory')
        rounding = mo.product_uom_id.rounding
        qty = float_round(self.qty_to_transfer, precision_rounding=rounding)
        remaining = mo.product_qty - mo.qty_produced
        if float_compare(qty, 0.0, precision_rounding=rounding) <= 0:
            raise UserError(_('Enter a quantity greater than zero.'))
        if float_compare(qty, remaining, precision_rounding=rounding) > 0:
            raise UserError(_(
                'You cannot transfer more than the remaining %(rem)s %(uom)s on %(mo)s.',
                rem=remaining, uom=mo.product_uom_id.name, mo=mo.name))
        if float_compare(qty, remaining, precision_rounding=rounding) == 0:
            self._post_final(mo, qty)
        else:
            self._post_partial(mo, qty)
        return {'type': 'ir.actions.act_window_close'}

    # ------------------------------------------------------------------
    def _prepare(self, mo, qty, target_producing):
        """Set what Odoo should post: qty_producing, and components pro rata
        through Odoo's own qty_producing logic. The finished quantity is left
        to Odoo: in Odoo 19 a picked finished move already counts as produced,
        so setting it here makes _post_inventory post nothing (verified on
        staging, Nov 6 2026)."""
        mo.qty_producing = target_producing
        mo._set_qty_producing()
        finished = mo._pt_open_finished_move()
        if not self.allow_component_shortage:
            shortages = mo._pt_component_shortages()
            if shortages:
                raise UserError(_(
                    'Components are short for %(mo)s, so nothing has been posted:\n%(lines)s\n\n'
                    'Pick the components to production first, or tick '
                    '"Post even if components are short".',
                    mo=mo.name, lines='\n'.join(shortages)))
        return finished

    def _post_partial(self, mo, qty):
        rounding = mo.product_uom_id.rounding
        done_before = mo.move_finished_ids.filtered(lambda m: m.state == 'done')
        self._prepare(mo, qty, mo.qty_produced + qty)
        mo._post_inventory(cancel_backorder=False)
        self.env.invalidate_all()
        new_done = mo.move_finished_ids.filtered(
            lambda m: m.state == 'done' and m.product_id == mo.product_id) - done_before
        posted = sum(m.product_uom._compute_quantity(m.quantity, mo.product_uom_id) for m in new_done)
        if float_compare(posted, qty, precision_rounding=rounding) != 0:
            raise UserError(_(
                'Odoo posted %(posted)s instead of %(qty)s %(uom)s on %(mo)s. '
                'Nothing has been saved.',
                posted=posted, qty=qty, uom=mo.product_uom_id.name, mo=mo.name))
        if mo.state in ('done', 'cancel') or not mo._pt_open_finished_move():
            raise UserError(_(
                'Posting a partial quantity unexpectedly closed %s. Nothing has been saved.',
                mo.name))
        mo.pt_used = True
        mo._pt_store_finished(new_done)
        mo._pt_log(_('Partial transfer posted'), _(
            '%(qty)s %(uom)s of %(product)s produced and put away. '
            'Produced %(done)s of %(total)s.',
            qty=qty, uom=mo.product_uom_id.name, product=mo.product_id.display_name,
            done=mo.qty_produced, total=mo.product_qty))

    def _post_final(self, mo, qty):
        self._prepare(mo, qty, mo.product_qty)
        mo.pt_used = True  # makes the button_mark_done hook put the goods away
        ctx = {'skip_consumption': True} if self.accept_consumption_differences else {}
        res = mo.with_context(**ctx).button_mark_done()
        if mo.state != 'done':
            raise UserError(self._explain_blocked_close(mo, res))
        mo._pt_log(_('Final transfer posted, MO closed'), _(
            '%(qty)s %(uom)s of %(product)s produced and put away. '
            'Manufacturing Order closed with the standard Mark as Done.',
            qty=qty, uom=mo.product_uom_id.name, product=mo.product_id.display_name))

    def _explain_blocked_close(self, mo, res):
        model = isinstance(res, dict) and res.get('res_model') or ''
        if model == 'mrp.consumption.warning':
            return _(
                'Component consumption on %s differs from the bill of materials, '
                'so Odoo asked for a confirmation. Nothing has been posted. Check '
                'the Components tab, then run the transfer again with '
                '"Accept consumption differences" ticked.', mo.name)
        reason = isinstance(res, dict) and (res.get('name') or model) or _('no reason given')
        return _(
            'Odoo could not close %(mo)s automatically (%(reason)s). Nothing has '
            'been posted. Close it with the standard Mark as Done button to see '
            'what Odoo needs.', mo=mo.name, reason=reason)
