"""Scheduling: validation, drag-and-drop rescheduling, dependency cascade, Work Order sync.

ERPNext field mapping (verified against 15.108.x source - see README "Compatibility matrix"):

Job Card   `scheduled_time_logs` (child "Job Card Scheduled Time": from_time / to_time / time_in_mins)
           is the source of truth that ERPNext's own capacity planner reads. On every save
           `Job Card.set_expected_and_actual_time()` recomputes `expected_start_date` /
           `expected_end_date` from those rows - so we edit the *rows* and let ERPNext derive the
           header dates. A Job Card whose rows are empty (capacity planning disabled) gets one row.
Work Order `planned_start_date`, `planned_end_date` are `allow_on_submit`; they are updated with a
           normal `save()`. Actual dates are never touched.

Policy for a Work Order's planned dates (one consistent rule, applied after every change):
   planned_start = earliest expected start of its *remaining* (not yet completed) Job Cards
   planned_end   = latest   expected end   of its *remaining* Job Cards
"""

from collections import defaultdict
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime

from ppc_production_control.services import audit, revisions
from ppc_production_control.services.constants import (
	A_AUTO_SCHEDULE,
	A_RESCHEDULE,
	A_SYNC_WO_SCHEDULE,
	CAP_OVERRIDE,
	CAP_RESCHEDULE,
	JC_DONE_STATUSES,
	JC_RESCHEDULABLE_STATUSES,
	R_FAILED,
	R_SUCCESS,
	WO_LOCKED_STATUSES,
)
from ppc_production_control.services.permissions import has_capability, require_capability
from ppc_production_control.services.utils import dt_str, get_settings, savepoint

JC_FIELDS = [
	"name",
	"work_order",
	"operation",
	"operation_id",
	"workstation",
	"sequence_id",
	"for_quantity",
	"status",
	"docstatus",
	"expected_start_date",
	"expected_end_date",
	"time_required",
	"ppc_production_plan",
	"modified",
]


# =====================================================================================
# Pure helpers (no database access) - unit tested in tests/test_scheduling_pure.py
# =====================================================================================


def as_dt(value) -> datetime | None:
	if value in (None, ""):
		return None
	try:
		return get_datetime(value)
	except Exception:
		return None


def minutes_between(start: datetime, end: datetime) -> float:
	return round((end - start).total_seconds() / 60.0, 2)


def peak_concurrency(intervals: list[tuple[datetime, datetime]]) -> int:
	"""Largest number of intervals that overlap at any single instant (half-open [start, end))."""
	events = []
	for start, end in intervals:
		if start is None or end is None or end <= start:
			continue
		events.append((start, 1))
		events.append((end, -1))
	# at equal timestamps process ends (-1) before starts (+1): back-to-back is not an overlap
	events.sort(key=lambda e: (e[0], e[1]))
	current = peak = 0
	for _t, delta in events:
		current += delta
		peak = max(peak, current)
	return peak


def group_by_sequence(jobs: list[dict]) -> list[list[dict]]:
	"""Job Cards of one Work Order grouped by `sequence_id`, ascending. Operations sharing a
	sequence_id may run in parallel; every group must finish before the next may start."""
	groups: dict[int, list[dict]] = defaultdict(list)
	for job in jobs:
		groups[cint(job.get("sequence_id"))].append(job)
	return [groups[k] for k in sorted(groups)]


def dependency_neighbours(jobs: list[dict], job_name: str) -> tuple[list[dict], list[dict]]:
	"""(predecessors, successors) of a Job Card: the nearest lower / higher sequence group."""
	groups = group_by_sequence(jobs)
	for i, group in enumerate(groups):
		if any(j["name"] == job_name for j in group):
			pred = groups[i - 1] if i > 0 else []
			succ = groups[i + 1] if i + 1 < len(groups) else []
			return pred, succ
	return [], []


