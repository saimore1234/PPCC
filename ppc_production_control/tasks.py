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
