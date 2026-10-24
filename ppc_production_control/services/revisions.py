"""Baseline and revision history of Job Card schedules.

Three things are kept strictly apart
------------------------------------
Original (baseline)   the first accepted schedule: `ppc_original_start/end` + revision 0. Captured once,
                      never changed afterwards (a `validate` guard refuses edits).
Current (revised)     the schedule ERPNext holds now (`expected_start/end_date`, derived from the Job Card's
                      `scheduled_time_logs`). PPC mirrors the last *accepted* value in `ppc_revised_*` so that
                      any later divergence (someone editing the card by hand) is detectable.
Actual                `actual_start/end_date` recorded by ERPNext from the time logs. PPC never writes them.

Every accepted reschedule appends one "PPC Schedule Revision" row, in the same database transaction as the
schedule change. A change that fails is rolled back and therefore never appears as a revision.
"""

import uuid

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from ppc_production_control.services import audit
from ppc_production_control.services.constants import A_CORRECT_REVISION, R_SUCCESS
from ppc_production_control.services.utils import dt_str

REVISION_DT = "PPC Schedule Revision"

BASELINE_CREATED = "Created"
BASELINE_INITIALIZED = "Initialized from existing schedule"
BASELINE_AUTO = "Auto-scheduled"

BASELINE_NOTES = {
	BASELINE_CREATED: "Initial schedule",
	BASELINE_INITIALIZED: "Baseline initialized from existing schedule",
	BASELINE_AUTO: "Initial schedule set by PPC auto-scheduling",
}

SOURCES = ("Gantt Drag", "Gantt Resize", "Date Dialog", "Cascade", "Auto Schedule", "Reconciliation", "Retry", "Migration", "API")

LEGACY_NOTE = "Schedule changed before revision history existed; who, when and why were not recorded"


def new_correlation_id() -> str:
	return uuid.uuid4().hex[:16]


def _card(name: str) -> frappe._dict | None:
	return frappe.db.get_value(
		"Job Card",
		name,
		[
			"name", "work_order", "operation", "workstation", "docstatus", "status", "ppc_production_plan",
			"expected_start_date", "expected_end_date", "ppc_original_start", "ppc_original_end",
			"ppc_schedule_revision", "ppc_revised_start", "ppc_revised_end", "ppc_baseline_source",
			"ppc_baseline_captured_at", "ppc_baseline_captured_by", "ppc_last_rescheduled_at",
			"ppc_last_rescheduled_by", "ppc_schedule_sync_status", "actual_start_date", "actual_end_date", "modified",
		],
		as_dict=True,
	)


def _context(card) -> dict:
	"""Denormalised reference data so a revision stays readable after the documents it describes are gone."""
	sales_order = frappe.db.get_value("Work Order", card.work_order, "sales_order") if card.work_order else None
	return {
		"job_card": card.name,
		"work_order": card.work_order,
		"ppc_plan": card.ppc_production_plan,
		"sales_order": sales_order,
		"operation": card.operation,
		"workstation": card.workstation,
	}


def _insert(values: dict):
	doc = frappe.get_doc({"doctype": REVISION_DT, **values})
	doc.flags.ppc_revision_write = True
	doc.insert(ignore_permissions=True)
	return doc


def capture_baseline(job_card: str, source: str = BASELINE_CREATED, *, correlation_id: str | None = None) -> bool:
	"""Record the first accepted schedule of a Job Card. Idempotent: returns False when a baseline already
	exists or the card has no schedule yet. Never overwrites an existing baseline."""
	card = _card(job_card)
	if not card or card.ppc_original_start or not (card.expected_start_date and card.expected_end_date):
		return False
	now, user = now_datetime(), frappe.session.user
	_insert(
		{
			**_context(card),
			"revision_no": 0,
			"revision_type": "Baseline",
			"accepted_start": card.expected_start_date,
			"accepted_end": card.expected_end_date,
			"change_source": "Baseline",
			"change_reason": BASELINE_NOTES.get(source, source),
			"baseline_note": BASELINE_NOTES.get(source, source),
			"changed_by": user,
			"changed_at": now,
			"correlation_id": correlation_id,
		}
	)
	frappe.db.set_value(
		"Job Card",
		job_card,
		{
			"ppc_original_start": card.expected_start_date,
			"ppc_original_end": card.expected_end_date,
			"ppc_baseline_captured_at": now,
			"ppc_baseline_captured_by": user,
			"ppc_baseline_source": source,
			"ppc_revised_start": card.expected_start_date,
			"ppc_revised_end": card.expected_end_date,
			"ppc_schedule_revision": 0,
			"ppc_schedule_sync_status": "Synced",
		},
		update_modified=False,
	)
	return True