def compute_cascade(jobs: list[dict], moved: str, new_start: datetime, new_end: datetime) -> dict:
	"""Which downstream Job Cards must shift so that no operation starts before the previous
	sequence group has finished? Durations are preserved.

	Returns {"shifts": [{name, old_start, old_end, new_start, new_end}], "blocked": [{name, reason}]}
	"""
	jobs = [dict(j) for j in jobs]
	for j in jobs:
		j["_start"] = as_dt(j.get("expected_start_date"))
		j["_end"] = as_dt(j.get("expected_end_date"))
		if j["name"] == moved:
			j["_start"], j["_end"] = new_start, new_end

	groups = group_by_sequence(jobs)
	idx = next((i for i, g in enumerate(groups) if any(j["name"] == moved for j in g)), None)
	shifts, blocked = [], []
	if idx is None:
		return {"shifts": shifts, "blocked": blocked}

	def latest_end(group, key):
		return max((j[key] for j in group if j[key]), default=None)

	# the group's end before the move (original dates) and after it (moved job replaced)
	orig_prev_end = max(
		(as_dt(j.get("expected_end_date")) for j in groups[idx] if j.get("expected_end_date")), default=None
	)
	prev_end = latest_end(groups[idx], "_end")
	for group in groups[idx + 1 :]:
		group_orig_end = max(
			(as_dt(j.get("expected_end_date")) for j in group if j.get("expected_end_date")), default=None
		)
		group_end = None
		for job in group:
			if job["_start"] is None or job["_end"] is None:
				continue  # unscheduled: nothing to violate
			if prev_end is not None and job["_start"] < prev_end:
				if not is_movable(job):
					blocked.append(
						{
							"name": job["name"],
							"reason": _("{0} is {1} and cannot be moved.").format(job["name"], job["status"]),
						}
					)
				else:
					# keep the slack the planner (or ERPNext's capacity planner) left between the groups
					original_start = as_dt(job.get("expected_start_date"))
					gap = (
						max(timedelta(0), original_start - orig_prev_end)
						if orig_prev_end is not None and original_start is not None
						else timedelta(0)
					)
					delta = (prev_end + gap) - job["_start"]
					shifts.append(
						{
							"name": job["name"],
							"old_start": job["_start"],
							"old_end": job["_end"],
							"new_start": job["_start"] + delta,
							"new_end": job["_end"] + delta,
						}
					)
					job["_start"] += delta
					job["_end"] += delta
			group_end = max(group_end, job["_end"]) if group_end else job["_end"]
		orig_prev_end = group_orig_end or orig_prev_end
		if group_end:
			prev_end = group_end
	return {"shifts": shifts, "blocked": blocked}


def is_movable(job: dict) -> bool:
	return cint(job.get("docstatus")) == 0 and job.get("status") in JC_RESCHEDULABLE_STATUSES


def plan_sequential_schedule(
	groups: list[list[dict]], anchor: datetime, duration_of
) -> list[dict]:
	"""Lay unscheduled Job Cards out group by group, starting at `anchor`.

	`duration_of(job) -> minutes`. Already scheduled Job Cards are kept and push the next
	group's start. Returns [{name, start, end}] for the *unscheduled* ones only.
	"""
	cursor = anchor
	out = []
	for group in groups:
		group_end = cursor
		for job in group:
			start, end = as_dt(job.get("expected_start_date")), as_dt(job.get("expected_end_date"))
			if start and end:
				group_end = max(group_end, end)
				continue
			minutes = max(1.0, flt(duration_of(job)))
			start = cursor
			end = start + timedelta(minutes=minutes)
			out.append({"name": job["name"], "start": start, "end": end})
			group_end = max(group_end, end)
		cursor = group_end
	return out


def shift_rows(rows: list[tuple[datetime, datetime]], delta: timedelta) -> list[tuple[datetime, datetime]]:
	return [(a + delta, b + delta) for a, b in rows]


# =====================================================================================
# Database-backed helpers
# =====================================================================================


def get_work_order_jobs(work_order: str, include_cancelled: bool = False) -> list[dict]:
	filters = {"work_order": work_order}
	if not include_cancelled:
		filters["docstatus"] = ["<", 2]
	return frappe.get_all(
		"Job Card", filters=filters, fields=JC_FIELDS, order_by="sequence_id asc, creation asc", limit_page_length=0
	)


