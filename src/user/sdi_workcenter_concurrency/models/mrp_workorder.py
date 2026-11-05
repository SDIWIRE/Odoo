# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import models


class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

    def _calculate_date_finished(self, date_start=False, new_workcenter=False):
        """Core answers "if I start at T, when do I finish?" by walking the work
        center calendar forward and skipping every other work order's leave as
        unavailable time (`domain = [('time_type', 'in', ['leave', 'other'])]`
        plus `resource=workcenter.resource_id`). That is correct for a
        single-lane machine, where another job really does make the station
        unavailable.

        On a multi-operator station it is wrong, and badly so: a colleague
        working at the next bench does not stop this operator working. Left
        alone it produces a runaway feedback loop - each started work order
        skips over every concurrent job's window, so its own finish date lands
        far in the future, which widens its leave, which the next job then skips
        over in turn. Observed in production: a 55-minute job occupying a 65-day
        window at a station with 8 operators.

        So for a work center with concurrent operators, other work orders'
        occupancy no longer counts as unavailable time. Genuine closures (time
        off, `time_type='leave'`) and hard blocks such as maintenance downtime
        still do. Whether the station has a free lane at all is a scheduling
        question, answered by `_get_first_available_slot` - deciding it here is
        what caused the smearing.

        Work centers at the default of 1 operator fall through to `super()` and
        keep core behavior exactly.
        """
        workcenter = new_workcenter or self.workcenter_id
        if workcenter.concurrent_capacity <= 1 or not workcenter.resource_calendar_id:
            return super()._calculate_date_finished(
                date_start=date_start, new_workcenter=new_workcenter)

        exclude_ids = set(workcenter._get_lane_occupancy_leave_ids(
            date_from=date_start or self.date_start))
        if self.leave_id:
            exclude_ids.add(self.leave_id.id)
        domain = [('time_type', 'in', ['leave', 'other'])]
        if exclude_ids:
            domain.append(('id', 'not in', list(exclude_ids)))
        return workcenter.resource_calendar_id.plan_hours(
            self.duration_expected / 60.0, date_start or self.date_start,
            compute_leaves=True, domain=domain,
            resource=workcenter.resource_id,
        )

    def _get_conflicted_workorder_ids(self):
        """Core flags a conflict for any two overlapping work orders on the same
        work center. Only used to show the warning triangle in
        _compute_json_popover - not a scheduling gate - so it's safe to relax:
        a work order is only flagged once its work center already has at least
        concurrent_capacity *other* overlapping ready/blocked work orders.

        Note this is a symmetric count, not a "who's the extra one" check -
        there's no ordering concept to single out a specific offender, so once
        a cluster of overlapping work orders exceeds capacity, every work order
        in that cluster gets flagged, not just the newest one. That only
        happens today if someone manually drags a work order into an
        already-full slot; normal scheduling (_get_first_available_slot) never
        lets a cluster exceed capacity in the first place. Collapses to
        identical behavior to core when concurrent_capacity == 1, the default
        for every work center that hasn't opted into concurrency.
        """
        conflicts = super()._get_conflicted_workorder_ids()
        if not conflicts:
            return conflicts
        # Key by the *real* database id, which is what super()'s SQL returns.
        # In an onchange (e.g. adding a work order to a saved MO) the records in
        # self are virtual: wo.id is a NewId and only wo._origin.id holds the
        # real id, so keying on wo.id would never match super()'s keys.
        capacity_by_wo = {}
        for wo in self:
            real_id = wo._origin.id or wo.id
            capacity_by_wo[real_id] = wo.workcenter_id.concurrent_capacity or 1
        # Preserve core's defaultdict(list) return type so callers relying on
        # auto-[] for non-conflicted ids keep working. Default to 1 for any id
        # we can't map, so an unexpected key degrades to core behavior (flag the
        # conflict) instead of raising.
        return defaultdict(list, {
            wo_id: others for wo_id, others in conflicts.items()
            if len(others) >= capacity_by_wo.get(wo_id, 1)
        })