def backfill_history(job_card: str) -> bool:
	"""Give a Job Card that already carries a baseline (the snapshot PPC 1.0.0 stored) the history it lacks.

	Adds revision 0 from the existing `ppc_original_*` values - which are never changed - and, when the card's
	schedule has moved since, one explanatory revision so that the history ends at the schedule ERPNext holds.
	Nothing is invented: who/when/why of an earlier move are unknown and the row says so. Idempotent."""
	card = _card(job_card)
	if not card or not (card.ppc_original_start and card.ppc_original_end) or frappe.db.exists(REVISION_DT, {"job_card": job_card}):
		return False
	created = frappe.db.get_value("Job Card", job_card, "creation")
	captured_at = card.ppc_baseline_captured_at or created
	source = card.ppc_baseline_source or BASELINE_INITIALIZED
	context = _context(card)
	_insert(
		{
			**context,
			"revision_no": 0,
			"revision_type": "Baseline",
			"accepted_start": card.ppc_original_start,
			"accepted_end": card.ppc_original_end,
			"change_source": "Baseline",
			"change_reason": BASELINE_NOTES.get(source, source),
			"baseline_note": BASELINE_NOTES.get(source, source),
			"changed_by": card.ppc_baseline_captured_by,
			"changed_at": captured_at,
		}
	)
	moved = bool(
		card.expected_start_date
		and card.expected_end_date
		and not (
			get_datetime(card.expected_start_date).replace(microsecond=0) == get_datetime(card.ppc_original_start).replace(microsecond=0)
			and get_datetime(card.expected_end_date).replace(microsecond=0) == get_datetime(card.ppc_original_end).replace(microsecond=0)
		)
	)
	current = (card.expected_start_date, card.expected_end_date) if moved else (card.ppc_original_start, card.ppc_original_end)
	if moved:
		_insert(
			{
				**context,
				"revision_no": 1,
				"revision_type": "Revision",
				"previous_start": card.ppc_original_start,
				"previous_end": card.ppc_original_end,
				"proposed_start": current[0],
				"proposed_end": current[1],
				"accepted_start": current[0],
				"accepted_end": current[1],
				"change_source": "Migration",
				"change_reason": LEGACY_NOTE,
				"changed_at": card.modified,
			}
		)
	values = {
		"ppc_baseline_source": source,
		"ppc_baseline_captured_at": captured_at,
		"ppc_revised_start": current[0],
		"ppc_revised_end": current[1],
		"ppc_schedule_revision": 1 if moved else 0,
	}
	if not card.ppc_schedule_sync_status or card.ppc_schedule_sync_status == "Synced":
		values["ppc_schedule_sync_status"] = "Synced"
	frappe.db.set_value("Job Card", job_card, values, update_modified=False)
	return True


def record_revision(
	job_card: str,
	*,
	previous: tuple,
	proposed: tuple,
	accepted: tuple,
	reason: str | None,
	source: str,
	correlation_id: str,
	wo_change: dict | None = None,
):
	"""Append the next revision of a Job Card and move its 'accepted current schedule' pointers.

	Must be called inside the same savepoint as the schedule change (the caller holds the row lock)."""
	card = _card(job_card)
	number = cint(card.ppc_schedule_revision) + 1
	now, user = now_datetime(), frappe.session.user
	wo_change = wo_change or {}
	doc = _insert(
		{
			**_context(card),
			"revision_no": number,
			"revision_type": "Revision",
			"previous_start": previous[0],
			"previous_end": previous[1],
			"proposed_start": proposed[0],
			"proposed_end": proposed[1],
			"accepted_start": accepted[0],
			"accepted_end": accepted[1],
			"change_reason": reason,
			"change_source": source if source in SOURCES else "API",
			"changed_by": user,
			"changed_at": now,
			"correlation_id": correlation_id,
			"work_order_previous_start": wo_change.get("old_start"),
			"work_order_previous_end": wo_change.get("old_end"),
			"work_order_new_start": wo_change.get("new_start"),
			"work_order_new_end": wo_change.get("new_end"),
		}
	)
	frappe.db.set_value(
		"Job Card",
		job_card,
		{
			"ppc_schedule_revision": number,
			"ppc_revised_start": accepted[0],
			"ppc_revised_end": accepted[1],
			"ppc_last_rescheduled_at": now,
			"ppc_last_rescheduled_by": user,
			"ppc_schedule_sync_status": "Synced",
		},
		update_modified=False,
	)
	return doc


def mark_sync_failed(job_card: str):
	"""Visible flag after a rolled-back attempt; cleared by the next successful reschedule or retry."""
	frappe.db.set_value("Job Card", job_card, "ppc_schedule_sync_status", "Failed", update_modified=False)


# --------------------------------------------------------------------------------------- reading
def _shift_text(label: str, minutes: float) -> str:
	if not minutes:
		return _("{0} unchanged").format(label)
	sign = "+" if minutes > 0 else "-"
	m = int(abs(round(minutes)))
	d, rem = divmod(m, 1440)
	h, mi = divmod(rem, 60)
	parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{mi}m" if mi else ""]
	return _("{0} moved by {1}{2}").format(label, sign, " ".join(p for p in parts if p))