def get_workstation_info(workstation: str | None) -> dict:
	if not workstation:
		return {"capacity": 1, "holiday_list": None}
	row = frappe.db.get_value("Workstation", workstation, ["production_capacity", "holiday_list"], as_dict=True)
	return {"capacity": max(1, cint(row.production_capacity) if row else 1), "holiday_list": row.holiday_list if row else None}


def find_workstation_overlaps(workstation: str, start: datetime, end: datetime, exclude: list[str]) -> list[dict]:
	"""Scheduled, unfinished Job Cards on the same workstation that overlap [start, end)."""
	if not workstation:
		return []
	exclude = exclude or [""]
	return frappe.db.sql(
		"""select name, work_order, operation, status, expected_start_date, expected_end_date
		from `tabJob Card`
		where workstation = %(ws)s and docstatus < 2 and name not in %(exclude)s
			and status not in ('Completed', 'Submitted', 'Cancelled')
			and expected_start_date < %(end)s and expected_end_date > %(start)s
		order by expected_start_date""",
		{"ws": workstation, "start": start, "end": end, "exclude": tuple(exclude)},
		as_dict=True,
	)


def workstation_conflicts(workstation, start, end, exclude) -> list[dict]:
	"""Overlapping Job Cards, but only if the workstation's capacity would actually be exceeded."""
	overlaps = find_workstation_overlaps(workstation, start, end, exclude)
	if not overlaps:
		return []
	capacity = get_workstation_info(workstation)["capacity"]
	intervals = [(start, end)] + [(as_dt(o.expected_start_date), as_dt(o.expected_end_date)) for o in overlaps]
	return overlaps if peak_concurrency(intervals) > capacity else []


def holiday_dates(workstation: str | None, start: datetime, end: datetime) -> list:
	info = get_workstation_info(workstation)
	holiday_list = info["holiday_list"]
	if not holiday_list:
		return []
	if cint(frappe.db.get_single_value("Manufacturing Settings", "allow_production_on_holidays")):
		return []
	days = {getdate(start), getdate(end)}
	return sorted(
		str(d)
		for d in frappe.get_all(
			"Holiday",
			filters={"parent": holiday_list, "holiday_date": ["in", list(days)]},
			pluck="holiday_date",
		)
	)


def _fmt(value) -> str:
	return frappe.utils.format_datetime(value, "yyyy-MM-dd HH:mm") if value else "-"


# =====================================================================================
# Validation
# =====================================================================================


