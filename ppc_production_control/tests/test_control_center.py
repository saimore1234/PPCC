"""Read models and pre-checks behind the PPC Control Center pages."""

from datetime import datetime, time, timedelta

import frappe
from frappe.utils import add_days, getdate, now_datetime, nowdate

from ppc_production_control import api
from ppc_production_control.services import audit, control_center, queries, scheduling
from ppc_production_control.services.constants import A_CREATE_WO, R_FAILED
from ppc_production_control.tests.test_api_security import ERP_MFG, make_user
from ppc_production_control.tests.utils import PPCTestCase


class ControlCenterCase(PPCTestCase):
	def at(self, days: int, hour: int, minute: int = 0) -> datetime:
		return datetime.combine(getdate(add_days(nowdate(), days)), time(hour, minute))

	def place(self, job_card: str, start: datetime, end: datetime):
		"""Give a Job Card an exact schedule (ERPNext derives expected_start/end from the scheduled rows)."""
		doc = frappe.get_doc("Job Card", job_card)
		doc.scheduled_time_logs = []
		doc.append("scheduled_time_logs", {"from_time": start, "to_time": end, "time_in_mins": (end - start).total_seconds() / 60})
		doc.save()

	def cards(self, wo):
		cut, asm = self.job_cards(wo)
		return cut.name, asm.name

	def card_names(self, **filters):
		return {r["name"] for r in queries.get_job_cards(filters, 1, 200)["rows"]}


# =============================================================================================
class TestPlansList(ControlCenterCase):
	def test_row_carries_real_counts_stage_and_links(self):
		plan, wo = self.ops_plan(10)
		res = control_center.get_plans({"plan": plan.name})
		self.assertEqual(res["total"], 1)
		r = res["rows"][0]
		self.assertEqual((r.work_orders, r.draft_work_orders, r.job_cards, r.planned_qty, r.fg_items), (1, 0, 2, 10, 1))
		self.assertEqual((r.stage, r.status, r.company), ("In production", plan.status, self.company))
		self.assertEqual((r.sales_orders, r.sales_order_count), ([plan.sales_orders[0].sales_order], 1))
		self.assertEqual(r.planner, "Administrator")
		self.assertEqual(str(r.posting_date), nowdate())

	def test_stage_follows_the_flow(self):
		drafted, _wo = self.ops_plan(10, submit_work_orders=False)
		r = control_center.get_plans({"plan": drafted.name})["rows"][0]
		self.assertEqual((r.stage, r.draft_work_orders, r.job_cards), ("Work Orders drafted", 1, 0))

		so = self.new_so((self.fg_ops, 5))
		manual = self.new_plan(so.name, auto_create=False)
		self.assertEqual(control_center.get_plans({"plan": manual.name})["rows"][0].stage, "Draft")
		manual.submit()
		self.assertEqual(control_center.get_plans({"plan": manual.name})["rows"][0].stage, "Awaiting Work Orders")

		audit.log(A_CREATE_WO, R_FAILED, plan=manual.name, error="boom")
		self.assertEqual(control_center.get_plans({"plan": manual.name})["rows"][0].stage, "Needs attention")

	def test_filters(self):
		p1, _w1 = self.ops_plan(10)
		so = self.new_so((self.fg_ops, 5))
		p2 = self.new_plan(so.name, auto_create=False)  # a draft plan
		both = {"customer": self.customer}
		self.assertEqual({r.name for r in control_center.get_plans(both)["rows"]}, {p1.name, p2.name})
		self.assertEqual({r.name for r in control_center.get_plans({**both, "search": p1.name[-5:]})["rows"]}, {p1.name})
		so1 = p1.sales_orders[0].sales_order
		fragment = next(so1[i:i + n] for n in range(3, len(so1) + 1) for i in range(len(so1) - n + 1) if so1[i:i + n] not in so.name)
		self.assertEqual({r.name for r in control_center.get_plans({**both, "sales_order_like": fragment})["rows"]}, {p1.name})
		self.assertEqual({r.name for r in control_center.get_plans({**both, "sales_order": so.name})["rows"]}, {p2.name})
		self.assertEqual({r.name for r in control_center.get_plans({**both, "plan_status": "Draft"})["rows"]}, {p2.name})
		self.assertEqual(control_center.get_plans({**both, "company": "No such company"})["total"], 0)
		self.assertEqual(control_center.get_plans({**both, "posting_from": add_days(nowdate(), 1)})["total"], 0)
		self.assertEqual(control_center.get_plans({**both, "posting_from": nowdate(), "posting_to": nowdate()})["total"], 2)
		# delivery date comes from the Sales Orders (10 days out in the fixtures)
		self.assertEqual(control_center.get_plans({**both, "delivery_from": add_days(nowdate(), 9), "delivery_to": add_days(nowdate(), 11)})["total"], 2)
		self.assertEqual(control_center.get_plans({**both, "delivery_to": add_days(nowdate(), 5)})["total"], 0)

	def test_sorting_and_paging(self):
		p1, _w = self.ops_plan(10)
		so = self.new_so((self.fg_ops, 5))
		p2 = self.new_plan(so.name, auto_create=False)
		f = {"customer": self.customer}
		asc = [r.name for r in control_center.get_plans(f, 1, 50, "name", "asc")["rows"]]
		self.assertEqual(asc, sorted([p1.name, p2.name]))
		first = control_center.get_plans(f, 1, 1, "name", "desc")
		second = control_center.get_plans(f, 2, 1, "name", "desc")
		self.assertEqual((first["total"], first["pages"], second["page"]), (2, 2, 2))
		self.assertEqual([first["rows"][0].name, second["rows"][0].name], sorted([p1.name, p2.name], reverse=True))
		by_wo = control_center.get_plans(f, 1, 50, "work_orders", "desc")["rows"]
		self.assertEqual(by_wo[0].name, p1.name)
		# an unknown sort column falls back instead of reaching SQL
		self.assertEqual(control_center.get_plans(f, 1, 50, "name; drop table x", "asc")["total"], 2)

	def test_invalid_filter_values_are_refused(self):
		for bad in ({"plan_status": "Bogus"}, {"sync_status": "Bogus"}, {"wo_status": "Bogus"}, {"posting_from": "not a date"}):
			with self.assertRaises(frappe.ValidationError):
				control_center.get_plans(bad)


