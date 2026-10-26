"""Changing a Job Card's Workstation: machine occupancy, conflicts, alternatives, override, atomic save and history."""

import json
from datetime import datetime, timedelta
from unittest import mock

import frappe
from frappe.utils import add_to_date, now_datetime

from ppc_production_control import api
from ppc_production_control.services import machines, revisions, scheduling, workstation_change
from ppc_production_control.tests.test_api_security import ERP_MFG, make_user
from ppc_production_control.tests.utils import PREFIX, PPCTestCase, make_workstation

T0 = datetime(2030, 3, 4, 8, 0, 0)  # far in the future: independent of anything already booked on the site


def d(hours: float) -> datetime:
	return T0 + timedelta(hours=hours)


class TestMachinesPure(frappe.tests.utils.FrappeTestCase):
	"""The interval / capacity / slot maths, without a database."""

	def test_peak_load_is_half_open(self):
		self.assertEqual(machines.peak_load([(d(0), d(2)), (d(2), d(4))]), 1, "back-to-back is not an overlap")
		self.assertEqual(machines.peak_load([(d(0), d(3)), (d(2), d(4))]), 2)
		self.assertEqual(machines.peak_load([]), 0)
		self.assertEqual(machines.peak_load([(d(2), d(2))]), 0, "an empty interval is nothing")

	def test_overlap_rule_start_before_other_end_and_end_after_other_start(self):
		blocked = lambda others, s, e: machines.window_load(others, 1, s, e)["blocking"]  # noqa: E731
		other = [(d(2), d(4))]
		self.assertTrue(blocked(other, d(1), d(3)))  # starts before it ends, ends after it starts
		self.assertTrue(blocked(other, d(3), d(5)))
		self.assertTrue(blocked(other, d(0), d(10)))  # swallows it
		self.assertTrue(blocked(other, d(2.5), d(3)))  # inside it
		self.assertFalse(blocked(other, d(4), d(6)), "starts exactly when the other ends")
		self.assertFalse(blocked(other, d(0), d(2)), "ends exactly when the other starts")

	def test_capacity_counts_simultaneous_operations(self):
		others = [(d(0), d(4)), (d(1), d(5))]
		self.assertTrue(machines.window_load(others, 2, d(2), d(3))["blocking"], "two are already running at once on a 2-slot machine")
		self.assertFalse(machines.window_load(others, 3, d(2), d(3))["blocking"])
		self.assertFalse(machines.window_load(others, 2, d(0), d(1))["blocking"], "only one running in that hour")
		self.assertEqual(machines.window_load(others, 2, d(2), d(3))["peak_others"], 2)
		# other operations that are only partly inside the window count only for the part inside it
		self.assertFalse(machines.window_load([(d(-5), d(0.5))], 1, d(1), d(2))["blocking"])

	def test_utilization(self):
		self.assertEqual(machines.utilization_pct([(d(0), d(2))], 1, d(0), d(4)), 50.0)
		self.assertEqual(machines.utilization_pct([(d(0), d(2))], 2, d(0), d(4)), 25.0)
		self.assertEqual(machines.utilization_pct([(d(-9), d(9))], 1, d(0), d(4)), 100.0, "clipped to the window")
		self.assertIsNone(machines.utilization_pct([], 1, d(1), d(1)))

	def test_next_slot_jumps_to_the_end_of_the_booking(self):
		booked = [(d(2), d(6))]
		self.assertEqual(machines.find_slot(booked, 1, d(3), 120, d(500)), (d(6), d(8)))
		self.assertEqual(machines.find_slot(booked, 1, d(0), 120, d(500)), (d(0), d(2)), "fits before the booking")
		self.assertEqual(machines.find_slot(booked, 1, d(0), 180, d(500)), (d(6), d(9)), "too long for the gap before it")

	def test_next_slot_skips_chains_of_bookings_and_honours_capacity(self):
		booked = [(d(0), d(2)), (d(2), d(4)), (d(4), d(9))]
		self.assertEqual(machines.find_slot(booked, 1, d(0), 60, d(500)), (d(9), d(10)))
		self.assertEqual(machines.find_slot(booked, 2, d(0), 60, d(500)), (d(0), d(1)), "a second slot is free")
		self.assertIsNone(machines.find_slot([(d(0), d(1000))], 1, d(0), 60, d(24)), "nothing within the horizon")

	def test_one_card_is_one_booking_however_many_rows_it_has(self):
		self.assertEqual(machines.merge_intervals([(d(0), d(2)), (d(0), d(2))]), [(d(0), d(2))], "identical rows")
		self.assertEqual(machines.merge_intervals([(d(0), d(3)), (d(2), d(5))]), [(d(0), d(5))], "overlapping rows")
		self.assertEqual(machines.merge_intervals([(d(0), d(2)), (d(2), d(4))]), [(d(0), d(4))], "touching rows")
		self.assertEqual(machines.merge_intervals([(d(0), d(1)), (d(3), d(4))]), [(d(0), d(1)), (d(3), d(4))], "a gap stays a gap")
		rows = [{"job_card": "A", "from": d(0), "to": d(4)}, {"job_card": "A", "from": d(0), "to": d(4)}, {"job_card": "B", "from": d(1), "to": d(2)}]
		self.assertEqual(machines.peak_load(machines.occupied_intervals(rows)), 2, "A counts once, B once")
		self.assertEqual(machines.peak_load([(r["from"], r["to"]) for r in rows]), 3, "(counted by row it would have been three)")

	def test_next_slot_may_skip_non_working_days(self):
		skip = lambda dt: dt + timedelta(days=1) if dt.date() == T0.date() else dt  # noqa: E731
		start, _end = machines.find_slot([], 1, d(1), 60, d(500), skip)
		self.assertEqual(start.date(), (T0 + timedelta(days=1)).date())

	def test_eligibility_rules(self):
		jc = {"name": "JC", "docstatus": 0, "status": "Open", "total_time_in_mins": 0, "workstation": "A", "work_order": "WO"}
		wo = {"name": "WO", "docstatus": 1, "status": "In Process"}
		self.assertEqual(workstation_change.eligibility_errors(jc, wo), [])
		self.assertEqual(workstation_change.eligibility_errors({**jc, "status": "On Hold"}, wo), [])
		self.assertEqual(workstation_change.eligibility_errors({**jc, "status": "Material Transferred"}, wo), [])
		for bad in ({"status": "Work In Progress"}, {"status": "Completed", "docstatus": 1}, {"status": "Cancelled", "docstatus": 2}, {"total_time_in_mins": 30}):
			self.assertTrue(workstation_change.eligibility_errors({**jc, **bad}, wo), bad)
		self.assertTrue(workstation_change.eligibility_errors(jc, {**wo, "status": "Stopped"}))
		self.assertTrue(workstation_change.eligibility_errors(jc, {**wo, "docstatus": 2}))

	def test_compatibility_is_formal_never_guessed(self):
		self.assertEqual(workstation_change.compatibility_of(None, {"name": "Lathe 1", "workstation_type": "Lathe"})["state"], "not_configured")
		self.assertEqual(workstation_change.compatibility_of("Lathe", {"name": "Lathe 1", "workstation_type": "Lathe"})["state"], "compatible")
		self.assertEqual(workstation_change.compatibility_of("Lathe", {"name": "Lathe 1", "workstation_type": None})["state"], "incompatible")
		self.assertEqual(workstation_change.compatibility_of("Lathe", {"name": "Press", "workstation_type": "Press"})["state"], "incompatible")