def validate_schedule(
	job_card: str,
	new_start,
	new_end,
	*,
	shift_dependents: bool = False,
	jobs: list[dict] | None = None,
) -> dict:
	"""Check a proposed schedule without changing anything.

	Returns {
	  "errors":   hard failures - the change can never be saved,
	  "warnings": [{"code", "message", "needs_override"}],
	  "cascade":  {"shifts": [...], "blocked": [...]},   # when shift_dependents
	  "conflicts": [{"name", ...}]                       # overlapping Job Cards on the workstation
	}
	"""
	result = {"errors": [], "warnings": [], "cascade": {"shifts": [], "blocked": []}, "conflicts": []}

	start, end = as_dt(new_start), as_dt(new_end)
	if not start or not end:
		result["errors"].append(_("A valid start and end date/time is required."))
		return result
	if end <= start:
		result["errors"].append(_("The end date/time must be after the start date/time."))
		return result

	jc = frappe.db.get_value("Job Card", job_card, JC_FIELDS, as_dict=True)
	if not jc:
		result["errors"].append(_("Job Card {0} does not exist.").format(job_card))
		return result

	if jc.docstatus == 2:
		result["errors"].append(_("Job Card {0} is cancelled and cannot be rescheduled.").format(job_card))
	elif jc.docstatus == 1:
		result["errors"].append(
			_("Job Card {0} is submitted ({1}) and cannot be rescheduled.").format(job_card, jc.status)
		)
	elif jc.status not in JC_RESCHEDULABLE_STATUSES:
		result["errors"].append(
			_("Job Card {0} is {1}; only Open, Material Transferred or On Hold operations can be rescheduled.").format(
				job_card, jc.status
			)
		)

	wo = frappe.db.get_value(
		"Work Order",
		jc.work_order,
		["name", "docstatus", "status", "expected_delivery_date", "planned_start_date"],
		as_dict=True,
	)
	if not wo:
		result["errors"].append(_("Work Order {0} of this Job Card does not exist.").format(jc.work_order))
	elif wo.docstatus == 2 or wo.status in WO_LOCKED_STATUSES:
		result["errors"].append(
			_("Work Order {0} is {1}; its Job Cards cannot be rescheduled.").format(wo.name, wo.status)
		)
	if result["errors"]:
		return result

	jobs = jobs if jobs is not None else get_work_order_jobs(jc.work_order)

	# --- dependencies ------------------------------------------------------------------
	preds, succs = dependency_neighbours(jobs, job_card)
	pred_end = max((as_dt(p["expected_end_date"]) for p in preds if p["expected_end_date"]), default=None)
	if pred_end and start < pred_end:
		names = ", ".join(p["name"] for p in preds if as_dt(p["expected_end_date"]) and as_dt(p["expected_end_date"]) > start)
		result["warnings"].append(
			{
				"code": "predecessor",
				"needs_override": True,
				"message": _(
					"Starts before the preceding operation ends ({0}). Operation sequence is violated by: {1}."
				).format(_fmt(pred_end), names),
			}
		)

	succ_start = min((as_dt(s["expected_start_date"]) for s in succs if s["expected_start_date"]), default=None)
	cascade = compute_cascade(jobs, job_card, start, end)
	if succ_start and end > succ_start:
		if shift_dependents:
			result["cascade"] = cascade
			for blocker in cascade["blocked"]:
				result["errors"].append(blocker["reason"])
			if cascade["shifts"]:
				result["warnings"].append(
					{
						"code": "cascade",
						"needs_override": False,
						"message": _("{0} following operation(s) will be shifted later to keep the sequence.").format(
							len(cascade["shifts"])
						),
					}
				)
		else:
			result["warnings"].append(
				{
					"code": "successor",
					"needs_override": True,
					"message": _(
						"Ends after the next operation starts ({0}). Enable 'shift dependent operations' or choose an earlier end."
					).format(_fmt(succ_start)),
				}
			)
	elif shift_dependents:
		result["cascade"] = cascade

	# --- workstation capacity ----------------------------------------------------------
	moved_names = [job_card] + [s["name"] for s in result["cascade"]["shifts"]]
	conflicts = workstation_conflicts(jc.workstation, start, end, moved_names)
	if conflicts:
		result["conflicts"] = conflicts
		result["warnings"].append(
			{
				"code": "workstation",
				"needs_override": True,
				"message": _("Workstation {0} is already booked in this period by: {1}.").format(
					jc.workstation, ", ".join(c.name for c in conflicts[:5])
				),
			}
		)
	for shift in result["cascade"]["shifts"]:
		sjc = next((j for j in jobs if j["name"] == shift["name"]), None)
		if sjc and sjc.workstation:
			also = workstation_conflicts(sjc.workstation, shift["new_start"], shift["new_end"], moved_names)
			if also:
				result["warnings"].append(
					{
						"code": "workstation",
						"needs_override": True,
						"message": _("Shifted operation {0} would overlap bookings on workstation {1}.").format(
							shift["name"], sjc.workstation
						),
					}
				)

	# --- informational -----------------------------------------------------------------
	holidays = holiday_dates(jc.workstation, start, end)
	if holidays:
		result["warnings"].append(
			{
				"code": "holiday",
				"needs_override": False,
				"message": _("Falls on a non-working day of workstation {0}: {1}.").format(
					jc.workstation, ", ".join(holidays)
				),
			}
		)
	if start < now_datetime():
		result["warnings"].append(
			{"code": "past", "needs_override": False, "message": _("The new start is in the past.")}
		)
	if wo and wo.expected_delivery_date and getdate(end) > getdate(wo.expected_delivery_date):
		result["warnings"].append(
			{
				"code": "delivery",
				"needs_override": False,
				"message": _("Ends after the Work Order's expected delivery date ({0}).").format(
					wo.expected_delivery_date
				),
			}
		)
	return result


# =====================================================================================
# Applying a schedule
# =====================================================================================


