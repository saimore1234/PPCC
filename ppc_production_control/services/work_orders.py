"""Work Order orchestration on top of the standard Production Plan.

What PPC adds to the standard flow
----------------------------------
* **Automatic draft Work Orders.** When a Production Plan is submitted, the Work Orders the
  standard "Work Order / Subcontract PO" button would make are created for you, as drafts. The
  standard code path is used (`ProductionPlan.create_work_order`), so the Work Orders are exactly
  what ERPNext produces for the same plan.
* **No duplicates.** A plan row maps to at most one live Work Order (`production_plan_item`). Before
  creating anything we look for it, so a retry, a refresh, a double click on the standard button, or
  a crashed worker never creates a second one. The plan row is locked (`SELECT ... FOR UPDATE`) so two
  workers cannot race.
* **No lost failures.** A row that fails is rolled back on its own (savepoint), audited and left
  retryable; the other rows continue.
* **Individually submittable.** Draft Work Orders are submitted through their normal lifecycle,
  one by one or in bulk; every ERPNext validation (and your site's own rules) applies.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt

from ppc_production_control.services import audit, sync
from ppc_production_control.services.constants import (
	A_CREATE_WO,
	A_RETRY,
	A_SUBMIT_WO,
	CAP_CREATE_WO,
	CAP_RETRY,
	CAP_SUBMIT_WO,
	PLAN_DT,
	R_FAILED,
	R_SKIPPED,
	R_SUCCESS,
)
from ppc_production_control.services.permissions import require_capability
from ppc_production_control.services.utils import (
	commit_if_allowed,
	error_text,
	in_test,
	lock_plan,
	savepoint,
)


# ----------------------------------------------------------------------------- creation
def create_draft_work_orders_job(plan: str):
	"""Background job queued when a plan is submitted."""
	create_draft_work_orders(plan, commit=True)


def create_draft_work_orders(plan_name: str, *, commit: bool = False, retry: bool = False) -> dict:
	"""Create the missing draft Work Orders of a submitted plan. Safe to call any number of times."""
	from erpnext.manufacturing.doctype.production_plan.production_plan import set_default_warehouses
	from erpnext.manufacturing.doctype.work_order.work_order import get_default_warehouse

	lock_plan(plan_name)
	plan = frappe.get_doc(PLAN_DT, plan_name)
	if plan.docstatus != 1:
		frappe.throw(_("Work Orders can only be created from a submitted Production Plan."))
	if plan.status in ("Closed", "Cancelled"):
		frappe.throw(_("Production Plan {0} is {1}.").format(plan_name, plan.status))

	result = {"created": [], "skipped": [], "failed": [], "submitted": [], "submit_failed": []}
	live = frappe.get_all(
		"Work Order",
		filters={"production_plan": plan_name, "docstatus": ["<", 2]},
		fields=["production_plan_item", "production_plan_sub_assembly_item"],
	)
	have_items = {w.production_plan_item for w in live if w.production_plan_item}
	have_sub = {w.production_plan_sub_assembly_item for w in live if w.production_plan_sub_assembly_item}
	defaults = get_default_warehouses_safe(get_default_warehouse)
	action = A_RETRY if retry else A_CREATE_WO

	def make(data: dict, ref: str, label: str):
		try:
			with savepoint():
				set_default_warehouses(data, defaults)
				name = plan.create_work_order(data)
		except Exception as exc:
			message = error_text(exc)
			audit.log(action, R_FAILED, plan=plan_name, error=message, idempotency_key=ref, details={"item": label})
			result["failed"].append({"item_code": label, "message": message, "ref": ref})
			return
		if not name:  # ERPNext swallowed an over-production error for this row
			result["skipped"].append({"item_code": label, "message": _("Nothing left to produce (over-production).")})
			return
		audit.log(action, R_SUCCESS, plan=plan_name, work_order=name, idempotency_key=ref, details={"item": label, "qty": flt(data.get("qty"), 3)})
		audit.resolve_failures(plan_name, idempotency_key=ref)
		result["created"].append({"work_order": name, "item_code": label, "ref": ref})

	for _key, item in plan.get_production_items().items():
		ref = item["production_plan_item"]
		if ref in have_items:
			result["skipped"].append({"item_code": item["production_item"], "message": _("Work Order already exists.")})
			continue
		if flt(item.get("qty")) <= 0:
			result["skipped"].append({"item_code": item["production_item"], "message": _("Nothing left to produce.")})
			continue
		make(dict(item), ref, item["production_item"])
		have_items.add(ref)
		if commit:
			commit_if_allowed()

	for row in plan.get("sub_assembly_items") or []:
		if row.type_of_manufacturing in ("Subcontract", "Material Request"):
			continue  # purchase orders / material requests stay with the standard buttons
		if row.name in have_sub or flt(row.qty) <= flt(row.ordered_qty):
			continue
		data = {
			"wip_warehouse": defaults.get("wip_warehouse"),
			"fg_warehouse": defaults.get("fg_warehouse"),
			"scrap_warehouse": defaults.get("scrap_warehouse"),
			"company": plan.company,
		}
		plan.prepare_data_for_sub_assembly_items(row, data)
		if flt(data.get("qty")) <= 0:
			continue
		make(data, row.name, row.production_item)
		if commit:
			commit_if_allowed()

	if cint(plan.get("ppc_auto_submit_work_orders")):
		for created in result["created"]:
			sub = submit_work_order(created["work_order"], plan_name=plan_name)
			result["submitted" if sub["ok"] else "submit_failed"].append(sub)

	sync.refresh_plan(plan_name)
	if commit:
		commit_if_allowed()
	_publish(plan_name, result)
	return result


def get_default_warehouses_safe(getter) -> dict:
	try:
		return getter() or {}
	except Exception:
		return {}


# ----------------------------------------------------------------------------- submitting
def submit_work_order(wo_name: str, *, plan_name: str | None = None) -> dict:
	"""Submit one Draft Work Order through its normal lifecycle. Never raises: returns the outcome."""
	result = {"name": wo_name, "ok": False, "skipped": False, "message": "", "job_cards": 0}
	doc = frappe.get_doc("Work Order", wo_name)
	plan_name = plan_name or doc.get("production_plan")

	if doc.docstatus == 1:
		return {**result, "skipped": True, "message": _("Already submitted.")}
	if doc.docstatus == 2:
		return {**result, "skipped": True, "message": _("Cancelled Work Orders cannot be submitted.")}
	if not frappe.has_permission("Work Order", "submit", doc=doc):
		return {**result, "message": _("You do not have permission to submit Work Order {0}.").format(wo_name)}

	try:
		with savepoint():
			doc.submit()  # ERPNext validates and creates the Job Cards in on_submit
	except Exception as exc:
		message = error_text(exc)
		audit.log(A_SUBMIT_WO, R_FAILED, plan=plan_name, work_order=wo_name, error=message)
		return {**result, "message": message}

	jc_count = frappe.db.count("Job Card", {"work_order": wo_name, "docstatus": ["<", 2]})
	result.update(ok=True, message=_("Submitted."), job_cards=jc_count)
	audit.log(A_SUBMIT_WO, R_SUCCESS, plan=plan_name, work_order=wo_name, details={"job_cards": jc_count})
	audit.resolve_failures(plan_name, work_order=wo_name) if plan_name else None
	if plan_name:
		frappe.flags.ppc_in_sync = True
		try:
			sync.sync_job_cards(plan_name, work_orders=[wo_name])
		finally:
			frappe.flags.ppc_in_sync = False
	return result


def submit_selected_work_orders(plan_name: str, work_orders: list[str]) -> dict:
	"""Bulk submit. Only Draft Work Orders that belong to this plan are processed; every other
	selection gets an explicit reason - nothing is silently ignored."""
	require_capability(CAP_SUBMIT_WO)
	if not frappe.has_permission(PLAN_DT, "read", doc=plan_name):
		frappe.throw(_("No permission on plan {0}").format(plan_name), frappe.PermissionError)

	owned = set(frappe.get_all("Work Order", filters={"production_plan": plan_name}, pluck="name"))
	results = []
	for name in dict.fromkeys(work_orders or []):
		if name not in owned:
			results.append({"name": name, "ok": False, "skipped": True, "message": _("Not part of this plan.")})
			continue
		results.append(submit_work_order(name, plan_name=plan_name))
		commit_if_allowed()  # keep earlier successes even if a later record crashes the request

	sync.refresh_plan(plan_name)
	return {
		"results": results,
		"submitted": sum(1 for r in results if r["ok"]),
		"failed": sum(1 for r in results if not r["ok"] and not r["skipped"]),
		"skipped": sum(1 for r in results if r["skipped"]),
	}


# ----------------------------------------------------------------------------- retry
def retry_failed_operations(plan_name: str) -> dict:
	"""Retry only what failed or is missing - verifying first that it does not already exist."""
	require_capability(CAP_RETRY)
	plan = frappe.get_doc(PLAN_DT, plan_name)
	if plan.docstatus != 1:
		frappe.throw(_("Only a submitted plan can be retried."))

	summary = {"work_orders": create_draft_work_orders(plan_name, retry=True, commit=not in_test()), "submitted": [], "job_cards": None}
	# draft Work Orders whose earlier submit failed
	failed_submits = {
		d.work_order
		for d in frappe.get_all(
			"PPC Audit Log",
			filters={"ppc_plan": plan_name, "action": A_SUBMIT_WO, "result": R_FAILED, "retry_status": "Pending"},
			fields=["work_order"],
		)
		if d.work_order
	}
	for name in sorted(failed_submits):
		if frappe.db.get_value("Work Order", name, "docstatus") == 0:
			summary["submitted"].append(submit_work_order(name, plan_name=plan_name))

	from ppc_production_control.services import job_cards

	summary["job_cards"] = job_cards.generate_job_cards(plan_name)
	sync.refresh_plan(plan_name)
	return summary


def create_work_orders(plan_name: str) -> dict:
	"""Manual 'Create Draft Work Orders' action (capability checked)."""
	require_capability(CAP_CREATE_WO)
	return create_draft_work_orders(plan_name, commit=not in_test())


def _publish(plan: str, result: dict):
	if in_test():
		return
	frappe.publish_realtime(
		"ppc_progress",
		{"plan": plan, "created": len(result["created"]), "failed": len(result["failed"]), "finished": True},
		doctype=PLAN_DT,
		docname=plan,
	)