def describe_difference(baseline: tuple, current: tuple, revision: int) -> dict:
	if not (baseline[0] and baseline[1] and current[0] and current[1]):
		return {"start_minutes": None, "end_minutes": None, "text": ""}
	ds = (get_datetime(current[0]) - get_datetime(baseline[0])).total_seconds() / 60
	de = (get_datetime(current[1]) - get_datetime(baseline[1])).total_seconds() / 60
	text = f"{_shift_text(_('Start'), ds)}; {_shift_text(_('End'), de)}"
	if revision:
		text += "; " + _("revision {0}").format(revision)
	return {"start_minutes": ds, "end_minutes": de, "text": text}


def get_history(job_card: str) -> dict:
	card = _card(job_card)
	if not card:
		frappe.throw(_("Job Card {0} does not exist.").format(job_card), frappe.DoesNotExistError)
	rows = frappe.get_all(
		REVISION_DT,
		filters={"job_card": job_card},
		fields=[
			"name", "revision_no", "revision_type", "previous_start", "previous_end", "proposed_start", "proposed_end",
			"accepted_start", "accepted_end", "change_reason", "change_source", "changed_by", "changed_at",
			"sync_status", "correlation_id", "baseline_note", "work_order_previous_start", "work_order_previous_end",
			"work_order_new_start", "work_order_new_end", "corrected", "correction_reason",
		],
		order_by="revision_no asc, creation asc",
		limit_page_length=500,
	)
	baseline_label = None
	if card.ppc_original_start:
		baseline_label = BASELINE_NOTES.get(card.ppc_baseline_source) or _("Baseline")
	diff = describe_difference(
		(card.ppc_original_start, card.ppc_original_end),
		(card.expected_start_date, card.expected_end_date),
		cint(card.ppc_schedule_revision),
	)
	drift = bool(
		card.ppc_revised_start
		and card.expected_start_date
		and (get_datetime(card.ppc_revised_start) != get_datetime(card.expected_start_date)
			or get_datetime(card.ppc_revised_end) != get_datetime(card.expected_end_date))
	)
	fmt = lambda v: dt_str(v) if v else None  # noqa: E731
	return {
		"job_card": job_card,
		"work_order": card.work_order,
		"plan": card.ppc_production_plan,
		"status": card.status,
		"modified": str(card.modified),
		"revision": cint(card.ppc_schedule_revision),
		"baseline": {"start": fmt(card.ppc_original_start), "end": fmt(card.ppc_original_end), "label": baseline_label,
			"source": card.ppc_baseline_source, "captured_at": fmt(card.ppc_baseline_captured_at), "captured_by": card.ppc_baseline_captured_by},
		"current": {"start": fmt(card.expected_start_date), "end": fmt(card.expected_end_date)},
		"accepted": {"start": fmt(card.ppc_revised_start), "end": fmt(card.ppc_revised_end)},
		"actual": {"start": fmt(card.actual_start_date), "end": fmt(card.actual_end_date)},
		"last_rescheduled_by": card.ppc_last_rescheduled_by,
		"last_rescheduled_at": fmt(card.ppc_last_rescheduled_at),
		"sync_status": "Drift" if drift and card.ppc_schedule_sync_status != "Failed" else (card.ppc_schedule_sync_status or ("Not baselined" if not card.ppc_original_start else "Synced")),
		"difference": diff,
		"has_baseline": bool(card.ppc_original_start),
		"revisions": [{k: (dt_str(v) if hasattr(v, "strftime") else v) for k, v in r.items()} for r in rows],
	}


# --------------------------------------------------------------------------------- correction
def correct_revision(revision: str, reason: str) -> dict:
	"""The only way a revision row is ever touched: an annotated, audited correction note.

	Dates and other facts of the revision stay exactly as recorded. System Manager only."""
	if "System Manager" not in frappe.get_roles() and frappe.session.user != "Administrator":
		frappe.throw(_("Only a System Manager may correct a schedule revision."), frappe.PermissionError)
	reason = (reason or "").strip()
	if len(reason) < 10:
		frappe.throw(_("Describe the correction (at least 10 characters)."))
	doc = frappe.get_doc(REVISION_DT, revision)
	doc.flags.ppc_audited_correction = True
	doc.corrected = 1
	doc.correction_reason = reason
	doc.corrected_by = frappe.session.user
	doc.save(ignore_permissions=True)
	audit.log(
		A_CORRECT_REVISION, R_SUCCESS, plan=doc.ppc_plan, work_order=doc.work_order, job_card=doc.job_card,
		reason=reason, details={"revision": doc.name, "revision_no": doc.revision_no},
	)
	return {"revision": doc.name, "corrected": True}
