import logging

from odoo import models, fields, api
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class MrpWorkcenterWorkerResource(models.Model):
    """One record per parallel worker slot on a work center.

    Each slot owns a dedicated resource.resource so Odoo's calendar/leave
    system can independently track its availability.  The resource inherits
    the work center's working-hours calendar and time efficiency.
    """
    _name = 'mrp.workcenter.worker.resource'
    _description = 'Work Center Worker Resource Slot'
    _order = 'workcenter_id, sequence'

    name = fields.Char(string='Slot Name', required=True)
    workcenter_id = fields.Many2one(
        'mrp.workcenter',
        string='Work Center',
        required=True,
        ondelete='cascade',
        index=True,
    )
    resource_id = fields.Many2one(
        'resource.resource',
        string='Resource',
        required=True,
        ondelete='cascade',
        copy=False,
    )
    sequence = fields.Integer(string='Slot #', default=1)
    active = fields.Boolean(related='resource_id.active', store=True)

    def unlink(self):
        # Delete the underlying resource.resource records first so they don't
        # become orphaned after the junction rows are gone.
        resources = self.mapped('resource_id')
        result = super().unlink()
        resources.unlink()
        return result


class MrpWorkcenter(models.Model):
    _inherit = 'mrp.workcenter'

    number_of_workers = fields.Integer(
        string='Number of Workers',
        default=1,
        help=(
            'How many workers can run independent jobs simultaneously at this '
            'work center.  Used by the scheduler to allow parallel work orders.'
        ),
    )
    worker_resource_ids = fields.One2many(
        'mrp.workcenter.worker.resource',
        'workcenter_id',
        string='Worker Resource Slots',
        copy=False,
    )
    worker_slot_count = fields.Integer(
        string='Active Slots',
        compute='_compute_worker_slot_count',
        store=True,
    )

    # ------------------------------------------------------------------ #
    #  Computed fields                                                     #
    # ------------------------------------------------------------------ #

    @api.depends('worker_resource_ids')
    def _compute_worker_slot_count(self):
        for wc in self:
            wc.worker_slot_count = len(wc.worker_resource_ids)

    # ------------------------------------------------------------------ #
    #  Constraints                                                         #
    # ------------------------------------------------------------------ #

    @api.constrains('number_of_workers')
    def _check_number_of_workers(self):
        for wc in self:
            if wc.number_of_workers < 1:
                raise ValidationError(
                    'Number of workers must be at least 1 for work center "%s".' % wc.name
                )

    # ------------------------------------------------------------------ #
    #  ORM overrides                                                       #
    # ------------------------------------------------------------------ #

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for wc in records:
            wc._sync_worker_resources()
        return records

    def write(self, vals):
        result = super().write(vals)
        if 'number_of_workers' in vals or 'resource_calendar_id' in vals or 'time_efficiency' in vals:
            for wc in self:
                wc._sync_worker_resources()
        return result

    # ------------------------------------------------------------------ #
    #  Resource sync                                                       #
    # ------------------------------------------------------------------ #

    def _sync_worker_resources(self):
        """Create or remove worker resource slots to match number_of_workers.

        Always keeps slots sorted by sequence so slot 1 is always the first
        resource record.  Existing slots are never re-created; only the delta
        is added or removed.
        """
        self.ensure_one()
        existing = self.worker_resource_ids.sorted('sequence')
        current = len(existing)
        target = self.number_of_workers

        if current < target:
            for i in range(current, target):
                slot_name = '%s – Worker %d' % (self.name, i + 1)
                resource = self.env['resource.resource'].create({
                    'name': slot_name,
                    'resource_type': 'user',
                    'calendar_id': self.resource_calendar_id.id if self.resource_calendar_id else False,
                    'company_id': self.company_id.id if self.company_id else False,
                    'time_efficiency': self.time_efficiency or 100.0,
                })
                self.env['mrp.workcenter.worker.resource'].create({
                    'name': slot_name,
                    'workcenter_id': self.id,
                    'resource_id': resource.id,
                    'sequence': i + 1,
                })
                _logger.info(
                    'mrp_workcenter_resources: created slot %d/%d for work center %s',
                    i + 1, target, self.name,
                )

        elif current > target:
            # Remove excess slots from the highest sequence number downward.
            # Warn if any of the slots being removed have active (planned)
            # work orders so the user can re-plan before decreasing the count.
            to_remove = existing[target:]
            active_wos = self.env['mrp.workorder'].search([
                ('worker_resource_id', 'in', to_remove.mapped('resource_id').ids),
                ('state', 'not in', ['done', 'cancel']),
            ])
            if active_wos:
                _logger.warning(
                    'mrp_workcenter_resources: removing %d slot(s) from %s that '
                    'have %d active work order(s).  Those work orders should be '
                    're-planned.',
                    len(to_remove), self.name, len(active_wos),
                )
            to_remove.unlink()

    def action_sync_worker_resources(self):
        """Manual sync button — useful after calendar or efficiency changes."""
        for wc in self:
            wc._sync_worker_resources()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Worker Slots Synced',
                'message': 'Resource slots have been updated to match the configured worker count.',
                'type': 'success',
                'sticky': False,
            },
        }
