"""Baseline, revision history and the atomic Job Card -> Work Order synchronisation."""

from datetime import datetime, timedelta
from unittest import mock

import frappe

from ppc_production_control import api
from ppc_production_control.services import job_cards as jc_service
from ppc_production_control.services import reconcile, revisions, scheduling
from ppc_production_control.tests.utils import PPCTestCase

REV = "PPC Schedule Revision"


def _dt(v):
	return scheduling.as_dt(v)


class TestBaselineAndRevisions(PPCTestCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.job_cards(self.wo)

	# helpers ----------------------------------------------------------------------------------
	def card(self, name):
		return frappe.db.get_value(
			"Job Card", name,
			["expected_start_date", "expected_end_date", "actual_start_date", "actual_end_date", "ppc_original_start", "ppc_original_end",
				"ppc_revised_start", "ppc_revised_end", "ppc_schedule_revision", "ppc_schedule_sync_status", "ppc_baseline_source",
				"ppc_last_rescheduled_by", "ppc_last_rescheduled_at", "modified", "status", "docstatus"],
			as_dict=True,
		)

	def wo_dates(self):
		return frappe.db.get_value("Work Order", self.wo, ["planned_start_date", "planned_end_date"], as_dict=True)

	def revs(self, name):
		return frappe.get_all(REV, filters={"job_card": name}, fields=["*"], order_by="revision_no asc")

	def move(self, name, start, end, **kw):
		kw.setdefault("confirmed", True)
		return scheduling.reschedule_job_card(name, start, end, **kw)

	def shift(self, name, **delta):
		c = self.card(name)
		d = timedelta(**delta)
		return self.move(name, c.expected_start_date + d, c.expected_end_date + d, reason=delta.get("reason"))

	# --------------------------------------------------------------- baseline
	def test_baseline_is_captured_when_the_card_first_gets_a_schedule(self):
		for c in (self.cut, self.asm):
			jc = self.card(c.name)
			self.assertEqual(jc.ppc_original_start, jc.expected_start_date)
			self.assertEqual(jc.ppc_original_end, jc.expected_end_date)
			self.assertEqual((jc.ppc_baseline_source, jc.ppc_schedule_revision, jc.ppc_schedule_sync_status), ("Created", 0, "Synced"))
			self.assertEqual(jc.ppc_revised_start, jc.expected_start_date)
			rows = self.revs(c.name)
			self.assertEqual(len(rows), 1)
			self.assertEqual((rows[0].revision_no, rows[0].revision_type, rows[0].change_source), (0, "Baseline", "Baseline"))
			self.assertEqual(rows[0].baseline_note, "Initial schedule")
			self.assertEqual(rows[0].accepted_start, jc.expected_start_date)
			self.assertEqual(rows[0].work_order, self.wo)
			self.assertEqual(rows[0].ppc_plan, self.plan.name)
			self.assertEqual(rows[0].changed_by, "Administrator")

	def test_baseline_can_never_be_overwritten(self):
		before = self.card(self.asm.name)
		doc = frappe.get_doc("Job Card", self.asm.name)
		doc.ppc_original_start = doc.ppc_original_start + timedelta(days=3)
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(self.card(self.asm.name).ppc_original_start, before.ppc_original_start)
		# capturing again is a no-op
		self.assertFalse(revisions.capture_baseline(self.asm.name, revisions.BASELINE_CREATED))
		self.assertEqual(len(self.revs(self.asm.name)), 1)
		# ...and later reschedules leave it alone
		self.shift(self.asm.name, days=1)
		self.shift(self.asm.name, days=1)
		after = self.card(self.asm.name)
		self.assertEqual((after.ppc_original_start, after.ppc_original_end), (before.ppc_original_start, before.ppc_original_end))

	def test_baseline_of_an_existing_card_is_labelled_not_claimed_as_original(self):
		# a card that predates PPC: it has a schedule but no recorded baseline
		frappe.db.delete(REV, {"job_card": self.asm.name})
		frappe.db.set_value("Job Card", self.asm.name, {k: None for k in (
			"ppc_original_start", "ppc_original_end", "ppc_baseline_source", "ppc_revised_start", "ppc_revised_end")}
			| {"ppc_schedule_revision": 0})
		pre = self.card(self.asm.name)

		res = self.shift(self.asm.name, days=1)
		self.assertTrue(res["ok"], res)
		post = self.card(self.asm.name)
		self.assertEqual(post.ppc_baseline_source, "Initialized from existing schedule")
		self.assertEqual(post.ppc_original_start, pre.expected_start_date, "the schedule as found, before PPC touched it")
		hist = revisions.get_history(self.asm.name)
		self.assertEqual(hist["baseline"]["label"], "Baseline initialized from existing schedule")
		self.assertEqual(hist["revisions"][0]["baseline_note"], "Baseline initialized from existing schedule")
		self.assertEqual([r["revision_no"] for r in hist["revisions"]], [0, 1])

	# --------------------------------------------------------------- revisions
	def test_first_and_second_reschedule_append_revisions_and_never_edit_old_ones(self):
		base = self.card(self.asm.name)
		r1 = self.shift(self.asm.name, days=1, reason="Machine unavailable") if False else self.move(
			self.asm.name, base.expected_start_date + timedelta(days=1), base.expected_end_date + timedelta(days=1), reason="Machine unavailable")
		self.assertTrue(r1["ok"], r1)
		rows = self.revs(self.asm.name)
		self.assertEqual([r.revision_no for r in rows], [0, 1])
		rev1 = rows[1]
		self.assertEqual((rev1.revision_type, rev1.change_source, rev1.sync_status), ("Revision", "Gantt Drag", "Synced"))
		self.assertEqual(rev1.previous_start, base.expected_start_date)
		self.assertEqual(rev1.previous_end, base.expected_end_date)
		self.assertEqual(rev1.accepted_start, base.expected_start_date + timedelta(days=1))
		self.assertEqual(rev1.change_reason, "Machine unavailable")
		self.assertEqual(rev1.changed_by, "Administrator")
		self.assertIsNotNone(rev1.changed_at)
		self.assertTrue(rev1.correlation_id)
		snapshot = dict(rev1)

		c1 = self.card(self.asm.name)
		second_start = c1.expected_start_date + timedelta(hours=5)
		r2 = self.move(self.asm.name, second_start, c1.expected_end_date + timedelta(hours=5), reason="Priority changed")
		self.assertTrue(r2["ok"], r2)
		rows = self.revs(self.asm.name)
		self.assertEqual([r.revision_no for r in rows], [0, 1, 2])
		self.assertEqual(rows[1], snapshot, "revision 1 is untouched by revision 2")
		self.assertEqual(rows[2].previous_start, c1.expected_start_date, "revision 2 starts from revision 1's accepted schedule")
		self.assertEqual(rows[2].accepted_start, second_start)

		final = self.card(self.asm.name)
		self.assertEqual(final.ppc_schedule_revision, 2)
		self.assertEqual((final.ppc_revised_start, final.ppc_revised_end), (final.expected_start_date, final.expected_end_date))
		self.assertEqual(final.ppc_original_start, base.ppc_original_start)
		self.assertEqual(final.ppc_last_rescheduled_by, "Administrator")
		# the Work Order carries the latest aggregate and the revision says what it did to it
		self.assertEqual(_dt(self.wo_dates().planned_end_date), final.expected_end_date)
		self.assertEqual(_dt(rows[2].work_order_new_end), final.expected_end_date.replace(microsecond=0))

	def test_earlier_and_later_dates(self):
		base = self.card(self.asm.name)
		self.assertTrue(self.shift(self.asm.name, days=2)["ok"])
		later = self.card(self.asm.name)
		self.assertEqual(later.expected_start_date, base.expected_start_date + timedelta(days=2))
		self.assertTrue(self.shift(self.asm.name, hours=-30)["ok"])  # earlier again, still after 'Cut' ends
		earlier = self.card(self.asm.name)
		self.assertEqual(earlier.expected_start_date, later.expected_start_date - timedelta(hours=30))
		self.assertEqual(earlier.ppc_schedule_revision, 2)
		self.assertEqual(_dt(self.wo_dates().planned_end_date), earlier.expected_end_date)

	def test_changing_only_the_start_or_only_the_end(self):
		base = self.card(self.asm.name)
		new_start = base.expected_start_date + timedelta(hours=2)
		self.assertTrue(self.move(self.asm.name, new_start, base.expected_end_date, mode="resize")["ok"])
		c = self.card(self.asm.name)
		self.assertEqual((c.expected_start_date, c.expected_end_date), (new_start, base.expected_end_date))
		new_end = base.expected_end_date + timedelta(hours=3)
		self.assertTrue(self.move(self.asm.name, new_start, new_end, mode="resize")["ok"])
		c = self.card(self.asm.name)
		self.assertEqual((c.expected_start_date, c.expected_end_date), (new_start, new_end))
		rows = self.revs(self.asm.name)
		self.assertEqual([r.change_source for r in rows], ["Baseline", "Gantt Resize", "Gantt Resize"])
		self.assertEqual(_dt(self.wo_dates().planned_end_date), new_end)
		jc = frappe.get_doc("Job Card", self.asm.name)
		self.assertEqual(len(jc.scheduled_time_logs), 1)

	def test_multiple_job_cards_aggregate_into_the_work_order(self):
		cut, asm = self.card(self.cut.name), self.card(self.asm.name)
		wo = self.wo_dates()
		self.assertEqual((_dt(wo.planned_start_date), _dt(wo.planned_end_date)), (cut.expected_start_date, asm.expected_end_date))
		# moving only the LAST operation later extends the Work Order end but never its start
		self.assertTrue(self.shift(self.asm.name, days=1)["ok"])
		wo = self.wo_dates()
		self.assertEqual(_dt(wo.planned_start_date), cut.expected_start_date)
		self.assertEqual(_dt(wo.planned_end_date), asm.expected_end_date + timedelta(days=1))
		# the Work Order is never simply set to one card's dates
		self.assertNotEqual((_dt(wo.planned_start_date), _dt(wo.planned_end_date)),
			(self.card(self.asm.name).expected_start_date, self.card(self.asm.name).expected_end_date))

	def test_upstream_move_shifts_downstream_and_records_a_revision_for_each(self):
		cut, asm = self.card(self.cut.name), self.card(self.asm.name)
		res = self.move(self.cut.name, cut.expected_start_date + timedelta(days=1), cut.expected_end_date + timedelta(days=1),
			shift_dependents=True, reason="Material late")
		self.assertTrue(res["ok"], res)
		self.assertEqual({c["job_card"] for c in res["changed"]}, {self.cut.name, self.asm.name})
		rc, ra = self.revs(self.cut.name)[-1], self.revs(self.asm.name)[-1]
		self.assertEqual((rc.revision_no, ra.revision_no), (1, 1))
		self.assertEqual((rc.change_source, ra.change_source), ("Gantt Drag", "Cascade"))
		self.assertEqual(rc.correlation_id, ra.correlation_id, "one action, one correlation id")
		self.assertEqual(self.card(self.asm.name).expected_start_date, asm.expected_start_date + timedelta(days=1))
		wo = self.wo_dates()
		self.assertEqual(_dt(wo.planned_start_date), cut.expected_start_date + timedelta(days=1))
		self.assertEqual(_dt(wo.planned_end_date), asm.expected_end_date + timedelta(days=1))

	def test_a_move_that_violates_the_sequence_is_not_accepted(self):
		cut, asm = self.card(self.cut.name), self.card(self.asm.name)
		res = self.move(self.cut.name, cut.expected_start_date + timedelta(days=1), cut.expected_end_date + timedelta(days=1))
		self.assertFalse(res["ok"])
		self.assertEqual(len(self.revs(self.cut.name)), 1, "rejected changes leave no revision")
		self.assertEqual(self.card(self.cut.name).expected_start_date, cut.expected_start_date)

	def test_restricted_cards_get_no_revision(self):
		self.complete_job_card(self.cut.name)  # started
		c = self.card(self.cut.name)
		res = self.move(self.cut.name, c.expected_start_date + timedelta(days=2), c.expected_end_date + timedelta(days=2))
		self.assertFalse(res["ok"])
		jc_service.submit_selected_job_cards(self.plan.name, [self.cut.name])  # completed + submitted
		res = self.move(self.cut.name, c.expected_start_date + timedelta(days=2), c.expected_end_date + timedelta(days=2))
		self.assertFalse(res["ok"])
		frappe.get_doc("Job Card", self.cut.name).cancel()  # cancelled
		res = self.move(self.cut.name, c.expected_start_date + timedelta(days=2), c.expected_end_date + timedelta(days=2))
		self.assertFalse(res["ok"])
		self.assertEqual([r.revision_no for r in self.revs(self.cut.name)], [0])

	def test_actual_dates_are_not_part_of_the_schedule(self):
		self.complete_job_card(self.asm.name)
		before = frappe.db.get_value("Job Card", self.asm.name, ["actual_start_date", "actual_end_date"], as_dict=True)
		self.assertIsNotNone(before.actual_start_date)
		hist = revisions.get_history(self.asm.name)
		self.assertEqual(hist["actual"]["start"], scheduling.dt_str(before.actual_start_date))
		self.assertEqual(hist["baseline"]["start"], scheduling.dt_str(self.card(self.asm.name).ppc_original_start))
		self.assertNotEqual(hist["actual"]["start"], hist["baseline"]["start"], "actual execution is a separate concept")

	# --------------------------------------------------------------- atomicity
	def assert_nothing_changed(self, name, before, revs_before):
		now = self.card(name)
		for f in ("expected_start_date", "expected_end_date", "ppc_original_start", "ppc_revised_start", "ppc_revised_end", "ppc_schedule_revision"):
			self.assertEqual(now[f], before[f], f)
		self.assertEqual(len(self.revs(name)), revs_before, "no revision may exist for a change that did not happen")
		self.assertEqual(len(frappe.get_doc("Job Card", name).scheduled_time_logs), len(self._rows_before))

	def test_failed_work_order_sync_rolls_everything_back(self):
		before, revs_before = self.card(self.asm.name), len(self.revs(self.asm.name))
		self._rows_before = frappe.get_doc("Job Card", self.asm.name).scheduled_time_logs
		wo_before = self.wo_dates()
		with mock.patch.object(scheduling, "sync_work_order_schedule", side_effect=RuntimeError("Work Order save failed")):
			res = self.shift(self.asm.name, days=1)
		self.assertFalse(res["ok"])
		self.assertIn("Work Order save failed", res["errors"][0])
		self.assert_nothing_changed(self.asm.name, before, revs_before)
		self.assertEqual(self.wo_dates(), wo_before)
		self.assertEqual(self.card(self.asm.name).ppc_schedule_sync_status, "Failed")
		failed = frappe.get_all("PPC Audit Log", filters={"job_card": self.asm.name, "result": "Failed", "action": "Reschedule Job Card"},
			fields=["retry_status", "details", "error_details", "idempotency_key"])
		self.assertEqual(len(failed), 1)
		self.assertEqual(failed[0].retry_status, "Pending")
		self.assertIn('"proposal"', failed[0].details)
		self.assertTrue(failed[0].idempotency_key, "correlation id recorded")

	def test_failure_while_writing_the_revision_also_undoes_the_card_and_work_order(self):
		before, revs_before = self.card(self.asm.name), len(self.revs(self.asm.name))
		self._rows_before = frappe.get_doc("Job Card", self.asm.name).scheduled_time_logs
		wo_before = self.wo_dates()
		with mock.patch.object(revisions, "record_revision", side_effect=RuntimeError("history write failed")):
			res = self.shift(self.asm.name, days=1)
		self.assertFalse(res["ok"])
		self.assert_nothing_changed(self.asm.name, before, revs_before)
		self.assertEqual(self.wo_dates(), wo_before, "the Work Order was synchronised inside the same unit, so it is undone too")

	def test_a_schedule_that_erpnext_did_not_actually_store_is_not_accepted(self):
		before, revs_before = self.card(self.asm.name), len(self.revs(self.asm.name))
		self._rows_before = frappe.get_doc("Job Card", self.asm.name).scheduled_time_logs
		with mock.patch.object(scheduling, "apply_dates", lambda *a, **k: None):  # "saved" but nothing changed
			res = self.shift(self.asm.name, days=1)
		self.assertFalse(res["ok"])
		self.assertIn("not applied", res["errors"][0])
		self.assert_nothing_changed(self.asm.name, before, revs_before)

	def test_retry_replays_the_stored_proposal_and_resolves_the_failure(self):
		base = self.card(self.asm.name)
		target = base.expected_start_date + timedelta(days=1)
		with mock.patch.object(scheduling, "sync_work_order_schedule", side_effect=RuntimeError("temporary outage")):
			self.assertFalse(self.move(self.asm.name, target, base.expected_end_date + timedelta(days=1), reason="Please move")["ok"])
		self.assertEqual(self.card(self.asm.name).ppc_schedule_sync_status, "Failed")

		out = reconcile.retry_failed_synchronizations(self.plan.name)
		self.assertEqual((out["retried"], out["succeeded"]), (1, 1), out)
		after = self.card(self.asm.name)
		self.assertEqual(after.expected_start_date, target.replace(microsecond=0), "the stored proposal has second precision")
		self.assertEqual(after.ppc_schedule_sync_status, "Synced")
		rows = self.revs(self.asm.name)
		self.assertEqual([r.revision_no for r in rows], [0, 1])
		self.assertEqual((rows[1].change_source, rows[1].change_reason), ("Retry", "Please move"))
		self.assertEqual(frappe.db.get_value("PPC Audit Log", {"job_card": self.asm.name, "result": "Failed"}, "retry_status"), "Resolved")
		again = reconcile.retry_failed_synchronizations(self.plan.name)
		self.assertEqual(again["retried"], 0, "nothing left to retry")
		self.assertEqual(len(self.revs(self.asm.name)), 2)

	# --------------------------------------------------------------- concurrency
	def test_stale_update_is_rejected_and_records_nothing(self):
		token = str(self.card(self.asm.name).modified)  # what the Gantt loaded
		c = self.card(self.asm.name)
		first = self.move(self.asm.name, c.expected_start_date + timedelta(days=1), c.expected_end_date + timedelta(days=1), expected_modified=token)
		self.assertTrue(first["ok"], first)
		self.assertNotEqual(first["modified"], token)
		c2 = self.card(self.asm.name)
		second = self.move(self.asm.name, c2.expected_start_date + timedelta(days=1), c2.expected_end_date + timedelta(days=1), expected_modified=token)
		self.assertFalse(second["ok"])
		self.assertTrue(second["stale"])
		self.assertEqual([r.revision_no for r in self.revs(self.asm.name)], [0, 1])
		# the fresh token returned by the first save is accepted
		third = self.move(self.asm.name, c2.expected_start_date + timedelta(days=1), c2.expected_end_date + timedelta(days=1), expected_modified=first["modified"])
		self.assertTrue(third["ok"], third)

	# --------------------------------------------------------------- time zone / boundaries
	def test_revision_dates_are_exact_across_midnight_month_end_and_leap_day(self):
		cases = [
			(datetime(2028, 2, 29, 23, 0, 0), datetime(2028, 3, 1, 1, 0, 0)),
			(datetime(2027, 12, 31, 22, 30, 0), datetime(2028, 1, 1, 0, 30, 0)),
			(datetime(2027, 3, 28, 1, 30, 0), datetime(2027, 3, 28, 3, 30, 0)),
		]
		for start, end in cases:
			res = self.move(self.asm.name, start, end, mode="resize", override_conflicts=True)
			self.assertTrue(res["ok"], res)
			last = self.revs(self.asm.name)[-1]
			self.assertEqual((last.accepted_start, last.accepted_end), (start, end))
			hist = revisions.get_history(self.asm.name)
			self.assertEqual(hist["revisions"][-1]["accepted_start"], start.strftime("%Y-%m-%d %H:%M:%S"))
			self.assertEqual(hist["current"]["start"], start.strftime("%Y-%m-%d %H:%M:%S"))

	# --------------------------------------------------------------- history view
	def test_history_reports_baseline_current_actual_and_the_difference(self):
		base = self.card(self.asm.name)
		self.move(self.asm.name, base.expected_start_date + timedelta(days=1), base.expected_end_date + timedelta(days=1, hours=2), reason="Slip")
		h = api.get_schedule_history(self.asm.name)
		self.assertTrue(h["ok"])
		self.assertEqual((h["revision"], h["has_baseline"], h["sync_status"]), (1, True, "Synced"))
		self.assertEqual(h["baseline"]["start"], scheduling.dt_str(base.ppc_original_start))
		self.assertEqual(h["baseline"]["label"], "Initial schedule")
		self.assertEqual(h["difference"]["start_minutes"], 1440)
		self.assertEqual(h["difference"]["end_minutes"], 1440 + 120)
		self.assertIn("Start moved by +1d", h["difference"]["text"])
		self.assertIn("End moved by +1d 2h", h["difference"]["text"])
		self.assertIn("revision 1", h["difference"]["text"])
		self.assertEqual([r["revision_no"] for r in h["revisions"]], [0, 1])
		self.assertEqual(h["work_order"], self.wo)
		row = next(r for r in api.get_job_cards({"plan": self.plan.name})["rows"] if r["name"] == self.asm.name)
		self.assertEqual((row["revision"], row["last_rescheduled_by"], row["sync_status"]), (1, "Administrator", "Rescheduled"))
		self.assertIn("Start moved by +1d", row["difference"])
		# what the Gantt shows is exactly what is persisted
		task = next(t for t in api.get_gantt_data({"plan": self.plan.name})["tasks"] if t["name"] == self.asm.name)
		persisted = self.card(self.asm.name)
		self.assertEqual(task["revised_start"], persisted.expected_start_date.strftime("%Y-%m-%d %H:%M:%S"))
		self.assertEqual(task["original_start"], persisted.ppc_original_start.strftime("%Y-%m-%d %H:%M:%S"))
		self.assertEqual(task["revision"], 1)

	def test_drift_and_failure_are_visible_in_the_status(self):
		frappe.db.set_value("Job Card", self.asm.name, "expected_start_date", self.card(self.asm.name).expected_start_date + timedelta(hours=1))
		row = next(r for r in api.get_job_cards({"plan": self.plan.name})["rows"] if r["name"] == self.asm.name)
		self.assertEqual(row["sync_status"], "Drift")
		frappe.db.set_value("Job Card", self.asm.name, "ppc_schedule_sync_status", "Failed")
		row = next(r for r in api.get_job_cards({"plan": self.plan.name})["rows"] if r["name"] == self.asm.name)
		self.assertEqual(row["sync_status"], "Failed")

	# --------------------------------------------------------------- append-only history
	def test_revision_history_cannot_be_written_edited_or_deleted_by_normal_means(self):
		rev = self.revs(self.asm.name)[0]
		with self.assertRaises(frappe.PermissionError):  # not created by hand
			frappe.get_doc({"doctype": REV, "job_card": self.asm.name, "revision_no": 99, "revision_type": "Revision"}).insert(ignore_permissions=True)
		doc = frappe.get_doc(REV, rev.name)
		doc.accepted_start = doc.accepted_start + timedelta(days=1)
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			frappe.delete_doc(REV, rev.name, ignore_permissions=True)
		self.assertTrue(frappe.db.exists(REV, rev.name))

	def test_audited_correction_only_annotates(self):
		rev = self.revs(self.asm.name)[0]
		with self.assertRaises(frappe.ValidationError):
			revisions.correct_revision(rev.name, "short")
		out = revisions.correct_revision(rev.name, "Operator keyed the wrong baseline reason")
		self.assertTrue(out["corrected"])
		after = frappe.get_doc(REV, rev.name)
		self.assertEqual((after.corrected, after.corrected_by), (1, "Administrator"))
		self.assertEqual(after.accepted_start, rev.accepted_start, "dates are never changed by a correction")
		self.assertTrue(frappe.db.exists("PPC Audit Log", {"action": "Correct Revision", "job_card": self.asm.name}))
		# a correction cannot smuggle in a date change
		doc = frappe.get_doc(REV, rev.name)
		doc.flags.ppc_audited_correction = True
		doc.accepted_start = doc.accepted_start + timedelta(days=1)
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_history_outlives_a_deleted_draft_card(self):
		self.shift(self.asm.name, days=1)
		frappe.delete_doc("Job Card", self.asm.name)
		self.assertEqual(len(self.revs(self.asm.name)), 2)

	def test_history_endpoints_respect_permissions(self):
		from ppc_production_control.tests.test_api_security import make_user, ERP_MFG

		viewer = make_user("revviewer", ["PPC Viewer", *ERP_MFG])
		manager = make_user("revmanager", ["PPC Manager", *ERP_MFG])
		rev = self.revs(self.asm.name)[0]
		frappe.set_user(viewer)
		self.assertTrue(api.get_schedule_history(self.asm.name)["ok"])
		with self.assertRaises(frappe.PermissionError):
			api.correct_schedule_revision(rev.name, "Not allowed for a viewer at all")
		frappe.set_user(manager)
		with self.assertRaises(frappe.PermissionError):  # PPC Manager is not a System Manager
			api.correct_schedule_revision(rev.name, "Managers may not edit history either")
		frappe.set_user("Administrator")
		self.assertTrue(api.correct_schedule_revision(rev.name, "Administrator annotation for the audit")["ok"])
