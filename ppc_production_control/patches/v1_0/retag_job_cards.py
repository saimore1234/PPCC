import frappe


def execute():
	"""Tag Job Cards of Production Plans that already have Work Orders with their plan. Idempotent.
	(Baselines and their history are handled by v1_1.backfill_schedule_history and the preview-first
	"Initialize Missing Baselines" action.)"""
	from ppc_production_control.services.sync import refresh_plan

	plans = frappe.get_all(
		"Work Order",
		filters={"production_plan": ["is", "set"], "docstatus": 1},
		pluck="production_plan",
		distinct=True,
	)
	for plan in plans:
		refresh_plan(plan, schedule_missing=False)