def _scheduled_rows(jc) -> list[tuple[datetime, datetime]]:
	return [
		(as_dt(r.from_time), as_dt(r.to_time))
		for r in jc.get("scheduled_time_logs") or []
		if r.from_time and r.to_time
	]


class StaleScheduleError(frappe.ValidationError):
	"""Someone changed the Job Card after the screen loaded it."""


def ensure_baseline(job_card: str):
	"""Make sure the card has a baseline *before* PPC changes its schedule. A card that predates the
	app (or was scheduled outside PPC) gets one labelled "initialized from existing schedule" - never
	passed off as the true original.

	Call this *before* loading the document you are about to save: the baseline is written straight to
	the database, and a document loaded earlier would overwrite it with stale blanks."""
	revisions.capture_baseline(job_card, revisions.BASELINE_INITIALIZED)


def apply_dates(jc, start: datetime, end: datetime, mode: str = "move"):
	"""Write a new schedule onto a (draft) Job Card document and save it.

	A pure move (same duration) shifts every scheduled row by the same delta, preserving
	ERPNext's working-hour fragmentation. A resize (or a Job Card without rows) collapses the
	schedule to a single row covering [start, end].
	"""
	rows = _scheduled_rows(jc)
	if rows and mode == "move":
		old_start = min(r[0] for r in rows)
		old_end = max(r[1] for r in rows)
		if (end - start) == (old_end - old_start):
			delta = start - old_start
			for r in jc.scheduled_time_logs:
				if r.from_time and r.to_time:
					r.from_time = as_dt(r.from_time) + delta
					r.to_time = as_dt(r.to_time) + delta
			jc.save()
			return
	jc.set(
		"scheduled_time_logs",
		[{"from_time": start, "to_time": end, "time_in_mins": minutes_between(start, end)}],
	)
	jc.save()


def _same_instant(a, b) -> bool:
	"""Equality at the precision users and the audit trail work with (whole seconds)."""
	a, b = as_dt(a), as_dt(b)
	return bool(a and b and a.replace(microsecond=0) == b.replace(microsecond=0))


