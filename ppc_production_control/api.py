"""Whitelisted API of PPC Production Control.

Conventions
-----------
* Every endpoint first checks a *capability* (configurable roles) and then the regular
  document permission of what it touches. Hiding a button in the browser is never relied on.
* Anything that changes data accepts POST only (so Frappe's CSRF protection applies).
* Expected failures raise Frappe validation/permission errors (shown as a message dialog);
  unexpected ones are written to the Error Log and replaced by a generic message - no stack trace
  ever reaches the browser.
* Success responses are plain dicts; endpoints that can partially succeed return a per-record
  `results` list.
"""

import functools

import frappe
from frappe import _
from frappe.utils import cint

from ppc_production_control.services import job_cards as jc_service
from ppc_production_control.services import control_center, queries, reconcile, revisions, scheduling, sync, workstation_change
from ppc_production_control.services import work_orders as wo_service
from ppc_production_control.services.constants import (
	AUDIT_DT,
	CAP_CREATE_WO,
	CAP_MANAGE_PLANS,
	CAP_RECONCILE,
	CAP_RESCHEDULE,
	CAP_RETRY,
	CAP_SUBMIT_JC,
	CAP_SUBMIT_WO,
	CAP_VIEW,
	PLAN_DT,
)
from ppc_production_control.services.permissions import (
	check_doc_permission,
	check_doctype_permission,
	has_capability,
	require_capability,
	user_capabilities,
)
from ppc_production_control.services.utils import (
	as_list,
	enqueue_once,
	get_settings,
	in_test,
	parse_json,
)

_PASS_THROUGH = (
	frappe.ValidationError,
	frappe.PermissionError,
	frappe.DoesNotExistError,
	frappe.AuthenticationError,
	frappe.CSRFTokenError,
)
BACKGROUND_THRESHOLD = 10  # rows; above this Work Order creation runs as a background job


def guarded(fn):
	"""Turn unexpected exceptions into a logged error + generic message (no stack trace to the user)."""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except _PASS_THROUGH:
			raise
		except Exception:
			frappe.log_error(title=f"PPC Production Control: {fn.__name__} failed", message=frappe.get_traceback())
			frappe.throw(
				_("Unexpected error in {0}. It has been written to the Error Log; please contact your administrator.").format(
					fn.__name__
				)
			)

	return wrapper


def _require_any(*capabilities: str):
	if not any(has_capability(c) for c in capabilities):
		require_capability(capabilities[0])


def _plan(plan: str, *, ptype: str = "read", submitted: bool = False) -> dict:
	if not plan or not frappe.db.exists(PLAN_DT, plan):
		frappe.throw(_("Production Plan {0} does not exist.").format(plan), frappe.DoesNotExistError)
	check_doc_permission(PLAN_DT, plan, ptype)
	info = frappe.db.get_value(PLAN_DT, plan, ["name", "docstatus", "company", "status"], as_dict=True)
	if submitted and info.docstatus != 1:
		frappe.throw(_("Production Plan {0} must be submitted for this action.").format(plan))
	return info


# ----------------------------------------------------------------------------------------- session
@frappe.whitelist()
@guarded
def get_capabilities() -> dict:
	"""What the current user may do (used only to show/hide buttons; the server re-checks)."""
	settings = get_settings()
	return {
		"capabilities": user_capabilities(),
		"bulk_batch_size": cint(settings.bulk_batch_size) or 25,
		"max_gantt_tasks": cint(settings.max_gantt_tasks) or 500,
	}


# ----------------------------------------------------------------------------------------- work orders
@frappe.whitelist(methods=["POST"])
@guarded
def create_work_orders(plan, background=None) -> dict:
	"""Create the missing draft Work Orders of a submitted Production Plan (idempotent)."""
	require_capability(CAP_CREATE_WO)
	_plan(plan, submitted=True)
	rows = frappe.db.count("Production Plan Item", {"parent": plan})
	made = frappe.db.count(
		"Work Order", {"production_plan": plan, "docstatus": ["<", 2], "production_plan_item": ["is", "set"]}
	)
	pending = max(rows - made, 0)
	if background is None:
		background = pending > BACKGROUND_THRESHOLD
	if cint(background) and not in_test():
		enqueue_once(
			"ppc_production_control.services.work_orders.create_draft_work_orders_job",
			job_id=f"ppc_create_wo::{plan}",
			queue="long",
			timeout=3600,
			plan=plan,
		)
		return {"ok": True, "queued": True, "pending": max(pending, 0)}
	return {"ok": True, "queued": False, **wo_service.create_work_orders(plan)}


