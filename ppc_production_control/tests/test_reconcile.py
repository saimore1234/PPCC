"""Consistency check, baseline initialisation and repair: preview first, idempotent, permission-controlled, audited."""

from datetime import timedelta

import frappe

from ppc_production_control import api
from ppc_production_control.services import job_cards as jc_service
from ppc_production_control.services import reconcile, revisions, scheduling
from ppc_production_control.tests.utils import PPCTestCase

REV = "PPC Schedule Revision"


class TestReconcile(PPCTestCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.job_cards(self.wo)

	def card(self, name):
		return frappe.db.get_value("Job Card", name, ["expected_start_date", "expected_end_date", "ppc_schedule_revision", "ppc_schedule_sync_status",
			"ppc_original_start", "ppc_original_end", "ppc_baseline_source", "ppc_revised_start", "ppc_revised_end"], as_dict=True)

	def kinds(self, plan=None):
		return sorted(i["type"] for i in reconcile.check_consistency(plan or self.plan.name)["issues"])

	def strip_baseline(self, name):
		frappe.db.delete(REV, {"job_card": name})
		frappe.db.set_value("Job Card", name, {k: None for k in ("ppc_original_start", "ppc_original_end", "ppc_baseline_source",
			"ppc_revised_start", "ppc_revised_end")} | {"ppc_schedule_revision": 0})

	# ---------------------------------------------------------------- clean data
	def test_consistent_plan_reports_nothing(self):
		out = reconcile.check_consistency(self.plan.name)
		self.assertEqual((out["total"], out["checked_cards"], out["checked_work_orders"], out["truncated"]), (0, 2, 1, False))

	# ---------------------------------------------------------------- detection
	def test_missing_baseline_is_detected(self):
		self.strip_baseline(self.asm.name)
		self.assertEqual(self.kinds(), ["missing_baseline"])

	def test_card_drift_is_detected(self):
		frappe.db.set_value("Job Card", self.asm.name, "expected_start_date", self.card(self.asm.name).expected_start_date + timedelta(hours=3))
		issues = reconcile.check_consistency(self.plan.name)["issues"]
		self.assertEqual([i["type"] for i in issues], ["schedule_drift"])
		self.assertTrue(issues[0]["repairable"])
		self.assertEqual(issues[0]["job_card"], self.asm.name)

	def test_work_order_drift_is_detected(self):
		wo = frappe.db.get_value("Work Order", self.wo, "planned_end_date")
		frappe.db.set_value("Work Order", self.wo, "planned_end_date", wo + timedelta(days=2))
		issues = reconcile.check_consistency(self.plan.name)["issues"]
		self.assertEqual([(i["type"], i["work_order"]) for i in issues], [("work_order_drift", self.wo)])

	def test_failed_sync_is_detected(self):
		frappe.db.set_value("Job Card", self.cut.name, "ppc_schedule_sync_status", "Failed")
		self.assertEqual(self.kinds(), ["failed_sync"])

	def test_revision_history_inconsistencies_are_detected_but_never_auto_repaired(self):
		frappe.db.set_value("Job Card", self.cut.name, "ppc_schedule_revision", 4)  # history ends at 0
		c = self.card(self.asm.name)
		self.assertTrue(scheduling.reschedule_job_card(self.asm.name, c.expected_start_date + timedelta(days=1),
			c.expected_end_date + timedelta(days=1), confirmed=True)["ok"])
		frappe.db.delete(REV, {"job_card": self.asm.name, "revision_no": 0})  # a hole: revision 1 without revision 0
		issues = reconcile.check_consistency(self.plan.name)["issues"]
		self.assertEqual(sorted((i["type"], i["job_card"]) for i in issues),
			sorted([("revision_mismatch", self.cut.name), ("revision_mismatch", self.asm.name)]))
		self.assertFalse(any(i["repairable"] for i in issues))
		out = reconcile.repair(self.plan.name, dry_run=False)
		self.assertEqual(out["repaired"], 0)
		self.assertEqual(frappe.db.get_value("Job Card", self.cut.name, "ppc_schedule_revision"), 4)
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 1, "history is never invented")

	def test_orphan_work_order_is_reported_only(self):
		frappe.db.set_value("Job Card", self.asm.name, "work_order", "WO-DOES-NOT-EXIST")
		issues = [i for i in reconcile.check_consistency(self.plan.name)["issues"] if i["type"] == "orphan_work_order"]
		self.assertEqual(len(issues), 1)
		self.assertFalse(issues[0]["repairable"])

	def test_check_is_read_only(self):
		self.strip_baseline(self.asm.name)
		frappe.db.set_value("Job Card", self.cut.name, "expected_start_date", self.card(self.cut.name).expected_start_date + timedelta(hours=1))
		snap = (self.card(self.asm.name), self.card(self.cut.name), frappe.db.count(REV), frappe.db.count("PPC Audit Log"))
		reconcile.check_consistency(self.plan.name)
		self.assertEqual((self.card(self.asm.name), self.card(self.cut.name), frappe.db.count(REV), frappe.db.count("PPC Audit Log")), snap)

	# ---------------------------------------------------------------- baseline initialisation
	def test_initialize_baselines_previews_then_applies_once(self):
		self.strip_baseline(self.asm.name)
		found = self.card(self.asm.name).expected_start_date

		preview = reconcile.initialize_missing_baselines(self.plan.name)  # dry run by default
		self.assertTrue(preview["dry_run"])
		self.assertEqual([c["job_card"] for c in preview["candidates"]], [self.asm.name])
		self.assertIsNone(self.card(self.asm.name).ppc_original_start, "preview wrote nothing")
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 0)

		done = reconcile.initialize_missing_baselines(self.plan.name, dry_run=False)
		self.assertEqual(done["initialized"], 1)
		after = self.card(self.asm.name)
		self.assertEqual((after.ppc_original_start, after.ppc_baseline_source), (found, "Initialized from existing schedule"))
		self.assertEqual(frappe.db.get_value(REV, {"job_card": self.asm.name}, "baseline_note"), "Baseline initialized from existing schedule")
		self.assertTrue(frappe.db.exists("PPC Audit Log", {"action": "Initialize Baseline", "ppc_plan": self.plan.name}))

		again = reconcile.initialize_missing_baselines(self.plan.name, dry_run=False)
		self.assertEqual(again["initialized"], 0, "idempotent")
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 1)
		self.assertEqual(self.kinds(), [])

	def test_initialization_never_overwrites_an_existing_baseline(self):
		before = self.card(self.cut.name)
		reconcile.initialize_missing_baselines(self.plan.name, dry_run=False)
		self.assertEqual(self.card(self.cut.name).ppc_original_start, before.ppc_original_start)
		self.assertEqual(self.card(self.cut.name).ppc_baseline_source, "Created")

	# ---------------------------------------------------------------- repair
	def test_repair_preview_changes_nothing(self):
		wanted = self.card(self.asm.name)
		frappe.db.set_value("Job Card", self.asm.name, "expected_start_date", wanted.expected_start_date + timedelta(hours=3))
		drifted = self.card(self.asm.name)
		out = reconcile.repair(self.plan.name)
		self.assertTrue(out["dry_run"])
		self.assertEqual((out["repaired"], out["results"][0]["applied"]), (0, False))
		self.assertIn("Would repair", out["results"][0]["message"])
		self.assertEqual(self.card(self.asm.name), drifted)
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 1)

	def test_card_drift_repair_restores_the_accepted_schedule_as_a_new_revision_and_is_idempotent(self):
		accepted = self.card(self.asm.name)
		frappe.db.set_value("Job Card", self.asm.name, "expected_end_date", accepted.expected_end_date + timedelta(hours=5))
		out = reconcile.repair(self.plan.name, dry_run=False)
		self.assertEqual(out["repaired"], 1, out)
		after = self.card(self.asm.name)
		self.assertEqual(after.expected_end_date.replace(microsecond=0), accepted.expected_end_date.replace(microsecond=0))
		self.assertEqual(after.expected_start_date.replace(microsecond=0), accepted.expected_start_date.replace(microsecond=0))
		self.assertEqual(after.ppc_schedule_revision, 1)
		last = frappe.get_all(REV, filters={"job_card": self.asm.name}, fields=["change_source", "revision_no"], order_by="revision_no desc", limit=1)[0]
		self.assertEqual((last.revision_no, last.change_source), (1, "Reconciliation"))
		self.assertEqual(self.card(self.asm.name).ppc_original_start, accepted.ppc_original_start, "the baseline is untouched")
		self.assertTrue(frappe.db.exists("PPC Audit Log", {"action": "Reconcile", "job_card": self.asm.name, "result": "Success"}))

		self.assertEqual(self.kinds(), [], "nothing left to repair")
		again = reconcile.repair(self.plan.name, dry_run=False)
		self.assertEqual((again["repaired"], again["issues"]), (0, 0))
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 2, "no further revisions")

	def test_work_order_drift_repair_uses_the_job_card_aggregate(self):
		cards_end = max(self.card(c.name).expected_end_date for c in (self.cut, self.asm))
		frappe.db.set_value("Work Order", self.wo, "planned_end_date", cards_end + timedelta(days=3))
		out = reconcile.repair(self.plan.name, types=["work_order_drift"], dry_run=False)
		self.assertEqual(out["repaired"], 1, out)
		self.assertEqual(frappe.db.get_value("Work Order", self.wo, "planned_end_date").replace(microsecond=0), cards_end.replace(microsecond=0))
		self.assertEqual(self.kinds(), [])
		self.assertEqual(reconcile.repair(self.plan.name, dry_run=False)["repaired"], 0)

	def test_a_work_order_still_spanning_a_finished_operation_is_not_reported(self):
		self.complete_job_card(self.cut.name)
		jc_service.submit_selected_job_cards(self.plan.name, [self.cut.name])
		self.assertEqual(self.kinds(), [], "finishing operation 1 alone is not an inconsistency")
		# ...but dates that match neither the remaining nor the full span still are
		wo_end = frappe.db.get_value("Work Order", self.wo, "planned_end_date")
		frappe.db.set_value("Work Order", self.wo, "planned_end_date", wo_end + timedelta(days=1))
		self.assertEqual(self.kinds(), ["work_order_drift"])

	# ---------------------------------------------------------------- upgrade from 1.0.0: baseline without history
	def legacy(self, name, *, moved_by_hours=0):
		"""A card as PPC 1.0.0 left it: original snapshot fields, no history rows, none of the 1.1 fields."""
		frappe.db.delete(REV, {"job_card": name})
		frappe.db.set_value("Job Card", name, {k: None for k in ("ppc_baseline_source", "ppc_baseline_captured_at", "ppc_baseline_captured_by",
			"ppc_revised_start", "ppc_revised_end", "ppc_last_rescheduled_at", "ppc_last_rescheduled_by")} | {"ppc_schedule_revision": 0}, update_modified=False)
		if moved_by_hours:  # 1.0.0 changed the schedule without keeping a record
			c = self.card(name)
			frappe.db.set_value("Job Card", name, "expected_end_date", c.expected_end_date + timedelta(hours=moved_by_hours), update_modified=False)
		return self.card(name)

	def test_legacy_baseline_gets_history_without_touching_the_baseline(self):
		before = self.legacy(self.asm.name)
		self.assertEqual(self.kinds(), ["missing_history"])
		self.assertTrue(reconcile.check_consistency(self.plan.name)["issues"][0]["repairable"])
		preview = reconcile.initialize_missing_baselines(self.plan.name)
		self.assertEqual([(c["type"], c["job_card"]) for c in preview["candidates"]], [("missing_history", self.asm.name)])
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 0, "preview writes nothing")

		done = reconcile.initialize_missing_baselines(self.plan.name, dry_run=False)
		self.assertEqual((done["initialized"], done["history_added"]), (0, 1))
		after = self.card(self.asm.name)
		self.assertEqual(after.ppc_original_start, before.ppc_original_start, "the 1.0.0 baseline is kept as it was")
		self.assertEqual((after.ppc_schedule_revision, after.ppc_baseline_source), (0, "Initialized from existing schedule"))
		rows = frappe.get_all(REV, filters={"job_card": self.asm.name}, fields=["revision_no", "change_source", "accepted_start"])
		self.assertEqual([(r.revision_no, r.change_source) for r in rows], [(0, "Baseline")])
		self.assertEqual(rows[0].accepted_start, before.ppc_original_start)
		self.assertEqual(self.kinds(), [])
		self.assertEqual(reconcile.initialize_missing_baselines(self.plan.name, dry_run=False)["history_added"], 0, "idempotent")

	def test_legacy_card_that_was_moved_gets_an_honest_explanatory_revision(self):
		before = self.legacy(self.asm.name, moved_by_hours=5)
		reconcile.repair(self.plan.name, types=["missing_history"], dry_run=False)
		rows = frappe.get_all(REV, filters={"job_card": self.asm.name}, fields=["revision_no", "change_source", "change_reason", "previous_end",
			"accepted_end", "changed_by"], order_by="revision_no")
		self.assertEqual([(r.revision_no, r.change_source) for r in rows], [(0, "Baseline"), (1, "Migration")])
		self.assertIn("not recorded", rows[1].change_reason)
		self.assertIsNone(rows[1].changed_by, "who moved it is unknown and not invented")
		self.assertEqual(rows[1].previous_end, before.ppc_original_end)
		after = self.card(self.asm.name)
		self.assertEqual(after.ppc_schedule_revision, 1)
		self.assertEqual(after.ppc_original_end, before.ppc_original_end)
		self.assertEqual(after.ppc_revised_end, before.expected_end_date, "history ends at the schedule ERPNext holds")
		self.assertNotIn("missing_history", self.kinds())

	def test_migration_patch_backfills_every_legacy_card_once(self):
		from ppc_production_control.patches.v1_1 import backfill_schedule_history

		self.legacy(self.cut.name)
		self.legacy(self.asm.name, moved_by_hours=2)
		cards = [self.cut.name, self.asm.name]
		self.assertEqual(backfill_schedule_history.backfill(cards), 2)
		self.assertEqual(frappe.db.count(REV, {"job_card": self.cut.name}), 1)
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 2)
		self.assertNotIn("missing_history", self.kinds())
		self.assertEqual(backfill_schedule_history.backfill(cards), 0)
		self.assertEqual((frappe.db.count(REV, {"job_card": self.cut.name}), frappe.db.count(REV, {"job_card": self.asm.name})), (1, 2), "re-running adds nothing")

	def test_migration_patch_creates_the_fields_it_needs_before_it_uses_them(self):
		"""Found by migrating a real 1.0.0 copy: patches run BEFORE the after_migrate hook that creates the Job Card
		fields, so on an upgraded site the columns this patch reads did not exist yet and `bench migrate` aborted."""
		from unittest import mock

		from ppc_production_control.patches.v1_1 import backfill_schedule_history

		order = []
		with mock.patch("ppc_production_control.install.ensure_custom_fields", side_effect=lambda: order.append("fields")), 			mock.patch.object(backfill_schedule_history, "backfill", side_effect=lambda names: order.append("backfill") or 0):
			backfill_schedule_history.execute()
		self.assertEqual(order, ["fields", "backfill"])

	def test_a_capability_added_by_an_upgrade_falls_back_to_its_default_role(self):
		"""An upgraded site's PPC Settings table was seeded by 1.0.0 and is never overwritten, so it has no row for a
		capability introduced later; that capability must still work for its default role and only for it."""
		from ppc_production_control.services.constants import CAP_RECONCILE
		from ppc_production_control.services.permissions import get_capability_roles

		frappe.db.delete("PPC Capability Role", {"parent": "PPC Settings", "capability": CAP_RECONCILE})
		frappe.clear_document_cache("PPC Settings", "PPC Settings")
		self.assertEqual(get_capability_roles()[CAP_RECONCILE], ["PPC Manager"])

	def test_cards_without_any_baseline_are_left_for_the_preview_first_action(self):
		from ppc_production_control.patches.v1_1 import backfill_schedule_history

		self.strip_baseline(self.asm.name)
		self.assertEqual(backfill_schedule_history.backfill([self.asm.name]), 0)
		self.assertEqual(frappe.db.count(REV, {"job_card": self.asm.name}), 0)
		self.assertEqual(self.kinds(), ["missing_baseline"])

	def test_failed_flag_with_nothing_pending_is_cleared(self):
		frappe.db.set_value("Job Card", self.cut.name, "ppc_schedule_sync_status", "Failed")
		out = reconcile.repair(self.plan.name, types=["failed_sync"], dry_run=False)
		self.assertEqual(out["repaired"], 1)
		self.assertEqual(self.card(self.cut.name).ppc_schedule_sync_status, "Synced")

	def test_repair_only_touches_the_requested_types(self):
		self.strip_baseline(self.asm.name)
		frappe.db.set_value("Job Card", self.cut.name, "ppc_schedule_sync_status", "Failed")
		out = reconcile.repair(self.plan.name, types=["failed_sync"], dry_run=False)
		self.assertEqual(out["repaired"], 1)
		self.assertEqual(self.kinds(), ["missing_baseline"])

	def test_finished_work_is_reported_but_never_changed(self):
		self.complete_job_card(self.cut.name)
		jc_service.submit_selected_job_cards(self.plan.name, [self.cut.name])
		frappe.db.set_value("Job Card", self.cut.name, "expected_start_date", self.card(self.cut.name).expected_start_date + timedelta(hours=2))
		drifted = self.card(self.cut.name)
		issues = [i for i in reconcile.check_consistency(self.plan.name)["issues"] if i["job_card"] == self.cut.name]
		self.assertEqual([(i["type"], i["repairable"]) for i in issues], [("schedule_drift", False)])
		out = reconcile.repair(self.plan.name, dry_run=False)
		# the test's own edit of the finished card also changes the Work Order's span, so a Work Order sync may be
		# offered; the finished Job Card itself is never touched
		self.assertEqual([r for r in out["results"] if r["job_card"] == self.cut.name and r["applied"]], [], out["results"])
		self.assertEqual({r["type"] for r in out["results"] if r["applied"]} - {"work_order_drift"}, set())
		self.assertEqual(self.card(self.cut.name), drifted)
		self.assertEqual(frappe.db.count(REV, {"job_card": self.cut.name}), 1)

	# ---------------------------------------------------------------- permissions
	def test_reconciliation_is_permission_controlled(self):
		from ppc_production_control.tests.test_api_security import ERP_MFG, make_user

		viewer = make_user("recviewer", ["PPC Viewer", *ERP_MFG])
		manager = make_user("recmanager", ["PPC Manager", *ERP_MFG])
		self.strip_baseline(self.asm.name)
		frappe.set_user(viewer)
		for call in (
			lambda: api.initialize_missing_baselines(self.plan.name, dry_run=0),
			lambda: api.repair_schedule_inconsistencies(self.plan.name, dry_run=0),
			lambda: api.retry_failed_synchronizations(self.plan.name),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.assertIsNone(self.card(self.asm.name).ppc_original_start)
		frappe.set_user(manager)
		self.assertTrue(api.check_schedule_consistency(self.plan.name)["total"] >= 1)
		self.assertEqual(api.initialize_missing_baselines(self.plan.name, dry_run=0)["initialized"], 1)

	def test_endpoints_default_to_preview(self):
		self.strip_baseline(self.asm.name)
		self.assertTrue(api.initialize_missing_baselines(self.plan.name)["dry_run"])
		self.assertTrue(api.repair_schedule_inconsistencies(self.plan.name)["dry_run"])
		self.assertIsNone(self.card(self.asm.name).ppc_original_start)
