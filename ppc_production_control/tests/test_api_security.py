"""Roles, capabilities, document permissions and safe error handling of the API."""

import frappe

from ppc_production_control import api
from ppc_production_control.services import permissions
from ppc_production_control.tests.utils import PREFIX, PPCTestCase

# ERPNext roles a real planner / supervisor / viewer would also hold
ERP_MFG = ["Manufacturing User", "Manufacturing Manager", "Sales User", "Stock User"]


def make_user(tag: str, roles: list[str]) -> str:
	email = f"{PREFIX.lower()}.{tag}@example.com"
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True)
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": f"PPC {tag}",
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": r} for r in roles],
		}
	)
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)
	return email


class TestApiSecurity(PPCTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.planner = make_user("planner", ["Production Planner", *ERP_MFG])
		cls.supervisor = make_user("supervisor", ["Production Supervisor", *ERP_MFG])
		cls.viewer = make_user("viewer", ["PPC Viewer", *ERP_MFG])  # ERPNext rights alone would allow writes
		cls.manager = make_user("manager", ["PPC Manager", *ERP_MFG])
		cls.erp_only = make_user("erponly", ERP_MFG)  # no PPC role at all
		cls.ppc_only = make_user("ppconly", ["Production Planner"])  # no ERPNext manufacturing role

	def as_user(self, user):
		frappe.set_user(user)

	def prepare(self):
		plan, wo = self.ops_plan(10)
		cards = self.job_cards(wo)
		return plan, wo, cards

	# 17. permission denial ------------------------------------------------------------------------
	def test_viewer_can_look_but_not_touch(self):
		plan, wo, cards = self.prepare()
		self.as_user(self.viewer)
		self.assertTrue(api.get_job_cards({"plan": plan.name})["rows"])
		self.assertTrue(api.get_gantt_data({"plan": plan.name})["tasks"])
		self.assertIn("kpis", api.get_ppc_dashboard({"plan": plan.name}))
		self.assertEqual(api.get_plan_overview(plan.name)["plan"]["name"], plan.name)
		self.assertTrue(api.get_work_orders({"plan": plan.name})["rows"])

		asm = frappe.db.get_value("Job Card", cards[1].name, ["expected_start_date", "expected_end_date"], as_dict=True)
		denied = [
			lambda: api.create_work_orders(plan.name),
			lambda: api.submit_selected_work_orders(plan.name, [wo]),
			lambda: api.submit_selected_job_cards(plan.name, [cards[0].name]),
			lambda: api.generate_job_cards(plan.name),
			lambda: api.retry_failed_operations(plan.name),
			lambda: api.reschedule_job_card(cards[1].name, str(asm.expected_start_date), str(asm.expected_end_date)),
			lambda: api.sync_work_order_schedule(wo),
			lambda: api.schedule_unscheduled(plan.name),
			lambda: api.sync_work_orders(plan.name),
			lambda: api.refresh_job_cards(plan.name),
		]
		for call in denied:
			with self.assertRaises(frappe.PermissionError):
				call()
		self.as_user("Administrator")
		self.assertEqual(frappe.db.get_value("Work Order", wo, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Job Card", cards[0].name, "docstatus"), 0)

	def test_user_without_any_ppc_role_is_denied_everything(self):
		plan, wo, cards = self.prepare()
		self.as_user(self.erp_only)
		for call in (
			lambda: api.get_job_cards({"plan": plan.name}),
			lambda: api.get_gantt_data({"plan": plan.name}),
			lambda: api.get_ppc_dashboard({}),
			lambda: api.get_plan_overview(plan.name),
			lambda: api.get_audit_log(plan.name),
		):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_ppc_role_does_not_bypass_erpnext_document_permissions(self):
		"""Planner role but no Work Order / Job Card rights in ERPNext -> still refused."""
		plan, wo, cards = self.prepare()
		self.as_user(self.ppc_only)
		with self.assertRaises(frappe.PermissionError):
			api.get_job_cards({"plan": plan.name})
		asm = frappe.db.get_value("Job Card", cards[1].name, ["expected_start_date", "expected_end_date"], as_dict=True)
		with self.assertRaises(frappe.PermissionError):
			api.reschedule_job_card(cards[1].name, str(asm.expected_start_date), str(asm.expected_end_date))

	def test_supervisor_may_submit_cards_but_not_reschedule(self):
		plan, wo, cards = self.prepare()
		for c in cards:
			self.complete_job_card(c.name)
		self.as_user(self.supervisor)
		res = api.submit_selected_job_cards(plan.name, [cards[0].name, cards[1].name])
		self.assertEqual(res["submitted"], 2)
		with self.assertRaises(frappe.PermissionError):
			api.submit_selected_work_orders(plan.name, [wo])
		asm = frappe.db.get_value("Job Card", cards[1].name, ["expected_start_date", "expected_end_date"], as_dict=True)
		with self.assertRaises(frappe.PermissionError):
			api.reschedule_job_card(cards[1].name, str(asm.expected_start_date), str(asm.expected_end_date))

	def test_planner_reschedules_but_cannot_override_conflicts_manager_can(self):
		plan, wo, cards = self.prepare()
		cut = frappe.db.get_value("Job Card", cards[0].name, ["expected_start_date", "expected_end_date"], as_dict=True)
		asm = frappe.db.get_value("Job Card", cards[1].name, ["expected_start_date", "expected_end_date"], as_dict=True)
		early_start = cut.expected_end_date - (cut.expected_end_date - cut.expected_start_date) / 2
		early_end = early_start + (asm.expected_end_date - asm.expected_start_date)

		self.as_user(self.planner)
		first = api.reschedule_job_card(cards[1].name, str(early_start), str(early_end), confirmed=1)
		self.assertFalse(first["ok"])
		self.assertTrue(first["needs_override"])
		self.assertFalse(first["can_override"], "the UI is told the planner may not override")
		with self.assertRaises(frappe.PermissionError):
			api.reschedule_job_card(cards[1].name, str(early_start), str(early_end), override_conflicts=1)
		# a conflict-free move is fine for the planner
		ok = api.reschedule_job_card(cards[1].name, str(asm.expected_start_date + (asm.expected_end_date - asm.expected_start_date)),
			str(asm.expected_end_date + (asm.expected_end_date - asm.expected_start_date)), confirmed=1)
		self.assertTrue(ok["ok"], ok)

		self.as_user(self.manager)
		res = api.reschedule_job_card(cards[1].name, str(early_start), str(early_end), override_conflicts=1, reason="Approved")
		self.assertTrue(res["ok"], res)

	def test_settings_can_remap_capabilities(self):
		plan, wo, cards = self.prepare()
		asm = frappe.db.get_value("Job Card", cards[1].name, ["expected_start_date", "expected_end_date"], as_dict=True)
		span = asm.expected_end_date - asm.expected_start_date
		self.as_user(self.planner)
		self.assertTrue(permissions.has_capability("Reschedule"))

		self.as_user("Administrator")
		settings = frappe.get_single("PPC Settings")
		settings.capability_roles = [r for r in settings.capability_roles if not (r.capability == "Reschedule" and r.role == "Production Planner")]
		settings.save()

		self.as_user(self.planner)
		self.assertFalse(permissions.has_capability("Reschedule"))
		with self.assertRaises(frappe.PermissionError):
			api.reschedule_job_card(cards[1].name, str(asm.expected_start_date + span), str(asm.expected_end_date + span))
		self.assertFalse(api.get_capabilities()["capabilities"]["Reschedule"])

	def test_planner_override_can_be_enabled_in_settings(self):
		self.as_user("Administrator")
		frappe.db.set_single_value("PPC Settings", "planner_can_override_conflicts", 1)
		self.as_user(self.planner)
		self.assertTrue(permissions.has_capability("Override Conflicts"))

	def test_work_order_foreign_to_plan_cannot_be_submitted_through_ppc(self):
		plan, wo, cards = self.prepare()
		other_plan, other_wo = self.ops_plan(3, submit_work_orders=False)
		self.as_user(self.planner)
		res = api.submit_selected_work_orders(plan.name, [other_wo])
		self.assertTrue(res["results"][0]["skipped"])
		self.as_user("Administrator")
		self.assertEqual(frappe.db.get_value("Work Order", other_wo, "docstatus"), 0)

	# input hardening -------------------------------------------------------------------------------------
	def test_sql_injection_attempts_are_inert(self):
		plan, wo, cards = self.prepare()
		evil = ["' OR 1=1 -- ", "\"; DROP TABLE `tabJob Card`; --", "%' UNION SELECT name FROM tabUser -- ", "\\"]
		for text in evil:
			for key in ("search", "sales_order_like", "sales_order", "customer", "operation", "plan"):
				res = api.get_job_cards({key: text})
				self.assertEqual(res["total"], 0, (key, text))
		# sort column is whitelisted, direction coerced
		res = api.get_job_cards({"plan": plan.name}, sort_by="name; drop table `tabJob Card`", sort_order="asc; --")
		self.assertEqual(res["total"], 2)
		self.assertTrue(frappe.db.table_exists("Job Card"))
		with self.assertRaises(frappe.ValidationError):
			api.get_job_cards({"status": "x' or '1'='1"})
		# dates are re-serialised from the parsed value, so trailing SQL can never reach the query
		from ppc_production_control.services import queries

		for payload in ("2020-01-01'; --", "2020-01-01'; DROP TABLE x; --", "'; DROP TABLE x; --", "1 OR 1=1"):
			try:
				clean = queries.normalize_filters({"revised_start_from": payload})["revised_start_from"]
			except frappe.ValidationError:
				continue  # rejected outright
			self.assertRegex(clean, r"^\d{4}-\d{2}-\d{2}$", payload)  # or reduced to a plain date

	def test_unexpected_errors_do_not_leak_internals(self):
		from unittest import mock

		plan, wo, cards = self.prepare()
		# log_error is mocked: the real one writes an Error Log row that outlives the test's rollback
		with mock.patch("ppc_production_control.services.queries.get_job_cards", side_effect=RuntimeError("secret db password=hunter2")), \
			mock.patch("frappe.log_error") as log_error:
			with self.assertRaises(frappe.ValidationError) as ctx:
				api.get_job_cards({"plan": plan.name})
		text = str(ctx.exception)
		self.assertNotIn("hunter2", text)
		self.assertNotIn("Traceback", text)
		self.assertIn("get_job_cards", text)
		log_error.assert_called_once()
		self.assertIn("get_job_cards failed", log_error.call_args.kwargs["title"])
		self.assertIn("hunter2", log_error.call_args.kwargs["message"], "the real cause goes to the administrator's log")

	def test_endpoints_are_not_guest_accessible_and_mutators_are_post_only(self):
		for name in dir(api):
			fn = getattr(api, name)
			if getattr(fn, "__module__", None) != api.__name__ or name.startswith("_") or not callable(fn):
				continue
			path = f"{api.__name__}.{name}"
			if fn in (api.guarded,):
				continue
			self.assertNotIn(fn, frappe.guest_methods, path)
		mutators = ["create_work_orders", "submit_selected_work_orders", "submit_selected_job_cards", "reschedule_job_card",
			"retry_failed_operations", "generate_job_cards", "sync_work_order_schedule", "schedule_unscheduled",
			"refresh_job_cards", "sync_work_orders"]
		for name in mutators:
			fn = getattr(api, name)
			self.assertIn(fn, frappe.whitelisted, name)
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"], name)
