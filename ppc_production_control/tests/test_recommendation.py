"""Best-machine recommendation for Sales Order lines: compatibility, ranking, scheduling maths, delivery risk, hand-over to the
Production Plan and the Work Order, permissions. Every record is created inside the test transaction and rolled back."""

import json
from datetime import datetime, timedelta
from unittest import mock

import frappe
from frappe.utils import add_to_date, now_datetime

from ppc_production_control import api, tasks
from ppc_production_control.services import machines, recommend, scheduling
from ppc_production_control.services.constants import A_APPLY_MACHINE_PLAN, AUDIT_DT
from ppc_production_control.tests.test_api_security import ERP_MFG, make_user
from ppc_production_control.tests.utils import PREFIX, PPCTestCase, make_bom, make_item, make_operation, make_workstation

NOW = datetime(2030, 3, 4, 8, 0, 0)  # far in the future: independent of whatever is booked on the site
START = datetime(2030, 3, 4, 8, 1, 0)  # the first minute the engine may use


def d(hours: float) -> datetime:
	return NOW + timedelta(hours=hours)


class TestRecommendationPure(frappe.tests.utils.FrappeTestCase):
	"""The duration, grouping, scoring and slot-planning rules, without a database."""

	def test_operation_minutes_follow_erpnext(self):
		self.assertEqual(recommend.operation_minutes(30, 0, 1, 1, 10), 300)
		self.assertEqual(recommend.operation_minutes(30, 0, 1, 10, 10), 30, "the BOM's time is for BOM quantity units")
		self.assertEqual(recommend.operation_minutes(30, 0, 5, 1, 10), 60, "a batch of 5 is processed at once")
		self.assertEqual(recommend.operation_minutes(45, 1, 1, 1, 10), 45, "a fixed-time operation is not scaled by quantity")
		self.assertEqual(recommend.operation_minutes(30, 0, 0, 0, 2), 60, "zero batch / BOM quantity behave as one")

	def test_missing_duration_is_unknown_not_zero(self):
		for t in (0, None, -5):
			self.assertIsNone(recommend.operation_minutes(t, 0, 1, 1, 10))

	def test_operations_group_by_sequence(self):
		ops = [{"idx": 1, "sequence_id": 1}, {"idx": 2, "sequence_id": 2}, {"idx": 3, "sequence_id": 2}, {"idx": 4, "sequence_id": 0}, {"idx": 5, "sequence_id": 0}]
		self.assertEqual([[o["idx"] for o in g] for g in recommend.group_operations(ops)], [[1], [2, 3], [4], [5]])
		self.assertEqual(len(recommend.group_operations([{"idx": 1, "sequence_id": 0}, {"idx": 2, "sequence_id": 0}])), 2, "no sequence id: strictly one after the other")

	def test_delivery_risk(self):
		finish = datetime(2030, 3, 10, 12, 0, 0)
		self.assertEqual(recommend.delivery_risk(finish, "2030-03-20")["state"], "meets")
		self.assertEqual(recommend.delivery_risk(finish, "2030-03-10")["state"], "tight", "the delivery date counts to the end of that day")
		self.assertEqual(recommend.delivery_risk(finish, "2030-03-09")["state"], "misses")
		self.assertLess(recommend.delivery_risk(finish, "2030-03-09")["slack_days"], 0)
		self.assertEqual(recommend.delivery_risk(finish, None)["state"], "unknown")
		self.assertEqual(recommend.delivery_risk(None, "2030-03-09")["state"], "unknown", "no completion date, no verdict")

	def test_score_ignores_factors_without_data(self):
		self.assertEqual(recommend.weighted_score([(45, 1.0), (25, 0.0)]), round(100 * 45 / 70, 1))
		self.assertEqual(recommend.weighted_score([(45, 1.0), (25, None)]), 100.0, "a factor with no data is dropped, not guessed as zero")
		self.assertEqual(recommend.weighted_score([(0, 1.0), (10, 0.5)]), 50.0, "a zero weight switches a factor off")
		self.assertIsNone(recommend.weighted_score([(0, 1.0)]))
		self.assertIsNone(recommend.weighted_score([]))

	def test_spread_score(self):
		self.assertEqual(recommend.spread_score(5, 5, 9), 1.0)
		self.assertEqual(recommend.spread_score(9, 5, 9), 0.0)
		self.assertEqual(recommend.spread_score(9, 5, 9, higher_is_better=True), 1.0)
		self.assertEqual(recommend.spread_score(7, 7, 7), 1.0, "all equal: nobody is penalised")

	def test_basis_changes_with_what_the_recommendation_depends_on(self):
		base = recommend.basis_hash("ITEM", 10, "Nos", "BOM-1", "2030-03-20")
		self.assertEqual(base, recommend.basis_hash("ITEM", 10.0, "Nos", "BOM-1", "2030-03-20 00:00:00"))
		for changed in (("OTHER", 10, "Nos", "BOM-1", "2030-03-20"), ("ITEM", 11, "Nos", "BOM-1", "2030-03-20"), ("ITEM", 10, "Kg", "BOM-1", "2030-03-20"),
			("ITEM", 10, "Nos", "BOM-2", "2030-03-20"), ("ITEM", 10, "Nos", "BOM-1", "2030-03-21")):
			self.assertNotEqual(base, recommend.basis_hash(*changed), changed)

	def test_layout_uses_working_hours_and_skips_holidays(self):
		hours = [(datetime(2000, 1, 1, 8).time(), datetime(2000, 1, 1, 12).time()), (datetime(2000, 1, 1, 13).time(), datetime(2000, 1, 1, 17).time())]
		rows = machines.layout_minutes(datetime(2030, 3, 4, 10, 0), 360, hours, set())
		self.assertEqual(rows, [(datetime(2030, 3, 4, 10), datetime(2030, 3, 4, 12)), (datetime(2030, 3, 4, 13), datetime(2030, 3, 4, 17))])
		rows = machines.layout_minutes(datetime(2030, 3, 4, 16, 0), 600, hours, set())
		self.assertEqual(rows[-1], (datetime(2030, 3, 6, 8), datetime(2030, 3, 6, 9)), "carried over into the following days' slots")
		holiday = {datetime(2030, 3, 5).date()}
		rows = machines.layout_minutes(datetime(2030, 3, 4, 16, 0), 120, hours, holiday)
		self.assertEqual(rows, [(datetime(2030, 3, 4, 16), datetime(2030, 3, 4, 17)), (datetime(2030, 3, 6, 8), datetime(2030, 3, 6, 9))], "the holiday is skipped")

	def test_layout_without_working_hours_is_continuous(self):
		self.assertEqual(machines.layout_minutes(NOW, 90, [], set()), [(NOW, NOW + timedelta(minutes=90))])
		hours = [(datetime(2000, 1, 1, 8).time(), datetime(2000, 1, 1, 9).time())]
		self.assertEqual(machines.layout_minutes(NOW, 90, hours, set(), allow_overtime=True), [(NOW, NOW + timedelta(minutes=90))])

	def test_plan_operation_waits_for_capacity(self):
		busy = [(d(0), d(10))]
		plan = machines.plan_operation(busy, 1, d(1), 120, [], set(), d(500))
		self.assertEqual((plan["start"], plan["end"]), (d(10), d(12)))
		plan = machines.plan_operation(busy, 2, d(1), 120, [], set(), d(500))
		self.assertEqual(plan["start"], d(1), "a second slot is free")
		self.assertIsNone(machines.plan_operation([(d(0), d(1000))], 1, d(1), 60, [], set(), d(24)), "nothing within the look-ahead")

	def test_plan_operation_needs_room_for_every_piece(self):
		hours = [(datetime(2000, 1, 1, 8).time(), datetime(2000, 1, 1, 12).time())]
		# 6 h of work in 4 h days: day 1 08-12 and day 2 08-10. Day 2 morning is booked => the whole thing moves to a day where both fit
		booked = [(datetime(2030, 3, 5, 8), datetime(2030, 3, 5, 9))]
		plan = machines.plan_operation(booked, 1, datetime(2030, 3, 4, 8), 360, hours, set(), datetime(2030, 4, 30))
		self.assertGreater(plan["start"], datetime(2030, 3, 4, 8))
		for a, b in plan["rows"]:
			self.assertFalse(machines.clip(booked, a, b))

	def test_zero_minutes_still_finds_the_first_free_moment(self):
		plan = machines.plan_operation([(d(0), d(3))], 1, d(1), 0, [], set(), d(100))
		self.assertEqual(plan["start"], d(3))