@frappe.whitelist(methods=["POST"])
@guarded
def sync_work_orders(plan) -> dict:
	"""Refresh Job Card tags/baselines for the plan and report its Work Order counts."""
	_require_any(CAP_MANAGE_PLANS, CAP_CREATE_WO, CAP_SUBMIT_WO, CAP_RESCHEDULE)
	_plan(plan, submitted=True)
	sync.refresh_plan(plan)
	counts = frappe.db.sql(
		"select docstatus, status, count(*) from `tabWork Order` where production_plan = %s group by docstatus, status",
		(plan,),
	)
	return {"ok": True, "work_orders": [{"docstatus": d, "status": s, "count": c} for d, s, c in counts]}


@frappe.whitelist(methods=["POST"])
@guarded
def submit_selected_work_orders(plan, work_orders) -> dict:
	_plan(plan, submitted=True)
	return {"ok": True, **wo_service.submit_selected_work_orders(plan, as_list(work_orders))}


@frappe.whitelist(methods=["POST"])
@guarded
def retry_failed_operations(plan) -> dict:
	require_capability(CAP_RETRY)
	_plan(plan, submitted=True)
	return {"ok": True, **wo_service.retry_failed_operations(plan)}


# ----------------------------------------------------------------------------------------- job cards
@frappe.whitelist(methods=["POST"])
@guarded
def generate_job_cards(plan, work_orders=None) -> dict:
	require_capability(CAP_CREATE_WO)
	_plan(plan, submitted=True)
	return {"ok": True, **jc_service.generate_job_cards(plan, as_list(work_orders) or None)}


@frappe.whitelist(methods=["POST"])
@guarded
def refresh_job_cards(plan, schedule_missing=None) -> dict:
	"""'Refresh Job Cards': tag, baseline and (optionally) schedule cards without duplicating anything."""
	_require_any(CAP_MANAGE_PLANS, CAP_CREATE_WO, CAP_SUBMIT_JC, CAP_RESCHEDULE)
	_plan(plan, submitted=True)
	flag = None if schedule_missing is None else bool(cint(schedule_missing))
	return {"ok": True, **sync.refresh_plan(plan, schedule_missing=flag)}


@frappe.whitelist(methods=["POST"])
@guarded
def submit_selected_job_cards(plan, job_cards) -> dict:
	_plan(plan, submitted=True)
	return {"ok": True, **jc_service.submit_selected_job_cards(plan, as_list(job_cards))}


# ----------------------------------------------------------------------------------------- reads
@frappe.whitelist()
@guarded
def get_job_cards(filters=None, page=1, page_length=50, sort_by="revised_start", sort_order="asc") -> dict:
	require_capability(CAP_VIEW)
	return {"ok": True, **queries.get_job_cards(parse_json(filters, {}), page, page_length, sort_by, sort_order)}


@frappe.whitelist()
@guarded
def get_work_orders(filters=None, page=1, page_length=50, sort_by="planned_start", sort_order="asc") -> dict:
	require_capability(CAP_VIEW)
	return {"ok": True, **queries.get_work_orders(parse_json(filters, {}), page, page_length, sort_by, sort_order)}


@frappe.whitelist()
@guarded
def get_gantt_data(filters=None, limit=None) -> dict:
	require_capability(CAP_VIEW)
	return {"ok": True, **queries.get_gantt_data(parse_json(filters, {}), limit)}


@frappe.whitelist()
@guarded
def get_ppc_dashboard(filters=None) -> dict:
	require_capability(CAP_VIEW)
	return {"ok": True, **queries.get_dashboard(parse_json(filters, {}))}


# ----------------------------------------------------------------------------------------- control center pages
@frappe.whitelist()
@guarded
def get_production_plans(filters=None, page=1, page_length=50, sort_by="posting_date", sort_order="desc") -> dict:
	require_capability(CAP_VIEW)
	return {"ok": True, **control_center.get_plans(parse_json(filters, {}), page, page_length, sort_by, sort_order)}


