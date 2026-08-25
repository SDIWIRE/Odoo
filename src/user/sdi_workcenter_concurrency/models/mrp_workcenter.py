# -*- coding: utf-8 -*-
from collections import defaultdict
from datetime import datetime, timedelta
from functools import partial
from pytz import timezone, UTC

from odoo import fields, models
from odoo.tools.date_utils import localized, to_timezone
from odoo.tools.float_utils import float_compare
from odoo.tools.intervals import Intervals


class MrpWorkcenter(models.Model):
    _inherit = 'mrp.workcenter'

    concurrent_capacity = fields.Integer(
        string='Concurrent Operators', default=1, required=True,
        help="How many work orders can run at the same time on this work center.\n"
             "Unlike Capacity (Product Capacities tab), this has no effect on the "
             "duration of a single work order - it only controls scheduling "
             "concurrency.\n"
             "Use this for stations staffed by several people who each build "
             "separate units sequentially, instead of a machine that processes "
             "several units together in one cycle.")

    _positive_concurrent_capacity = models.Constraint(
        'CHECK(concurrent_capacity >= 1)',
        'Concurrent Operators must be at least 1.',
    )

    def _get_overcapacity_intervals(self, date_start, date_stop, leaves_to_ignore=False):
        """Sub-intervals of [date_start, date_stop] where the number of overlapping
        work-order leaves on this work center's calendar already meets or exceeds
        concurrent_capacity - i.e. no more room to schedule another work order.

        Same {resource.id: Intervals(...)} shape as
        resource.calendar._leave_intervals_batch, so it drops into
        _get_first_available_slot in place of the core single-lane leave check.
        date_start/date_stop are expected to be tz-aware (as produced by
        odoo.tools.date_utils.localized), matching how _get_first_available_slot
        calls this.
        """
        self.ensure_one()
        resource = self.resource_id
        date_start_naive = date_start.astimezone(UTC).replace(tzinfo=None)
        date_stop_naive = date_stop.astimezone(UTC).replace(tzinfo=None)
        # Mirror the filters resource.calendar._leave_intervals_batch applies for
        # core's single-lane check (same calendar-or-none, same resource-or-none,
        # same inclusive date bounds), so capacity>1 sees the same leave set core
        # would - just counted instead of treated as binary.
        domain = [
            ('time_type', '=', 'other'),
            ('calendar_id', 'in', [False, self.resource_calendar_id.id]),
            ('resource_id', 'in', [False, resource.id]),
            ('date_from', '<=', date_stop_naive),
            ('date_to', '>=', date_start_naive),
        ]
        if leaves_to_ignore:
            domain.append(('id', 'not in', leaves_to_ignore.ids))
        leaves = self.env['resource.calendar.leaves'].search(domain)

        # Only leaves actually attached to a work order occupy one of the N lanes.
        # Any other time_type='other' leave - calendar-wide ones (no resource) or
        # ones created by another module (e.g. maintenance downtime) - keeps core's
        # semantics: a full block regardless of concurrency.
        # sudo: whether a leave belongs to a work order is a factual
        # classification, not user-scoped data. Without it, a work order the
        # current user can't read (record rules / another company) would have its
        # leave misread as a hard block, silently making a capacity-N station
        # behave like capacity-1 for that window.
        occupancy_leave_ids = set(self.env['mrp.workorder'].sudo().search(
            [('leave_id', 'in', leaves.ids)]).leave_id.ids) if leaves else set()

        # Sweep-line: net concurrency change at each distinct timestamp, clipped to
        # the query window so partially-overlapping leaves still contribute correctly.
        deltas = defaultdict(int)
        full_blocks = []
        for leave in leaves:
            start = max(localized(leave.date_from), date_start)
            stop = min(localized(leave.date_to), date_stop)
            if stop <= start:
                continue
            if leave.id in occupancy_leave_ids:
                deltas[start] += 1
                deltas[stop] -= 1
            else:
                full_blocks.append((start, stop))

        segments = []
        count = 0
        seg_start = None
        for moment in sorted(deltas):
            count += deltas[moment]
            if count >= self.concurrent_capacity:
                if seg_start is None:
                    seg_start = moment
            elif seg_start is not None:
                segments.append((seg_start, moment))
                seg_start = None
        if seg_start is not None:
            segments.append((seg_start, date_stop))

        empty_leaves = self.env['resource.calendar.leaves']
        result = Intervals([(start, stop, empty_leaves) for start, stop in segments])
        if full_blocks:
            result |= Intervals([(start, stop, empty_leaves) for start, stop in full_blocks])
        return {resource.id: result}

    def _get_first_available_slot(self, start_datetime, duration, forward=True, leaves_to_ignore=False, extra_leaves_slots=[]):
        self.ensure_one()
        if self.concurrent_capacity <= 1:
            # Every work center that hasn't opted into concurrency (the default)
            # keeps the exact stock single-lane behavior - zero regression surface.
            return super()._get_first_available_slot(
                start_datetime, duration, forward=forward,
                leaves_to_ignore=leaves_to_ignore, extra_leaves_slots=extra_leaves_slots)

        # From here on this is a full override, not a small patch: core inlines its
        # busy-interval computation with no smaller extension point. This mirrors
        # core 19.0's _get_first_available_slot structure exactly, with the single
        # "any overlapping leave blocks" check replaced by
        # _get_overcapacity_intervals (blocks only once concurrent_capacity
        # overlapping leaves already exist). Re-diff against core on Odoo upgrades.
        ICP = self.env['ir.config_parameter'].sudo()
        max_planning_iterations = max(int(ICP.get_param('mrp.workcenter_max_planning_iterations', '50')), 1)
        resource = self.resource_id
        revert = to_timezone(start_datetime.tzinfo)
        start_datetime = localized(start_datetime)
        get_available_intervals = partial(self.resource_calendar_id._work_intervals_batch, resources=resource, tz=timezone(self.resource_calendar_id.tz))
        get_overcapacity_intervals = partial(self._get_overcapacity_intervals, leaves_to_ignore=leaves_to_ignore)
        # extra_leaves_slots stay full blocks (core semantics) rather than counting
        # as one lane each: nothing in community mrp passes them, their meaning in
        # other modules is unknown, and blocking errs toward under-scheduling
        # instead of overbooking.
        extra_leaves_slots_intervals = Intervals([(localized(start), localized(stop), self.env['resource.calendar.leaves']) for start, stop in extra_leaves_slots])

        remaining = duration = max(duration, 1 / 60)
        now = localized(datetime.now())
        delta = timedelta(days=14)
        start_interval, stop_interval = None, None
        for n in range(max_planning_iterations):  # 50 * 14 = 700 days in advance
            if forward:
                date_start = start_datetime + delta * n
                date_stop = date_start + delta
                available_intervals = get_available_intervals(date_start, date_stop)[resource.id]
                workorder_intervals = get_overcapacity_intervals(date_start, date_stop)[resource.id]
                for start, stop, _records in available_intervals:
                    start_interval = start_interval or start
                    interval_minutes = (stop - start).total_seconds() / 60
                    while (interval := Intervals([(start_interval or start, start + timedelta(minutes=min(remaining, interval_minutes)), _records)])) \
                      and (conflict := interval & workorder_intervals or interval & extra_leaves_slots_intervals):
                        (_start, start, _records) = conflict._items[0]  # restart available interval at conflicting interval stop
                        interval_minutes = (stop - start).total_seconds() / 60
                        start_interval, remaining = start if interval_minutes else None, duration
                    if float_compare(interval_minutes, remaining, precision_digits=3) >= 0:
                        return revert(start_interval), revert(start + timedelta(minutes=remaining))
                    remaining -= interval_minutes
            else:
                # same process but starting from end on reversed intervals
                date_stop = start_datetime - delta * n
                date_start = date_stop - delta
                available_intervals = get_available_intervals(date_start, date_stop)[resource.id]
                available_intervals = reversed(available_intervals)
                workorder_intervals = get_overcapacity_intervals(date_start, date_stop)[resource.id]
                for start, stop, _records in available_intervals:
                    stop_interval = stop_interval or stop
                    interval_minutes = (stop - start).total_seconds() / 60
                    while (interval := Intervals([(stop - timedelta(minutes=min(remaining, interval_minutes)), stop_interval or stop, _records)])) \
                      and (conflict := interval & workorder_intervals or interval & extra_leaves_slots_intervals):
                        (stop, _stop, _records) = conflict._items[0]  # restart available interval at conflicting interval start
                        interval_minutes = (stop - start).total_seconds() / 60
                        stop_interval, remaining = stop if interval_minutes else None, duration
                    if float_compare(interval_minutes, remaining, precision_digits=3) >= 0:
                        return revert(stop - timedelta(minutes=remaining)), revert(stop_interval)
                    remaining -= interval_minutes
                if date_start <= now:
                    break
        return False, 'No available slot 700 days after the planned start'
