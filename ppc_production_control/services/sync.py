"""Keep the Job Cards of a Production Plan tagged, baselined and scheduled.

Everything here is *idempotent*: running it twice leaves the same state, so it can safely be called
from buttons, from the event-driven background job and from the hourly scheduler.
"""

import frappe
from frappe.utils import cint, now_datetime

from ppc_production_control.services import audit, scheduling
from ppc_production_control.services.constants import A_SYNC_JC, PLAN_DT, R_SUCCESS, WO_LOCKED_STATUSES
from ppc_production_control.services.utils import get_settings


def refresh_plan_job(plan: str):
	"""Background-job wrapper (event driven)."""
	refresh_plan(plan)
	frappe.db.commit()


def refresh_plan(plan: str, *, schedule_missing: bool | None = None) -> dict:
	info = frappe.db.get_value(PLAN_DT, plan, ["docstatus", "status"], as_dict=True)
	if not info or info.docstatus != 1:
		return {}
	frappe.flags.ppc_in_sync = True
	try:
		return {"job_cards": sync_job_cards(plan, schedule_missing=schedule_missing)}
	finally:
		frappe.flags.ppc_in_sync = False


def plan_work_orders(plan: str, *, submitted_only: bool = False) -> list[str]:
	filters = {"production_plan": plan, "docstatus": 1 if submitted_only else ["<", 2]}
	return frappe.get_all("Work Order", filters=filters, pluck="name", order_by="creation")


def sync_job_cards(plan: str, *, schedule_missing: bool | None = None, work_orders: list[str] | None = None) -> dict:
	"""Tag every Job Card of the plan's Work Orders with the plan, record its first schedule as the
	'original', and (optionally) schedule cards that have no dates.

	Job Cards are created by ERPNext itself when a Work Order is submitted; this function only
	discovers and tracks them - it never creates one and so can never duplicate one.
	"""
	settings = get_settings()
	if schedule_missing is None:
		schedule_missing = bool(cint(settings.auto_schedule_unscheduled))
	wos = [w for w in plan_work_orders(plan, submitted_only=True) if work_orders is None or w in work_orders]
	summary = {"tagged": 0, "missing_baseline": 0, "scheduled": 0, "cards": 0}
	if not wos:
		return summary

	if schedule_missing:
		for name in wos:
			status = frappe.db.get_value("Work Order", name, "status")
			if status not in WO_LOCKED_STATUSES:
				summary["scheduled"] += len(scheduling.auto_schedule_work_order(name))

	cards = frappe.get_all(
		"Job Card",
		filters={"work_order": ["in", wos]},
		fields=["name", "ppc_production_plan", "expected_start_date", "expected_end_date", "ppc_original_start", "docstatus"],
		limit_page_length=0,
	)
	for jc in cards:
		summary["cards"] += 1
		values = {}
		if jc.ppc_production_plan != plan:
			values["ppc_production_plan"] = plan  # metadata-only tag on our own custom field
			summary["tagged"] += 1
		if jc.expected_start_date and jc.expected_end_date and not jc.ppc_original_start and jc.docstatus < 2:
			# Deliberately NOT baselined here: a card seen for the first time by a refresh cannot be claimed
			# to carry its original schedule. It is reported, and "Initialize Missing Baselines" records
			# one explicitly labelled as initialized from the existing schedule.
			summary["missing_baseline"] += 1
		if values:
			frappe.db.set_value("Job Card", jc.name, values, update_modified=False)

	if summary["tagged"] or summary["scheduled"]:
		audit.log(A_SYNC_JC, R_SUCCESS, plan=plan, details=summary)
	touch_scheduling_sync(plan)
	return summary


def touch_scheduling_sync(plan: str):
	frappe.db.set_value(PLAN_DT, plan, "ppc_last_scheduling_sync", now_datetime(), update_modified=False)