@frappe.whitelist()
@guarded
def get_dashboard_overview(filters=None) -> dict:
	"""Everything the PPC dashboard shows, in one response."""
	require_capability(CAP_VIEW)
	return {"ok": True, **control_center.get_overview(parse_json(filters, {}))}


@frappe.whitelist()
@guarded
def get_workload(filters=None, from_date=None, to_date=None, include_idle=1) -> dict:
	require_capability(CAP_VIEW)
	return {"ok": True, **control_center.get_workload(parse_json(filters, {}), from_date, to_date, bool(cint(include_idle)))}


@frappe.whitelist()
@guarded
def get_revision_log(filters=None, page=1, page_length=50, sort_by="changed_at", sort_order="desc") -> dict:
	require_capability(CAP_VIEW)
	return {"ok": True, **control_center.get_revision_log(parse_json(filters, {}), page, page_length, sort_by, sort_order)}


@frappe.whitelist()
@guarded
def get_exceptions(filters=None, types=None, page=1, page_length=50) -> dict:
	require_capability(CAP_VIEW)
	filters = parse_json(filters, {})
	if filters.get("plan"):
		_plan(filters["plan"])
	return {"ok": True, **control_center.get_exceptions(filters, as_list(types), page, page_length)}


@frappe.whitelist(methods=["POST"])
@guarded
def precheck_work_orders(work_orders, plan=None) -> dict:
	"""Dry-run of ERPNext's validation for draft Work Orders (nothing is saved)."""
	require_capability(CAP_SUBMIT_WO)
	if plan:
		_plan(plan)
	return {"ok": True, **control_center.precheck_work_orders(as_list(work_orders), plan)}


@frappe.whitelist()
@guarded
def get_plan_overview(plan) -> dict:
	require_capability(CAP_VIEW)
	_plan(plan)
	doc = frappe.get_doc(PLAN_DT, plan)
	made = {
		w.production_plan_item
		for w in frappe.get_all(
			"Work Order",
			filters={"production_plan": plan, "docstatus": ["<", 2], "production_plan_item": ["is", "set"]},
			fields=["production_plan_item"],
		)
	}
	rows = [
		{
			"idx": r.idx, "sales_order": r.sales_order, "item_code": r.item_code, "planned_qty": r.planned_qty,
			"produced_qty": r.produced_qty, "pending_qty": r.pending_qty, "bom_no": r.bom_no,
			"has_work_order": r.name in made,
		}
		for r in doc.po_items
	]
	wo = frappe.db.sql(
		"select sum(docstatus = 0), sum(docstatus = 1), count(*) from `tabWork Order` where production_plan = %s and docstatus < 2",
		(plan,),
	)[0]
	failed = frappe.db.count(AUDIT_DT, {"ppc_plan": plan, "result": "Failed", "retry_status": "Pending"})
	return {
		"ok": True,
		"plan": {
			"name": doc.name, "company": doc.company, "status": doc.status, "docstatus": doc.docstatus,
			"priority": doc.get("ppc_priority"), "production_planner": doc.get("ppc_planner") or doc.owner,
			"posting_date": str(doc.posting_date or ""), "last_scheduling_sync": str(doc.get("ppc_last_scheduling_sync") or ""),
			"auto_create_work_orders": doc.get("ppc_auto_create_work_orders"),
			"auto_submit_work_orders": doc.get("ppc_auto_submit_work_orders"),
			"draft_work_orders": int(wo[0] or 0), "submitted_work_orders": int(wo[1] or 0), "total_work_orders": int(wo[2] or 0),
			"total_job_cards": frappe.db.count("Job Card", {"ppc_production_plan": plan, "docstatus": ["<", 2]}),
			"total_planned_qty": doc.total_planned_qty,
		},
		"items": rows,
		"has_failures": bool(failed),
		"has_pending": doc.docstatus == 1 and doc.status not in ("Closed", "Completed", "Cancelled") and bool(_rows_without_work_order(doc, made)),
	}


def _rows_without_work_order(doc, made: set) -> set:
	"""Plan rows that still need a Work Order. Uses the plan's own grouping (identical items of one
	Sales Order share one Work Order), exactly like the creator does."""
	from frappe.utils import flt

	wanted = {i["production_plan_item"] for i in doc.get_production_items().values() if flt(i.get("qty")) > 0}
	return wanted - made