class TestRecommendation(PPCTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for name in ("Lathe", "Press", "Mill"):
			if not frappe.db.exists("Workstation Type", f"{PREFIX} {name}"):
				frappe.get_doc({"doctype": "Workstation Type", "workstation_type": f"{PREFIX} {name}"}).insert(ignore_permissions=True)
		cls.t_lathe, cls.t_press, cls.t_mill = (f"{PREFIX} {n}" for n in ("Lathe", "Press", "Mill"))
		# three lathes (the BOM names L0), one press, one mill, and a machine whose NAME says lathe but whose type is Press
		cls.l0, cls.la, cls.lb = (make_workstation(f"{PREFIX} REC L{n}", capacity=1) for n in ("0", "A", "B"))
		cls.press = make_workstation(f"{PREFIX} REC Press", capacity=1)
		cls.mill = make_workstation(f"{PREFIX} REC Mill", capacity=1)
		cls.fake = make_workstation(f"{PREFIX} REC Lathe Fake", capacity=1)
		for ws, wtype in ((cls.l0, cls.t_lathe), (cls.la, cls.t_lathe), (cls.lb, cls.t_lathe), (cls.press, cls.t_press), (cls.mill, cls.t_mill), (cls.fake, cls.t_press)):
			frappe.db.set_value("Workstation", ws, {"workstation_type": wtype, "status": "Production"})
		frappe.db.sql("delete from `tabWorkstation Working Hour` where parent like %s", f"{PREFIX} REC%")  # round the clock: deterministic
		cls.op_turn = make_operation(f"{PREFIX} Turn", cls.l0)
		cls.op_finish = make_operation(f"{PREFIX} Finish", cls.l0)
		cls.op_mill = make_operation(f"{PREFIX} Mill Op", cls.mill)
		cls.item1 = make_item(f"{PREFIX}-REC-1", manufacture=True)
		cls.item2 = make_item(f"{PREFIX}-REC-2", manufacture=True)
		cls.item3 = make_item(f"{PREFIX}-REC-3", manufacture=True)
		cls.item_nobom = make_item(f"{PREFIX}-REC-NOBOM", manufacture=True)
		cls.item_noops = make_item(f"{PREFIX}-REC-NOOPS", manufacture=True)
		turn = {"operation": cls.op_turn, "workstation": cls.l0, "workstation_type": cls.t_lathe, "time_in_mins": 30, "sequence_id": 1}
		cls.bom1 = make_bom(cls.item1, cls.company, [(cls.rm1, 1)], [turn], source_warehouse=cls.rm_wh).name
		cls.bom2 = make_bom(cls.item2, cls.company, [(cls.rm1, 1)], [
			turn, {"operation": cls.op_mill, "workstation": cls.mill, "workstation_type": cls.t_mill, "time_in_mins": 60, "sequence_id": 2}], source_warehouse=cls.rm_wh).name
		cls.bom3 = make_bom(cls.item3, cls.company, [(cls.rm1, 1)], [
			turn, {"operation": cls.op_finish, "workstation": cls.l0, "workstation_type": cls.t_lathe, "time_in_mins": 15, "sequence_id": 2}], source_warehouse=cls.rm_wh).name
		make_bom(cls.item_noops, cls.company, [(cls.rm1, 1)], source_warehouse=cls.rm_wh)
		cls.planner = make_user("recplanner", ["Production Planner", *ERP_MFG])
		cls.sales = make_user("recsales", ["Sales User"])
		cls.viewer = make_user("recviewer", ["PPC Viewer", "Item Manager"])
		cls.nobody = make_user("recnobody", ["Blogger"])

	# helpers ------------------------------------------------------------------------------------------
	def rec(self, item=None, qty=10, **kw):
		kw.setdefault("now", NOW)
		return recommend.recommend(item or self.item1, qty, **kw)

	def machines_of(self, result, i=0):
		return [c["workstation"] for c in result["operations"][i]["candidates"]]

	def place(self, ws, start, end):
		"""Park a Job Card of another Work Order on a machine at an exact interval, so the machine is really booked."""
		_plan, wo = self.ops_plan(10)
		name = self.job_cards(wo)[0].name
		frappe.db.set_value("Job Card", name, "workstation", ws)
		scheduling.apply_dates(frappe.get_doc("Job Card", name), start, end, "resize")
		return name

	def so_with(self, item, workstation=None, preference="Manual", qty=10, submit=False, delivery_in_days=10):
		so = self.new_so((item, qty), submit=False, delivery_in_days=delivery_in_days)
		if workstation:
			row = so.items[0]
			row.ppc_preferred_workstation = workstation
			row.ppc_machine_preference = preference
			so.save()
		if submit:
			so.submit()
		return so

	# ---- compatibility comes from the configuration, never from names -----------------------------------
	def test_machines_of_the_workstation_type_are_compatible(self):
		r = self.rec()
		self.assertEqual(r["status"], "Recommended")
		self.assertCountEqual(self.machines_of(r), [self.l0, self.la, self.lb])

	def test_incompatible_machines_are_never_offered(self):
		r = self.rec()
		for ws in (self.press, self.mill):
			self.assertNotIn(ws, self.machines_of(r))

	def test_a_machine_is_not_compatible_because_of_its_name(self):
		self.assertNotIn(self.fake, self.machines_of(self.rec()), "its name says Lathe but its Workstation Type is Press")

	def test_the_boms_own_machine_counts_as_compatible(self):
		with mock.patch.object(recommend, "bom_operations", return_value=[frappe._dict(idx=1, operation=self.op_turn, workstation=self.press, workstation_type=None, time_in_mins=30, fixed_time=0, batch_size=1, sequence_id=1, hour_rate=0)]):
			self.assertEqual(self.machines_of(self.rec()), [self.press])

	def test_missing_operation_configuration_recommends_nothing(self):
		op = frappe._dict(idx=1, operation=self.op_turn, workstation=None, workstation_type=None, time_in_mins=30, fixed_time=0, batch_size=1, sequence_id=1, hour_rate=0)
		with mock.patch.object(recommend, "bom_operations", return_value=[op]):
			r = self.rec()
		self.assertEqual(r["operations"][0]["candidates"], [])
		self.assertIsNone(r["operations"][0]["selected"])
		self.assertIn("No compatible Workstation is configured", " ".join(r["warnings"]))
		self.assertEqual(r["status"], "Unavailable")
		self.assertIsNone(r["overall"]["completion"])

	def test_operation_list_adds_machines_and_can_switch_machines_off(self):
		op = frappe.get_doc("Operation", self.op_turn)
		op.append("ppc_workstations", {"workstation": self.press, "enabled": 1, "efficiency_pct": 100})
		op.append("ppc_workstations", {"workstation": self.lb, "enabled": 0})
		op.save()
		r = self.rec()
		self.assertIn(self.press, self.machines_of(r), "listed on the Operation")
		self.assertNotIn(self.lb, self.machines_of(r), "switched off for the operation although its type matches")
		self.assertIn(self.lb, [e["workstation"] for e in r["operations"][0]["excluded"]])
		self.assertIn("listed for the Operation", " ".join(next(c for c in r["operations"][0]["candidates"] if c["workstation"] == self.press)["compatibility"]["sources"]))

	def test_machines_that_are_off_or_in_maintenance_are_excluded_with_a_reason(self):
		frappe.db.set_value("Workstation", self.la, "status", "Maintenance")
		frappe.db.set_value("Workstation", self.lb, "status", "Off")
		r = self.rec()
		self.assertEqual(self.machines_of(r), [self.l0])
		reasons = {e["workstation"]: e["reason"] for e in r["operations"][0]["excluded"]}
		self.assertIn("Maintenance", reasons[self.la])
		self.assertIn("Off", reasons[self.lb])

	# ---- BOM and operation data gaps ---------------------------------------------------------------------
	def test_item_without_bom(self):
		r = self.rec(self.item_nobom)
		self.assertEqual(r["status"], "Incomplete Data")
		self.assertEqual(r["operations"], [])
		self.assertIn("bom", r["data_gaps"])
		self.assertIn("no active BOM", " ".join(r["warnings"]))

	def test_bom_without_operations(self):
		r = self.rec(self.item_noops)
		self.assertEqual(r["status"], "Incomplete Data")
		self.assertIn("operations", r["data_gaps"])

	def test_wrong_or_inactive_bom_is_rejected(self):
		self.assertEqual(self.rec(self.item1, bom_no=self.bom2)["status"], "Incomplete Data", "BOM of another item")
		self.assertEqual(self.rec(self.item1, bom_no="NO-SUCH-BOM")["status"], "Incomplete Data")

	def test_zero_or_missing_quantity_and_unknown_item(self):
		self.assertEqual(self.rec(qty=0)["status"], "Incomplete Data")
		self.assertFalse(self.rec(item="PPCT-NO-SUCH-ITEM")["ok"])

	def test_missing_duration_gives_no_invented_dates(self):
		op = frappe._dict(idx=1, operation=self.op_turn, workstation=self.l0, workstation_type=self.t_lathe, time_in_mins=0, fixed_time=0, batch_size=1, sequence_id=1, hour_rate=0)
		with mock.patch.object(recommend, "bom_operations", return_value=[op]):
			r = self.rec()
		o = r["operations"][0]
		self.assertFalse(o["duration_known"])
		self.assertIsNone(o["minutes"])
		self.assertIsNone(r["overall"]["completion"], "no completion date is made up")
		self.assertIsNone(r["overall"]["start"])
		self.assertEqual(r["status"], "Incomplete Data")
		self.assertIn("durations", r["data_gaps"])
		self.assertTrue(any("no time in the BOM" in w for w in r["warnings"]))
		self.assertTrue(o["candidates"], "machines are still ranked, on workload")

	def test_missing_capacity_is_reported_and_one_is_used(self):
		frappe.db.set_value("Workstation", self.la, "production_capacity", 0)
		c = next(c for c in self.rec()["operations"][0]["candidates"] if c["workstation"] == self.la)
		self.assertEqual(c["capacity"], 1)
		self.assertTrue(any("Capacity is not configured" in w for w in c["warnings"]))

	def test_no_cost_or_efficiency_is_invented(self):
		frappe.db.set_value("Workstation", self.la, "hour_rate", 0)
		c = self.rec()["operations"][0]["candidates"][0]
		self.assertNotIn("cost", c["factors"], "one machine has no hourly rate: the cost factor is dropped for all")
		self.assertNotIn("efficiency", c["factors"], "no efficiency is configured anywhere")
		self.assertIsNone(c["efficiency"])

	# ---- scheduling maths ----------------------------------------------------------------------------------
	def test_quantity_scales_duration_and_completion(self):
		small, big = self.rec(qty=10), self.rec(qty=100)
		self.assertEqual(small["operations"][0]["minutes"], 300)
		self.assertEqual(big["operations"][0]["minutes"], 3000)
		self.assertEqual(small["overall"]["start"], "2030-03-04 08:01:00")
		self.assertEqual(small["overall"]["completion"], "2030-03-04 13:01:00")
		self.assertEqual(big["overall"]["completion"], "2030-03-06 10:01:00")

	def test_estimates_are_not_a_reservation(self):
		counts = lambda: [frappe.db.count(dt) for dt in ("Job Card", "Work Order", "Stock Ledger Entry", "Production Plan", AUDIT_DT)]  # noqa: E731
		before = counts()
		self.rec(qty=500)
		self.assertEqual(counts(), before, "nothing is created, scheduled or booked by asking for a recommendation")
		self.assertEqual(self.rec(qty=500)["overall"], self.rec(qty=500)["overall"])

	def test_existing_booking_pushes_a_machine_back_and_changes_the_ranking(self):
		self.place(self.la, d(0), d(48))
		r = self.rec()
		ranked = self.machines_of(r)
		self.assertEqual(ranked[-1], self.la, "the machine that is booked for two days comes last")
		la = next(c for c in r["operations"][0]["candidates"] if c["workstation"] == self.la)
		self.assertEqual(la["start"], "2030-03-06 08:00:00")
		self.assertGreater(la["utilization"], 0)
		self.assertIn(r["recommended_workstation"], (self.l0, self.lb))
		self.assertTrue(any("later than the earliest option" in x for x in la["explanation"]))

	def test_capacity_two_machine_runs_alongside_a_booking(self):
		frappe.db.set_value("Workstation", self.la, "production_capacity", 2)
		self.place(self.la, d(0), d(48))
		la = next(c for c in self.rec()["operations"][0]["candidates"] if c["workstation"] == self.la)
		self.assertEqual(la["start"], "2030-03-04 08:01:00", "the second slot is free")
		self.assertEqual(la["capacity"], 2)

	def test_alternatives_are_ranked_and_explained(self):
		self.place(self.l0, d(0), d(30))
		self.place(self.la, d(0), d(10))
		r = self.rec()
		cands = r["operations"][0]["candidates"]
		self.assertEqual([c["rank"] for c in cands], [1, 2, 3])
		self.assertEqual(cands[0]["workstation"], self.lb, "free now")
		self.assertEqual([c["workstation"] for c in cands], [self.lb, self.la, self.l0])
		self.assertEqual(sorted((c["score"] for c in cands), reverse=True), [c["score"] for c in cands], "scores fall with rank")
		for c in cands:
			self.assertTrue(c["explanation"])
			self.assertEqual(c["compatibility"]["state"], "compatible")
		self.assertTrue(any("Available now" in x for x in cands[0]["explanation"]))
		self.assertEqual(r["recommended_workstation"], self.lb)

	def test_recommendation_changes_after_the_schedule_changes(self):
		first = self.rec()["recommended_workstation"]
		self.place(first, d(0), d(72))
		self.assertNotEqual(self.rec()["recommended_workstation"], first)

	def test_weights_decide_between_machines(self):
		self.place(self.la, d(0.0), d(2))  # la frees up 2 h in, a little later than the others
		by_completion = self.rec()
		self.assertEqual(by_completion["operations"][0]["candidates"][-1]["workstation"], self.la, "the default weights favour the earliest completion")
		for field, value in (("rec_weight_completion", 0), ("rec_weight_load", 100), ("rec_weight_delivery", 0), ("rec_weight_cost", 0), ("rec_weight_efficiency", 0)):
			frappe.db.set_single_value("PPC Settings", field, value)
		r = self.rec()
		self.assertEqual(r["weights"]["completion"], 0)
		self.assertEqual(r["weights"]["load"], 100)
		self.assertNotEqual(r["recommended_workstation"], self.la, "la is the most loaded machine")

	def test_weights_never_override_compatibility(self):
		for field in ("rec_weight_completion", "rec_weight_load", "rec_weight_delivery"):
			frappe.db.set_single_value("PPC Settings", field, 0)
		frappe.db.set_single_value("PPC Settings", "rec_weight_cost", 100)
		r = self.rec()
		self.assertNotIn(self.press, self.machines_of(r))
		self.assertNotIn(self.fake, self.machines_of(r))

	def test_settings_of_an_upgraded_site_save_and_take_the_defaults(self):
		"""A site migrated from 1.4.0 has no stored values for the new fields: saving the settings must not fail."""
		for field in ("rec_weight_completion", "rec_weight_load", "rec_weight_delivery", "rec_weight_cost", "rec_weight_efficiency",
			"rec_horizon_days", "rec_load_window_days", "rec_unavailable_tolerance_hours"):
			frappe.db.set_single_value("PPC Settings", field, 0)
		s = frappe.get_single("PPC Settings")
		s.save()
		self.assertEqual((s.rec_weight_completion, s.rec_horizon_days, s.rec_load_window_days), (45, 120, 7))
		self.assertEqual(self.rec()["weights"]["completion"], 45)

	def test_settings_reject_bad_weights(self):
		s = frappe.get_single("PPC Settings")
		s.rec_horizon_days, s.rec_load_window_days, s.rec_unavailable_tolerance_hours = 120, 7, 24  # configured, so not an upgraded site
		s.rec_weight_completion = -1
		self.assertRaises(frappe.ValidationError, s.validate_recommendation)
		s.rec_weight_completion = s.rec_weight_load = s.rec_weight_delivery = s.rec_weight_cost = s.rec_weight_efficiency = 0
		self.assertRaises(frappe.ValidationError, s.validate_recommendation)
		s.rec_weight_completion, s.rec_horizon_days = 10, 3
		self.assertRaises(frappe.ValidationError, s.validate_recommendation)  # look-ahead below the minimum

	# ---- several operations --------------------------------------------------------------------------------
	def test_each_operation_is_recommended_and_they_run_in_sequence(self):
		r = self.rec(self.item2)
		turn, mill = r["operations"]
		self.assertEqual((turn["operation"], mill["operation"]), (self.op_turn, self.op_mill))
		self.assertEqual(self.machines_of(r, 1), [self.mill], "the mill operation is only compatible with the mill")
		self.assertGreaterEqual(mill["candidates"][0]["start"], turn["candidates"][0]["end"], "the second operation starts after the first has finished")
		self.assertEqual(r["overall"]["start"], turn["candidates"][0]["start"])
		self.assertEqual(r["overall"]["completion"], mill["candidates"][0]["end"], "overall completion is the last operation's end")

	def test_gap_between_operations_comes_from_manufacturing_settings(self):
		frappe.db.set_single_value("Manufacturing Settings", "mins_between_operations", 30)
		turn, mill = self.rec(self.item2)["operations"]
		gap = datetime.strptime(mill["candidates"][0]["start"], "%Y-%m-%d %H:%M:%S") - datetime.strptime(turn["candidates"][0]["end"], "%Y-%m-%d %H:%M:%S")
		self.assertGreaterEqual(gap, timedelta(minutes=30))

	def test_a_busy_first_machine_delays_the_whole_route(self):
		free = self.rec(self.item2)["overall"]["completion"]
		for ws in (self.l0, self.la, self.lb):
			self.place(ws, d(0), d(24))
		self.assertGreater(self.rec(self.item2)["overall"]["completion"], free)

	def test_one_machine_for_all_operations_when_configured(self):
		frappe.db.set_single_value("PPC Settings", "rec_single_machine", 1)
		r = self.rec(self.item3)
		self.assertEqual(r["operations"][0]["selected"], r["operations"][1]["selected"])
		self.assertTrue(r["single_machine_options"])
		r2 = self.rec(self.item2)
		self.assertIn("No single machine", " ".join(r2["warnings"]), "lathe and mill have no machine in common")
		self.assertTrue(all(o["selected"] for o in r2["operations"]), "the operations are recommended separately instead")

	def test_without_single_machine_mode_a_busy_machine_is_not_forced_on_operation_two(self):
		self.place(self.l0, d(0), d(20))
		r = self.rec(self.item3)
		self.assertNotEqual(r["operations"][0]["selected"], self.l0)

	def test_planner_can_choose_another_machine_and_the_following_operations_follow(self):
		auto = self.rec(self.item3)
		pick = self.lb if auto["operations"][0]["selected"] != self.lb else self.la
		r = self.rec(self.item3, forced={self.op_turn: pick})
		self.assertEqual(r["operations"][0]["selected"], pick)
		self.assertEqual(r["operations"][0]["recommended"], auto["operations"][0]["recommended"], "the ranking itself is unchanged")
		self.assertTrue(r["manual"] or pick == r["operations"][0]["recommended"])
		bad = self.rec(self.item3, forced={self.op_turn: self.press})
		self.assertNotEqual(bad["operations"][0]["selected"], self.press)
		self.assertTrue(any("cannot be used" in w for w in bad["warnings"]))

	# ---- delivery date --------------------------------------------------------------------------------------
	def test_delivery_risk_in_the_result(self):
		self.assertEqual(self.rec(delivery_date="2030-03-30")["overall"]["risk"], "meets")
		late = self.rec(delivery_date="2030-03-03")
		self.assertEqual(late["overall"]["risk"], "misses")
		self.assertTrue(any("after the delivery date" in w for w in late["warnings"]))
		self.assertEqual(self.rec(delivery_date="2030-03-04")["overall"]["risk"], "tight")
		none = self.rec()
		self.assertEqual(none["overall"]["risk"], "unknown")
		self.assertIn("delivery_date", none["data_gaps"])

	def test_delivery_date_is_kept_apart_from_the_estimate(self):
		r = self.rec(delivery_date="2030-03-30")
		self.assertEqual(r["overall"]["delivery_date"], "2030-03-30")
		self.assertEqual(r["overall"]["completion"], "2030-03-04 13:01:00")

	def test_delivery_pushes_a_machine_that_would_miss_it_down(self):
		self.place(self.l0, d(0), d(40))
		r = self.rec(delivery_date="2030-03-04")  # only a machine that is free today can make it
		self.assertNotEqual(r["recommended_workstation"], self.l0)
		l0 = next(c for c in r["operations"][0]["candidates"] if c["workstation"] == self.l0)
		self.assertEqual(l0["delivery_risk"], "misses")
		self.assertTrue(any("miss the delivery date" in x for x in l0["explanation"]))

	# ---- Sales Order line ------------------------------------------------------------------------------------
	def test_selection_is_saved_on_the_correct_line_with_server_side_estimates(self):
		so = self.new_so((self.item1, 10), (self.item3, 5), submit=False)
		so.items[1].ppc_preferred_workstation = self.lb
		so.items[1].ppc_machine_preference = "Manual"
		so.items[1].ppc_est_completion = "2001-01-01 00:00:00"  # a value made up by the browser
		so.save()
		so.reload()
		first, second = so.items
		self.assertFalse(first.ppc_preferred_workstation)
		self.assertFalse(first.ppc_recommendation_status)
		self.assertEqual(second.ppc_preferred_workstation, self.lb)
		self.assertEqual(second.ppc_recommendation_status, "Selected")
		self.assertNotEqual(str(second.ppc_est_completion)[:4], "2001", "estimates are recalculated on the server")
		self.assertTrue(second.ppc_est_start and second.ppc_recommendation_updated_at)
		plan = json.loads(second.ppc_machine_plan)
		self.assertEqual({e["workstation"] for e in plan["operations"]}, {self.lb})
		self.assertEqual(plan["bom"], self.bom3)

	def test_recommended_choice_is_marked_recommended(self):
		best = self.rec()["recommended_workstation"]
		so = self.so_with(self.item1, best, "Recommended")
		self.assertEqual(so.items[0].ppc_recommendation_status, "Recommended")

	def test_incompatible_machine_is_refused_whatever_the_role(self):
		so = self.new_so((self.item1, 10), submit=False)
		so.items[0].ppc_preferred_workstation = self.press
		so.items[0].ppc_machine_preference = "Manual"
		self.assertRaisesRegex(frappe.ValidationError, "cannot perform any operation", so.save)
		so.reload()
		so.items[0].ppc_preferred_workstation = "PPCT No Such Workstation"
		self.assertRaisesRegex(frappe.ValidationError, "does not exist|Could not find", so.save)

	def test_per_operation_selection_is_validated(self):
		so = self.new_so((self.item2, 10), submit=False)
		row = so.items[0]
		row.ppc_preferred_workstation = self.la
		row.ppc_machine_preference = "Manual"
		row.ppc_machine_plan = json.dumps({"operations": [{"operation": self.op_turn, "workstation": self.la}, {"operation": self.op_mill, "workstation": self.la}]})
		self.assertRaisesRegex(frappe.ValidationError, "cannot perform", so.save)  # a lathe cannot mill
		so.reload()
		row = so.items[0]
		row.ppc_preferred_workstation = self.la
		row.ppc_machine_preference = "Manual"
		row.ppc_machine_plan = json.dumps({"operations": [{"operation": self.op_turn, "workstation": self.la}, {"operation": self.op_mill, "workstation": self.mill}]})
		so.save()
		self.assertEqual(so.items[0].ppc_recommendation_status, "Selected")

	def test_changing_quantity_marks_the_recommendation_stale(self):
		so = self.so_with(self.item1, self.lb)
		self.assertEqual(so.items[0].ppc_recommendation_status, "Selected")
		so.items[0].qty = 40
		so.save()
		self.assertEqual(so.items[0].ppc_recommendation_status, "Stale")
		self.assertEqual(so.items[0].ppc_preferred_workstation, self.lb, "the choice is kept, only flagged")

	def test_changing_item_or_delivery_date_marks_it_stale_too(self):
		so = self.so_with(self.item1, self.lb)
		so.items[0].delivery_date = frappe.utils.add_days(so.items[0].delivery_date, 20)
		so.save()
		self.assertEqual(so.items[0].ppc_recommendation_status, "Stale")

	def test_no_preference_clears_everything(self):
		so = self.so_with(self.item1, self.lb)
		so.items[0].ppc_machine_preference = "No Preference"
		so.save()
		row = so.items[0]
		self.assertFalse(row.ppc_preferred_workstation)
		self.assertFalse(row.ppc_machine_plan)
		self.assertFalse(row.ppc_est_completion)

	def test_selection_can_be_changed_after_submit(self):
		so = self.so_with(self.item1, self.lb, submit=True)
		so.items[0].ppc_preferred_workstation = self.la
		so.items[0].ppc_machine_preference = "Manual"
		so.save()
		self.assertEqual(frappe.db.get_value("Sales Order Item", so.items[0].name, "ppc_preferred_workstation"), self.la)

	# ---- Production Plan and Work Order ------------------------------------------------------------------------
	def test_preference_is_carried_into_the_production_plan(self):
		so = self.so_with(self.item1, self.lb, submit=True)
		plan = self.new_plan(so.name)
		row = plan.po_items[0]
		self.assertEqual(row.sales_order_item, so.items[0].name, "the source line stays referenced")
		self.assertEqual(row.ppc_preferred_workstation, self.lb)
		self.assertEqual(row.ppc_machine_preference, "Manual")
		self.assertEqual(json.loads(row.ppc_machine_plan)["operations"][0]["workstation"], self.lb)

	def test_lines_without_preference_are_untouched(self):
		so = self.new_so((self.item1, 10))
		row = self.new_plan(so.name).po_items[0]
		self.assertFalse(row.ppc_preferred_workstation)

	def test_the_work_order_and_its_job_cards_use_the_preferred_machine(self):
		so = self.so_with(self.item1, self.lb, submit=True)
		plan = self.new_plan(so.name, auto_create=True, auto_submit=True)
		plan.submit()
		wo = self.work_order_of(plan, self.item1)
		self.assertEqual(frappe.db.get_value("Work Order Operation", {"parent": wo}, "workstation"), self.lb)
		self.assertEqual(self.job_cards(wo)[0].workstation, self.lb)
		self.assertIn("applied", frappe.db.get_value("Work Order", wo, "ppc_machine_note"))

	def test_without_a_preference_the_boms_machine_is_used(self):
		so = self.new_so((self.item1, 10))
		plan = self.new_plan(so.name, auto_create=True, auto_submit=True)
		plan.submit()
		wo = self.work_order_of(plan, self.item1)
		self.assertEqual(frappe.db.get_value("Work Order Operation", {"parent": wo}, "workstation"), self.l0)
		self.assertFalse(frappe.db.get_value("Work Order", wo, "ppc_machine_note"))

	def test_a_machine_that_became_unavailable_is_not_applied_and_the_planner_is_told(self):
		so = self.so_with(self.item1, self.lb, submit=True)
		frappe.db.set_value("Workstation", self.lb, "status", "Maintenance")  # between the order and the Work Order
		plan = self.new_plan(so.name, auto_create=True, auto_submit=False)
		plan.submit()
		wo_name = self.work_order_of(plan, self.item1)
		wo = frappe.get_doc("Work Order", wo_name)
		self.assertEqual(wo.operations[0].workstation, self.l0, "the BOM's machine is kept")
		self.assertIn("NOT applied", wo.ppc_machine_note)
		self.assertIn("Maintenance", wo.ppc_machine_note)
		self.assertTrue(frappe.db.exists("Notification Log", {"document_type": "Work Order", "document_name": wo_name}), "the planner is notified")
		audit = frappe.db.get_value(AUDIT_DT, {"work_order": wo_name, "action": A_APPLY_MACHINE_PLAN}, ["result", "details"], as_dict=True)
		self.assertEqual(audit.result, "Warning")
		self.assertIn("not_applied", audit.details)
		self.assertIn("alternatives", audit.details)
		self.assertEqual(wo.docstatus, 0, "nothing is submitted automatically")
		self.assertFalse(frappe.db.count("Job Card", {"work_order": wo_name}))

	def test_a_machine_that_is_busy_for_too_long_is_not_applied(self):
		now = now_datetime()
		self.place(self.lb, add_to_date(now, hours=-1), add_to_date(now, hours=200))
		so = self.so_with(self.item1, self.lb, submit=True)
		plan = self.new_plan(so.name, auto_create=True, auto_submit=False)
		plan.submit()
		wo = frappe.get_doc("Work Order", self.work_order_of(plan, self.item1))
		self.assertEqual(wo.operations[0].workstation, self.l0)
		self.assertIn("busy until", wo.ppc_machine_note)

	def test_a_machine_that_is_no_longer_compatible_is_not_applied(self):
		so = self.so_with(self.item1, self.lb, submit=True)
		frappe.db.set_value("Workstation", self.lb, "workstation_type", self.t_press)  # reconfigured after the order was saved
		plan = self.new_plan(so.name, auto_create=True, auto_submit=False)
		plan.submit()
		wo = frappe.get_doc("Work Order", self.work_order_of(plan, self.item1))
		self.assertEqual(wo.operations[0].workstation, self.l0)
		self.assertIn("no longer compatible", wo.ppc_machine_note)

	def test_the_hourly_check_flags_lines_whose_machine_went_down(self):
		so = self.so_with(self.item1, self.lb, submit=True)
		frappe.db.set_value("Workstation", self.lb, "status", "Problem")
		with mock.patch.object(frappe.db, "commit"):
			tasks.flag_unavailable_machine_plans()
		self.assertEqual(frappe.db.get_value("Sales Order Item", so.items[0].name, "ppc_recommendation_status"), "Unavailable")
		self.assertTrue(frappe.db.exists("Notification Log", {"document_type": "Sales Order", "document_name": so.name}))

	# ---- permissions ----------------------------------------------------------------------------------------------
	def test_who_may_ask_for_a_recommendation(self):
		args = {"item_code": self.item1, "qty": 10}
		for user in (self.planner, self.sales, self.viewer):
			frappe.set_user(user)
			self.assertEqual(api.recommend_machines(**args)["status"], "Recommended", user)
		frappe.set_user(self.nobody)
		self.assertRaises(frappe.PermissionError, api.recommend_machines, **args)

	def test_api_validates_its_input(self):
		frappe.set_user(self.planner)
		self.assertRaises(frappe.ValidationError, api.recommend_machines, self.item1, 10, forced="[1, 2]")
		r = api.recommend_machines(self.item1, "10", forced=json.dumps({self.op_turn: self.la}))
		self.assertEqual(r["operations"][0]["selected"], self.la)

	def test_a_user_who_cannot_read_the_order_cannot_use_it_as_context(self):
		so = self.new_so((self.item1, 10))
		self.assertFalse(frappe.has_permission("Sales Order", "read", so.name, user=self.viewer), "precondition")
		frappe.set_user(self.viewer)
		self.assertRaises(frappe.PermissionError, api.recommend_machines, self.item1, 10, sales_order=so.name)

	def test_the_disclaimer_says_it_is_not_a_reservation(self):
		self.assertIn("not a reservation", self.rec()["disclaimer"])
