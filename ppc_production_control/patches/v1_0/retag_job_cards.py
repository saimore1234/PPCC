import frappe


def execute():
	"""Tag Job Cards of Production Plans that already have Work Orders and record their baseline
	schedule. Idempotent."""
	from ppc_production_control.services.sync import refresh_plan

	plans = frappe.get_all(
		"Work Order",
		filters={"production_plan": ["is", "set"], "docstatus": 1},
		pluck="production_plan",
		distinct=True,
	)
	for plan in plans:
		refresh_plan(plan, schedule_missing=False)
