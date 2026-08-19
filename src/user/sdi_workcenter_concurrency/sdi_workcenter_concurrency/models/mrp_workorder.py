# -*- coding: utf-8 -*-
from collections import defaultdict

from odoo import models


class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

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
