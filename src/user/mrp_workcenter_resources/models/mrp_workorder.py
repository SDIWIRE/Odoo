"""
Parallel scheduling override for mrp.workorder.

When a work center has number_of_workers > 1 and at least one worker resource
slot configured, this module replaces the standard _plan_workorder logic with
a multi-slot search:

  1. For each worker resource slot (resource.resource record), ask the calendar
     API for the next free working interval long enough to fit the work order.
  2. Pick the slot whose first free window starts earliest (i.e., the least
     loaded worker right now).
  3. Book that interval by creating a resource.calendar.leaves record on the
     chosen resource — the same mechanism Odoo uses for the single-resource case,
     so the standard unplan / cancel flows clean it up automatically.

If no free slot is found (e.g., all calendars are empty or _work_intervals_batch
raises), we fall back to the standard single-resource planner so the work order
is never left unplanned.
"""
import logging
import pytz
from datetime import datetime, timedelta

from odoo import models, fields, api

_logger = logging.getLogger(__name__)


class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

    worker_resource_id = fields.Many2one(
        'resource.resource',
        string='Assigned Worker Slot',
        copy=False,
        index=True,
        ondelete='set null',
        help='Worker resource slot chosen by the parallel scheduler.',
    )

    # ------------------------------------------------------------------ #
    #  Planning override                                                   #
    # ------------------------------------------------------------------ #

    def _plan_workorder(self, replan=False):
        """Entry point called by mrp.production._plan_workorders().

        Delegates to multi-slot logic when the work center has parallel workers;
        falls back to super() (standard single-resource logic) otherwise.
        """
        self.ensure_one()
        wc = self.workcenter_id

        if wc.number_of_workers <= 1 or not wc.worker_resource_ids:
            return super()._plan_workorder(replan=replan)

        # When replanning, clean up the leave that was created for the
        # previously assigned slot so the slot is freed before we search again.
        if replan:
            if hasattr(self, 'leave_id') and self.leave_id:
                self.leave_id.unlink()
                self.write({'leave_id': False, 'worker_resource_id': False})
            else:
                self.worker_resource_id = False

        resources = wc.worker_resource_ids.sorted('sequence').mapped('resource_id')
        start_from = self._get_parallel_start_date()
        duration_minutes = self.duration_expected or 60.0

        booked = self._book_parallel_slot(resources, start_from, duration_minutes)
        if not booked:
            _logger.warning(
                'mrp_workcenter_resources: no parallel slot found for WO %s '
                'on %s — falling back to standard scheduler.',
                self.display_name, wc.display_name,
            )
            return super()._plan_workorder(replan=replan)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _get_parallel_start_date(self):
        """Return the earliest datetime this work order is allowed to start.

        Respects:
          - The MO's planned/actual start date.
          - The planned finish of the preceding operation in the same MO.
        """
        self.ensure_one()
        mo = self.production_id
        base = mo.date_start or mo.date_planned_start or fields.Datetime.now()

        # Look for a work order from an earlier operation in this MO.
        if self.operation_id:
            prev = self.env['mrp.workorder'].search([
                ('production_id', '=', mo.id),
                ('id', '!=', self.id),
                ('operation_id.sequence', '<', self.operation_id.sequence),
                ('state', 'not in', ['done', 'cancel']),
            ], order='operation_id.sequence desc', limit=1)
            if prev and prev.date_planned_finished:
                base = max(base, prev.date_planned_finished)

        return base

    def _book_parallel_slot(self, resources, from_dt, duration_minutes):
        """Find the earliest free slot across all worker resources and reserve it.

        Creates a resource.calendar.leaves record on the chosen resource so the
        standard Odoo planner (and unplan/cancel) handles it correctly.

        Returns True on success, False if no slot could be found.
        """
        self.ensure_one()
        needed = timedelta(minutes=duration_minutes)
        utc = pytz.utc

        # Normalise from_dt to a timezone-aware UTC datetime for the calendar API.
        if not from_dt:
            from_dt = fields.Datetime.now()
        if isinstance(from_dt, datetime) and from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=utc)

        search_end = from_dt + timedelta(days=365)

        best_resource = None
        best_start = None
        best_end = None

        for resource in resources:
            calendar = resource.calendar_id or self.workcenter_id.resource_calendar_id

            if not calendar:
                # No calendar means the resource is always available — take it
                # immediately if it gives an earlier start than anything found so far.
                candidate_start = from_dt
                candidate_end = from_dt + needed
                if best_start is None or candidate_start < best_start:
                    best_resource = resource
                    best_start = candidate_start
                    best_end = candidate_end
                continue

            try:
                # _work_intervals_batch returns working time minus all leaves
                # (both global calendar leaves and resource-specific leaves).
                # Passing compute_leaves=True is the default in modern Odoo.
                intervals = calendar._work_intervals_batch(
                    from_dt, search_end, resources=resource,
                ).get(resource.id, [])
            except Exception:
                _logger.exception(
                    'mrp_workcenter_resources: _work_intervals_batch failed '
                    'for resource %s (id=%s)', resource.name, resource.id,
                )
                continue

            for iv_start, iv_end, _rec in intervals:
                # Intervals may be timezone-aware; normalise to naive UTC so
                # comparison with from_dt (which may be naive) is safe.
                s = iv_start.replace(tzinfo=None) if getattr(iv_start, 'tzinfo', None) else iv_start
                e = iv_end.replace(tzinfo=None) if getattr(iv_end, 'tzinfo', None) else iv_end
                f = from_dt.replace(tzinfo=None) if getattr(from_dt, 'tzinfo', None) else from_dt

                # Candidate start is the later of the interval opening and the
                # allowed-from date, so we never schedule in the past.
                cand_start = max(s, f)
                cand_end = cand_start + needed

                if cand_end <= e:
                    # This interval has room.  Keep it if it starts earlier
                    # than anything found so far, then stop searching this
                    # resource (we already have its best slot).
                    if best_start is None or cand_start < best_start:
                        best_resource = resource
                        best_start = cand_start
                        best_end = cand_end
                    break

        if not best_resource:
            return False

        # Strip timezone info for Odoo's naive-UTC datetime fields.
        if isinstance(best_start, datetime) and best_start.tzinfo:
            best_start = best_start.replace(tzinfo=None)
        if isinstance(best_end, datetime) and best_end.tzinfo:
            best_end = best_end.replace(tzinfo=None)

        calendar = best_resource.calendar_id or self.workcenter_id.resource_calendar_id
        company = self.company_id or self.workcenter_id.company_id

        leave = self.env['resource.calendar.leaves'].sudo().create({
            'name': self.display_name or self.name or 'Work Order',
            'calendar_id': calendar.id if calendar else False,
            'company_id': company.id if company else False,
            'resource_id': best_resource.id,
            'date_from': fields.Datetime.to_string(best_start),
            'date_to': fields.Datetime.to_string(best_end),
            'time_type': 'leave',
        })

        write_vals = {
            'worker_resource_id': best_resource.id,
            'date_planned_start': fields.Datetime.to_string(best_start),
            'date_planned_finished': fields.Datetime.to_string(best_end),
        }
        # leave_id is defined by mrp_workorder; only set it when present so the
        # standard cancel/unplan flows clean up the leave automatically.
        if 'leave_id' in self._fields:
            write_vals['leave_id'] = leave.id

        self.write(write_vals)

        _logger.info(
            'mrp_workcenter_resources: booked WO %s on %s slot "%s" '
            '%s → %s',
            self.display_name,
            self.workcenter_id.display_name,
            best_resource.name,
            best_start,
            best_end,
        )
        return True
