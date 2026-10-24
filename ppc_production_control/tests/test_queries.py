"""Job Card table, Work Order table, Gantt data and dashboard KPIs: filters, pagination, scope."""

from datetime import timedelta

import frappe
from frappe.utils import add_days, add_to_date, getdate, now_datetime

from ppc_production_control import api
from ppc_production_control.services import queries
from ppc_production_control.services import work_orders as wo_service
from ppc_production_control.tests.utils import PREFIX, PPCTestCase, make_customer, make_sales_order


class TestQueries(PPCTestCase):
	def setUp(self):
		super().setUp()
		# plan A: customer 1, Medium; plan B: customer 2, Urgent
		self.plan_a, self.wo_a = self.ops_plan(10)
		self.customer_b = make_customer(f"{PREFIX} Customer B")
		so_b = make_sales_order(self.company, self.customer_b, [(self.fg_ops, 4), (self.fg_plain, 3)], warehouse=self.fg)
		self.so_b = so_b
		plan_b = self.new_plan(so_b.name, auto_create=True, auto_submit=True, priority="Urgent")
		plan_b.submit()
		self.plan_b = plan_b.reload()
		self.wo_b = self.work_order_of(self.plan_b, self.fg_ops)
		self.cards_a = self.job_cards(self.wo_a)
		self.cards_b = self.job_cards(self.wo_b)

	def names(self, **filters):
		"""Names matching the filters, restricted to this test's own plans: a shared development site can
		hold other users' real Job Cards (already tagged with their plans) that must not leak into asserts."""
		unscoped = any(k in filters for k in ("plan", "work_order", "job_card", "include_non_ppc"))
		plans = [None] if unscoped else [self.plan_a.name, self.plan_b.name]
		out = set()
		for plan in plans:
			f = dict(filters, plan=plan) if plan else filters
			out |= {r["name"] for r in api.get_job_cards(f, page_length=200)["rows"]}
		return out

	@property
	def all_names(self):
		return {c.name for c in self.cards_a + self.cards_b}

	# 18. filtering ---------------------------------------------------------------------------------
	def test_scope_is_ppc_only_unless_asked(self):
		so = self.new_so((self.fg_ops, 2))
		wo = frappe.get_doc({"doctype": "Work Order", "production_item": self.fg_ops, "bom_no": self.bom_ops, "company": self.company,
			"qty": 2, "sales_order": so.name, "fg_warehouse": self.fg, "wip_warehouse": self.wip,
			"planned_start_date": add_days(now_datetime(), 1), "use_multi_level_bom": 0})
		wo.get_items_and_operations_from_bom()
		wo.insert()
		wo.submit()  # ordinary ERPNext Work Order, not created by PPC
		plain = {c.name for c in self.job_cards(wo.name)}
		self.assertEqual(len(plain), 2)
		self.assertFalse(plain & self.names())
		self.assertEqual(self.names(work_order=wo.name, include_non_ppc=1), plain)
		self.assertEqual(self.names(work_order=wo.name), set())

	def test_each_filter(self):
		A = {c.name for c in self.cards_a}
		B = {c.name for c in self.cards_b}
		cut_a, asm_a = self.cards_a
		cut_b, asm_b = self.cards_b

		self.assertEqual(self.names(plan=self.plan_a.name), A)
		self.assertEqual(self.names(plan=self.plan_b.name), B)
		so_a = frappe.db.get_value("Work Order", self.wo_a, "sales_order")
		self.assertEqual(self.names(sales_order=so_a), A)
		self.assertEqual(self.names(sales_order=self.so_b.name), B)
		# partial number: the longest digit run of the name (site naming series differ: TTC/SD/1620/25-26)
		import re

		fragment = max(re.findall(r"\d+", so_a), key=len)
		self.assertEqual(self.names(sales_order_like=fragment, plan=self.plan_a.name), A, "partial Sales Order number")
		self.assertTrue(A <= self.names(sales_order_like=fragment))
		self.assertEqual(self.names(customer=self.customer), A)
		self.assertEqual(self.names(customer=self.customer_b), B)
		self.assertEqual(self.names(company=self.company, plan=self.plan_a.name), A)
		self.assertEqual(self.names(company="No Such Company"), set())
		self.assertEqual(self.names(item_code=self.fg_ops), A | B)
		self.assertEqual(self.names(item_code=self.fg_plain), set())  # no operations -> no cards
		self.assertEqual(self.names(component_item=self.rm2), A | B)
		self.assertEqual(self.names(component_item="NOT-A-COMPONENT"), set())
		self.assertEqual(self.names(work_order=self.wo_a), A)
		self.assertEqual(self.names(job_card=cut_a.name), {cut_a.name})
		self.assertEqual(self.names(operation=self.op_cut), {cut_a.name, cut_b.name})
		self.assertEqual(self.names(workstation=self.ws2), {asm_a.name, asm_b.name})
		self.assertEqual(self.names(planner="Administrator"), A | B)
		self.assertEqual(self.names(planner="nobody@example.com"), set())
		self.assertEqual(self.names(priority="Urgent"), B)
		self.assertEqual(self.names(priority="Medium"), A)
		self.assertEqual(self.names(status="Open"), A | B)
		self.assertEqual(self.names(status="Draft"), A | B)
		self.assertEqual(self.names(status="Completed"), set())
		self.assertEqual(self.names(search="Assemble", plan=self.plan_a.name), {asm_a.name})
		self.assertEqual(self.names(search=self.customer_b[:10] + "%"), set(), "wildcards are literal")

	def test_date_filters(self):
		cut_a = self.cards_a[0]
		start = getdate(cut_a.expected_start_date)
		scope = {"plan": self.plan_a.name}
		# which cards start on that calendar day depends on the time of day the test runs: derive it from the data
		same_day = {c.name for c in self.cards_a if getdate(c.expected_start_date) == start}
		self.assertIn(cut_a.name, same_day)
		self.assertEqual(self.names(**scope, revised_start_from=str(start), revised_start_to=str(start)), same_day)
		self.assertEqual(self.names(**scope, revised_start_from=str(add_days(start, 30))), set())
		self.assertEqual(self.names(**scope, revised_start_to=str(add_days(start, -1))), set())
		wo_start = getdate(frappe.db.get_value("Work Order", self.wo_a, "planned_start_date"))
		self.assertEqual(len(self.names(**scope, planned_start_from=str(wo_start), planned_start_to=str(wo_start))), 2)
		self.assertEqual(self.names(**scope, planned_start_from=str(add_days(wo_start, 1))), set())
		frappe.db.set_value("Work Order", self.wo_a, "expected_delivery_date", add_days(getdate(), 20))
		self.assertEqual(len(self.names(**scope, delivery_from=str(add_days(getdate(), 20)), delivery_to=str(add_days(getdate(), 20)))), 2)
		self.assertEqual(self.names(**scope, delivery_to=str(add_days(getdate(), 19))), set())

	def test_overdue_unassigned_and_conflict_flags(self):
		cut_a, asm_a = self.cards_a
		cut_b, asm_b = self.cards_b
		past = add_to_date(now_datetime(), days=-3)
		frappe.db.set_value("Job Card", cut_a.name, {"expected_start_date": add_to_date(past, hours=-5), "expected_end_date": past})
		self.assertEqual(self.names(overdue_only=1), {cut_a.name})

		frappe.db.set_value("Job Card", asm_b.name, "workstation", None)
		self.assertEqual(self.names(unassigned_only=1), {asm_b.name})

		# put cut_b on top of cut_a's slot, same workstation -> both conflict
		c = frappe.db.get_value("Job Card", cut_a.name, ["expected_start_date", "expected_end_date"], as_dict=True)
		frappe.db.set_value("Job Card", cut_b.name, {"expected_start_date": c.expected_start_date, "expected_end_date": c.expected_end_date})
		self.assertEqual(self.names(conflict_only=1), {cut_a.name, cut_b.name})
		rows = {r["name"]: r for r in api.get_job_cards({"plan": self.plan_a.name})["rows"]}
		self.assertTrue(rows[cut_a.name]["conflict"])
		self.assertTrue(rows[cut_a.name]["conflict_reasons"])
		self.assertFalse(rows[asm_a.name]["conflict"])
		self.assertTrue(rows[cut_a.name]["overdue"])

		# a sequence violation is a conflict too
		frappe.db.set_value("Job Card", asm_a.name, "expected_start_date", add_to_date(past, hours=-1))
		self.assertIn(asm_a.name, self.names(conflict_only=1))

	def test_cancelled_cards_are_excluded_unless_asked(self):
		cut_a, asm_a = self.cards_a
		frappe.db.set_value("Job Card", asm_a.name, "docstatus", 2)
		self.assertNotIn(asm_a.name, self.names(plan=self.plan_a.name))
		self.assertEqual(self.names(plan=self.plan_a.name, status="Cancelled"), {asm_a.name})
		kpi = api.get_ppc_dashboard({"plan": self.plan_a.name})["kpis"]
		self.assertEqual(kpi["total_job_cards"], 1)

	def test_filters_are_normalised(self):
		self.assertEqual(queries.normalize_filters({"plan": "  X ", "evil": "1", "overdue_only": "1", "conflict_only": 0}), {"plan": "X", "overdue_only": 1})
		with self.assertRaises(frappe.ValidationError):
			queries.normalize_filters({"status": "nonsense"})

	# 27. large / paginated lists -------------------------------------------------------------------------
	def make_cards(self, n):
		plan, wo = self.ops_plan(n + 80, submit_work_orders=False)  # draft Work Order: no cards yet
		doc = frappe.get_doc("Work Order", wo)
		op = doc.operations[0]
		base = add_days(now_datetime(), 2).replace(microsecond=0)
		for i in range(n):
			frappe.get_doc({
				"doctype": "Job Card", "work_order": wo, "operation": op.operation, "workstation": op.workstation,
				"for_quantity": 1, "operation_id": op.name, "sequence_id": 1, "bom_no": doc.bom_no, "company": doc.company,
				"production_item": doc.production_item, "posting_date": frappe.utils.nowdate(),
				"ppc_production_plan": plan.name,
				"expected_start_date": base + timedelta(hours=i), "expected_end_date": base + timedelta(hours=i, minutes=30),
			}).insert()
		return plan

	def test_large_list_is_paginated_sorted_and_never_loaded_at_once(self):
		plan = self.make_cards(120)
		first = api.get_job_cards({"plan": plan.name}, page=1, page_length=50)
		self.assertEqual((first["total"], first["pages"], len(first["rows"])), (120, 3, 50))
		last = api.get_job_cards({"plan": plan.name}, page=3, page_length=50)
		self.assertEqual(len(last["rows"]), 20)

		seen = []
		for page in (1, 2, 3):
			seen += [r["name"] for r in api.get_job_cards({"plan": plan.name}, page=page, page_length=50)["rows"]]
		self.assertEqual(len(seen), 120)
		self.assertEqual(len(set(seen)), 120, "no card repeats or is skipped across pages")

		starts = [r["revised_start"] for r in api.get_job_cards({"plan": plan.name}, page_length=200)["rows"]]
		self.assertEqual(starts, sorted(starts))
		desc = [r["revised_start"] for r in api.get_job_cards({"plan": plan.name}, page_length=200, sort_order="desc")["rows"]]
		self.assertEqual(desc, sorted(starts, reverse=True))
		self.assertEqual(api.get_job_cards({"plan": plan.name}, page_length=100000)["page_length"], queries.MAX_PAGE)
		self.assertEqual(api.get_job_cards({"plan": plan.name}, page=0, page_length=0)["page"], 1)
		beyond = api.get_job_cards({"plan": plan.name}, page=99, page_length=50)
		self.assertEqual((beyond["rows"], beyond["total"]), ([], 120))

		by_qty = api.get_job_cards({"plan": plan.name}, sort_by="planned_qty", page_length=5)
		self.assertEqual(len(by_qty["rows"]), 5)

	def test_gantt_respects_the_cap_and_reports_truncation(self):
		plan = self.make_cards(60)
		res = api.get_gantt_data({"plan": plan.name}, limit=50)
		self.assertEqual(len(res["tasks"]), 50)
		self.assertTrue(res["truncated"])
		self.assertEqual(res["total"], 60)
		self.assertFalse(api.get_gantt_data({"plan": plan.name}, limit=500)["truncated"])

	# 18. gantt data ---------------------------------------------------------------------------------------
	def test_gantt_data_is_filtered_and_has_dependencies(self):
		res = api.get_gantt_data({"plan": self.plan_a.name})
		tasks = {t["name"]: t for t in res["tasks"]}
		self.assertEqual(set(tasks), {c.name for c in self.cards_a})
		cut, asm = self.cards_a
		self.assertEqual(res["links"], [{"from": cut.name, "to": asm.name}])
		t = tasks[cut.name]
		for key in ("revised_start", "revised_end", "original_start", "work_order", "sales_order", "operation", "workstation",
			"item_code", "status", "planned_qty", "movable", "conflict", "progress"):
			self.assertIn(key, t)
		self.assertTrue(t["movable"])
		self.assertFalse(t["is_estimated"])
		self.assertEqual(res["range"]["start"], t["revised_start"])

		both = {x["name"] for p in (self.plan_a.name, self.plan_b.name) for x in api.get_gantt_data({"plan": p})["tasks"]}
		self.assertTrue({c.name for c in self.cards_a + self.cards_b} <= both)
		only_ws2 = api.get_gantt_data({"plan": self.plan_a.name, "workstation": self.ws2})
		self.assertEqual([x["name"] for x in only_ws2["tasks"]], [asm.name])
		self.assertEqual(only_ws2["links"], [], "a link needs both ends in the result")

	def test_gantt_marks_locked_and_estimated_cards(self):
		cut, asm = self.cards_a
		self.complete_job_card(cut.name)
		from ppc_production_control.services import job_cards as jc_service
		jc_service.submit_selected_job_cards(self.plan_a.name, [cut.name])
		for tbl in ("Job Card Scheduled Time",):
			frappe.db.delete(tbl, {"parent": asm.name})
		frappe.db.set_value("Job Card", asm.name, {"expected_start_date": None, "expected_end_date": None})
		frappe.db.set_value("Work Order Operation", frappe.db.get_value("Job Card", asm.name, "operation_id"),
			{"planned_start_time": None, "planned_end_time": None})

		tasks = {t["name"]: t for t in api.get_gantt_data({"plan": self.plan_a.name})["tasks"]}
		self.assertFalse(tasks[cut.name]["movable"])
		self.assertEqual(tasks[cut.name]["lock_reason"], "Submitted")
		self.assertTrue(tasks[asm.name]["is_estimated"], "unscheduled card gets a display-only estimate")
		self.assertTrue(tasks[asm.name]["revised_start"] < tasks[asm.name]["revised_end"])
		self.assertIsNone(frappe.db.get_value("Job Card", asm.name, "expected_start_date"), "estimates are never written")

	# 13. dashboard -----------------------------------------------------------------------------------------------
	def test_dashboard_kpis_and_filters(self):
		kpi = api.get_ppc_dashboard({"plan": self.plan_a.name})["kpis"]
		self.assertEqual(kpi["total_ppc_plans"], 1)
		self.assertEqual(kpi["total_sales_orders"], 1)
		self.assertEqual(kpi["pending_production_qty"], 10)
		self.assertEqual((kpi["work_orders_created"], kpi["draft_work_orders"], kpi["open_work_orders"]), (1, 0, 1))
		self.assertEqual(kpi["total_job_cards"], 2)
		self.assertEqual((kpi["draft_job_cards"], kpi["open_job_cards"], kpi["work_in_progress"], kpi["completed_job_cards"]), (2, 2, 0, 0))
		self.assertEqual((kpi["overdue_operations"], kpi["unassigned_operations"], kpi["scheduling_conflicts"]), (0, 0, 0))

		kpi_b = api.get_ppc_dashboard({"plan": self.plan_b.name})["kpis"]
		self.assertEqual(kpi_b["pending_production_qty"], 7)  # 4 + 3
		self.assertEqual(kpi_b["work_orders_created"], 2)
		self.assertEqual(kpi_b["total_job_cards"], 2)

		both = api.get_ppc_dashboard({"priority": "Urgent"})["kpis"]
		self.assertEqual(both["total_ppc_plans"], 1)
		self.assertEqual(both["total_job_cards"], 2)

		# a started card moves between buckets, never double counts
		self.complete_job_card(self.cards_a[0].name)
		kpi = api.get_ppc_dashboard({"plan": self.plan_a.name})["kpis"]
		self.assertEqual((kpi["total_job_cards"], kpi["work_in_progress"], kpi["open_job_cards"]), (2, 1, 1))

	def test_dashboard_pending_quantity_follows_production(self):
		# ERPNext keeps produced qty on the plan's item row; what is still owed is planned - produced
		frappe.db.set_value("Production Plan Item", self.plan_a.po_items[0].name, "produced_qty", 4)
		self.assertEqual(api.get_ppc_dashboard({"plan": self.plan_a.name})["kpis"]["pending_production_qty"], 6)

	def test_dashboard_cancelled_work_order_is_not_counted(self):
		frappe.db.set_value("Work Order", self.wo_a, "docstatus", 2)
		kpi = api.get_ppc_dashboard({"plan": self.plan_a.name})["kpis"]
		self.assertEqual(kpi["work_orders_created"], 0)
		self.assertEqual(kpi["pending_production_qty"], 10, "the plan row still owes its planned quantity")

	def test_workload_by_workstation_and_schedule_overview(self):
		res = api.get_ppc_dashboard({"plan": self.plan_a.name})
		by_ws = {w["workstation"]: w for w in res["workload"]}
		self.assertEqual(set(by_ws), {self.ws1, self.ws2})
		self.assertEqual(by_ws[self.ws1]["job_cards"], 1)
		self.assertEqual(by_ws[self.ws1]["scheduled_hours"], 5.0)   # 300 min
		self.assertEqual(by_ws[self.ws2]["scheduled_hours"], 10.0)  # 600 min
		self.assertEqual(by_ws[self.ws1]["capacity"], 1)
		self.assertEqual(sum(s["job_cards"] for s in res["schedule"]), 2)

	def test_work_order_list(self):
		res = api.get_work_orders({"plan": self.plan_b.name})
		self.assertEqual(res["total"], 2)
		by_item = {r["item_code"]: r for r in res["rows"]}
		self.assertEqual(by_item[self.fg_ops]["job_cards"], 2)
		self.assertEqual(by_item[self.fg_plain]["job_cards"], 0)
		self.assertFalse(by_item[self.fg_ops]["can_submit"])
		self.assertEqual(api.get_work_orders({"plan": self.plan_b.name, "item_code": self.fg_plain})["total"], 1)
		self.assertEqual(api.get_work_orders({"plan": self.plan_b.name, "component_item": self.rm2})["total"], 1)

	def test_audit_log_endpoint(self):
		rows = api.get_audit_log(plan=self.plan_a.name)["rows"]
		actions = {r["action"] for r in rows}
		self.assertTrue({"Create Work Order", "Submit Work Order"} <= actions)
		self.assertEqual(api.get_audit_log(plan=self.plan_a.name, result="Failed")["rows"], [])
