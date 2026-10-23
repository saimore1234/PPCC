"""Automatic draft Work Orders on a standard Production Plan: creation, duplicates, failures, retry."""

from unittest import mock

import frappe
from frappe.utils import flt

from ppc_production_control.services import work_orders as wo_service
from ppc_production_control.tests.utils import PPCTestCase


class TestStandardPlanFlow(PPCTestCase):
	def submitted(self, *lines, **kw):
		so = self.new_so(*lines)
		plan = self.new_plan(so.name, **kw)
		plan.submit()
		plan.reload()
		return so, plan

	# the plan is ERPNext's own document ----------------------------------------------------------
	def test_it_is_the_standard_production_plan(self):
		so = self.new_so((self.fg_ops, 10))
		plan = self.new_plan(so.name, auto_create=True, priority="Urgent")
		self.assertEqual(plan.doctype, "Production Plan")
		self.assertRegex(plan.name, r"^MFG-PP-")
		self.assertEqual(len(plan.po_items), 1)
		self.assertEqual(plan.po_items[0].bom_no, self.bom_ops)
		self.assertEqual(flt(plan.po_items[0].planned_qty), 10)
		self.assertEqual(plan.ppc_priority, "Urgent")
		self.assertEqual(plan.ppc_auto_create_work_orders, 1)
		self.assertFalse(frappe.db.exists("DocType", "PPC Production Plan"), "no custom plan document exists")

	def test_custom_fields_and_connections_are_installed(self):
		for dt, fields in {
			"Production Plan": ["ppc_auto_create_work_orders", "ppc_auto_submit_work_orders", "ppc_planner", "ppc_priority"],
			"Job Card": ["ppc_production_plan", "ppc_original_start", "ppc_original_end"],
		}.items():
			for f in fields:
				self.assertTrue(frappe.get_meta(dt).has_field(f), f"{dt}.{f}")
		self.assertFalse(frappe.get_meta("Work Order").has_field("ppc_production_plan"), "Work Order needs no extra field")

		from frappe.desk.notifications import get_open_count  # noqa: F401

		data = frappe.get_attr(frappe.get_hooks("override_doctype_dashboards")["Production Plan"][-1])(
			{"fieldname": "production_plan", "transactions": [{"label": "Transactions", "items": ["Work Order"]}]}
		)
		self.assertEqual(data["non_standard_fieldnames"]["Job Card"], "ppc_production_plan")
		self.assertIn("Job Card", [i for g in data["transactions"] for i in g["items"]])

	# automatic draft Work Orders -------------------------------------------------------------------
	def test_submit_creates_draft_work_orders_automatically(self):
		so, plan = self.submitted((self.fg_ops, 10), (self.fg_plain, 4), auto_create=True)
		wos = {w.production_item: w for w in self.work_orders_of(plan)}
		self.assertEqual(set(wos), {self.fg_ops, self.fg_plain})
		w = wos[self.fg_ops]
		self.assertEqual(w.docstatus, 0)
		self.assertEqual(flt(w.qty), 10)
		self.assertEqual(w.bom_no, self.bom_ops)
		self.assertEqual(w.sales_order, so.name)
		self.assertTrue(w.production_plan_item, "linked to its plan row")
		self.assertEqual(w.fg_warehouse, self.fg)
		self.assertEqual(w.wip_warehouse, self.wip)
		self.assertTrue(frappe.get_doc("Work Order", w.name).operations, "operations come from the BOM")

	def test_nothing_is_created_when_the_option_is_off(self):
		_so, plan = self.submitted((self.fg_plain, 4), auto_create=False)
		self.assertEqual(self.work_orders_of(plan), [])
		res = wo_service.create_work_orders(plan.name)  # the manual button
		self.assertEqual(len(res["created"]), 1)

	def test_submit_of_the_plan_is_not_blocked_by_the_background_job(self):
		with mock.patch("ppc_production_control.events.enqueue_once") as q:
			_so, plan = self.submitted((self.fg_plain, 4), auto_create=True)
		q.assert_called_once()
		self.assertEqual(q.call_args.kwargs["plan"], plan.name)
		self.assertEqual(self.work_orders_of(plan), [])  # nothing ran inline: only queued

	# duplicate prevention -----------------------------------------------------------------------------
	def test_running_it_again_never_duplicates(self):
		_so, plan = self.submitted((self.fg_ops, 10), auto_create=True)
		first = {w.name for w in self.work_orders_of(plan)}
		for _ in range(3):
			res = wo_service.create_draft_work_orders(plan.name)
			self.assertEqual(res["created"], [])
			wo_service.create_draft_work_orders(plan.name, retry=True)
		self.assertEqual({w.name for w in self.work_orders_of(plan)}, first)

	def test_the_standard_button_cannot_create_a_second_work_order(self):
		"""'Work Order / Subcontract PO' keeps offering the rows until the drafts are submitted."""
		_so, plan = self.submitted((self.fg_ops, 10), auto_create=True)
		before = len(self.work_orders_of(plan))
		with self.assertRaises(frappe.ValidationError) as ctx:
			frappe.get_doc("Production Plan", plan.name).make_work_order()
		self.assertIn("already exists", str(ctx.exception))
		self.assertEqual(len(self.work_orders_of(plan)), before)

	def test_guard_is_off_for_plans_that_do_not_use_auto_creation(self):
		_so, plan = self.submitted((self.fg_plain, 4), auto_create=False)
		frappe.get_doc("Production Plan", plan.name).make_work_order()  # standard behaviour untouched
		self.assertEqual(len(self.work_orders_of(plan)), 1)

	def test_a_row_deleted_by_hand_is_recreated_once(self):
		_so, plan = self.submitted((self.fg_plain, 4), auto_create=True)
		name = self.work_order_of(plan, self.fg_plain)
		frappe.delete_doc("Work Order", name)
		res = wo_service.create_draft_work_orders(plan.name)
		self.assertEqual(len(res["created"]), 1)
		self.assertEqual(len(self.work_orders_of(plan)), 1)
		self.assertEqual(wo_service.create_draft_work_orders(plan.name)["created"], [])

	# failures and retry ---------------------------------------------------------------------------------
	def test_a_failing_row_does_not_stop_the_others_and_retry_recovers(self):
		_so, plan = self.submitted((self.fg_ops, 10), (self.fg_plain, 4), auto_create=False)
		real = type(frappe.get_doc("Production Plan", plan.name)).create_work_order

		def flaky(self_, item):
			if item["production_item"] == self.fg_plain:
				raise RuntimeError("simulated failure")
			return real(self_, item)

		with mock.patch.object(type(plan), "create_work_order", flaky):
			res = wo_service.create_draft_work_orders(plan.name)
		self.assertEqual([c["item_code"] for c in res["created"]], [self.fg_ops])
		self.assertEqual([f["item_code"] for f in res["failed"]], [self.fg_plain])
		self.assertIn("simulated failure", res["failed"][0]["message"])
		self.assertEqual(len(self.work_orders_of(plan)), 1, "the failed row left nothing behind")

		failed = frappe.get_all("PPC Audit Log", filters={"ppc_plan": plan.name, "result": "Failed"}, fields=["retry_status", "error_details"])
		self.assertEqual(len(failed), 1)
		self.assertEqual(failed[0].retry_status, "Pending")

		# cause fixed -> retry processes only the missing row
		before = {w.name for w in self.work_orders_of(plan)}
		res = wo_service.retry_failed_operations(plan.name)
		self.assertEqual([c["item_code"] for c in res["work_orders"]["created"]], [self.fg_plain])
		after = {w.name for w in self.work_orders_of(plan)}
		self.assertTrue(before < after and len(after) == 2)
		self.assertEqual(
			frappe.db.get_value("PPC Audit Log", {"ppc_plan": plan.name, "result": "Failed"}, "retry_status"), "Resolved"
		)

	def test_pending_flag_follows_missing_work_orders(self):
		from ppc_production_control import api

		_so, plan = self.submitted((self.fg_plain, 4), auto_create=False)
		self.assertTrue(api.get_plan_overview(plan.name)["has_pending"])
		wo_service.create_draft_work_orders(plan.name)
		self.assertFalse(api.get_plan_overview(plan.name)["has_pending"])

	def test_audit_trail_of_creation(self):
		_so, plan = self.submitted((self.fg_plain, 4), auto_create=True)
		rows = frappe.get_all("PPC Audit Log", filters={"ppc_plan": plan.name}, fields=["action", "result", "work_order"])
		self.assertTrue(any(r.action == "Create Work Order" and r.result == "Success" and r.work_order for r in rows))

	# auto submit ------------------------------------------------------------------------------------------
	def test_auto_submit_submits_and_generates_job_cards(self):
		_so, plan = self.submitted((self.fg_ops, 10), (self.fg_plain, 4), auto_create=True, auto_submit=True)
		wos = {w.production_item: w for w in self.work_orders_of(plan)}
		self.assertEqual({w.docstatus for w in wos.values()}, {1})
		cards = frappe.get_all("Job Card", filters={"work_order": wos[self.fg_ops].name, "docstatus": ["<", 2]},
			fields=["name", "operation", "ppc_production_plan", "ppc_original_start", "ppc_original_end"], order_by="sequence_id")
		self.assertEqual([c.operation for c in cards], [self.op_cut, self.op_asm])
		self.assertEqual({c.ppc_production_plan for c in cards}, {plan.name})
		self.assertTrue(all(c.ppc_original_start and c.ppc_original_end for c in cards), "baseline recorded")
		self.assertEqual(frappe.db.count("Job Card", {"work_order": wos[self.fg_plain].name}), 0, "no cards without operations")

	# cancelling ---------------------------------------------------------------------------------------------
	def test_cancelling_the_plan_removes_its_draft_work_orders(self):
		_so, plan = self.submitted((self.fg_plain, 4), auto_create=True)
		self.assertEqual(len(self.work_orders_of(plan)), 1)
		plan.cancel()  # standard behaviour
		self.assertEqual(self.work_orders_of(plan), [])

	def test_cancelled_plan_is_refused(self):
		_so, plan = self.submitted((self.fg_plain, 4), auto_create=False)
		plan.cancel()
		with self.assertRaises(frappe.ValidationError):
			wo_service.create_draft_work_orders(plan.name)
