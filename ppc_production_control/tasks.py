"""Scheduler jobs."""

import frappe

from ppc_production_control.services.constants import PLAN_DT


def refresh_active_plans():
	"""Hourly safety net: re-sync plans that still have unfinished work, so Job Card tags and
	baselines converge even if an event-triggered refresh was lost."""
	from ppc_production_control.services.sync import refresh_plan

	plans = frappe.get_all(
		PLAN_DT,
		filters={"docstatus": 1, "status": ["not in", ["Completed", "Closed", "Cancelled", "Draft"]]},
		pluck="name",
		limit_page_length=200,
	)
	for plan in plans:
		try:
			refresh_plan(plan)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"PPC scheduled refresh failed for {plan}", message=frappe.get_traceback())


def flag_unavailable_machine_plans():
	"""Hourly: a Sales Order line whose preferred machine has since gone Off / Problem / Maintenance is marked Unavailable, and the order's
	owner is told once. The recommendation is not silently changed - the planner recalculates it with Find Best Machine."""
	from ppc_production_control.services import recommend

	rows = frappe.db.sql(
		"""select i.name, i.parent, i.ppc_preferred_workstation ws, w.status, so.owner
		from `tabSales Order Item` i join `tabSales Order` so on so.name = i.parent join `tabWorkstation` w on w.name = i.ppc_preferred_workstation
		where so.docstatus = 1 and so.status not in ('Closed', 'Completed', 'Cancelled')
			and i.ppc_recommendation_status in ('Recommended', 'Selected') and w.status in %(bad)s limit 500""",
		{"bad": recommend.UNUSABLE_STATES}, as_dict=True,
	)
	for r in rows:
		try:
			frappe.db.set_value("Sales Order Item", r.name, "ppc_recommendation_status", recommend.STATUS_UNAVAILABLE, update_modified=False)
			frappe.get_doc({
				"doctype": "Notification Log", "type": "Alert", "for_user": r.owner, "subject": frappe._("Preferred machine {0} is {1} (Sales Order {2})").format(r.ws, r.status, r.parent),
				"document_type": "Sales Order", "document_name": r.parent,
				"email_content": frappe._("Open the order and use Find Best Machine to recalculate the alternatives."),
			}).insert(ignore_permissions=True)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"PPC machine plan check failed for {r.parent}", message=frappe.get_traceback())