def reschedule_job_card(
	job_card: str,
	new_start,
	new_end,
	*,
	reason: str | None = None,
	mode: str = "move",
	confirmed: bool = False,
	override_conflicts: bool = False,
	shift_dependents: bool = False,
	expected_modified: str | None = None,
	source: str | None = None,
	correlation_id: str | None = None,
) -> dict:
	"""Validate and persist a reschedule *atomically*.

	Inside one savepoint: lock the Job Card and Work Order rows, reject a stale screen, re-validate,
	write the real ERPNext schedule (`scheduled_time_logs`), read it back and verify it, synchronise the
	Work Order, verify that too, and only then append the revision rows. Any failure rolls the whole unit
	back - no half-applied schedule and no revision for a change that did not happen.

	Returns {"ok": True, "changed": [...], "work_orders": [...]} or
	{"ok": False, "errors": [...], "warnings": [...], "needs_confirmation": bool, "needs_override": bool}.
	"""
	require_capability(CAP_RESCHEDULE)
	if mode not in ("move", "resize"):
		frappe.throw(_("Invalid mode {0}.").format(mode))
	if not frappe.has_permission("Job Card", "write", doc=job_card):
		frappe.throw(_("No write permission on Job Card {0}").format(job_card), frappe.PermissionError)

	start, end = as_dt(new_start), as_dt(new_end)
	check = validate_schedule(job_card, start, end, shift_dependents=shift_dependents)
	if check["errors"]:
		return {"ok": False, "errors": check["errors"], "warnings": check["warnings"], "needs_confirmation": False, "needs_override": False}

	hard = [w for w in check["warnings"] if w.get("needs_override")]
	if hard and not override_conflicts:
		return {
			"ok": False, "errors": [], "warnings": check["warnings"], "conflicts": check["conflicts"],
			"needs_confirmation": True, "needs_override": True, "can_override": has_capability(CAP_OVERRIDE),
		}
	if hard and override_conflicts:
		require_capability(CAP_OVERRIDE)
	if check["warnings"] and not confirmed and not override_conflicts:
		return {
			"ok": False, "errors": [], "warnings": check["warnings"], "conflicts": check["conflicts"],
			"needs_confirmation": True, "needs_override": False, "can_override": has_capability(CAP_OVERRIDE),
		}

	for shift in check["cascade"]["shifts"]:
		if not frappe.has_permission("Job Card", "write", doc=shift["name"]):
			frappe.throw(_("No write permission on Job Card {0}").format(shift["name"]), frappe.PermissionError)

	source = source or ("Gantt Resize" if mode == "resize" else "Gantt Drag")
	correlation = correlation_id or revisions.new_correlation_id()
	head = frappe.db.get_value("Job Card", job_card, ["work_order", "ppc_production_plan", "expected_start_date", "expected_end_date"], as_dict=True)
	proposal = {
		"start": dt_str(start), "end": dt_str(end), "mode": mode, "reason": reason, "source": source,
		"shift_dependents": bool(shift_dependents), "override_conflicts": bool(override_conflicts),
	}
	try:
		with savepoint():
			changed, wo_results = _apply_accepted_change(
				job_card, start, end, mode, check, reason, source, correlation, expected_modified, override_conflicts
			)
	except StaleScheduleError as exc:
		return {"ok": False, "stale": True, "errors": [str(exc)], "warnings": [], "needs_confirmation": False, "needs_override": False}
	except Exception as exc:
		# the savepoint has rolled everything back; record the failure so it can be retried
		from ppc_production_control.services.utils import error_text

		message = error_text(exc)
		revisions.mark_sync_failed(job_card)
		audit.log(
			A_RESCHEDULE, R_FAILED, plan=head.ppc_production_plan if head else None, work_order=head.work_order if head else None,
			job_card=job_card, old_start=head.expected_start_date if head else None, old_end=head.expected_end_date if head else None,
			new_start=start, new_end=end, reason=reason, error=message, idempotency_key=correlation,
			details={"proposal": proposal, "correlation_id": correlation},
		)
		return {"ok": False, "errors": [message], "warnings": [], "needs_confirmation": False, "needs_override": False}

	plan_names = set()
	for c in changed:
		plan = frappe.db.get_value("Job Card", c["job_card"], "ppc_production_plan")
		plan_names.add(plan)
		audit.log(
			A_RESCHEDULE, R_SUCCESS, plan=plan, work_order=frappe.db.get_value("Job Card", c["job_card"], "work_order"),
			job_card=c["job_card"], old_start=c["old_start"], old_end=c["old_end"], new_start=c["new_start"],
			new_end=c["new_end"], reason=reason or (_("Cascade shift") if c["job_card"] != job_card else None),
			idempotency_key=correlation,
			details={"mode": mode, "override": bool(override_conflicts), "revision": c["revision"], "correlation_id": correlation,
				"warnings": [w["message"] for w in check["warnings"]]},
		)
	for plan in plan_names:
		if plan:
			frappe.db.set_value("Production Plan", plan, "ppc_last_scheduling_sync", now_datetime(), update_modified=False)

	return {
		"ok": True,
		"changed": [{k: dt_str(v) if k in ("old_start", "old_end", "new_start", "new_end") else v for k, v in c.items()} for c in changed],
		"work_orders": [w for w in wo_results if w],
		"warnings": check["warnings"],
		"correlation_id": correlation,
		"modified": str(frappe.db.get_value("Job Card", job_card, "modified")),  # the concurrency token for the next change
	}


