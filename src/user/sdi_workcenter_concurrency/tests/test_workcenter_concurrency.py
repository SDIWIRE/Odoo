# -*- coding: utf-8 -*-
from datetime import datetime

from odoo import Command
from odoo.tests import Form, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWorkcenterConcurrency(TransactionCase):
    """Covers the acceptance criteria from the SDI work center concurrency brief:
    duration math must stay untouched by concurrent_capacity, up to N work orders
    must be schedulable in the same overlapping window, the (N+1)th must be pushed
    to the next opening, and work centers left at the default (1) must behave
    exactly as stock mrp - no regression for KOMAX/GAMMA/SD ARTOS-style setups.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # 24/7 calendar so scheduling math is deterministic and never blocked by
        # weekends/off-hours - same trick core's own mrp tests use
        # (TestMrpCommon.full_availability), just scoped to one calendar here.
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Test 24/7',
            'attendance_ids': [
                Command.create({'name': day, 'dayofweek': str(i), 'hour_from': 0, 'hour_to': 24, 'day_period': 'morning'})
                for i, day in enumerate(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'])
            ],
        })

        cls.workcenter = cls.env['mrp.workcenter'].create({
            'name': 'Test Assembly',
            'resource_calendar_id': cls.calendar.id,
            'time_start': 0,
            'time_stop': 0,
            'time_efficiency': 100,
            'concurrent_capacity': 5,
        })
        cls.single_lane_workcenter = cls.env['mrp.workcenter'].create({
            'name': 'Test Single Lane',
            'resource_calendar_id': cls.calendar.id,
            'time_start': 0,
            'time_stop': 0,
            'time_efficiency': 100,
            # concurrent_capacity left at its default (1) on purpose.
        })

        cls.product_to_build = cls.env['product.product'].create({'name': 'Test Finished Good', 'is_storable': True})
        cls.product_to_use = cls.env['product.product'].create({'name': 'Test Component', 'is_storable': True})

        cls.time_cycle = 10.0  # minutes per unit

        def make_bom(workcenter):
            return cls.env['mrp.bom'].create({
                'product_id': cls.product_to_build.id,
                'product_tmpl_id': cls.product_to_build.product_tmpl_id.id,
                'product_qty': 1.0,
                'type': 'normal',
                'consumption': 'flexible',
                'operation_ids': [
                    # time_cycle itself is a readonly compute (no inverse) - the
                    # writable source field in 'manual' time_mode is time_cycle_manual.
                    Command.create({'name': 'Assemble', 'workcenter_id': workcenter.id, 'time_cycle_manual': cls.time_cycle, 'sequence': 1}),
                ],
                'bom_line_ids': [
                    Command.create({'product_id': cls.product_to_use.id, 'product_qty': 1}),
                ],
            })

        cls.bom = make_bom(cls.workcenter)
        cls.single_lane_bom = make_bom(cls.single_lane_workcenter)

        # Fixed, far-future start so scheduling is never clamped against "now" -
        # matters for _plan_workorder's `max(production.date_start, datetime.now())`.
        cls.start = datetime(2035, 1, 1, 8, 0, 0)

    def _make_mo(self, bom, qty):
        mo_form = Form(self.env['mrp.production'])
        mo_form.product_id = bom.product_id
        mo_form.bom_id = bom
        mo_form.product_qty = qty
        mo = mo_form.save()
        mo.date_start = self.start
        mo.action_confirm()
        return mo

    def test_duration_expected_unaffected_by_concurrent_capacity(self):
        """qty=80 x 10 min/unit must be 800 minutes, not divided by concurrent_capacity."""
        mo = self._make_mo(self.bom, 80)
        mo.button_plan()
        self.assertEqual(mo.workorder_ids.duration_expected, 800.0)

    def test_five_workorders_overlap_at_full_capacity(self):
        """5 separate work orders on a concurrent_capacity=5 work center should all
        land in the same time window instead of queuing one after another."""
        mos = [self._make_mo(self.bom, 1) for _ in range(5)]
        for mo in mos:
            mo.button_plan()
        starts = {mo.workorder_ids.date_start for mo in mos}
        finishes = {mo.workorder_ids.date_finished for mo in mos}
        self.assertEqual(len(starts), 1, "all 5 work orders should start at the same time")
        self.assertEqual(len(finishes), 1, "all 5 work orders should finish at the same time")

    def test_sixth_workorder_pushed_to_next_slot(self):
        """A 6th work order must not overlap a 6th time - it gets the next opening."""
        mos = [self._make_mo(self.bom, 1) for _ in range(5)]
        for mo in mos:
            mo.button_plan()
        first_start = mos[0].workorder_ids.date_start
        first_finish = mos[0].workorder_ids.date_finished

        sixth = self._make_mo(self.bom, 1)
        sixth.button_plan()
        self.assertEqual(sixth.workorder_ids.date_start, first_finish,
                          "the 6th work order should start only once the first slot frees up")
        self.assertNotEqual(sixth.workorder_ids.date_start, first_start)

    def test_default_capacity_one_keeps_single_lane_behavior(self):
        """concurrent_capacity=1 (the default) must not allow any overlap - this is
        the regression guard standing in for KOMAX/GAMMA/SD ARTOS."""
        mo_1 = self._make_mo(self.single_lane_bom, 1)
        mo_1.button_plan()
        mo_2 = self._make_mo(self.single_lane_bom, 1)
        mo_2.button_plan()
        self.assertEqual(
            mo_2.workorder_ids.date_start, mo_1.workorder_ids.date_finished,
            "with concurrent_capacity=1, the second work order must queue after the first, not overlap it")

    def test_conflicted_workorder_ids_quiet_at_exact_capacity(self):
        """5 work orders exactly at capacity 5 (the normal case, since the
        scheduler itself never lets a real 6th overlap) should raise no
        popover warning at all."""
        mos = [self._make_mo(self.bom, 1) for _ in range(5)]
        for mo in mos:
            mo.button_plan()
        all_workorders = sum((mo.workorder_ids for mo in mos[1:]), mos[0].workorder_ids)
        conflicts = all_workorders._get_conflicted_workorder_ids()
        self.assertEqual(conflicts, {}, "exactly-at-capacity overlap should never be flagged")

    def test_conflicted_workorder_ids_with_virtual_onchange_records(self):
        """Regression for the production KeyError that blocked adding a work
        order to an existing MO.

        Editing a saved MO runs this compute inside an onchange, where the
        work orders are virtual records: wo.id is a NewId while recordset.ids
        (what core's SQL keys its results by) still holds the real database id.
        Building the capacity map from wo.id therefore matched nothing and
        raised KeyError on every real id core returned. Any station with two or
        more overlapping work orders hit it - i.e. normal operation once
        Concurrent Operators is above 1.
        """
        mos = [self._make_mo(self.bom, 1) for _ in range(5)]
        for mo in mos:
            mo.button_plan()
        saved = sum((mo.workorder_ids for mo in mos[1:]), mos[0].workorder_ids)

        Workorder = self.env['mrp.workorder']
        virtual = Workorder
        for wo in saved:
            virtual |= Workorder.new(origin=wo)

        # Sanity-check we really reproduced the real/virtual id split: the
        # in-memory ids are NewId objects, while .ids resolves to real ints.
        self.assertTrue(any(not isinstance(i, int) for i in virtual._ids),
                        "expected virtual (NewId) records")
        self.assertEqual(set(virtual.ids), set(saved.ids))

        # Must not raise, and must still filter correctly (5 at capacity 5).
        conflicts = virtual._get_conflicted_workorder_ids()
        self.assertEqual(dict(conflicts), {})

    def test_conflicted_workorder_ids_flags_manual_overbooking(self):
        """If a 6th work order is manually forced to overlap an already-full
        slot (bypassing the scheduler, e.g. a manual Gantt drag), the popover
        should flag the over-booked cluster. There's no ordering concept to
        single out "the extra one" - see the docstring on the override - so
        every work order in the cluster is flagged, not just the 6th."""
        mos = [self._make_mo(self.bom, 1) for _ in range(5)]
        for mo in mos:
            mo.button_plan()
        first_start = mos[0].workorder_ids.date_start
        first_finish = mos[0].workorder_ids.date_finished

        sixth = self._make_mo(self.bom, 1)
        sixth.button_plan()
        # Force the 6th to overlap the already-full first slot, simulating a
        # manual override of the scheduler's decision.
        sixth.workorder_ids.write({'date_start': first_start, 'date_finished': first_finish})

        all_workorders = sum((mo.workorder_ids for mo in mos[1:]), mos[0].workorder_ids) + sixth.workorder_ids
        conflicts = all_workorders._get_conflicted_workorder_ids()
        self.assertEqual(set(conflicts.keys()), set(all_workorders.ids),
                          "the whole over-booked cluster of 6 should be flagged, not just the 6th")
