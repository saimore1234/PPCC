"""Phase 5: rescheduling Job Cards, dependency / capacity rules and Work Order synchronisation."""

from datetime import datetime, timedelta

import frappe
from frappe.utils import add_days, add_to_date, flt, getdate, now_datetime

from ppc_production_control import api
from ppc_production_control.services import scheduling
from ppc_production_control.services import job_cards as jc_service
from ppc_production_control.tests.utils import PREFIX, PPCTestCase


def _dt(value):
	return scheduling.as_dt(value)


class TestReschedule(PPCTestCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.job_cards(self.wo)

	def card(self, name):
		return frappe.db.get_value(
			"Job Card", name, ["expected_start_date", "expected_end_date", "status", "docstatus", "modified"], as_dict=True
		)

	def wo_dates(self):
		return frappe.db.get_value("Work Order", self.wo, ["planned_start_date", "planned_end_date"], as_dict=True)

	# 19/20. move + Work Order sync ---------------------------------------------------------------
	def test_move_shifts_rows_and_syncs_work_order(self):
		old = self.card(self.asm.name)
		new_start, new_end = old.expected_start_date + timedelta(days=1), old.expected_end_date + timedelta(days=1)

		res = scheduling.reschedule_job_card(self.asm.name, new_start, new_end, reason="Machine busy", confirmed=True)
		self.assertTrue(res["ok"], res)

		doc = frappe.get_doc("Job Card", self.asm.name)
		self.assertEqual(_dt(doc.expected_start_date), new_start)
		self.assertEqual(_dt(doc.expected_end_date), new_end)
		self.assertEqual(len(doc.scheduled_time_logs), 1)
		self.assertEqual(_dt(doc.scheduled_time_logs[0].from_time), new_start)
		self.assertEqual(flt(doc.scheduled_time_logs[0].time_in_mins), flt(old.expected_end_date and 600))

		# Work Order: start = earliest remaining start (Cut, unchanged), end = latest end (Asm, moved)
		wo = self.wo_dates()
		self.assertEqual(_dt(wo.planned_start_date), _dt(self.card(self.cut.name).expected_start_date))
		self.assertEqual(_dt(wo.planned_end_date), new_end)
		self.assertEqual(res["work_orders"][0]["work_order"], self.wo)

		# the first schedule is kept as "original"; the card itself now shows the revised one
		jc = frappe.db.get_value("Job Card", self.asm.name, ["ppc_original_start", "ppc_original_end"], as_dict=True)
		self.assertEqual(_dt(jc.ppc_original_start), old.expected_start_date)
		self.assertEqual(_dt(jc.ppc_original_end), old.expected_end_date)
		row = next(r for r in api.get_job_cards({"plan": self.plan.name})["rows"] if r["name"] == self.asm.name)
		self.assertEqual(row["sync_status"], "Rescheduled")
		self.assertEqual(row["original_start"], old.expected_start_date.strftime("%Y-%m-%d %H:%M:%S"))
		self.assertEqual(row["revised_start"], new_start.strftime("%Y-%m-%d %H:%M:%S"))

	def test_audit_trail_records_old_new_user_and_reason(self):
		old = self.card(self.asm.name)
		new_start = old.expected_start_date + timedelta(hours=5)
		new_end = old.expected_end_date + timedelta(hours=5)
		scheduling.reschedule_job_card(self.asm.name, new_start, new_end, reason="Operator absent", confirmed=True)

		log = frappe.get_all(
			"PPC Audit Log",
			filters={"job_card": self.asm.name, "action": "Reschedule Job Card"},
			fields=["old_start_date", "old_end_date", "new_start_date", "new_end_date", "user", "timestamp", "result", "reason", "ppc_plan", "work_order"],
		)
		self.assertEqual(len(log), 1)
		row = log[0]
		# the audit log keeps whole seconds (ERPNext stamps plan dates with microseconds)
		self.assertEqual(_dt(row.old_start_date), old.expected_start_date.replace(microsecond=0))
		self.assertEqual(_dt(row.new_end_date), new_end.replace(microsecond=0))
		self.assertEqual((row.user, row.result, row.reason), ("Administrator", "Success", "Operator absent"))
		self.assertEqual((row.ppc_plan, row.work_order), (self.plan.name, self.wo))
		self.assertIsNotNone(row.timestamp)
		self.assertTrue(frappe.get_all("PPC Audit Log", filters={"work_order": self.wo, "action": "Sync Work Order Schedule"}))

	def test_resize_collapses_to_one_row_and_extends_the_end(self):
		old = self.card(self.asm.name)
		new_end = old.expected_end_date + timedelta(hours=2)
		res = scheduling.reschedule_job_card(self.asm.name, old.expected_start_date, new_end, mode="resize", confirmed=True)
		self.assertTrue(res["ok"], res)
		doc = frappe.get_doc("Job Card", self.asm.name)
		self.assertEqual(_dt(doc.expected_end_date), new_end)
		self.assertEqual(len(doc.scheduled_time_logs), 1)
		self.assertEqual(flt(doc.scheduled_time_logs[0].time_in_mins), 720)  # 600 + 120
		self.assertEqual(_dt(self.wo_dates().planned_end_date), new_end)

	def test_actual_dates_are_never_touched(self):
		before = frappe.db.get_value("Work Order", self.wo, ["actual_start_date", "actual_end_date"], as_dict=True)
		old = self.card(self.asm.name)
		scheduling.reschedule_job_card(
			self.asm.name, old.expected_start_date + timedelta(days=1), old.expected_end_date + timedelta(days=1), confirmed=True
		)
		after = frappe.db.get_value("Work Order", self.wo, ["actual_start_date", "actual_end_date"], as_dict=True)
		self.assertEqual(before, after)
		self.assertFalse(frappe.db.get_value("Job Card", self.asm.name, "actual_start_date"))

	# 21. invalid dates -------------------------------------------------------------------------------
	def test_invalid_dates_are_rejected_and_nothing_changes(self):
		old = self.card(self.asm.name)
		start = old.expected_start_date
		cases = [
			(start, start - timedelta(hours=1)),  # end before start
			(start, start),  # zero length
			(None, start),
			(start, None),
			("garbage", "also garbage"),
			("", ""),
		]
		for new_start, new_end in cases:
			res = scheduling.reschedule_job_card(self.asm.name, new_start, new_end, confirmed=True)
			self.assertFalse(res["ok"], (new_start, new_end))
			self.assertTrue(res["errors"])
		now = self.card(self.asm.name)
		self.assertEqual((now.expected_start_date, now.expected_end_date), (old.expected_start_date, old.expected_end_date))
		self.assertEqual(frappe.db.count("PPC Audit Log", {"job_card": self.asm.name, "action": "Reschedule Job Card"}), 0)

	# 22. dependencies --------------------------------------------------------------------------------
	def test_predecessor_violation_needs_override(self):
		cut = self.card(self.cut.name)
		asm = self.card(self.asm.name)
		early_start = cut.expected_end_date - timedelta(hours=1)  # starts before 'Cut' ends
		early_end = early_start + (asm.expected_end_date - asm.expected_start_date)

		res = scheduling.reschedule_job_card(self.asm.name, early_start, early_end, confirmed=True)
		self.assertFalse(res["ok"])
		self.assertTrue(res["needs_override"])
		self.assertEqual(res["warnings"][0]["code"], "predecessor")
		self.assertEqual(_dt(self.card(self.asm.name).expected_start_date), asm.expected_start_date, "nothing is saved silently")

		res = scheduling.reschedule_job_card(self.asm.name, early_start, early_end, override_conflicts=True, reason="Overlap approved")
		self.assertTrue(res["ok"], res)
		self.assertEqual(_dt(self.card(self.asm.name).expected_start_date), early_start)
		audit = frappe.get_all("PPC Audit Log", filters={"job_card": self.asm.name, "result": "Success"}, fields=["details", "reason"])
		self.assertIn("preceding operation", audit[0].details, "the overridden warning is kept in the audit trail")
		self.assertIn('"override": true', audit[0].details)
		self.assertEqual(audit[0].reason, "Overlap approved")

	def test_successor_violation_needs_shift_or_override(self):
		cut = self.card(self.cut.name)
		asm = self.card(self.asm.name)
		day = timedelta(days=1)

		# moving 'Cut' later than the start of 'Assemble' violates the sequence
		res = scheduling.reschedule_job_card(self.cut.name, cut.expected_start_date + day, cut.expected_end_date + day, confirmed=True)
		self.assertFalse(res["ok"])
		self.assertEqual(res["warnings"][0]["code"], "successor")

		# explicit cascade: 'Assemble' follows, keeping duration and the 10 minute gap
		res = scheduling.reschedule_job_card(
			self.cut.name, cut.expected_start_date + day, cut.expected_end_date + day, shift_dependents=True, confirmed=True
		)
		self.assertTrue(res["ok"], res)
		self.assertEqual({c["job_card"] for c in res["changed"]}, {self.cut.name, self.asm.name})
		new_asm = self.card(self.asm.name)
		self.assertEqual(new_asm.expected_start_date, asm.expected_start_date + day)
		self.assertEqual(new_asm.expected_end_date, asm.expected_end_date + day)
		wo = self.wo_dates()
		self.assertEqual(_dt(wo.planned_start_date), cut.expected_start_date + day)
		self.assertEqual(_dt(wo.planned_end_date), asm.expected_end_date + day)
		self.assertEqual(frappe.db.count("PPC Audit Log", {"work_order": self.wo, "action": "Reschedule Job Card", "result": "Success"}), 2)

	def test_cascade_is_blocked_by_a_finished_successor(self):
		self.complete_job_card(self.cut.name)
		# make the *second* operation finished instead: cannot be shifted
		frappe.db.set_value("Job Card", self.asm.name, "status", "Work In Progress")
		cut = self.card(self.cut.name)
		res = scheduling.reschedule_job_card(
			self.cut.name, cut.expected_start_date + timedelta(days=2), cut.expected_end_date + timedelta(days=2), shift_dependents=True, confirmed=True
		)
		self.assertFalse(res["ok"])

	# 23. workstation conflicts --------------------------------------------------------------------
	def other_plan_card_on_ws1(self):
		_plan2, wo2 = self.ops_plan(4)
		return self.job_cards(wo2)[0]

	def test_workstation_overlap_is_a_conflict(self):
		cut = self.card(self.cut.name)
		other = self.other_plan_card_on_ws1()
		self.assertEqual(other.workstation, self.ws1)
		# put the other card exactly onto this one
		res = scheduling.reschedule_job_card(
			other.name, cut.expected_start_date, cut.expected_start_date + timedelta(hours=2), confirmed=True
		)
		self.assertFalse(res["ok"])
		self.assertEqual({w["code"] for w in res["warnings"]} & {"workstation"}, {"workstation"})
		self.assertIn(self.cut.name, [c.name for c in res["conflicts"]])

		res = scheduling.reschedule_job_card(
			other.name, cut.expected_start_date, cut.expected_start_date + timedelta(hours=2), override_conflicts=True
		)
		self.assertTrue(res["ok"], res)

	def test_back_to_back_on_a_workstation_is_not_a_conflict(self):
		cut = self.card(self.cut.name)
		other = self.other_plan_card_on_ws1()
		start = cut.expected_end_date
		res = scheduling.validate_schedule(other.name, start, start + timedelta(hours=1))
		self.assertEqual([w for w in res["warnings"] if w["code"] == "workstation"], [])

	def test_workstation_capacity_allows_parallel_work(self):
		cut = self.card(self.cut.name)
		other = self.other_plan_card_on_ws1()
		args = (other.name, cut.expected_start_date, cut.expected_start_date + timedelta(hours=2))
		self.assertTrue(any(w["code"] == "workstation" for w in scheduling.validate_schedule(*args)["warnings"]))
		frappe.db.set_value("Workstation", self.ws1, "production_capacity", 2)
		self.assertFalse(any(w["code"] == "workstation" for w in scheduling.validate_schedule(*args)["warnings"]))

	# 24. protection ----------------------------------------------------------------------------------
	def test_started_completed_and_cancelled_cards_cannot_be_moved(self):
		cut = self.card(self.cut.name)
		start, end = cut.expected_start_date + timedelta(days=3), cut.expected_end_date + timedelta(days=3)

		# work in progress (time logged, still a draft)
		doc = self.complete_job_card(self.cut.name)
		doc.reload()
		res = scheduling.reschedule_job_card(self.cut.name, start, end, confirmed=True)
		self.assertFalse(res["ok"])
		self.assertIn("Work In Progress", res["errors"][0])

		# submitted / completed
		jc_service.submit_selected_job_cards(self.plan.name, [self.cut.name])
		res = scheduling.reschedule_job_card(self.cut.name, start, end, confirmed=True)
		self.assertFalse(res["ok"])
		self.assertIn("submitted", res["errors"][0])

		# cancelled
		frappe.get_doc("Job Card", self.cut.name).cancel()
		res = scheduling.reschedule_job_card(self.cut.name, start, end, confirmed=True)
		self.assertFalse(res["ok"])
		self.assertIn("cancelled", res["errors"][0])

	def test_dates_of_finished_cards_do_not_pull_the_work_order_back(self):
		self.complete_job_card(self.cut.name)
		jc_service.submit_selected_job_cards(self.plan.name, [self.cut.name])
		asm = self.card(self.asm.name)
		new_start, new_end = asm.expected_start_date + timedelta(days=1), asm.expected_end_date + timedelta(days=1)
		res = scheduling.reschedule_job_card(self.asm.name, new_start, new_end, confirmed=True)
		self.assertTrue(res["ok"], res)
		wo = self.wo_dates()
		# only the remaining operation counts for the Work Order's planned window
		self.assertEqual(_dt(wo.planned_start_date), new_start)
		self.assertEqual(_dt(wo.planned_end_date), new_end)

	def test_locked_work_order_blocks_rescheduling(self):
		asm = self.card(self.asm.name)
		frappe.db.set_value("Work Order", self.wo, "status", "Stopped")
		res = scheduling.reschedule_job_card(
			self.asm.name, asm.expected_start_date + timedelta(hours=1), asm.expected_end_date + timedelta(hours=1), confirmed=True
		)
		self.assertFalse(res["ok"])
		self.assertIn("Stopped", res["errors"][0])

	def test_stale_screen_is_rejected(self):
		asm = self.card(self.asm.name)
		res = scheduling.reschedule_job_card(
			self.asm.name, asm.expected_start_date + timedelta(hours=1), asm.expected_end_date + timedelta(hours=1),
			confirmed=True, expected_modified="2000-01-01 00:00:00.000000",
		)
		self.assertFalse(res["ok"])
		self.assertIn("changed by someone else", res["errors"][0])

	# informational warnings need confirmation but not an override ----------------------------------------
	def test_warnings_require_confirmation(self):
		asm = self.card(self.asm.name)
		past_start = now_datetime() - timedelta(days=2)
		past_end = past_start + (asm.expected_end_date - asm.expected_start_date)
		# (start also precedes 'Cut', so ask for the Cut card instead to keep it a pure 'past' warning)
		cut = self.card(self.cut.name)
		span = cut.expected_end_date - cut.expected_start_date
		res = scheduling.reschedule_job_card(self.cut.name, past_start, past_start + span)
		self.assertFalse(res["ok"])
		self.assertTrue(res["needs_confirmation"])
		self.assertFalse(res["needs_override"])
		self.assertIn("past", {w["code"] for w in res["warnings"]})

	def test_delivery_date_warning(self):
		frappe.db.set_value("Work Order", self.wo, "expected_delivery_date", add_days(getdate(), 1))
		asm = self.card(self.asm.name)
		res = scheduling.validate_schedule(
			self.asm.name, asm.expected_start_date + timedelta(days=5), asm.expected_end_date + timedelta(days=5)
		)
		self.assertIn("delivery", {w["code"] for w in res["warnings"]})

	def test_holiday_warning(self):
		asm = self.card(self.asm.name)
		target = getdate(asm.expected_start_date + timedelta(days=9))
		hl = frappe.get_doc({
			"doctype": "Holiday List", "holiday_list_name": f"{PREFIX} HL {target}",
			"from_date": add_days(target, -3), "to_date": add_days(target, 3),
			"holidays": [{"holiday_date": target, "description": "Plant shutdown"}],
		}).insert()
		frappe.db.set_value("Workstation", self.ws2, "holiday_list", hl.name)
		res = scheduling.validate_schedule(
			self.asm.name, datetime.combine(target, datetime.min.time().replace(hour=9)),
			datetime.combine(target, datetime.min.time().replace(hour=19)),
		)
		self.assertIn("holiday", {w["code"] for w in res["warnings"]})

	# 28. time zone / date boundaries --------------------------------------------------------------------
	def test_midnight_month_end_and_leap_day_are_exact(self):
		cases = [
			(datetime(2028, 2, 29, 23, 0, 0), datetime(2028, 3, 1, 1, 0, 0)),   # leap day -> March
			(datetime(2027, 12, 31, 22, 30, 0), datetime(2028, 1, 1, 0, 30, 0)),  # year boundary
			(datetime(2027, 3, 28, 1, 30, 0), datetime(2027, 3, 28, 3, 30, 0)),  # EU DST gap hour (naive: unchanged)
		]
		for start, end in cases:
			res = scheduling.reschedule_job_card(
				self.asm.name, start, end, override_conflicts=True, confirmed=True, mode="resize"
			)
			self.assertTrue(res["ok"], (start, res))
			stored = self.card(self.asm.name)
			self.assertEqual((stored.expected_start_date, stored.expected_end_date), (start, end))
			row = frappe.get_doc("Job Card", self.asm.name).scheduled_time_logs[0]
			self.assertEqual(flt(row.time_in_mins), 120.0)

	def test_whole_day_shift_keeps_time_of_day(self):
		old = self.card(self.asm.name)
		for days in (1, 7, 31):
			current = self.card(self.asm.name)
			res = scheduling.reschedule_job_card(
				self.asm.name, current.expected_start_date + timedelta(days=days),
				current.expected_end_date + timedelta(days=days), confirmed=True, override_conflicts=True,
			)
			self.assertTrue(res["ok"], res)
		final = self.card(self.asm.name)
		self.assertEqual(final.expected_start_date.time(), old.expected_start_date.time())
		self.assertEqual(final.expected_start_date - old.expected_start_date, timedelta(days=39))

	def test_string_input_from_the_browser_is_parsed_without_time_zone_shift(self):
		res = scheduling.reschedule_job_card(
			self.asm.name, "2030-06-30 23:15:00", "2030-07-01 04:45:00", confirmed=True, override_conflicts=True, mode="resize"
		)
		self.assertTrue(res["ok"], res)
		stored = self.card(self.asm.name)
		self.assertEqual(stored.expected_start_date, datetime(2030, 6, 30, 23, 15))
		self.assertEqual(stored.expected_end_date, datetime(2030, 7, 1, 4, 45))

	# auto scheduling -----------------------------------------------------------------------------------
	def test_auto_schedule_fills_only_missing_dates(self):
		# simulate capacity planning being off: cards have no schedule
		for c in (self.cut, self.asm):
			frappe.db.delete("Job Card Scheduled Time", {"parent": c.name})
			frappe.db.set_value("Job Card", c.name, {"expected_start_date": None, "expected_end_date": None})
		anchor = _dt(frappe.db.get_value("Work Order", self.wo, "planned_start_date"))

		made = scheduling.auto_schedule_work_order(self.wo)
		self.assertEqual(len(made), 2)
		cut, asm = self.card(self.cut.name), self.card(self.asm.name)
		self.assertEqual(cut.expected_start_date, anchor)
		self.assertEqual((cut.expected_end_date - cut.expected_start_date), timedelta(minutes=300))
		self.assertEqual(asm.expected_start_date, cut.expected_end_date)  # sequential
		self.assertEqual((asm.expected_end_date - asm.expected_start_date), timedelta(minutes=600))
		self.assertEqual(_dt(self.wo_dates().planned_end_date), asm.expected_end_date)

		self.assertEqual(scheduling.auto_schedule_work_order(self.wo), [], "second run changes nothing")
		self.assertEqual(self.card(self.cut.name).expected_start_date, anchor)