def _apply_accepted_change(job_card, start, end, mode, check, reason, source, correlation, expected_modified, override_conflicts):
	"""The atomic unit. Must run inside a savepoint; raises on any inconsistency."""
	# 1. lock + stale check ---------------------------------------------------------------------
	locked_modified = frappe.db.get_value("Job Card", job_card, "modified", for_update=True)
	if expected_modified and str(locked_modified) != str(expected_modified):
		raise StaleScheduleError(_("Job Card {0} was changed by someone else. Refresh and try again.").format(job_card))
	work_order = frappe.db.get_value("Job Card", job_card, "work_order")
	frappe.db.get_value("Work Order", work_order, "modified", for_update=True)

	# 2. re-validate under the lock (state may have moved since the advisory check) -----------------
	again = validate_schedule(job_card, start, end, shift_dependents=bool(check["cascade"]["shifts"]) or bool(check["cascade"]["blocked"]))
	if again["errors"]:
		raise frappe.ValidationError("; ".join(again["errors"]))
	if [w for w in again["warnings"] if w.get("needs_override")] and not override_conflicts:
		raise frappe.ValidationError(_("The schedule situation changed while saving; review the conflicts and try again."))
	cascade = again["cascade"]["shifts"]

	# 3. write the real ERPNext schedule, read it back, verify --------------------------------------
	targets = [(job_card, start, end, mode, source)] + [(s["name"], s["new_start"], s["new_end"], "move", "Cascade") for s in cascade]
	changed, touched_wos = [], {work_order}
	pending_revisions = []
	for name, t_start, t_end, t_mode, t_source in targets:
		ensure_baseline(name)
		doc = frappe.get_doc("Job Card", name)
		previous = (doc.expected_start_date, doc.expected_end_date)
		apply_dates(doc, t_start, t_end, t_mode)
		saved = frappe.db.get_value("Job Card", name, ["expected_start_date", "expected_end_date"], as_dict=True)
		if not (_same_instant(saved.expected_start_date, t_start) and _same_instant(saved.expected_end_date, t_end)):
			raise frappe.ValidationError(
				_("Job Card {0}: ERPNext stored {1} - {2} instead of the requested {3} - {4}; the change was not applied.").format(
					name, saved.expected_start_date, saved.expected_end_date, t_start, t_end)
			)
		touched_wos.add(doc.work_order)
		pending_revisions.append((name, previous, (t_start, t_end), (saved.expected_start_date, saved.expected_end_date), t_source))

	# 4. Work Order(s): synchronise by the documented policy, then verify ----------------------------
	wo_results = []
	for wo in sorted(touched_wos):
		change = sync_work_order_schedule(wo)
		wo_results.append(change)
		window = work_order_window(wo)
		current = frappe.db.get_value("Work Order", wo, ["planned_start_date", "planned_end_date"], as_dict=True)
		if window and not (_same_instant(current.planned_start_date, window[0]) and _same_instant(current.planned_end_date, window[1])):
			raise frappe.ValidationError(
				_("Work Order {0} did not take the synchronised dates ({1} - {2}); the change was not applied.").format(
					wo, window[0], window[1])
			)

	# 5. only now append the revisions (same transaction) ------------------------------------------------
	wo_by_name = {r["work_order"]: r for r in wo_results if r}
	for name, previous, proposed, accepted, t_source in pending_revisions:
		wo_name = frappe.db.get_value("Job Card", name, "work_order")
		doc = revisions.record_revision(
			name, previous=previous, proposed=proposed, accepted=accepted,
			reason=reason or (_("Cascade shift") if t_source == "Cascade" else None),
			source=t_source, correlation_id=correlation, wo_change=wo_by_name.get(wo_name),
		)
		changed.append({
			"job_card": name, "old_start": previous[0], "old_end": previous[1], "new_start": accepted[0], "new_end": accepted[1],
			"revision": doc.revision_no,
		})
	return changed, wo_results


# =====================================================================================
# Work Order synchronisation
# =====================================================================================


def work_order_window(work_order: str) -> tuple[datetime, datetime] | None:
	"""The Work Order's planned window under PPC's policy: earliest start / latest end of its *remaining*
	(not completed, submitted or cancelled) Job Cards. None when there is nothing to derive it from."""
	jobs = [
		j for j in get_work_order_jobs(work_order)
		if j.expected_start_date and j.expected_end_date and j.status not in JC_DONE_STATUSES
	]
	if not jobs:
		return None
	return min(as_dt(j.expected_start_date) for j in jobs), max(as_dt(j.expected_end_date) for j in jobs)


def work_order_full_window(work_order: str) -> tuple[datetime, datetime] | None:
	"""Earliest start / latest end over *all* live Job Cards, finished ones included. A Work Order that still spans
	its finished operations is not out of step - it just has not been re-aggregated since they finished."""
	jobs = [j for j in get_work_order_jobs(work_order) if j.expected_start_date and j.expected_end_date]
	if not jobs:
		return None
	return min(as_dt(j.expected_start_date) for j in jobs), max(as_dt(j.expected_end_date) for j in jobs)