# =============================================================================================
class TestFiltersAndStates(ControlCenterCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.cards(self.wo)

	def state_of(self, name):
		return next(r for r in queries.get_job_cards({"plan": self.plan.name}, 1, 50)["rows"] if r["name"] == name)["sync_status"]

	def test_sql_filter_and_python_badge_agree_for_every_state(self):
		frappe.db.set_value("Job Card", self.cut, "ppc_schedule_sync_status", "Failed")
		self.assertTrue(scheduling.reschedule_job_card(self.asm, self.at(9, 8), self.at(9, 18), confirmed=True, override_conflicts=True)["ok"])
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="Failed"), {self.cut})
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="Rescheduled"), {self.asm})
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="Synced"), set())
		self.assertEqual((self.state_of(self.cut), self.state_of(self.asm)), ("Failed", "Rescheduled"))

		frappe.db.set_value("Job Card", self.cut, "ppc_schedule_sync_status", "Synced")
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="Synced"), {self.cut})
		self.assertEqual(self.state_of(self.cut), "Synced")

		frappe.db.set_value("Job Card", self.cut, "expected_start_date", self.at(5, 7))  # moved behind PPC's back
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="Drift"), {self.cut})
		self.assertEqual(self.state_of(self.cut), "Drift")

		frappe.db.set_value("Job Card", self.asm, {"ppc_original_start": None, "ppc_original_end": None})
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="No baseline"), {self.asm})
		self.assertEqual(self.state_of(self.asm), "No baseline")

		frappe.db.set_value("Job Card", self.cut, {"expected_start_date": None, "expected_end_date": None})
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="Estimated"), {self.cut})

	def test_sub_second_noise_is_not_a_difference(self):
		start = frappe.db.get_value("Job Card", self.cut, "expected_start_date")
		frappe.db.set_value("Job Card", self.cut, "ppc_original_start", start + timedelta(microseconds=400000))
		self.assertEqual(self.card_names(plan=self.plan.name, sync_status="Synced"), {self.cut, self.asm})

	def test_schedule_window_means_overlap(self):
		self.place(self.cut, self.at(3, 8), self.at(3, 12))
		wf = lambda **kw: self.card_names(plan=self.plan.name, **kw) & {self.cut}  # noqa: E731
		self.assertEqual(wf(window_from=add_days(nowdate(), 3), window_to=add_days(nowdate(), 3)), {self.cut})
		self.assertEqual(wf(window_from=add_days(nowdate(), 2), window_to=add_days(nowdate(), 3)), {self.cut}, "ending/starting on the edge counts")
		self.assertEqual(wf(window_from=add_days(nowdate(), 4)), set(), "window starts after the card ended")
		self.assertEqual(wf(window_to=add_days(nowdate(), 2)), set(), "window ends before the card starts")
		self.assertEqual(wf(window_from=add_days(nowdate(), 3), window_to=add_days(nowdate(), 30)), {self.cut})

	def test_work_order_filters_columns_sorting(self):
		rows = queries.get_work_orders({"plan": self.plan.name})["rows"]
		r = rows[0]
		self.assertEqual((r.name, r.bom_no, r.customer, r.job_cards), (self.wo, frappe.db.get_value("Work Order", self.wo, "bom_no"), self.customer, 2))
		self.assertTrue(r.fg_warehouse or r.wip_warehouse)
		self.assertEqual(r.sync_status, "Synced")
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "wo_status": "Draft"})["total"], 0)
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "wo_status": "Not Started"})["total"], 1)
		fg = frappe.db.get_value("Work Order", self.wo, "fg_warehouse")
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "warehouse": fg})["total"], 1)
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "warehouse": "No such warehouse"})["total"], 0)
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "search": self.customer})["total"], 1)
		frappe.db.set_value("Job Card", self.asm, "ppc_schedule_sync_status", "Failed")
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name})["rows"][0].sync_status, "Failed")
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name}, 1, 10, "qty; select 1", "desc")["total"], 1)

	def test_open_status_and_schedule_window_on_the_work_order_list(self):
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "wo_status": "Open"})["total"], 1)
		drafts, _wo = self.ops_plan(5, submit_work_orders=False)
		self.assertEqual(queries.get_work_orders({"plan": drafts.name, "wo_status": "Open"})["total"], 0, "drafts are not 'open'")
		start = frappe.db.get_value("Work Order", self.wo, "planned_start_date")
		end = frappe.db.get_value("Work Order", self.wo, "planned_end_date")
		inside = {"plan": self.plan.name, "window_from": str(getdate(start)), "window_to": str(getdate(end))}
		self.assertEqual(queries.get_work_orders(inside)["total"], 1)
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "window_from": str(add_days(getdate(end), 1))})["total"], 0)
		self.assertEqual(queries.get_work_orders({"plan": self.plan.name, "window_to": str(add_days(getdate(start), -1))})["total"], 0)

	def test_draft_work_order_has_no_sync_label(self):
		plan, _wo = self.ops_plan(5, submit_work_orders=False)
		r = queries.get_work_orders({"plan": plan.name})["rows"][0]
		self.assertEqual((r.status, r.can_submit, r.sync_status, r.job_cards), ("Draft", True, None, 0))
		self.assertEqual(queries.get_work_orders({"plan": plan.name, "wo_status": "Draft"})["total"], 1)