@frappe.whitelist()
@guarded
def get_audit_log(plan=None, work_order=None, job_card=None, result=None, limit=100) -> dict:
	require_capability(CAP_VIEW)
	check_doctype_permission(AUDIT_DT, "read")
	filters = {k: v for k, v in {"ppc_plan": plan, "work_order": work_order, "job_card": job_card, "result": result}.items() if v}
	rows = frappe.get_list(
		AUDIT_DT,
		filters=filters,
		fields=["name", "timestamp", "action", "result", "ppc_plan", "work_order", "job_card", "old_start_date",
			"old_end_date", "new_start_date", "new_end_date", "user", "reason", "error_details", "retry_status"],
		order_by="creation desc",
		limit_page_length=min(max(1, cint(limit)), 500),
	)
	return {"ok": True, "rows": rows}


# ----------------------------------------------------------------------------------------- scheduling
@frappe.whitelist()
@guarded
def validate_schedule(job_card, start, end, shift_dependents=0) -> dict:
	require_capability(CAP_VIEW)
	check_doc_permission("Job Card", job_card, "read")
	res = scheduling.validate_schedule(job_card, start, end, shift_dependents=bool(cint(shift_dependents)))
	shifts = [
		{k: (v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else v) for k, v in s.items()}
		for s in res["cascade"]["shifts"]
	]
	return {
		"ok": not res["errors"],
		"errors": res["errors"],
		"warnings": res["warnings"],
		"conflicts": [{"name": c.name, "operation": c.operation, "work_order": c.work_order} for c in res["conflicts"]],
		"cascade": {"shifts": shifts, "blocked": res["cascade"]["blocked"]},
	}


@frappe.whitelist(methods=["POST"])
@guarded
def reschedule_job_card(job_card, start, end, reason=None, mode="move", confirmed=0, override_conflicts=0,
	shift_dependents=0, expected_modified=None, source=None) -> dict:
	if source and source not in ("Gantt Drag", "Gantt Resize", "Date Dialog", "API"):
		source = None  # clients may only claim the sources a user can actually trigger
	result = scheduling.reschedule_job_card(
		job_card,
		start,
		end,
		reason=(reason or "")[:500] or None,
		mode=mode,
		confirmed=bool(cint(confirmed)),
		override_conflicts=bool(cint(override_conflicts)),
		shift_dependents=bool(cint(shift_dependents)),
		expected_modified=expected_modified,
		source=source,
	)
	if result.get("conflicts"):
		result["conflicts"] = [
			{"name": c.name, "operation": c.operation, "work_order": c.work_order} for c in result["conflicts"]
		]
	return result


# ----------------------------------------------------------------------------------- change Workstation
def _ws_job(job_card):
	require_capability(CAP_RESCHEDULE)
	check_doc_permission("Job Card", job_card, "write")


@frappe.whitelist()
@guarded
def check_workstation_change(job_card, workstation, start=None, end=None, shift_dependents=0) -> dict:
	"""Is this Job Card allowed on this Workstation over this interval? Conflicts, dependencies, compatibility. Changes nothing."""
	_ws_job(job_card)
	return workstation_change.check_change(job_card, workstation, start, end, shift_dependents=bool(cint(shift_dependents)))


@frappe.whitelist()
@guarded
def get_workstation_candidates(job_card, start=None, end=None, search=None, limit=40) -> dict:
	"""Workstations to choose from, with availability for the Job Card's interval."""
	_ws_job(job_card)
	return workstation_change.get_candidates(job_card, start, end, search, cint(limit) or 40)


@frappe.whitelist()
@guarded
def find_next_workstation_slot(job_card, workstation, start=None, end=None) -> dict:
	"""The next feasible slot for this Job Card on the given Workstation."""
	_ws_job(job_card)
	return workstation_change.find_next_slot(job_card, workstation, start, end)


