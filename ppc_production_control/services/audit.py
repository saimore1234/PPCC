"""Audit trail. Every orchestration step and every schedule change leaves one PPC Audit Log row."""

import json

import frappe
from frappe.utils import now_datetime

from ppc_production_control.services.constants import AUDIT_DT, R_FAILED
from ppc_production_control.services.utils import dt_str


def log(
	action: str,
	result: str = "Success",
	*,
	plan: str | None = None,
	work_order: str | None = None,
	job_card: str | None = None,
	old_start=None,
	old_end=None,
	new_start=None,
	new_end=None,
	reason: str | None = None,
	error: str | None = None,
	idempotency_key: str | None = None,
	details: dict | list | None = None,
	retry_status: str | None = None,
) -> str | None:
	"""Insert an audit row. Never raises - auditing must not break the operation being audited.

	Note for callers: log failures *after* rolling a savepoint back, otherwise the row itself is
	rolled back together with the failed work.
	"""
	try:
		doc = frappe.get_doc(
			{
				"doctype": AUDIT_DT,
				"ppc_plan": plan,
				"work_order": work_order,
				"job_card": job_card,
				"action": action,
				"result": result,
				"user": frappe.session.user,
				"timestamp": now_datetime(),
				"old_start_date": dt_str(old_start),
				"old_end_date": dt_str(old_end),
				"new_start_date": dt_str(new_start),
				"new_end_date": dt_str(new_end),
				"reason": reason,
				"error_details": error,
				"idempotency_key": idempotency_key,
				"details": json.dumps(details, default=str) if details is not None else None,
				"retry_status": retry_status or ("Pending" if result == R_FAILED else "Not Required"),
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(title="PPC audit log write failed", message=frappe.get_traceback())
		return None


def resolve_failures(plan: str, *, work_order: str | None = None, idempotency_key: str | None = None, to="Resolved"):
	"""Mark earlier failed rows as handled once a retry has succeeded."""
	filters = {"ppc_plan": plan, "result": R_FAILED, "retry_status": "Pending"}
	if work_order:
		filters["work_order"] = work_order
	if idempotency_key:
		filters["idempotency_key"] = idempotency_key
	for name in frappe.get_all(AUDIT_DT, filters=filters, pluck="name"):
		frappe.db.set_value(AUDIT_DT, name, "retry_status", to, update_modified=False)