# =============================================================================================
class TestOverview(ControlCenterCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.cards(self.wo)

	def overview(self, **f):
		return control_center.get_overview({"plan": self.plan.name, **f})

	def test_kpis_are_the_real_counts(self):
		k = self.overview()["kpis"]
		self.assertEqual(
			k,
			{"total_plans": 1, "pending_qty": 10, "draft_work_orders": 0, "open_work_orders": 1, "total_job_cards": 2, "work_in_progress": 0,
				"completed_job_cards": 0, "overdue_operations": 0, "scheduling_conflicts": 0, "failed_syncs": 0},
		)
		self.assertEqual(set(k), {"total_plans", "pending_qty", "draft_work_orders", "open_work_orders", "total_job_cards", "work_in_progress",
			"completed_job_cards", "overdue_operations", "scheduling_conflicts", "failed_syncs"}, "ten concise KPIs")

	def test_each_kpi_moves_with_the_data_and_matches_its_list(self):
		frappe.db.set_value("Job Card", self.cut, "ppc_schedule_sync_status", "Failed")
		frappe.db.set_value("Job Card", self.asm, {"expected_end_date": self.at(-2, 10)})  # long past -> overdue
		frappe.db.set_value("Job Card", self.asm, "expected_start_date", self.at(-3, 8))
		o = self.overview()
		k = o["kpis"]
		self.assertEqual((k["failed_syncs"], k["overdue_operations"]), (1, 1))
		self.assertEqual([r["name"] for r in o["failed"]["rows"]], [self.cut])
		self.assertEqual([r["name"] for r in o["overdue"]["rows"]], [self.asm])
		# "each KPI opens the corresponding filtered list": the list for the same filters has exactly that many rows
		self.assertEqual(queries.get_job_cards({"plan": self.plan.name, "overdue_only": 1})["total"], k["overdue_operations"])
		self.assertEqual(queries.get_job_cards({"plan": self.plan.name, "sync_status": "Failed"})["total"], k["failed_syncs"])

	def test_conflicts_are_counted(self):
		frappe.db.set_value("Job Card", self.asm, "expected_start_date", frappe.db.get_value("Job Card", self.cut, "expected_start_date"))
		self.assertGreaterEqual(self.overview()["kpis"]["scheduling_conflicts"], 1)

	def test_date_range_filters_every_part(self):
		far = {"date_from": add_days(nowdate(), 300), "date_to": add_days(nowdate(), 330)}
		k = self.overview(**far)["kpis"]
		self.assertEqual((k["total_plans"], k["total_job_cards"], k["open_work_orders"]), (0, 0, 0))
		near = {"date_from": add_days(nowdate(), -1), "date_to": add_days(nowdate(), 60)}
		k = self.overview(**near)["kpis"]
		self.assertEqual((k["total_plans"], k["total_job_cards"], k["open_work_orders"]), (1, 2, 1))

	def test_filters_apply(self):
		self.assertEqual(control_center.get_overview({"plan": self.plan.name, "company": "No such company"})["kpis"]["total_job_cards"], 0)
		self.assertEqual(control_center.get_overview({"plan": self.plan.name, "planner": "Administrator"})["kpis"]["total_job_cards"], 2)
		self.assertEqual(control_center.get_overview({"plan": self.plan.name, "planner": "someone.else@example.com"})["kpis"]["total_job_cards"], 0)

	def test_delivery_risks(self):
		risks = {r["work_order"]: r for r in self.overview()["delivery_risks"]}
		self.assertEqual(risks[self.wo]["risk"], "Due soon")  # delivery is 10 days out in the fixtures
		delivery = getdate(frappe.db.get_value("Work Order", self.wo, "expected_delivery_date"))
		frappe.db.set_value("Work Order", self.wo, "planned_end_date", datetime.combine(add_days(delivery, 2), time(9)))
		self.assertEqual({r["work_order"]: r for r in self.overview()["delivery_risks"]}[self.wo]["risk"], "Planned after delivery")
		frappe.db.set_value("Work Order", self.wo, "expected_delivery_date", add_days(nowdate(), -3))
		self.assertEqual({r["work_order"]: r for r in self.overview()["delivery_risks"]}[self.wo]["risk"], "Overdue")
		frappe.db.set_value("Work Order", self.wo, "status", "Completed")
		self.assertNotIn(self.wo, [r["work_order"] for r in self.overview()["delivery_risks"]], "finished work is no risk")

	def test_recent_revisions_and_workload_panels(self):
		self.assertEqual(self.overview()["recent_revisions"], [])
		self.assertTrue(scheduling.reschedule_job_card(self.asm, self.at(9, 8), self.at(9, 18), confirmed=True, override_conflicts=True, reason="Dashboard test")["ok"])
		rev = self.overview()["recent_revisions"]
		self.assertEqual([(r.job_card, r.revision_no, r.change_reason) for r in rev], [(self.asm, 1, "Dashboard test")])
		self.assertTrue(self.overview()["workload"])
		self.assertTrue(self.overview()["as_of"])


# =============================================================================================
class TestWorkload(ControlCenterCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.cards(self.wo)
		self.plan2, self.wo2 = self.ops_plan(10)
		self.cut2, self.asm2 = self.cards(self.wo2)
		self.day = add_days(nowdate(), 3)

	def working_hours(self, workstation, capacity=1, hours=8, holiday_list=None):
		ws = frappe.get_doc("Workstation", workstation)
		ws.working_hours = []
		row = {"start_time": "08:00:00", "end_time": f"{8 + hours:02d}:00:00"}
		if frappe.get_meta("Workstation Working Hour").has_field("hours"):
			row["hours"] = hours
		if frappe.get_meta("Workstation Working Hour").has_field("enabled"):
			row["enabled"] = 1
		ws.append("working_hours", row)
		ws.production_capacity = capacity
		if holiday_list:
			ws.holiday_list = holiday_list
		ws.save(ignore_permissions=True)

	def row(self, res, ws):
		return next(r for r in res["rows"] if r["workstation"] == ws)

	def load(self, **kw):
		return control_center.get_workload({"customer": self.customer}, self.day, kw.pop("to", self.day), **kw)

	def test_known_hours_utilisation_and_overload(self):
		self.working_hours(self.ws1)
		self.place(self.cut, self.at(3, 8), self.at(3, 12))   # 4 h
		self.place(self.cut2, self.at(3, 10), self.at(3, 15))  # 5 h on the same single-capacity workstation
		r = self.row(self.load(), self.ws1)
		self.assertEqual((r["available_known"], r["available_hours"], r["scheduled_hours"], r["job_cards"]), (True, 8.0, 9.0, 2))
		self.assertEqual((r["remaining_hours"], r["utilization"], r["overloaded_days"]), (0.0, 112.5, 1))
		self.assertEqual(r["periods"], [{"from": self.day, "to": self.day, "peak_hours": 9.0}])
		self.assertEqual((r["daily"], r["daily_available"]), ([9.0], [8.0]))

	def test_capacity_multiplies_available_hours(self):
		self.working_hours(self.ws1, capacity=2)
		self.place(self.cut, self.at(3, 8), self.at(3, 12))
		self.place(self.cut2, self.at(3, 10), self.at(3, 15))
		r = self.row(self.load(), self.ws1)
		self.assertEqual((r["available_hours"], r["scheduled_hours"], r["overloaded_days"], r["remaining_hours"]), (16.0, 9.0, 0, 7.0))

	def test_unknown_working_hours_are_reported_as_unavailable_not_invented(self):
		self.place(self.asm, self.at(3, 8), self.at(3, 11))
		res = self.load()
		r = self.row(res, self.ws2)
		self.assertEqual((r["available_known"], r["available_hours"], r["remaining_hours"], r["utilization"], r["periods"]), (False, None, None, None, []))
		self.assertEqual(r["scheduled_hours"], 3.0)
		self.assertTrue(res["notes"])

	def test_holiday_means_no_availability(self):
		holiday = frappe.get_doc({"doctype": "Holiday List", "holiday_list_name": "PPCT Holidays", "from_date": add_days(nowdate(), -5), "to_date": add_days(nowdate(), 40),
			"holidays": [{"holiday_date": self.day, "description": "test"}]}).insert(ignore_permissions=True)
		self.working_hours(self.ws1, holiday_list=holiday.name)
		self.place(self.cut, self.at(3, 8), self.at(3, 10))
		r = self.row(self.load(), self.ws1)
		self.assertEqual((r["available_hours"], r["scheduled_hours"], r["overloaded_days"]), (0.0, 2.0, 1))

	def test_work_spanning_midnight_is_split_between_days(self):
		self.working_hours(self.ws1)
		self.place(self.cut, self.at(3, 22), self.at(4, 3))  # 2 h on day 3, 3 h on day 4
		res = control_center.get_workload({"customer": self.customer}, self.day, add_days(self.day, 1))
		r = self.row(res, self.ws1)
		self.assertEqual(r["daily"], [2.0, 3.0])
		self.assertEqual(res["days"], [self.day, str(add_days(self.day, 1))])

	def test_unassigned_operations_idle_stations_and_window(self):
		frappe.db.set_value("Job Card", self.asm, "workstation", None)
		self.assertEqual(self.load()["unassigned_operations"], 1)
		rows = [r["workstation"] for r in self.load(include_idle=True)["rows"]]
		self.assertIn(self.ws1, rows)
		only = control_center.get_workload({"customer": self.customer, "workstation": self.ws1}, self.day, self.day)
		self.assertEqual([r["workstation"] for r in only["rows"]], [self.ws1], "a workstation filter shows just that station")
		huge = control_center.get_workload({"customer": self.customer}, self.day, add_days(self.day, 500))
		self.assertEqual(len(huge["days"]), 92, "the window is capped")

	def test_finished_work_is_not_load(self):
		self.place(self.cut, self.at(3, 8), self.at(3, 12))
		frappe.db.set_value("Job Card", self.cut, "status", "Completed")
		self.assertEqual(self.row(self.load(), self.ws1)["scheduled_hours"], 0.0)


# =============================================================================================
class TestRevisionLog(ControlCenterCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.cards(self.wo)
		self.assertTrue(scheduling.reschedule_job_card(self.asm, self.at(9, 8), self.at(9, 18), confirmed=True, override_conflicts=True, reason="Log test", source="Date Dialog")["ok"])

	def test_rows_show_previous_new_reason_user_and_sync(self):
		res = control_center.get_revision_log({"plan": self.plan.name})
		self.assertEqual(res["total"], 1)
		r = res["rows"][0]
		self.assertEqual((r.job_card, r.work_order, r.revision_no, r.change_reason, r.changed_by, r.sync_status, r.change_source),
			(self.asm, self.wo, 1, "Log test", "Administrator", "Synced", "Date Dialog"))
		self.assertTrue(r.previous_start and r.previous_end and r.accepted_start and r.accepted_end and r.changed_at)
		self.assertEqual(r.accepted_start[:16], str(self.at(9, 8))[:16])
		self.assertEqual(r.note, "Log test")

	def test_baselines_are_available_on_request(self):
		self.assertEqual(control_center.get_revision_log({"plan": self.plan.name, "revision_type": "Baseline"})["total"], 2)
		self.assertEqual(control_center.get_revision_log({"plan": self.plan.name, "revision_type": "All"})["total"], 3)

	def test_filters_sorting_paging(self):
		f = {"plan": self.plan.name, "revision_type": "All"}
		self.assertEqual(control_center.get_revision_log({**f, "job_card": self.cut})["total"], 1)
		self.assertEqual(control_center.get_revision_log({**f, "source": "Date Dialog"})["total"], 1)
		self.assertEqual(control_center.get_revision_log({**f, "user": "nobody@example.com"})["total"], 0)
		self.assertEqual(control_center.get_revision_log({**f, "search": "Log test"})["total"], 1)
		self.assertEqual(control_center.get_revision_log({**f, "from": nowdate(), "to": nowdate()})["total"], 3)
		self.assertEqual(control_center.get_revision_log({**f, "from": add_days(nowdate(), 1)})["total"], 0)
		self.assertEqual(control_center.get_revision_log({**f, "to": add_days(nowdate(), -1)})["total"], 0)
		page1 = control_center.get_revision_log(f, 1, 2, "revision_no", "desc")
		self.assertEqual((page1["pages"], len(page1["rows"]), page1["rows"][0].revision_no), (2, 2, 1))
		self.assertEqual(control_center.get_revision_log(f, 1, 50, "bogus; drop", "asc")["total"], 3)

	def test_log_respects_permissions(self):
		nobody = make_user("ccnoperm", [])
		frappe.set_user(nobody)
		with self.assertRaises(frappe.PermissionError):
			control_center.get_revision_log({})


# =============================================================================================
class TestExceptions(ControlCenterCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.cards(self.wo)

	def found(self, **kw):
		res = control_center.get_exceptions({"plan": self.plan.name}, **kw)
		return res, {(r["type"], r["job_card"]) for r in res["rows"]}

	def test_a_clean_plan_has_no_exceptions(self):
		res, items = self.found()
		self.assertEqual((res["total"], items), (0, set()))
		self.assertEqual(set(res["counts"]), set(control_center.EXCEPTION_TYPES))

	def test_every_exception_type(self):
		frappe.db.set_value("Job Card", self.cut, "ppc_schedule_sync_status", "Failed")                 # failed sync
		frappe.db.set_value("Job Card", self.asm, {"ppc_original_start": None, "ppc_original_end": None})  # missing baseline (+ history)
		_res, items = self.found()
		self.assertIn(("failed_sync", self.cut), items)
		self.assertTrue({"missing_baseline", "revision_mismatch"} & {t for t, _j in items} or ("missing_history", self.asm) in items)
		frappe.db.set_value("Job Card", self.asm, "expected_start_date", frappe.db.get_value("Job Card", self.cut, "expected_start_date"))
		_res, items = self.found()
		self.assertIn(("dependency_conflict", self.asm), items)
		self.assertIn(("schedule_drift", self.asm), items)
		frappe.db.set_value("Job Card", self.cut, {"expected_start_date": self.at(-4, 8), "expected_end_date": self.at(-3, 8), "ppc_revised_start": self.at(-4, 8), "ppc_revised_end": self.at(-3, 8)})
		_res, items = self.found()
		self.assertIn(("overdue_operation", self.cut), items)
		frappe.db.set_value("Job Card", self.asm, "work_order", "WO-DOES-NOT-EXIST")
		_res, items = self.found()
		self.assertIn(("orphan_work_order", self.asm), items)

	def test_workstation_conflict(self):
		plan2, wo2 = self.ops_plan(10)
		cut2 = self.cards(wo2)[0]
		self.place(self.cut, self.at(5, 8), self.at(5, 12))
		self.place(cut2, self.at(5, 10), self.at(5, 14))  # same single-capacity workstation, overlapping
		res = control_center.get_exceptions({"customer": self.customer}, types=["workstation_conflict"])
		self.assertEqual({r["job_card"] for r in res["rows"]}, {self.cut, cut2})
		self.assertTrue(all(r["workstation"] == self.ws1 and r["message"] for r in res["rows"]))

	def test_type_filter_counts_search_paging_and_order(self):
		frappe.db.set_value("Job Card", self.cut, "ppc_schedule_sync_status", "Failed")
		frappe.db.set_value("Job Card", self.asm, "expected_start_date", frappe.db.get_value("Job Card", self.cut, "expected_start_date"))
		everything, _items = self.found()
		self.assertEqual(everything["rows"][0]["severity"], "high", "most serious first")
		self.assertEqual(everything["counts"]["failed_sync"], 1)
		only = control_center.get_exceptions({"plan": self.plan.name}, types=["failed_sync"])
		self.assertEqual([r["type"] for r in only["rows"]], ["failed_sync"])
		self.assertEqual(only["counts"], everything["counts"], "chips show totals even when a type is selected")
		self.assertEqual(control_center.get_exceptions({"plan": self.plan.name, "search": self.asm})["total"], len([r for r in everything["rows"] if r["job_card"] == self.asm]))
		self.assertEqual(control_center.get_exceptions({"plan": self.plan.name, "job_card": self.cut})["total"], 1)
		paged = control_center.get_exceptions({"plan": self.plan.name}, None, 1, 1)
		self.assertEqual((len(paged["rows"]), paged["pages"]), (1, everything["total"]))
		self.assertEqual(control_center.get_exceptions({"plan": self.plan.name}, ["not a type"])["total"], everything["total"])

	def test_consistency_findings_carry_repair_hints(self):
		frappe.db.set_value("Job Card", self.cut, "expected_start_date", self.at(6, 8))
		res = control_center.get_exceptions({"plan": self.plan.name}, ["schedule_drift"])
		row = res["rows"][0]
		self.assertTrue(row["repairable"] and row["action"] and row["plan"] == self.plan.name and row["operation"])


# =============================================================================================
class TestPrecheck(ControlCenterCase):
	def test_valid_drafts_pass_and_nothing_is_changed(self):
		plan, wo = self.ops_plan(10, submit_work_orders=False)
		before = frappe.db.get_value("Work Order", wo, ["modified", "docstatus", "qty", "status"])
		res = control_center.precheck_work_orders([wo], plan.name)
		self.assertEqual((res["ok_count"], res["blocked_count"]), (1, 0), res)
		self.assertEqual(res["results"][0]["blockers"], [])
		self.assertEqual(frappe.db.get_value("Work Order", wo, ["modified", "docstatus", "qty", "status"]), before)
		self.assertEqual(frappe.db.count("Job Card", {"work_order": wo}), 0, "a pre-check never creates Job Cards")

	def test_blockers_are_listed_per_work_order(self):
		plan, wo = self.ops_plan(10, submit_work_orders=False)
		plan2, wo2 = self.ops_plan(10, submit_work_orders=False)
		frappe.db.set_value("Work Order", wo, "qty", 0)
		res = control_center.precheck_work_orders([wo, wo2, "WO-NOPE", wo])
		by = {r["name"]: r for r in res["results"]}
		self.assertEqual(len(res["results"]), 3, "duplicates are collapsed")
		self.assertFalse(by[wo]["ok"])
		self.assertTrue(by[wo]["blockers"])
		self.assertTrue(by[wo2]["ok"])
		self.assertFalse(by["WO-NOPE"]["ok"])
		self.assertEqual((res["ok_count"], res["blocked_count"]), (1, 2))

	def test_already_submitted_and_other_plan(self):
		plan, wo = self.ops_plan(10)
		other, other_wo = self.ops_plan(10, submit_work_orders=False)
		self.assertIn("submitted", control_center.precheck_work_orders([wo])["results"][0]["blockers"][0])
		blocked = control_center.precheck_work_orders([other_wo], plan.name)["results"][0]
		self.assertFalse(blocked["ok"])
		self.assertIn("different Production Plan", blocked["blockers"][0])

	def test_mandatory_fields_are_reported(self):
		plan, wo = self.ops_plan(10, submit_work_orders=False)
		frappe.db.set_value("Work Order", wo, "production_item", None)
		r = control_center.precheck_work_orders([wo])["results"][0]
		self.assertFalse(r["ok"])
		self.assertTrue(r["blockers"])

	def test_a_real_submit_still_enforces_its_own_rules(self):
		"""The pre-check is advice; submission is still ERPNext's."""
		plan, wo = self.ops_plan(10, submit_work_orders=False)
		self.assertTrue(control_center.precheck_work_orders([wo])["results"][0]["ok"])
		doc = frappe.get_doc("Work Order", wo)
		doc.submit()
		self.assertEqual(frappe.db.get_value("Work Order", wo, "docstatus"), 1)


# =============================================================================================
class TestEndpointsAndPermissions(ControlCenterCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)

	def calls(self):
		p = self.plan.name
		return {
			"get_production_plans": lambda: api.get_production_plans({"plan": p}),
			"get_dashboard_overview": lambda: api.get_dashboard_overview({"plan": p}),
			"get_workload": lambda: api.get_workload({"plan": p}),
			"get_revision_log": lambda: api.get_revision_log({"plan": p}),
			"get_exceptions": lambda: api.get_exceptions({"plan": p}),
			"get_work_orders": lambda: api.get_work_orders({"plan": p}),
		}

	def test_every_read_endpoint_answers_a_viewer(self):
		viewer = make_user("ccviewer", ["PPC Viewer", *ERP_MFG])
		frappe.set_user(viewer)
		for name, call in self.calls().items():
			self.assertTrue(call()["ok"], name)

	def test_users_without_a_ppc_role_are_refused_everywhere(self):
		nobody = make_user("ccnorole", ERP_MFG)
		frappe.set_user(nobody)
		for name, call in self.calls().items():
			with self.assertRaises(frappe.PermissionError, msg=name):
				call()
		with self.assertRaises(frappe.PermissionError):
			api.precheck_work_orders([self.wo])

	def test_only_submitters_may_precheck(self):
		viewer = make_user("ccviewer2", ["PPC Viewer", *ERP_MFG])
		planner = make_user("ccplanner", ["Production Planner", *ERP_MFG])
		frappe.set_user(viewer)
		with self.assertRaises(frappe.PermissionError):
			api.precheck_work_orders([self.wo])
		frappe.set_user(planner)
		self.assertTrue(api.precheck_work_orders([self.wo])["ok"])

	def test_a_user_who_cannot_read_job_cards_gets_no_data(self):
		blind = make_user("ccblind", ["PPC Viewer"])  # PPC role but no ERPNext manufacturing access
		frappe.set_user(blind)
		with self.assertRaises(frappe.PermissionError):
			api.get_dashboard_overview({"plan": self.plan.name})

	def test_exceptions_endpoint_checks_the_plan(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.get_exceptions({"plan": "MFG-PP-DOES-NOT-EXIST"})

	def test_sql_injection_is_inert(self):
		evil = "x' or 1=1 -- "
		for call in (lambda: control_center.get_plans({"search": evil}), lambda: control_center.get_exceptions({"search": evil}),
				lambda: control_center.get_revision_log({"search": evil}), lambda: queries.get_work_orders({"search": evil, "warehouse": evil})):
			res = call()
			self.assertEqual(res["total"], 0)
