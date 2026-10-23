"""Job Card generation, synchronisation and bulk submission.

ERPNext creates a Work Order's Job Cards itself, inside `WorkOrder.on_submit`. PPC therefore does
not "create" Job Cards in the normal flow - it *discovers, tags and tracks* them. The only time PPC
asks ERPNext to create one is when a submitted Work Order has an operation with no Job Card at all
(for example capacity planning failed or cards were deleted); that uses ERPNext's own
`make_job_card()` and computes the missing quantity so nothing is duplicated.
"""

import frappe
from frappe import _
from frappe.utils import flt

from ppc_production_control.services import audit, sync
from ppc_production_control.services.constants import (
	A_GENERATE_JC,
	A_SUBMIT_JC,
	CAP_CREATE_WO,
	CAP_SUBMIT_JC,
	PLAN_DT,
	R_FAILED,
	R_SKIPPED,
	R_SUCCESS,
	WO_LOCKED_STATUSES,
)
from ppc_production_control.services.permissions import require_capability
from ppc_production_control.services.utils import commit_if_allowed, error_text, get_settings, savepoint

MAX_PER_CALL = 200


def generate_job_cards(plan_name: str, work_orders: list[str] | None = None) -> dict:
	"""Make sure every operation of the plan's submitted Work Orders has its Job Card(s), then link
	them. Idempotent: running it again creates nothing new."""
	require_capability(CAP_CREATE_WO)
	results = []
	for wo_name in sync.plan_work_orders(plan_name):
		if work_orders is not None and wo_name not in work_orders:
			continue
		results.append(_generate_for_work_order(plan_name, wo_name))

	sync_result = sync.sync_job_cards(plan_name, work_orders=work_orders)
	return {"work_orders": results, "sync": sync_result}


def _generate_for_work_order(plan_name: str, wo_name: str) -> dict:
	out = {"work_order": wo_name, "created": 0, "message": ""}
	wo = frappe.db.get_value("Work Order", wo_name, ["name", "docstatus", "status"], as_dict=True)
	if not wo or wo.docstatus == 2:
		out["message"] = _("Work Order is cancelled or missing.")
		return out
	if wo.docstatus == 0:
		out["message"] = _("Draft Work Order: ERPNext generates Job Cards when it is submitted.")
		return out
	if wo.status in WO_LOCKED_STATUSES:
		out["message"] = _("Work Order is {0}.").format(wo.status)
		return out

	doc = frappe.get_doc("Work Order", wo_name)
	if not doc.operations:
		out["message"] = _("BOM has no operations; no Job Cards are required.")
		return out

	existing = {}
	for jc in frappe.get_all(
		"Job Card", filters={"work_order": wo_name, "docstatus": ["<", 2]}, fields=["operation_id", "for_quantity"], limit_page_length=0
	):
		existing[jc.operation_id] = existing.get(jc.operation_id, 0.0) + flt(jc.for_quantity)

	missing = []
	for op in doc.operations:
		if op.status == "Completed":
			continue
		gap = flt(doc.qty) - existing.get(op.name, 0.0)
		if gap > 0.000001:
			missing.append(
				{
					"name": op.name,
					"operation": op.operation,
					"workstation": op.workstation,
					"workstation_type": op.workstation_type,
					"batch_size": op.batch_size,
					"sequence_id": op.sequence_id,
					"bom": op.bom,
					"hour_rate": op.hour_rate,
					"qty": gap,
					"pending_qty": gap,
				}
			)
	if not missing:
		out["message"] = _("All operations already have Job Cards.")
		return out

	from erpnext.manufacturing.doctype.work_order.work_order import make_job_card

	count_filters = {"work_order": wo_name, "docstatus": ["<", 2]}
	before = frappe.db.count("Job Card", count_filters)
	try:
		with savepoint():
			make_job_card(wo_name, missing)
	except Exception as exc:
		message = error_text(exc)
		audit.log(A_GENERATE_JC, R_FAILED, plan=plan_name, work_order=wo_name, error=message)
		out["message"] = message
		return out

	out["created"] = frappe.db.count("Job Card", count_filters) - before
	out["message"] = _("Created {0} missing operation(s).").format(len(missing))
	audit.log(A_GENERATE_JC, R_SUCCESS, plan=plan_name, work_order=wo_name, details={"operations": [m["operation"] for m in missing]})
	return out


