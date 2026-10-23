"""Submitting the draft Work Orders of a standard Production Plan: single, bulk, failures, retry."""

import frappe

from ppc_production_control.services import work_orders as wo_service
from ppc_production_control.tests.utils import PPCTestCase


class TestWorkOrderSubmission(PPCTestCase):
	def drafts(self, *lines):
		so = self.new_so(*lines)
		plan = self.new_plan(so.name, auto_create=True)
		plan.submit()
		plan.reload()
		return plan

	def test_submit_goes_through_the_normal_lifecycle_and_creates_job_cards(self):
		plan = self.drafts((self.fg_ops, 10))
		name = self.work_order_of(plan, self.fg_ops)
		res = wo_service.submit_work_order(name, plan_name=plan.name)
		self.assertTrue(res["ok"], res)
		self.assertEqual(res["job_cards"], 2)
		self.assertEqual(frappe.db.get_value("Work Order", name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Work Order", name, "status"), "Not Started")
		self.assertEqual(frappe.db.count("Job Card", {"work_order": name, "ppc_production_plan": plan.name}), 2)
		ok = frappe.get_all("PPC Audit Log", filters={"ppc_plan": plan.name, "action": "Submit Work Order", "result": "Success"})
		self.assertEqual(len(ok), 1)

	def test_submit_failure_is_reported_not_bypassed_and_retry_recovers(self):
		"""ERPNext refuses a Work Order without a WIP warehouse at submit; PPC must surface that."""
		plan = self.drafts((self.fg_plain, 4))
		name = self.work_order_of(plan, self.fg_plain)
		frappe.db.set_value("Work Order", name, "wip_warehouse", None)
		frappe.db.set_single_value("Manufacturing Settings", "default_wip_warehouse", None)

		res = wo_service.submit_work_order(name, plan_name=plan.name)
		self.assertFalse(res["ok"])
		self.assertIn("Work-in-Progress Warehouse", res["message"])
		self.assertEqual(frappe.db.get_value("Work Order", name, "docstatus"), 0, "left as a draft")
		failed = frappe.get_all("PPC Audit Log", filters={"ppc_plan": plan.name, "work_order": name, "result": "Failed"},
			fields=["error_details", "retry_status"])
		self.assertEqual(len(failed), 1)
		self.assertIn("Work-in-Progress", failed[0].error_details)
		self.assertEqual(failed[0].retry_status, "Pending")

		# prerequisite fixed -> the retry action submits exactly that Work Order and resolves the entry
		frappe.db.set_single_value("Manufacturing Settings", "default_wip_warehouse", self.wip)
		frappe.db.set_value("Work Order", name, "wip_warehouse", self.wip)
		out = wo_service.retry_failed_operations(plan.name)
		self.assertEqual([s["name"] for s in out["submitted"]], [name])
		self.assertEqual(frappe.db.get_value("Work Order", name, "docstatus"), 1)
		self.assertEqual(
			frappe.db.get_value("PPC Audit Log", {"ppc_plan": plan.name, "work_order": name, "action": "Submit Work Order", "result": "Failed"}, "retry_status"),
			"Resolved",
		)

	def test_bulk_submit_only_drafts_of_the_plan_and_reports_each(self):
		plan = self.drafts((self.fg_ops, 5), (self.fg_plain, 5))
		names = [w.name for w in self.work_orders_of(plan)]
		self.assertEqual(len(names), 2)
		foreign_plan = self.drafts((self.fg_plain, 3))
		foreign = self.work_order_of(foreign_plan, self.fg_plain)

		wo_service.submit_work_order(names[0], plan_name=plan.name)  # submitted before the bulk call
		res = wo_service.submit_selected_work_orders(plan.name, names + [foreign, "WO-DOES-NOT-EXIST"])
		by = {r["name"]: r for r in res["results"]}
		self.assertTrue(by[names[0]]["skipped"], "never resubmitted")
		self.assertTrue(by[names[1]]["ok"])
		self.assertTrue(by[foreign]["skipped"], "documents of other plans are not touched")
		self.assertEqual(frappe.db.get_value("Work Order", foreign, "docstatus"), 0)
		self.assertTrue(by["WO-DOES-NOT-EXIST"]["skipped"])
		self.assertEqual((res["submitted"], res["skipped"], res["failed"]), (1, 3, 0))

	def test_bulk_submit_continues_after_one_failure(self):
		plan = self.drafts((self.fg_ops, 5), (self.fg_plain, 5))
		bad = self.work_order_of(plan, self.fg_ops)
		good = self.work_order_of(plan, self.fg_plain)
		frappe.db.set_value("Work Order", bad, "wip_warehouse", None)
		frappe.db.set_single_value("Manufacturing Settings", "default_wip_warehouse", None)
		res = wo_service.submit_selected_work_orders(plan.name, [bad, good])
		by = {r["name"]: r for r in res["results"]}
		self.assertFalse(by[bad]["ok"])
		self.assertIn("Work-in-Progress", by[bad]["message"])
		# the good one has its own WIP warehouse stored, so it still submits
		self.assertTrue(by[good]["ok"], by[good])
		self.assertEqual((res["submitted"], res["failed"]), (1, 1))

	def test_cancelled_work_order_is_never_submitted(self):
		plan = self.drafts((self.fg_plain, 4))
		name = self.work_order_of(plan, self.fg_plain)
		wo_service.submit_work_order(name, plan_name=plan.name)
		frappe.get_doc("Work Order", name).cancel()
		res = wo_service.submit_selected_work_orders(plan.name, [name])
		self.assertTrue(res["results"][0]["skipped"])
		self.assertEqual(frappe.db.get_value("Work Order", name, "docstatus"), 2)

	def test_work_orders_can_be_cancelled_and_deleted_despite_ppc(self):
		"""PPC adds no link fields to Work Order, so ERPNext's own cancel/delete rules are untouched."""
		plan = self.drafts((self.fg_plain, 4), (self.fg_ops, 2))
		plain = self.work_order_of(plan, self.fg_plain)
		ops = self.work_order_of(plan, self.fg_ops)
		wo_service.submit_work_order(plain, plan_name=plan.name)
		frappe.get_doc("Work Order", plain).cancel()
		frappe.delete_doc("Work Order", ops)  # a draft with audit rows pointing at it
		self.assertFalse(frappe.db.exists("Work Order", ops))
		self.assertEqual(frappe.db.get_value("Work Order", plain, "docstatus"), 2)
		self.assertTrue(frappe.db.exists("PPC Audit Log", {"work_order": ops}), "the audit trail outlives the document")

	def test_auto_submit_option_reports_per_work_order(self):
		so = self.new_so((self.fg_ops, 5), (self.fg_plain, 5))
		plan = self.new_plan(so.name, auto_create=True, auto_submit=True)
		plan.submit()
		plan.reload()
		self.assertEqual({w.docstatus for w in self.work_orders_of(plan)}, {1})