@frappe.whitelist(methods=["POST"])
@guarded
def change_job_card_workstation(job_card, workstation, start=None, end=None, reason=None, expected_modified=None, confirmed=0,
	shift_dependents=0, override_conflicts=0, override_reason=None, reschedule_conflicts=0, correlation_id=None) -> dict:
	"""Move a Job Card to another Workstation (optionally with new dates). See services/workstation_change.py."""
	result = workstation_change.change_job_card_workstation(
		job_card, workstation, start=start, end=end, reason=(reason or "")[:500] or None, expected_modified=expected_modified,
		confirmed=bool(cint(confirmed)), shift_dependents=bool(cint(shift_dependents)), override_conflicts=bool(cint(override_conflicts)),
		override_reason=(override_reason or "")[:500] or None, reschedule_conflicts=bool(cint(reschedule_conflicts)),
		correlation_id=(correlation_id or "")[:64] or None,
	)
	result.pop("check", None) if result.get("conflict") else None  # the dialog already holds the conflicts; keep responses small
	return result


@frappe.whitelist(methods=["POST"])
@guarded
def sync_work_order_schedule(work_order) -> dict:
	require_capability(CAP_RESCHEDULE)
	check_doc_permission("Work Order", work_order, "write")
	change = scheduling.sync_work_order_schedule(work_order, reason=_("Manual synchronisation"))
	return {"ok": True, "changed": bool(change), "change": change}


@frappe.whitelist(methods=["POST"])
@guarded
def schedule_unscheduled(plan) -> dict:
	"""Give Job Cards that have no dates an estimated sequential schedule (never changes existing dates)."""
	require_capability(CAP_RESCHEDULE)
	_plan(plan, submitted=True)
	made = []
	for wo in sync.plan_work_orders(plan, submitted_only=True):
		made.extend(scheduling.auto_schedule_work_order(wo))
	sync.refresh_plan(plan, schedule_missing=False)
	return {
		"ok": True,
		"scheduled": [
			{"job_card": m["job_card"], "start": m["start"].strftime("%Y-%m-%d %H:%M:%S"),
			 "end": m["end"].strftime("%Y-%m-%d %H:%M:%S"), "estimated": m["estimated"]}
			for m in made
		],
	}


# ----------------------------------------------------------------------------------------- schedule history
@frappe.whitelist()
@guarded
def get_schedule_history(job_card) -> dict:
	"""Baseline, current schedule, actual dates and the full revision history of one Job Card."""
	require_capability(CAP_VIEW)
	check_doc_permission("Job Card", job_card, "read")
	check_doctype_permission("PPC Schedule Revision", "read")
	return {"ok": True, **revisions.get_history(job_card)}


@frappe.whitelist(methods=["POST"])
@guarded
def correct_schedule_revision(revision, reason) -> dict:
	"""Annotate a revision (audited). System Manager only; dates are never changed."""
	return {"ok": True, **revisions.correct_revision(revision, reason)}


# ----------------------------------------------------------------------------------------- reconciliation
@frappe.whitelist()
@guarded
def check_schedule_consistency(plan=None) -> dict:
	"""Preview every disagreement between Job Cards, Work Orders and the revision history. Changes nothing."""
	require_capability(CAP_VIEW)
	if plan:
		_plan(plan)
	else:
		check_doctype_permission("Job Card", "read")
	return {"ok": True, **reconcile.check_consistency(plan)}


@frappe.whitelist(methods=["POST"])
@guarded
def initialize_missing_baselines(plan=None, dry_run=1) -> dict:
	require_capability(CAP_RECONCILE)
	if plan:
		_plan(plan)
	return {"ok": True, **reconcile.initialize_missing_baselines(plan, dry_run=bool(cint(dry_run)))}


@frappe.whitelist(methods=["POST"])
@guarded
def repair_schedule_inconsistencies(plan=None, types=None, dry_run=1) -> dict:
	require_capability(CAP_RECONCILE)
	if plan:
		_plan(plan)
	return {"ok": True, **reconcile.repair(plan, as_list(types) or None, dry_run=bool(cint(dry_run)))}


@frappe.whitelist(methods=["POST"])
@guarded
def retry_failed_synchronizations(plan=None, job_card=None) -> dict:
	require_capability(CAP_RETRY)
	if plan:
		_plan(plan)
	if job_card:
		check_doc_permission("Job Card", job_card, "write")
	return {"ok": True, **reconcile.retry_failed_synchronizations(plan, job_card)}