# ----------------------------------------------------------------------------- bulk submit
def submit_selected_job_cards(plan_name: str, job_cards: list[str]) -> dict:
	"""Submit Draft Job Cards of a plan through their standard lifecycle, one by one.

	ERPNext's own rules stay in force (time logs, completed quantity, sequence, transferred
	material ...); a Job Card that does not satisfy them is reported with ERPNext's message.
	"""
	require_capability(CAP_SUBMIT_JC)
	if not frappe.has_permission(PLAN_DT, "read", doc=plan_name):
		frappe.throw(_("No permission on plan {0}").format(plan_name), frappe.PermissionError)

	job_cards = list(dict.fromkeys(job_cards or []))
	if len(job_cards) > MAX_PER_CALL:
		frappe.throw(_("Send at most {0} Job Cards per request; the screen submits in batches.").format(MAX_PER_CALL))

	wos = sync.plan_work_orders(plan_name)
	owned = set(frappe.get_all("Job Card", filters={"ppc_production_plan": plan_name}, pluck="name"))
	if wos:  # cards created before PPC tagged them
		owned |= set(frappe.get_all("Job Card", filters={"work_order": ["in", wos]}, pluck="name"))

	# ERPNext only lets an operation be completed after the previous sequence is done, so process
	# earlier operations first regardless of the order the user ticked them.
	order = {
		d.name: (d.work_order or "", d.sequence_id or 0, str(d.creation))
		for d in frappe.get_all(
			"Job Card", filters={"name": ["in", job_cards or [""]]}, fields=["name", "work_order", "sequence_id", "creation"]
		)
	}
	job_cards.sort(key=lambda n: order.get(n, ("~", 0, "")))

	results = []
	for name in job_cards:
		results.append(_submit_one(plan_name, name, name in owned))
		commit_if_allowed()

	return {
		"results": results,
		"submitted": sum(1 for r in results if r["ok"]),
		"failed": sum(1 for r in results if not r["ok"] and not r["skipped"]),
		"skipped": sum(1 for r in results if r["skipped"]),
	}


def _submit_one(plan_name: str, name: str, owned: bool) -> dict:
	res = {"name": name, "ok": False, "skipped": False, "message": ""}
	if not owned:
		return {**res, "skipped": True, "message": _("Not part of this plan.")}
	if not frappe.db.exists("Job Card", name):
		return {**res, "message": _("Job Card does not exist.")}

	doc = frappe.get_doc("Job Card", name)
	if doc.docstatus == 1:
		return {**res, "skipped": True, "message": _("Already submitted.")}
	if doc.docstatus == 2:
		return {**res, "skipped": True, "message": _("Cancelled Job Cards cannot be submitted.")}
	if not doc.work_order:
		return {**res, "message": _("Job Card has no Work Order.")}
	wo = frappe.db.get_value("Work Order", doc.work_order, ["docstatus", "status"], as_dict=True)
	if not wo:
		return {**res, "message": _("Work Order {0} does not exist.").format(doc.work_order)}
	if wo.docstatus != 1:
		return {**res, "message": _("Work Order {0} is not submitted.").format(doc.work_order)}
	if not doc.operation:
		return {**res, "message": _("Job Card has no operation.")}
	if not frappe.has_permission("Job Card", "submit", doc=doc):
		return {**res, "message": _("You do not have permission to submit this Job Card.")}

	try:
		with savepoint():
			doc.submit()
	except Exception as exc:
		message = error_text(exc)
		audit.log(A_SUBMIT_JC, R_FAILED, plan=plan_name, work_order=doc.work_order, job_card=name, error=message)
		return {**res, "message": message}

	audit.log(A_SUBMIT_JC, R_SUCCESS, plan=plan_name, work_order=doc.work_order, job_card=name)
	return {**res, "ok": True, "message": _("Submitted."), "status": doc.status}