def sync_work_order_schedule(work_order: str, *, reason: str | None = None) -> dict | None:
	"""Apply the documented policy to the Work Order's planned dates. Returns a change record,
	or None when nothing needed to change (or the Work Order may not be touched)."""
	wo = frappe.get_doc("Work Order", work_order)
	if wo.docstatus == 2 or wo.status in WO_LOCKED_STATUSES:
		return None

	window = work_order_window(work_order)
	if not window:
		return None
	new_start, new_end = window
	old_start, old_end = as_dt(wo.planned_start_date), as_dt(wo.planned_end_date)
	if old_start == new_start and old_end == new_end:
		return None

	if not frappe.has_permission("Work Order", "write", doc=wo):
		frappe.throw(_("No write permission on Work Order {0}").format(wo.name), frappe.PermissionError)

	wo.planned_start_date = new_start
	wo.planned_end_date = new_end
	wo.save()  # supported "update after submit": both fields are allow_on_submit

	audit.log(
		A_SYNC_WO_SCHEDULE, R_SUCCESS, plan=wo.get("production_plan"), work_order=wo.name,
		old_start=old_start, old_end=old_end, new_start=new_start, new_end=new_end,
		reason=reason or _("Derived from Job Card schedule"),
	)
	return {
		"work_order": wo.name,
		"old_start": dt_str(old_start), "old_end": dt_str(old_end),
		"new_start": dt_str(new_start), "new_end": dt_str(new_end),
	}


# =====================================================================================
# Auto scheduling of Job Cards that have no dates (capacity planning disabled)
# =====================================================================================


def operation_minutes(jc: dict, wo, ops_by_id: dict) -> tuple[float, bool]:
	"""(minutes, estimated?) for a Job Card. `estimated` is True when ERPNext holds no time."""
	if flt(jc.get("time_required")) > 0:
		return flt(jc["time_required"]), False
	op = ops_by_id.get(jc.get("operation_id"))
	if op and flt(op.time_in_mins) > 0 and flt(wo.qty) > 0:
		share = flt(jc.get("for_quantity")) / flt(wo.qty) if flt(jc.get("for_quantity")) else 1.0
		return flt(op.time_in_mins) * share, False
	return float(get_settings().default_operation_minutes or 60), True


def auto_schedule_work_order(work_order: str) -> list[dict]:
	"""Give every unscheduled draft Job Card of a submitted Work Order a start/end, sequentially
	from the Work Order's planned start. Existing schedules are never changed."""
	wo = frappe.get_doc("Work Order", work_order)
	if wo.docstatus != 1 or wo.status in WO_LOCKED_STATUSES:
		return []
	jobs = get_work_order_jobs(work_order)
	if not any(not j.expected_start_date and is_movable(j) for j in jobs):
		return []

	ops = {op.name: op for op in wo.operations}
	flags: dict[str, bool] = {}

	def duration(job):
		minutes, estimated = operation_minutes(job, wo, ops)
		flags[job["name"]] = estimated
		return minutes

	anchor = as_dt(wo.planned_start_date) or now_datetime()
	plan = plan_sequential_schedule(group_by_sequence(jobs), anchor, duration)
	results = []
	for item in plan:
		job = next(j for j in jobs if j.name == item["name"])
		if not is_movable(job):
			continue
		jc = frappe.get_doc("Job Card", item["name"])
		with savepoint():
			apply_dates(jc, item["start"], item["end"], "resize")
			revisions.capture_baseline(jc.name, revisions.BASELINE_AUTO)  # its first schedule is this one
		audit.log(
			A_AUTO_SCHEDULE, R_SUCCESS, plan=jc.get("ppc_production_plan"), work_order=work_order, job_card=jc.name,
			new_start=item["start"], new_end=item["end"],
			reason=_("Estimated duration used") if flags.get(jc.name) else _("Scheduled from operation time"),
		)
		results.append({"job_card": jc.name, "start": item["start"], "end": item["end"], "estimated": flags.get(jc.name, False)})
	if results:
		sync_work_order_schedule(work_order, reason=_("Auto schedule"))
	return results