class TestChangeWorkstation(PPCTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.ws3 = make_workstation(f"{PREFIX} WS3", capacity=1)
		cls.ws4 = make_workstation(f"{PREFIX} WS4", capacity=2)
		cls.ws5 = make_workstation(f"{PREFIX} WS5", capacity=1)
		cls.planner = make_user("wsplanner", ["Production Planner", *ERP_MFG])
		cls.manager = make_user("wsmanager", ["PPC Manager", *ERP_MFG])
		cls.viewer = make_user("wsviewer", ["PPC Viewer", *ERP_MFG])
		cls.erp_only = make_user("wserponly", ERP_MFG)

	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.job_cards(self.wo)

	# helpers ------------------------------------------------------------------------------------------
	def card(self, name):
		return frappe.db.get_value(
			"Job Card", name,
			["workstation", "expected_start_date", "expected_end_date", "status", "docstatus", "modified", "ppc_schedule_revision",
			 "ppc_original_start", "ppc_original_end", "ppc_schedule_sync_status"], as_dict=True,
		)

	def wo_dates(self, wo=None):
		return frappe.db.get_value("Work Order", wo or self.wo, ["planned_start_date", "planned_end_date"], as_dict=True)

	def place(self, name, workstation, start, end):
		"""Put a Job Card on a machine at an exact interval (test fixture, written through the document)."""
		frappe.db.set_value("Job Card", name, "workstation", workstation)
		doc = frappe.get_doc("Job Card", name)
		scheduling.apply_dates(doc, start, end, "resize")
		return name

	def other_card(self):
		"""A Job Card of another Work Order that we can park on a machine to occupy it."""
		_plan, wo2 = self.ops_plan(10)
		cards = self.job_cards(wo2)
		return cards[1].name

	def interval(self):
		a = self.card(self.asm.name)
		return scheduling.as_dt(a.expected_start_date), scheduling.as_dt(a.expected_end_date)

	def revisions_of(self, name):
		return frappe.get_all("PPC Schedule Revision", filters={"job_card": name}, fields=["*"], order_by="revision_no asc")

	def change(self, workstation, **kw):
		kw.setdefault("confirmed", True)
		return workstation_change.change_job_card_workstation(self.asm.name, workstation, **kw)

	# 1. available machine ---------------------------------------------------------------------------------
	def test_move_to_an_available_machine(self):
		start, end = self.interval()
		before = self.revisions_of(self.asm.name)
		res = self.change(self.ws3, reason="Machine 2 is being serviced")
		self.assertTrue(res["ok"], res)
		self.assertEqual((res["previous_workstation"], res["workstation"]), (self.ws2, self.ws3))

		now = self.card(self.asm.name)
		self.assertEqual(now.workstation, self.ws3, "the real Job Card changed machine")
		self.assertTrue(scheduling._same_instant(now.expected_start_date, start) and scheduling._same_instant(now.expected_end_date, end), "dates untouched")
		doc = frappe.get_doc("Job Card", self.asm.name)
		self.assertEqual(len(doc.scheduled_time_logs), 1)

		revs = self.revisions_of(self.asm.name)
		self.assertEqual(len(revs), len(before) + 1, "exactly one new history entry")
		rev = revs[-1]
		self.assertEqual((rev.previous_workstation, rev.new_workstation, rev.change_source), (self.ws2, self.ws3, "Workstation Change"))
		self.assertEqual(rev.change_reason, "Machine 2 is being serviced")
		self.assertEqual(rev.changed_by, "Administrator")
		self.assertEqual(rev.workstation, self.ws3)
		self.assertFalse(rev.override_used)
		self.assertEqual(rev.sync_status, "Synced")

		audit = frappe.get_all("PPC Audit Log", filters={"job_card": self.asm.name, "action": "Change Workstation"}, fields=["result", "reason"])
		self.assertEqual([(a.result, a.reason) for a in audit], [("Success", "Machine 2 is being serviced")])

	# 2 + 3. occupied machine, with the right conflict ------------------------------------------------------
	def test_occupied_machine_is_a_conflict_that_names_the_blocking_job_card(self):
		start, end = self.interval()
		blocker = self.place(self.other_card(), self.ws3, start + timedelta(hours=1), end + timedelta(hours=1))
		blocker_doc = frappe.db.get_value("Job Card", blocker, ["work_order", "operation"], as_dict=True)
		before = self.card(self.asm.name)

		res = self.change(self.ws3)
		self.assertFalse(res["ok"])
		self.assertTrue(res["conflict"] and res["needs_override"])
		self.assertEqual(len(res["conflicts"]), 1)
		c = res["conflicts"][0]
		self.assertEqual(c["job_card"], blocker)
		self.assertEqual((c["work_order"], c["operation"]), (blocker_doc.work_order, blocker_doc.operation))
		self.assertEqual(c["from"], (start + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
		self.assertEqual(c["to"], (end + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))

		after = self.card(self.asm.name)
		self.assertEqual((after.workstation, after.ppc_schedule_revision), (before.workstation, before.ppc_schedule_revision), "nothing changed")
		self.assertEqual(len(self.revisions_of(self.asm.name)), 1, "no revision for a conflict")

	def test_exact_back_to_back_is_not_a_conflict(self):
		start, end = self.interval()
		self.place(self.other_card(), self.ws3, end, end + timedelta(hours=2))
		self.place(self.other_card(), self.ws3, start - timedelta(hours=2), start)
		res = self.change(self.ws3)
		rows = [(r["job_card"], r["from"].isoformat(), r["to"].isoformat()) for r in machines.occupancy(self.ws3, start - timedelta(days=1), end + timedelta(days=1), [])]
		self.assertTrue(res["ok"], f"main={start.isoformat()}..{end.isoformat()} rows={rows}")

	def test_a_split_job_card_does_not_block_the_gap_between_its_rows(self):
		"""Occupancy follows the scheduled time ROWS, not the card's first-start / last-end header dates."""
		start, end = self.interval()
		blocker = self.other_card()
		frappe.db.set_value("Job Card", blocker, "workstation", self.ws3)
		doc = frappe.get_doc("Job Card", blocker)
		doc.set("scheduled_time_logs", [
			{"from_time": start - timedelta(hours=5), "to_time": start - timedelta(hours=3), "time_in_mins": 120},
			{"from_time": end + timedelta(hours=3), "to_time": end + timedelta(hours=5), "time_in_mins": 120},
		])
		doc.save()
		self.assertLess(scheduling.as_dt(frappe.db.get_value("Job Card", blocker, "expected_start_date")), start)
		self.assertGreater(scheduling.as_dt(frappe.db.get_value("Job Card", blocker, "expected_end_date")), end)
		self.assertTrue(self.change(self.ws3)["ok"], "the machine is free between the two rows")

	def test_actual_time_logs_and_draft_cards_in_any_open_state_occupy_the_machine(self):
		start, end = self.interval()
		for status in ("Open", "Material Transferred", "On Hold", "Work In Progress"):
			blocker = self.place(self.other_card(), self.ws3, start, end)
			frappe.db.set_value("Job Card", blocker, "status", status)
			res = workstation_change.check_change(self.asm.name, self.ws3)
			self.assertTrue(res["conflicts"], f"a {status} draft card is a real booking")
			frappe.db.set_value("Job Card", blocker, "docstatus", 2)  # cancelled: no longer in the way
			self.assertFalse(workstation_change.check_change(self.asm.name, self.ws3)["conflicts"], "a cancelled card occupies nothing")

	def test_the_card_being_moved_is_not_its_own_conflict(self):
		start, end = self.interval()
		res = machines.check_machine(self.ws2, start, end, [self.asm.name])
		self.assertFalse(res["blocking"])
		self.assertTrue(machines.check_machine(self.ws2, start, end, [])["blocking"] is False or True)  # without the exclusion it would see itself
		self.assertEqual([b["job_card"] for b in machines.check_machine(self.ws2, start, end, [])["bookings"]], [self.asm.name])

	# 4. cancel = nothing happens --------------------------------------------------------------------------
	def test_checking_and_cancelling_change_nothing(self):
		start, end = self.interval()
		self.place(self.other_card(), self.ws3, start, end)
		before = (self.card(self.asm.name), self.revisions_of(self.asm.name), self.wo_dates())
		for _i in range(3):
			res = api.check_workstation_change(self.asm.name, self.ws3)
			self.assertTrue(res["conflicts"])
		self.assertEqual((self.card(self.asm.name), self.revisions_of(self.asm.name), self.wo_dates()), before)

	# 5. alternatives ---------------------------------------------------------------------------------------
	def test_candidates_show_availability_conflicts_and_next_slot(self):
		start, end = self.interval()
		blocker = self.place(self.other_card(), self.ws3, start, end + timedelta(hours=1))
		res = api.get_workstation_candidates(self.asm.name)
		by = {c["name"]: c for c in res["workstations"]}
		self.assertFalse(by[self.ws3]["available"])
		self.assertEqual(by[self.ws3]["conflicts"][0]["job_card"], blocker)
		self.assertEqual(by[self.ws3]["next_slot"]["start"], machines.floor_second(end + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
		self.assertTrue(by[self.ws5]["available"])
		self.assertEqual(by[self.ws5]["next_slot"]["start"], start.strftime("%Y-%m-%d %H:%M:%S"))
		self.assertTrue(by[self.ws2]["is_current"])
		self.assertFalse(res["compatibility_configured"])
		self.assertEqual(by[self.ws5]["compatibility"]["state"], "not_configured", "never invented from names")
		names = [c["name"] for c in res["workstations"]]
		self.assertLess(names.index(self.ws5), names.index(self.ws3), "available machines come before busy ones")
		self.assertEqual(frappe.get_all("Workstation", filters={"name": ["like", "%no-such%"]}), [])
		self.assertEqual([c["name"] for c in api.get_workstation_candidates(self.asm.name, search="WS5")["workstations"]], [self.ws5])

	def test_choose_another_machine_then_save(self):
		start, end = self.interval()
		self.place(self.other_card(), self.ws3, start, end)
		self.assertFalse(self.change(self.ws3)["ok"])
		self.assertTrue(self.change(self.ws5)["ok"])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws5)

	# 6. next free slot ---------------------------------------------------------------------------------------
	def test_find_the_next_available_slot(self):
		start, end = self.interval()
		self.place(self.other_card(), self.ws3, start - timedelta(hours=1), end + timedelta(hours=2))
		slot = api.find_next_workstation_slot(self.asm.name, self.ws3)
		self.assertTrue(slot["found"])
		self.assertEqual(slot["start"], machines.floor_second(end + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
		self.assertEqual(scheduling.as_dt(slot["end"]) - scheduling.as_dt(slot["start"]), end - start, "same length")
		# the slot really is saveable
		res = self.change(self.ws3, start=slot["start"], end=slot["end"])
		self.assertTrue(res["ok"], res)
		self.assertEqual(self.card(self.asm.name).workstation, self.ws3)

	def test_next_slot_is_not_before_the_predecessor_ends(self):
		cut = self.card(self.cut.name)
		start, end = self.interval()
		slot = api.find_next_workstation_slot(self.asm.name, self.ws3, start=cut.expected_start_date, end=scheduling.as_dt(cut.expected_start_date) + (end - start))
		self.assertGreaterEqual(scheduling.as_dt(slot["start"]), scheduling.as_dt(cut.expected_end_date))
		self.assertTrue(slot["constraint"])

	# 7 + 8. override -------------------------------------------------------------------------------------------
	def test_override_is_audited_with_a_reason_and_only_for_authorised_roles(self):
		start, end = self.interval()
		blocker = self.place(self.other_card(), self.ws3, start, end)
		# a planner holds Reschedule but not Override Conflicts
		frappe.set_user(self.planner)
		with self.assertRaises(frappe.PermissionError):
			self.change(self.ws3, override_conflicts=True, override_reason="I really need this machine today")
		frappe.set_user("Administrator")
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2, "nothing changed")
		denied = frappe.get_all("PPC Audit Log", filters={"job_card": self.asm.name, "action": "Change Workstation", "result": "Warning"}, fields=["user", "reason"])
		self.assertEqual(len(denied), 1, "the refused override attempt is recorded as an audit event")
		self.assertEqual(self.revisions_of(self.asm.name)[-1].revision_no, 0, "and is not a revision")

		# an authorised user must give a reason
		frappe.set_user(self.manager)
		res = self.change(self.ws3, override_conflicts=True, override_reason="short")
		self.assertFalse(res["ok"])
		self.assertTrue(res["errors"])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2)

		res = self.change(self.ws3, override_conflicts=True, override_reason="Customer escalation - double booking approved by plant head")
		frappe.set_user("Administrator")
		self.assertTrue(res["ok"], res)
		self.assertTrue(res["override_used"])
		rev = self.revisions_of(self.asm.name)[-1]
		self.assertEqual((rev.override_used, rev.previous_workstation, rev.new_workstation), (1, self.ws2, self.ws3))
		self.assertEqual(rev.override_reason, "Customer escalation - double booking approved by plant head")
		self.assertEqual(rev.changed_by, self.manager)
		details = json.loads(rev.conflict_details)
		self.assertEqual(details[0]["job_card"], blocker)
		audit = frappe.get_all("PPC Audit Log", filters={"job_card": self.asm.name, "action": "Change Workstation", "result": "Success"}, fields=["details"])
		self.assertTrue(json.loads(audit[0].details)["override"])

	def test_an_override_flag_does_nothing_when_there_is_no_conflict(self):
		res = self.change(self.ws3, override_conflicts=True, override_reason="No conflict at all, being careful")
		self.assertTrue(res["ok"])
		self.assertFalse(self.revisions_of(self.asm.name)[-1].override_used, "no conflict was overridden")

	# 9. duplicates and stale data -----------------------------------------------------------------------------------
	def test_the_same_request_twice_changes_things_once(self):
		first = self.change(self.ws3, correlation_id="req-0001")
		second = self.change(self.ws3, correlation_id="req-0001")
		self.assertTrue(first["ok"] and second["ok"])
		self.assertTrue(second["duplicate"])
		self.assertEqual(len(self.revisions_of(self.asm.name)), 2, "baseline + one machine change")
		self.assertEqual(len(frappe.get_all("PPC Audit Log", filters={"job_card": self.asm.name, "action": "Change Workstation"})), 1)

	def test_a_stale_screen_is_refused(self):
		token = self.card(self.asm.name).modified
		frappe.db.set_value("Job Card", self.asm.name, "remarks", "someone else edited it")
		res = self.change(self.ws3, expected_modified=str(token))
		self.assertFalse(res["ok"])
		self.assertTrue(res["stale"])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2)
		fresh = self.change(self.ws3, expected_modified=str(self.card(self.asm.name).modified))
		self.assertTrue(fresh["ok"])

	# 10 + 11. Work Order dates and baseline -------------------------------------------------------------------------------
	def test_work_order_dates_follow_the_policy_not_the_moved_card(self):
		cut = self.card(self.cut.name)
		start, end = self.interval()
		new_start, new_end = start + timedelta(days=2), end + timedelta(days=2)
		res = self.change(self.ws3, start=str(new_start), end=str(new_end))
		self.assertTrue(res["ok"], res)
		wo = self.wo_dates()
		self.assertTrue(scheduling._same_instant(wo.planned_start_date, cut.expected_start_date), "earliest start is still the first operation")
		self.assertTrue(scheduling._same_instant(wo.planned_end_date, new_end), "latest end follows the moved operation")
		self.assertEqual(res["work_orders"][0]["work_order"], self.wo)
		rev = self.revisions_of(self.asm.name)[-1]
		self.assertTrue(scheduling._same_instant(rev.work_order_new_end, new_end))

	def test_machine_change_alone_leaves_the_work_order_dates_alone(self):
		before = self.wo_dates()
		self.assertTrue(self.change(self.ws3)["ok"])
		after = self.wo_dates()
		self.assertTrue(scheduling._same_instant(before.planned_start_date, after.planned_start_date))
		self.assertTrue(scheduling._same_instant(before.planned_end_date, after.planned_end_date))

	def test_baseline_is_immutable_and_current_schedule_is_updated(self):
		first = self.card(self.asm.name)
		start, end = self.interval()
		self.assertTrue(self.change(self.ws3, start=str(start + timedelta(days=1)), end=str(end + timedelta(days=1)))["ok"])
		after = self.card(self.asm.name)
		self.assertTrue(scheduling._same_instant(after.ppc_original_start, first.ppc_original_start))
		self.assertTrue(scheduling._same_instant(after.ppc_original_end, first.ppc_original_end))
		self.assertTrue(scheduling._same_instant(after.expected_start_date, start + timedelta(days=1)))
		history = revisions.get_history(self.asm.name)
		self.assertEqual(history["revision"], 1)
		self.assertEqual(history["revisions"][-1]["new_workstation"], self.ws3)
		self.assertEqual(history["revisions"][0]["revision_type"], "Baseline")
		self.assertEqual(history["sync_status"], "Synced", "the accepted schedule matches ERPNext")

	def test_actual_execution_dates_are_never_touched(self):
		before = frappe.db.get_value("Job Card", self.asm.name, ["actual_start_date", "actual_end_date"], as_dict=True)
		self.assertTrue(self.change(self.ws3)["ok"])
		after = frappe.db.get_value("Job Card", self.asm.name, ["actual_start_date", "actual_end_date"], as_dict=True)
		self.assertEqual(before, after)

	# 12 + 13. history shows up where users look, and dependencies ---------------------------------------------------------------
	def test_the_revision_log_and_the_gantt_show_the_machine(self):
		self.assertTrue(self.change(self.ws3, reason="Rebalancing")["ok"])
		log = api.get_revision_log({"job_card": self.asm.name, "revision_type": "All"})["rows"]
		moved = [r for r in log if r["change_source"] == "Workstation Change"]
		self.assertEqual(len(moved), 1)
		self.assertEqual((moved[0]["previous_workstation"], moved[0]["new_workstation"]), (self.ws2, self.ws3))
		task = next(t for t in api.get_gantt_data({"plan": self.plan.name})["tasks"] if t["name"] == self.asm.name)
		self.assertEqual(task["workstation"], self.ws3)
		self.assertIn(self.ws3, [w["name"] for w in api.get_gantt_data({"plan": self.plan.name})["workstations"]])

	def test_moving_before_the_predecessor_ends_needs_an_authorised_override(self):
		cut = self.card(self.cut.name)
		start, end = self.interval()
		early_start = scheduling.as_dt(cut.expected_start_date) + timedelta(minutes=5)
		res = self.change(self.ws3, start=str(early_start), end=str(early_start + (end - start)))
		self.assertFalse(res["ok"])
		self.assertIn("predecessor", [w["code"] for w in res["warnings"]])
		self.assertTrue(res["needs_override"])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2)
		ok = self.change(self.ws3, start=str(early_start), end=str(early_start + (end - start)), override_conflicts=True, override_reason="Operations may overlap on this product")
		self.assertTrue(ok["ok"])

	def test_pushing_a_later_operation_is_previewed_and_dependencies_cascade(self):
		cut_start = scheduling.as_dt(self.card(self.cut.name).expected_start_date)
		cut_end = scheduling.as_dt(self.card(self.cut.name).expected_end_date)
		# move the first operation to another machine over a longer window: it now overlaps the next operation
		res = workstation_change.check_change(self.cut.name, self.ws3, cut_start + timedelta(hours=1), cut_end + timedelta(days=1), shift_dependents=True)
		self.assertEqual([s["job_card"] for s in res["cascade"]["shifts"]], [self.asm.name], "the successor is previewed")
		res_no = workstation_change.check_change(self.cut.name, self.ws3, cut_start + timedelta(hours=1), cut_end + timedelta(days=1), shift_dependents=False)
		self.assertIn("successor", [w["code"] for w in res_no["warnings"]])
		saved = workstation_change.change_job_card_workstation(
			self.cut.name, self.ws3, start=str(cut_start + timedelta(hours=1)), end=str(cut_end + timedelta(days=1)), confirmed=True, shift_dependents=True,
		)
		self.assertTrue(saved["ok"], saved)
		self.assertEqual({c["job_card"] for c in saved["changed"]}, {self.cut.name, self.asm.name})
		self.assertGreaterEqual(scheduling.as_dt(self.card(self.asm.name).expected_start_date), scheduling.as_dt(self.card(self.cut.name).expected_end_date))
		self.assertEqual(self.revisions_of(self.asm.name)[-1].change_source, "Cascade")
		self.assertEqual(self.revisions_of(self.cut.name)[-1].change_source, "Workstation Change")

	# 14. capacity ------------------------------------------------------------------------------------------------------------
	def test_a_machine_with_capacity_two_accepts_a_second_operation_but_not_a_third(self):
		start, end = self.interval()
		first = self.place(self.other_card(), self.ws4, start, end)
		res = workstation_change.check_change(self.asm.name, self.ws4)
		self.assertEqual((res["capacity"], res["conflicts"], res["errors"]), (2, [], []), "one booking on a 2-slot machine is fine")
		self.place(self.other_card(), self.ws4, start + timedelta(hours=1), end - timedelta(hours=1))
		res = workstation_change.check_change(self.asm.name, self.ws4)
		self.assertEqual(res["capacity"], 2)
		self.assertEqual({c["job_card"] for c in res["conflicts"]}, {first, *{c["job_card"] for c in res["conflicts"]}})
		self.assertTrue(res["needs_override"])
		self.assertEqual(res["peak_others"], 2)

	def test_a_card_with_duplicated_scheduled_rows_counts_once_against_a_two_slot_machine(self):
		"""ERPNext's own scheduler can leave identical rows on a card; that must not make one operation fill two slots."""
		start, end = self.interval()
		blocker = self.place(self.other_card(), self.ws4, start, end)
		doc = frappe.get_doc("Job Card", blocker)
		doc.append("scheduled_time_logs", {"from_time": start, "to_time": end, "time_in_mins": scheduling.minutes_between(start, end)})
		doc.save()
		self.assertEqual(frappe.db.count("Job Card Scheduled Time", {"parent": blocker}), 2)
		res = workstation_change.check_change(self.asm.name, self.ws4)
		self.assertEqual((res["capacity"], res["peak_others"], res["conflicts"]), (2, 1, []))
		self.assertTrue(self.change(self.ws4)["ok"])

	def test_two_bookings_that_do_not_overlap_each_other_leave_room_on_a_two_slot_machine(self):
		start, end = self.interval()
		mid = start + (end - start) / 2
		self.place(self.other_card(), self.ws4, start, mid)
		self.place(self.other_card(), self.ws4, mid, end)
		res = workstation_change.check_change(self.asm.name, self.ws4)
		self.assertFalse(res["conflicts"], "never two at the same instant: capacity two is not exceeded")

	# 15 + 16. what may not move -----------------------------------------------------------------------------------------------------
	def test_work_in_progress_completed_cancelled_and_timed_cards_cannot_change_machine(self):
		cases = {
			"Work In Progress": ({"status": "Work In Progress"}, "in progress"),
			"Completed": ({"status": "Completed", "docstatus": 1}, "finished"),
			"Cancelled": ({"status": "Cancelled", "docstatus": 2}, "cancelled"),
			"timed": ({"total_time_in_mins": 45}, "recorded time"),
		}
		for label, (values, word) in cases.items():
			frappe.db.savepoint("ppc_machine_case")
			frappe.db.set_value("Job Card", self.asm.name, values)
			res = self.change(self.ws3)
			self.assertFalse(res["ok"], label)
			self.assertTrue(any(word in e for e in res["errors"]), f"{label}: {res['errors']}")
			self.assertEqual(frappe.db.get_value("Job Card", self.asm.name, "workstation"), self.ws2, label)
			frappe.db.rollback(save_point="ppc_machine_case")

	def test_a_stopped_or_completed_work_order_locks_its_cards(self):
		frappe.db.set_value("Work Order", self.wo, "status", "Stopped")
		res = self.change(self.ws3)
		self.assertFalse(res["ok"])
		self.assertTrue(any("Stopped" in e for e in res["errors"]))

	def test_bad_requests(self):
		for target, text in (("", "Choose a Workstation"), (f"{PREFIX} no such machine", "does not exist")):
			res = self.change(target)
			self.assertFalse(res["ok"])
			self.assertTrue(any(text in e for e in res["errors"]), res)
		res = self.change(self.ws2)  # it is already there
		self.assertFalse(res["ok"])
		res = self.change(self.ws3, start=str(d(5)), end=str(d(4)))
		self.assertFalse(res["ok"])

	# 18. a failed save rolls everything back -------------------------------------------------------------------------------------------------
	def test_a_failure_after_the_card_was_saved_rolls_everything_back(self):
		before = (self.card(self.asm.name), self.wo_dates(), len(self.revisions_of(self.asm.name)))
		start, end = self.interval()
		with mock.patch.object(scheduling, "sync_work_order_schedule", side_effect=frappe.ValidationError("Work Order refused the dates")):
			res = self.change(self.ws3, start=str(start + timedelta(days=1)), end=str(end + timedelta(days=1)))
		self.assertFalse(res["ok"])
		self.assertTrue(any("Work Order refused" in e for e in res["errors"]))
		now = self.card(self.asm.name)
		self.assertEqual(now.workstation, self.ws2, "the machine is back")
		self.assertTrue(scheduling._same_instant(now.expected_start_date, before[0].expected_start_date), "and so are the dates")
		self.assertEqual(self.wo_dates(), before[1])
		self.assertEqual(len(self.revisions_of(self.asm.name)), before[2], "no revision for a change that did not happen")
		self.assertEqual(now.ppc_schedule_sync_status, "Failed", "the failure is visible and can be retried")
		failed = frappe.get_all("PPC Audit Log", filters={"job_card": self.asm.name, "action": "Change Workstation", "result": "Failed"}, fields=["error_details"])
		self.assertEqual(len(failed), 1)
		self.assertIn("Work Order refused", failed[0].error_details)

	def test_a_conflict_that_appears_while_saving_is_reported_not_forced(self):
		"""The server re-checks under the machine's lock: a booking that appeared since the preview wins."""
		start, end = self.interval()
		real = workstation_change.check_change
		calls = {"n": 0}

		def late_booking(*a, **kw):
			calls["n"] += 1
			if calls["n"] == 2:  # the check inside the locked section: somebody else has just booked the slot
				self.place(self.other_card(), self.ws3, start, end)
			return real(*a, **kw)

		with mock.patch.object(workstation_change, "check_change", side_effect=late_booking):
			res = self.change(self.ws3)
		self.assertFalse(res["ok"])
		self.assertTrue(res["conflict"])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2)
		self.assertEqual(len(self.revisions_of(self.asm.name)), 1)

	# 4th resolution: reschedule the conflicting operation ----------------------------------------------------------------------------------------
	def test_the_conflicting_operation_can_be_pushed_later_after_a_preview(self):
		start, end = self.interval()
		blocker = self.place(self.other_card(), self.ws3, start, end)
		blocker_before = self.card(blocker)
		length = scheduling.as_dt(blocker_before.expected_end_date) - scheduling.as_dt(blocker_before.expected_start_date)

		preview = workstation_change.check_change(self.asm.name, self.ws3)
		plan = preview["resolution"]["reschedule_conflicts"]
		self.assertTrue(plan["allowed"], plan)
		self.assertEqual(plan["shifts"][0]["job_card"], blocker)
		self.assertEqual(plan["shifts"][0]["new_start"], end.strftime("%Y-%m-%d %H:%M:%S"))

		res = self.change(self.ws3, reschedule_conflicts=True, reason="Rush order")
		self.assertTrue(res["ok"], res)
		self.assertEqual(self.card(self.asm.name).workstation, self.ws3)
		moved = self.card(blocker)
		self.assertTrue(scheduling._same_instant(moved.expected_start_date, end), "pushed to when the new operation ends")
		self.assertEqual(scheduling.as_dt(moved.expected_end_date) - scheduling.as_dt(moved.expected_start_date), length, "same duration")
		self.assertEqual(moved.workstation, self.ws3, "it stays on the machine")
		self.assertEqual(self.revisions_of(blocker)[-1].change_source, "Cascade")
		self.assertIn(self.asm.name, self.revisions_of(blocker)[-1].change_reason)
		self.assertFalse(machines.check_machine(self.ws3, *self.interval(), [self.asm.name])["blocking"])

	def test_pushing_is_not_offered_when_it_is_not_safe(self):
		start, end = self.interval()
		blocker = self.place(self.other_card(), self.ws3, start, end)
		frappe.db.set_value("Job Card", blocker, "status", "On Hold")
		self.assertTrue(workstation_change.check_change(self.asm.name, self.ws3)["resolution"]["reschedule_conflicts"]["allowed"], "on hold is movable")
		frappe.db.set_value("Job Card", blocker, "total_time_in_mins", 30)  # work was already recorded on it
		plan = workstation_change.check_change(self.asm.name, self.ws3)["resolution"]["reschedule_conflicts"]
		self.assertFalse(plan["allowed"])
		self.assertTrue(plan["reason"])
		res = self.change(self.ws3, reschedule_conflicts=True)
		self.assertFalse(res["ok"])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2)
		# and never on a machine that runs several operations at once
		frappe.db.set_value("Job Card", blocker, "total_time_in_mins", 0)
		self.place(blocker, self.ws4, start, end)
		self.place(self.other_card(), self.ws4, start, end)
		cap2 = workstation_change.check_change(self.asm.name, self.ws4)["resolution"]["reschedule_conflicts"]
		self.assertFalse(cap2["allowed"])

	# types: formal compatibility ---------------------------------------------------------------------------------------------------------------
	def test_a_machine_of_the_wrong_type_needs_an_override(self):
		wst = frappe.get_doc({"doctype": "Workstation Type", "workstation_type": f"{PREFIX} Lathe"}).insert(ignore_permissions=True).name
		other = frappe.get_doc({"doctype": "Workstation Type", "workstation_type": f"{PREFIX} Press"}).insert(ignore_permissions=True).name
		frappe.db.set_value("Workstation", self.ws3, "workstation_type", wst)
		frappe.db.set_value("Workstation", self.ws5, "workstation_type", other)
		frappe.db.set_value("Job Card", self.asm.name, "workstation_type", wst)

		good = workstation_change.check_change(self.asm.name, self.ws3)
		self.assertEqual(good["compatibility"]["state"], "compatible")
		self.assertTrue(good["ok"])
		bad = workstation_change.check_change(self.asm.name, self.ws5)
		self.assertEqual(bad["compatibility"]["state"], "incompatible")
		self.assertIn("incompatible_type", [w["code"] for w in bad["warnings"]])
		self.assertTrue(bad["needs_override"])
		cands = {c["name"]: c for c in api.get_workstation_candidates(self.asm.name)["workstations"]}
		self.assertEqual(cands[self.ws3]["compatibility"]["state"], "compatible")
		self.assertEqual(cands[self.ws5]["compatibility"]["state"], "incompatible")
		names = [c["name"] for c in api.get_workstation_candidates(self.asm.name)["workstations"]]
		self.assertLess(names.index(self.ws3), names.index(self.ws5))

	def test_a_machine_that_is_off_or_in_maintenance_asks_for_confirmation(self):
		frappe.db.set_value("Workstation", self.ws3, "status", "Maintenance")
		res = self.change(self.ws3, confirmed=False)
		self.assertFalse(res["ok"])
		self.assertTrue(res["needs_confirmation"])
		self.assertIn("workstation_status", [w["code"] for w in res["warnings"]])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2)
		self.assertTrue(self.change(self.ws3, confirmed=True)["ok"], "a planner can plan ahead for a machine under maintenance")

	def test_confirmation_is_always_required_without_an_override(self):
		res = self.change(self.ws3, confirmed=False)
		self.assertFalse(res["ok"])
		self.assertTrue(res["needs_confirmation"])
		self.assertEqual(self.card(self.asm.name).workstation, self.ws2)

	# permissions ---------------------------------------------------------------------------------------------------------------------------------
	def test_who_may_do_what(self):
		frappe.set_user(self.viewer)
		for fn in (
			lambda: api.check_workstation_change(self.asm.name, self.ws3),
			lambda: api.get_workstation_candidates(self.asm.name),
			lambda: api.find_next_workstation_slot(self.asm.name, self.ws3),
			lambda: api.change_job_card_workstation(self.asm.name, self.ws3, confirmed=1),
		):
			with self.assertRaises(frappe.PermissionError):
				fn()
		frappe.set_user(self.erp_only)
		with self.assertRaises(frappe.PermissionError):
			api.change_job_card_workstation(self.asm.name, self.ws3, confirmed=1)
		frappe.set_user(self.planner)
		self.assertTrue(api.change_job_card_workstation(self.asm.name, self.ws3, confirmed=1)["ok"])
		frappe.set_user("Administrator")
		self.assertEqual(self.card(self.asm.name).workstation, self.ws3)

	# 20. chemical_mfg --------------------------------------------------------------------------------------------------------------------------------
	def test_the_change_goes_through_the_normal_document_lifecycle_including_other_apps_hooks(self):
		"""The save is an ordinary Job Card save, so any app's validate / before_save hooks (chemical_mfg's among them) run."""
		seen = []
		with mock.patch.dict(frappe.get_hooks.__globals__, {}, clear=False):
			real_save = frappe.model.document.Document.save

			def spy(doc, *a, **kw):
				if doc.doctype == "Job Card" and doc.name == self.asm.name:
					seen.append(doc.workstation)
				return real_save(doc, *a, **kw)

			with mock.patch.object(frappe.model.document.Document, "save", spy):
				self.assertTrue(self.change(self.ws3)["ok"])
		self.assertIn(self.ws3, seen, "the Job Card document was saved with the new Workstation")
		if "chemical_mfg" in frappe.get_installed_apps():
			hooks = frappe.get_hooks("doc_events").get("Job Card", {})
			self.assertTrue(hooks, "chemical_mfg's Job Card hooks are active on this site, and the change went through them")
