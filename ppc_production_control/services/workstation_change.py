"""Change the Workstation of a Job Card from the Gantt chart.

The unit of work is the same as for a date change (services/scheduling.py) - lock, re-validate, write through the
Job Card's own document lifecycle, read back, synchronise the Work Order, append ONE revision - with these additions:

* the target machine is **re-checked on the server under a row lock on the Workstation**, so two planners cannot both
  take the same slot; the second one waits for the lock, reads the first one's committed booking and gets a conflict;
* occupancy comes from the Job Cards' real time rows (services/machines.py), honouring the machine's capacity;
* a conflict can be resolved four ways - another machine, the next free slot, pushing the conflicting operation later
  (only when that is safe and previewed), or an audited override by an authorised role with a reason.

Rules for what may move (decided from ERPNext 15.108 behaviour and the existing PPC rules):
* Only a draft Job Card that is Open, Material Transferred or On Hold, **with no recorded time**, can change machine. ERPNext
  books actual time against the Job Card's workstation, so a started card (Work In Progress, or any recorded time log)
  cannot be re-assigned safely; completed, submitted and cancelled cards never.
* Actual execution dates are never touched.
* The Work Order's planned dates are re-derived by the same policy as every other change (earliest start / latest end of
  its remaining Job Cards) - never copied from the card that moved.
* The Work Order Operation row keeps the planned workstation of the BOM; ERPNext itself lets the Job Card differ.
"""

from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from ppc_production_control.services import audit, machines, revisions, scheduling
from ppc_production_control.services.constants import (
	A_CHANGE_WORKSTATION,
	CAP_OVERRIDE,
	CAP_RESCHEDULE,
	JC_RESCHEDULABLE_STATUSES,
	R_FAILED,
	R_SUCCESS,
	R_WARNING,
	WO_LOCKED_STATUSES,
)
from ppc_production_control.services.permissions import has_capability, require_capability
from ppc_production_control.services.utils import begin_read_committed, commit_if_allowed, dt_str, error_text, savepoint

MIN_OVERRIDE_REASON = 10
MAX_PUSHED_OPERATIONS = 5
BAD_MACHINE_STATES = ("Off", "Problem", "Maintenance")
JOB_FIELDS = [
	"name", "work_order", "operation", "operation_id", "workstation", "workstation_type", "sequence_id", "for_quantity", "status",
	"docstatus", "expected_start_date", "expected_end_date", "time_required", "total_time_in_mins", "ppc_production_plan", "modified",
]


class WorkstationConflict(frappe.ValidationError):
	"""The target machine turned out to be taken when the change was about to be saved."""

	def __init__(self, check: dict):
		super().__init__(_("The target Workstation is not available."))
		self.check = check


# =====================================================================================
# Pure helpers (unit tested without a database)
# =====================================================================================


def eligibility_errors(jc: dict, wo: dict | None) -> list[str]:
	"""Why this Job Card may not be given another machine ([] when it may)."""
	errors = []
	name = jc["name"]
	if jc.get("docstatus") == 2 or jc.get("status") == "Cancelled":
		errors.append(_("Job Card {0} is cancelled; a cancelled operation cannot be moved to another Workstation.").format(name))
	elif jc.get("docstatus") == 1 or jc.get("status") in ("Completed", "Submitted"):
		errors.append(_("Job Card {0} is {1}; a finished operation keeps the Workstation it ran on.").format(name, jc.get("status")))
	elif jc.get("status") == "Work In Progress":
		errors.append(
			_("Job Card {0} is already in progress on {1}. ERPNext books the actual time against that Workstation, so a started operation cannot be re-assigned.").format(
				name, jc.get("workstation") or "-"
			)
		)
	elif jc.get("status") not in JC_RESCHEDULABLE_STATUSES:
		errors.append(
			_("Job Card {0} is {1}; only Open, Material Transferred or On Hold operations can change Workstation.").format(name, jc.get("status"))
		)
	elif (jc.get("total_time_in_mins") or 0) > 0:
		errors.append(_("Job Card {0} already has recorded time on {1}; it cannot be re-assigned.").format(name, jc.get("workstation") or "-"))
	if wo is None:
		errors.append(_("Work Order {0} of this Job Card does not exist.").format(jc.get("work_order")))
	elif wo.get("docstatus") == 2 or wo.get("status") in WO_LOCKED_STATUSES:
		errors.append(_("Work Order {0} is {1}; its Job Cards cannot be re-assigned.").format(wo.get("name"), wo.get("status")))
	return errors


def compatibility_of(required_type: str | None, info: dict) -> dict:
	"""Formal compatibility only: the operation's required Workstation Type against the machine's type. Never guessed from names."""
	if not required_type:
		return {
			"state": "not_configured", "required": None,
			"message": _("No Workstation Type is configured for this operation, so compatibility has not been checked."),
		}
	if info.get("workstation_type") == required_type:
		return {"state": "compatible", "required": required_type, "message": _("Workstation Type {0} matches the operation.").format(required_type)}
	return {
		"state": "incompatible", "required": required_type,
		"message": _("The operation requires Workstation Type {0}; {1} is {2}.").format(
			required_type, info.get("name"), info.get("workstation_type") or _("not typed")
		),
	}


def outside_working_hours(info: dict, start: datetime, end: datetime) -> bool:
	"""True when the machine has working hours and the start (or end) falls outside all of them. Only the edges are
	judged: a multi-day interval necessarily spans the nights, which ERPNext splits into rows."""
	hours = info.get("working_hours") or []
	if not hours:
		return False

	def inside(moment: datetime) -> bool:
		t = moment.time()
		for a, b in hours:
			lo, hi = _as_time(a), _as_time(b)
			if lo <= t <= hi:
				return True
		return False

	return not (inside(start) and inside(end))


_as_time = machines.as_time


def _booking_json(b: dict, movable: bool | None = None) -> dict:
	return {
		"job_card": b["job_card"], "work_order": b.get("work_order"), "operation": b.get("operation"), "status": b.get("status"),
		"quantity": b.get("quantity"), "from": dt_str(b["from"]), "to": dt_str(b["to"]), "source": b.get("source"), "movable": movable,
	}


# =====================================================================================
# Reading
# =====================================================================================


def _job(job_card: str) -> dict | None:
	return frappe.db.get_value("Job Card", job_card, JOB_FIELDS, as_dict=True)


def _required_type(jc: dict) -> str | None:
	if jc.get("workstation_type"):
		return jc["workstation_type"]
	if jc.get("operation_id"):
		return frappe.db.get_value("Work Order Operation", jc["operation_id"], "workstation_type")
	return None


def _resolve_dates(jc: dict, start, end):
	s = scheduling.as_dt(start) or scheduling.as_dt(jc.get("expected_start_date"))
	e = scheduling.as_dt(end) or scheduling.as_dt(jc.get("expected_end_date"))
	return s, e


def plan_conflict_reschedule(jc: dict, target: str, start: datetime, end: datetime, conflicts: list[dict], capacity: int) -> dict:
	"""Preview of pushing the conflicting operations later so that `jc` can have the slot.

	Offered only when it is safe: the machine runs one operation at a time, there are at most a few conflicting
	operations, every one of them is a movable draft, and each pushed operation (with the operations that depend on it)
	fits where it would land. The pushed operations are laid out one after another starting when `jc` ends; durations are
	kept. Returns {"allowed", "reason", "shifts": [{job_card, old_start, old_end, new_start, new_end, cascade: [...]}]}."""
	none = {"allowed": False, "reason": None, "shifts": []}
	if not conflicts:
		return none
	if capacity != 1:
		return {**none, "reason": _("This machine can run {0} operations at once; choose another machine or time instead.").format(capacity)}
	if len(conflicts) > MAX_PUSHED_OPERATIONS:
		return {**none, "reason": _("{0} operations are in the way; too many to move automatically.").format(len(conflicts))}
	if not has_capability(CAP_RESCHEDULE):
		return {**none, "reason": _("You are not allowed to reschedule operations.")}

	shifts, cursor = [], end
	exclude = [jc["name"]]
	for c in sorted(conflicts, key=lambda b: (b["from"], b["job_card"])):
		card = _job(c["job_card"])
		if not card or not scheduling.is_movable(card) or (card.get("total_time_in_mins") or 0) > 0:
			return {**none, "reason": _("{0} ({1}) is {2} and cannot be moved.").format(c["job_card"], c.get("operation") or "-", c.get("status"))}
		old_s, old_e = scheduling.as_dt(card.expected_start_date), scheduling.as_dt(card.expected_end_date)
		if not (old_s and old_e):
			return {**none, "reason": _("{0} has no schedule to move.").format(c["job_card"])}
		length = old_e - old_s
		new_s, new_e = cursor, cursor + length
		if not frappe.has_permission("Job Card", "write", doc=card["name"]):
			return {**none, "reason": _("No write permission on Job Card {0}.").format(card["name"])}
		check = scheduling.validate_schedule(card["name"], new_s, new_e, shift_dependents=True, workstation=target)
		if check["errors"]:
			return {**none, "reason": "; ".join(check["errors"])}
		in_the_way = [w for w in check["warnings"] if w.get("needs_override")]
		if in_the_way:
			return {**none, "reason": _("{0} cannot be moved to {1}: {2}").format(card["name"], dt_str(new_s), in_the_way[0]["message"])}
		exclude.append(card["name"])
		shifts.append({
			"job_card": card["name"], "operation": card.get("operation"), "work_order": card.get("work_order"),
			"old_start": dt_str(old_s), "old_end": dt_str(old_e), "new_start": dt_str(new_s), "new_end": dt_str(new_e),
			"cascade": [
				{"job_card": s["name"], "old_start": dt_str(s["old_start"]), "old_end": dt_str(s["old_end"]),
				 "new_start": dt_str(s["new_start"]), "new_end": dt_str(s["new_end"])}
				for s in check["cascade"]["shifts"]
			],
		})
		cursor = new_e
	return {"allowed": True, "reason": None, "shifts": shifts}


def check_change(
	job_card: str,
	workstation: str | None,
	start=None,
	end=None,
	*,
	shift_dependents: bool = False,
	include_resolution: bool = True,
) -> dict:
	"""Advisory check of "move this Job Card to this Workstation over this interval". Changes nothing.

	`errors` can never be overridden. `warnings` with `needs_override` need an authorised override (or a different
	choice); the others only need a confirmation."""
	out = {
		"ok": False, "job_card": job_card, "errors": [], "warnings": [], "conflicts": [], "cascade": {"shifts": [], "blocked": []},
		"capacity": 1, "peak_others": 0, "needs_override": False, "can_override": has_capability(CAP_OVERRIDE),
		"compatibility": None, "workstation_status": None, "resolution": {"reschedule_conflicts": {"allowed": False, "reason": None, "shifts": []}},
		"modified": None,
	}
	jc = _job(job_card)
	if not jc:
		out["errors"].append(_("Job Card {0} does not exist.").format(job_card))
		return out
	wo = frappe.db.get_value("Work Order", jc.work_order, ["name", "docstatus", "status"], as_dict=True) if jc.work_order else None
	out.update({
		"work_order": jc.work_order, "operation": jc.operation, "current_workstation": jc.workstation, "status": jc.status,
		"modified": str(jc.modified),
	})
	out["errors"].extend(eligibility_errors(jc, wo))

	target = (workstation or "").strip()
	out["target_workstation"] = target
	info = machines.workstation_info(target) if target else None
	if not target:
		out["errors"].append(_("Choose a Workstation."))
	elif not info:
		out["errors"].append(_("Workstation {0} does not exist.").format(target))

	s, e = _resolve_dates(jc, start, end)
	if not (s and e):
		out["errors"].append(_("Job Card {0} has no schedule; give it dates before choosing a machine.").format(job_card))
	elif e <= s:
		out["errors"].append(_("The end date/time must be after the start date/time."))
	out["start"], out["end"] = dt_str(s), dt_str(e)
	out["duration_minutes"] = scheduling.minutes_between(s, e) if s and e and e > s else None
	if out["errors"]:
		return out

	unchanged_dates = scheduling._same_instant(s, jc.expected_start_date) and scheduling._same_instant(e, jc.expected_end_date)
	if target == jc.workstation and unchanged_dates:
		out["errors"].append(_("Job Card {0} is already on {1} at this time.").format(job_card, target))
		return out
	if target == jc.workstation:
		out["errors"].append(_("Job Card {0} is already on {1}; change its dates instead.").format(job_card, target))
		return out

	out["capacity"] = info["capacity"]
	out["target_type"] = info["workstation_type"]
	out["workstation_status"] = info["status"]

	check = scheduling.validate_schedule(job_card, s, e, shift_dependents=shift_dependents, workstation=target)
	out["errors"].extend(check["errors"])
	out["warnings"].extend(check["warnings"])
	out["cascade"] = {
		"shifts": [{"job_card": x["name"], "old_start": dt_str(x["old_start"]), "old_end": dt_str(x["old_end"]), "new_start": dt_str(x["new_start"]), "new_end": dt_str(x["new_end"])} for x in check["cascade"]["shifts"]],
		"blocked": check["cascade"]["blocked"],
	}

	machine = machines.check_machine(target, s, e, [job_card] + [x["name"] for x in check["cascade"]["shifts"]], info=info)
	out["peak_others"] = machine["peak_others"]
	out["utilization"] = machine["utilization"]
	if machine["blocking"]:
		bookings = machine["bookings"]
		out["conflicts"] = [
			_booking_json(b, movable=bool(scheduling.is_movable({"docstatus": b.get("docstatus"), "status": b.get("status")}) and b["source"] != "actual"))
			for b in bookings
		]
		if include_resolution:
			out["resolution"]["reschedule_conflicts"] = plan_conflict_reschedule(jc, target, s, e, bookings, info["capacity"])

	compat = compatibility_of(_required_type(jc), info)
	out["compatibility"] = compat
	if compat["state"] == "incompatible":
		out["warnings"].append({"code": "incompatible_type", "needs_override": True, "message": compat["message"]})
	if info["status"] in BAD_MACHINE_STATES:
		out["warnings"].append({
			"code": "workstation_status", "needs_override": False,
			"message": _("Workstation {0} is currently marked {1}.").format(target, info["status"]),
		})
	if outside_working_hours(info, s, e) and not cint(frappe.db.get_single_value("Manufacturing Settings", "allow_overtime")):
		out["warnings"].append({
			"code": "outside_hours", "needs_override": False,
			"message": _("The start or end is outside the working hours of {0}.").format(target),
		})

	out["needs_override"] = any(w.get("needs_override") for w in out["warnings"])
	out["ok"] = not out["errors"] and not out["needs_override"]
	return out


def find_next_slot(job_card: str, workstation: str, start=None, end=None) -> dict:
	"""The next feasible slot for this Job Card on `workstation`: same length, not before its predecessors end, room on the machine."""
	jc = _job(job_card)
	if not jc:
		frappe.throw(_("Job Card {0} does not exist.").format(job_card), frappe.DoesNotExistError)
	s, e = _resolve_dates(jc, start, end)
	if not (s and e and e > s):
		frappe.throw(_("Job Card {0} has no schedule to place.").format(job_card))
	info = machines.workstation_info(workstation)
	if not info:
		frappe.throw(_("Workstation {0} does not exist.").format(workstation))
	minutes = scheduling.minutes_between(s, e)

	earliest, reason = s, None
	preds, _succs = scheduling.dependency_neighbours(scheduling.get_work_order_jobs(jc.work_order), job_card)
	pred_end = max((scheduling.as_dt(p["expected_end_date"]) for p in preds if p["expected_end_date"]), default=None)
	if pred_end and pred_end > earliest:
		earliest, reason = pred_end, _("not before the preceding operation ends")
	slot = machines.next_free_slot(workstation, earliest, minutes, [job_card], info=info)
	if not slot:
		return {"ok": True, "found": False, "message": _("No free slot of {0} minutes within {1} days on {2}.").format(round(minutes), machines.DEFAULT_HORIZON_DAYS, workstation)}
	return {
		"ok": True, "found": True, "workstation": workstation, "start": dt_str(slot["start"]), "end": dt_str(slot["end"]),
		"duration_minutes": minutes, "same_as_requested": scheduling._same_instant(slot["start"], s) and scheduling._same_instant(slot["end"], e),
		"constraint": reason,
	}


def get_candidates(job_card: str, start=None, end=None, search: str | None = None, limit: int = 40) -> dict:
	"""Workstations the planner may choose from, with availability for the interval, conflicts, load and next slot."""
	jc = _job(job_card)
	if not jc:
		frappe.throw(_("Job Card {0} does not exist.").format(job_card), frappe.DoesNotExistError)
	s, e = _resolve_dates(jc, start, end)
	if not (s and e and e > s):
		frappe.throw(_("Job Card {0} has no schedule to place.").format(job_card))
	minutes = scheduling.minutes_between(s, e)
	required = _required_type(jc)

	or_filters = None
	if search and search.strip():
		like = f"%{search.strip()}%"
		or_filters = [["name", "like", like], ["workstation_type", "like", like], ["description", "like", like]]
	rows = frappe.get_all(
		"Workstation", or_filters=or_filters, fields=["name", "workstation_type", "status", "production_capacity", "holiday_list"],
		order_by="name asc", limit_page_length=300,
	)
	names = [r.name for r in rows]
	horizon_end = s + timedelta(days=machines.DEFAULT_HORIZON_DAYS)
	raw_window = machines.occupancy_many(names, s, e, [job_card])
	raw_horizon = machines.occupancy_many(names, s, horizon_end + timedelta(minutes=minutes), [job_card])
	by_ws_window, by_ws_horizon = {}, {}
	for r in raw_window:
		by_ws_window.setdefault(r["workstation"], []).append(r)
	for r in raw_horizon:
		by_ws_horizon.setdefault(r["workstation"], []).append(r)

	hours = frappe.get_all("Workstation Working Hour", filters={"parenttype": "Workstation"}, fields=["parent", "start_time", "end_time"], limit_page_length=0)
	hours_by_ws = {}
	for h in hours:
		hours_by_ws.setdefault(h.parent, []).append((h.start_time, h.end_time))

	out = []
	for r in rows:
		capacity = max(1, cint(r.production_capacity) or 1)
		info = {"name": r.name, "workstation_type": r.workstation_type, "status": r.status, "capacity": capacity, "holiday_list": r.holiday_list, "working_hours": hours_by_ws.get(r.name, [])}
		window = by_ws_window.get(r.name, [])
		intervals = machines.occupied_intervals(window)
		load = machines.window_load(intervals, capacity, machines.floor_second(s), machines.floor_second(e))
		available = not load["blocking"]
		slot = None
		if available:
			slot = {"start": dt_str(s), "end": dt_str(e)}
		else:
			holidays = machines._holiday_dates(info, s.date(), horizon_end.date())

			def skip(dt, holidays=holidays):
				moved = dt
				while moved.date() in holidays:
					moved = datetime.combine(moved.date() + timedelta(days=1), moved.time())
				return moved

			found = machines.find_slot(machines.occupied_intervals(by_ws_horizon.get(r.name, [])), capacity, s, minutes, horizon_end, skip)
			slot = {"start": dt_str(found[0]), "end": dt_str(found[1])} if found else None
		compat = compatibility_of(required, info)
		out.append({
			"name": r.name, "workstation_type": r.workstation_type, "status": r.status, "capacity": capacity,
			"is_current": r.name == jc.workstation, "available": available,
			"compatibility": {"state": compat["state"], "message": compat["message"]},
			"utilization": machines.utilization_pct(intervals, capacity, machines.floor_second(s), machines.floor_second(e)),
			"conflicts": [_booking_json(b) for b in machines.group_bookings(window)[:3]] if not available else [],
			"conflict_count": len(machines.group_bookings(window)) if not available else 0,
			"next_slot": slot,
		})

	order = {"compatible": 0, "not_configured": 1, "incompatible": 2}
	out.sort(key=lambda c: (c["is_current"], not c["available"], order.get(c["compatibility"]["state"], 3), c["utilization"] or 0, c["name"]))
	return {
		"ok": True, "job_card": job_card, "current_workstation": jc.workstation, "required_type": required,
		"compatibility_configured": bool(required), "start": dt_str(s), "end": dt_str(e), "workstations": out[: max(1, min(cint(limit) or 40, 300))],
		"total": len(out),
	}


# =====================================================================================
# Saving
# =====================================================================================


def _refuse(override_reason: str | None) -> str:
	return (override_reason or "").strip()


def change_job_card_workstation(
	job_card: str,
	workstation: str,
	*,
	start=None,
	end=None,
	reason: str | None = None,
	expected_modified: str | None = None,
	confirmed: bool = False,
	shift_dependents: bool = False,
	override_conflicts: bool = False,
	override_reason: str | None = None,
	reschedule_conflicts: bool = False,
	correlation_id: str | None = None,
) -> dict:
	"""Validate and persist a Workstation change atomically.

	Returns {"ok": True, ...} on success. Otherwise {"ok": False, ...} with one of: errors (cannot be done),
	conflict (+ conflicts, can_override, resolution), needs_confirmation, or stale. Nothing is changed unless ok."""
	require_capability(CAP_RESCHEDULE)
	if not frappe.has_permission("Job Card", "write", doc=job_card):
		frappe.throw(_("No write permission on Job Card {0}").format(job_card), frappe.PermissionError)
	begin_read_committed()  # see other planners' commits while waiting on the machine's lock (no-op in tests)

	correlation = correlation_id or revisions.new_correlation_id()
	if correlation_id:
		earlier = frappe.db.get_value(revisions.REVISION_DT, {"job_card": job_card, "correlation_id": correlation_id}, ["name", "revision_no"], as_dict=True)
		if earlier:  # the same request arrived twice: it was already applied exactly once
			return {
				"ok": True, "duplicate": True, "job_card": job_card, "workstation": frappe.db.get_value("Job Card", job_card, "workstation"),
				"revision": earlier.revision_no, "correlation_id": correlation_id, "warnings": [],
				"modified": str(frappe.db.get_value("Job Card", job_card, "modified")),
			}

	head = _job(job_card)
	override_reason = _refuse(override_reason)
	if override_conflicts:
		if not has_capability(CAP_OVERRIDE):
			audit.log(
				A_CHANGE_WORKSTATION, R_WARNING, plan=head.ppc_production_plan if head else None, work_order=head.work_order if head else None,
				job_card=job_card, reason=_("Conflict override refused: the user does not hold the Override Conflicts capability."),
				details={"workstation": workstation, "attempted_override": True, "override_reason": override_reason},
			)
			commit_if_allowed()  # the refusal rolls this request back: the audit row must survive it
			frappe.throw(_("You are not allowed to override a scheduling conflict."), frappe.PermissionError)
		if len(override_reason) < MIN_OVERRIDE_REASON:
			return {"ok": False, "errors": [_("Give a reason for overriding the conflict (at least {0} characters).").format(MIN_OVERRIDE_REASON)], "warnings": [], "needs_confirmation": False}

	check = check_change(job_card, workstation, start, end, shift_dependents=shift_dependents)
	if check["errors"]:
		return {"ok": False, "errors": check["errors"], "warnings": check["warnings"], "needs_confirmation": False}
	start_dt, end_dt = scheduling.as_dt(check["start"]), scheduling.as_dt(check["end"])
	if head and scheduling._same_instant(start_dt, head.expected_start_date) and scheduling._same_instant(end_dt, head.expected_end_date):
		# only the machine changes: keep the card's own exact times (ERPNext's microseconds) so nothing about its dates moves
		start_dt, end_dt = scheduling.as_dt(head.expected_start_date), scheduling.as_dt(head.expected_end_date)

	pushing = bool(reschedule_conflicts) and bool(check["conflicts"])
	hard = [w for w in check["warnings"] if w.get("needs_override") and not (pushing and w["code"] == "workstation")]
	if pushing and not check["resolution"]["reschedule_conflicts"]["allowed"]:
		return {**_conflict_response(check), "errors": [check["resolution"]["reschedule_conflicts"]["reason"] or _("The conflicting operations cannot be moved safely.")]}
	if (hard or (check["conflicts"] and not pushing)) and not override_conflicts:
		return _conflict_response(check)
	if not confirmed and not override_conflicts:
		return {"ok": False, "errors": [], "needs_confirmation": True, "warnings": check["warnings"], "check": check, "can_override": check["can_override"]}

	proposal = {"workstation": workstation, "start": dt_str(start_dt), "end": dt_str(end_dt), "override": bool(override_conflicts), "push": pushing}
	try:
		with savepoint():
			changed, wo_results, conflicts_seen = _apply(
				job_card, workstation, start_dt, end_dt, reason=reason, expected_modified=expected_modified,
				shift_dependents=shift_dependents, override=bool(override_conflicts), override_reason=override_reason, push=pushing,
				correlation=correlation,
			)
	except scheduling.StaleScheduleError as exc:
		return {"ok": False, "stale": True, "errors": [str(exc)], "warnings": [], "needs_confirmation": False}
	except WorkstationConflict as exc:
		return _conflict_response(exc.check)
	except Exception as exc:
		message = error_text(exc)
		revisions.mark_sync_failed(job_card)  # the savepoint rolled everything back; leave a visible, retryable trace
		audit.log(
			A_CHANGE_WORKSTATION, R_FAILED, plan=head.ppc_production_plan if head else None, work_order=head.work_order if head else None,
			job_card=job_card, old_start=head.expected_start_date if head else None, old_end=head.expected_end_date if head else None,
			new_start=start_dt, new_end=end_dt, reason=reason, error=message, idempotency_key=correlation,
			details={"proposal": proposal, "correlation_id": correlation},
		)
		return {"ok": False, "errors": [message], "warnings": [], "needs_confirmation": False}

	plan_names = set()
	for c in changed:
		plan = frappe.db.get_value("Job Card", c["job_card"], "ppc_production_plan")
		plan_names.add(plan)
		is_main = c["job_card"] == job_card
		audit.log(
			A_CHANGE_WORKSTATION if is_main else scheduling.A_RESCHEDULE, R_SUCCESS, plan=plan,
			work_order=frappe.db.get_value("Job Card", c["job_card"], "work_order"), job_card=c["job_card"],
			old_start=c["old_start"], old_end=c["old_end"], new_start=c["new_start"], new_end=c["new_end"],
			reason=reason if is_main else _("Moved to make room for {0} on {1}").format(job_card, workstation), idempotency_key=correlation,
			details={
				"previous_workstation": check.get("current_workstation") if is_main else None, "workstation": workstation if is_main else None,
				"override": bool(override_conflicts) and is_main, "override_reason": override_reason if override_conflicts and is_main else None,
				"conflicts": conflicts_seen if is_main else None, "revision": c["revision"], "correlation_id": correlation,
				"warnings": [w["message"] for w in check["warnings"]] if is_main else None,
			},
		)
	for plan in plan_names:
		if plan:
			frappe.db.set_value("Production Plan", plan, "ppc_last_scheduling_sync", now_datetime(), update_modified=False)

	main = next(c for c in changed if c["job_card"] == job_card)
	return {
		"ok": True, "job_card": job_card, "workstation": workstation, "previous_workstation": check.get("current_workstation"),
		"changed": [{k: dt_str(v) if k in ("old_start", "old_end", "new_start", "new_end") else v for k, v in c.items()} for c in changed],
		"work_orders": [w for w in wo_results if w], "warnings": check["warnings"], "correlation_id": correlation, "revision": main["revision"],
		"override_used": bool(override_conflicts),
		"modified": str(frappe.db.get_value("Job Card", job_card, "modified")),
	}


def _conflict_response(check: dict) -> dict:
	return {
		"ok": False, "conflict": bool(check.get("conflicts")) or bool(check.get("needs_override")), "errors": [], "needs_confirmation": True,
		"needs_override": True, "warnings": check["warnings"], "conflicts": check["conflicts"], "can_override": check.get("can_override", False),
		"resolution": check.get("resolution"), "check": check,
	}


def _apply(job_card, target, start, end, *, reason, expected_modified, shift_dependents, override, override_reason, push, correlation):
	"""The atomic unit. Runs inside a savepoint; raises on any inconsistency."""
	head = frappe.db.get_value("Job Card", job_card, ["workstation", "work_order"], as_dict=True)
	previous_ws = head.workstation

	# 1. locks: both machines (sorted), then the card, then its Work Order ------------------------------------------
	scheduling.lock_workstations(previous_ws, target)
	locked = frappe.db.get_value("Job Card", job_card, "modified", for_update=True)
	if expected_modified and str(locked) != str(expected_modified):
		raise scheduling.StaleScheduleError(_("Job Card {0} was changed by someone else. Refresh and try again.").format(job_card))
	frappe.db.get_value("Work Order", head.work_order, "modified", for_update=True)

	# 2. the availability check again, now that nobody else can take the slot -------------------------------------------
	again = check_change(job_card, target, start, end, shift_dependents=shift_dependents)
	if again["errors"]:
		raise frappe.ValidationError("; ".join(again["errors"]))
	hard = [w for w in again["warnings"] if w.get("needs_override") and not (push and w["code"] == "workstation")]
	if (hard or (again["conflicts"] and not push)) and not override:
		raise WorkstationConflict(again)
	conflicts_seen = again["conflicts"]
	cascade = [s for s in scheduling.validate_schedule(job_card, start, end, shift_dependents=shift_dependents, workstation=target)["cascade"]["shifts"]]

	targets = []  # (job card, start, end, mode, source, machine_change)
	touched_wos = {head.work_order}
	if push:
		plan = again["resolution"]["reschedule_conflicts"]
		if not plan["allowed"]:
			raise WorkstationConflict(again)
		for shift in plan["shifts"]:
			card_check = scheduling.validate_schedule(shift["job_card"], scheduling.as_dt(shift["new_start"]), scheduling.as_dt(shift["new_end"]), shift_dependents=True, workstation=target)
			if card_check["errors"] or [w for w in card_check["warnings"] if w.get("needs_override")]:
				raise WorkstationConflict(again)
			frappe.db.get_value("Work Order", shift["work_order"], "modified", for_update=True)
			targets.append((shift["job_card"], scheduling.as_dt(shift["new_start"]), scheduling.as_dt(shift["new_end"]), "move", "Cascade", None))
			for dep in card_check["cascade"]["shifts"]:
				targets.append((dep["name"], dep["new_start"], dep["new_end"], "move", "Cascade", None))
	machine_change = {
		"previous": previous_ws, "new": target, "conflicts": conflicts_seen or None, "override_used": bool(override and (hard or conflicts_seen)),
		"override_reason": override_reason if override else None,
	}
	main_old = frappe.db.get_value("Job Card", job_card, ["expected_start_date", "expected_end_date"], as_dict=True)
	same_length = (end - start) == (scheduling.as_dt(main_old.expected_end_date) - scheduling.as_dt(main_old.expected_start_date))
	targets.append((job_card, start, end, "move" if same_length else "resize", "Workstation Change", machine_change))
	for s in cascade:
		targets.append((s["name"], s["new_start"], s["new_end"], "move", "Cascade", None))

	# 3. write through the Job Card's own lifecycle, read back, verify ---------------------------------------------------
	pending = []
	for name, t_start, t_end, t_mode, t_source, machine in targets:
		scheduling.ensure_baseline(name)
		doc = frappe.get_doc("Job Card", name)
		previous = (doc.expected_start_date, doc.expected_end_date)
		if machine:
			doc.workstation = target
		scheduling.apply_dates(doc, t_start, t_end, t_mode)
		saved = frappe.db.get_value("Job Card", name, ["workstation", "expected_start_date", "expected_end_date"], as_dict=True)
		if not (scheduling._same_instant(saved.expected_start_date, t_start) and scheduling._same_instant(saved.expected_end_date, t_end)):
			raise frappe.ValidationError(
				_("Job Card {0}: ERPNext stored {1} - {2} instead of the requested {3} - {4}; the change was not applied.").format(
					name, saved.expected_start_date, saved.expected_end_date, t_start, t_end)
			)
		if machine and saved.workstation != target:
			raise frappe.ValidationError(_("Job Card {0}: ERPNext kept Workstation {1} instead of {2}; the change was not applied.").format(name, saved.workstation, target))
		touched_wos.add(doc.work_order)
		pending.append((name, previous, (t_start, t_end), (saved.expected_start_date, saved.expected_end_date), t_source, machine))

		if machine:
			# the main card's own slot, re-checked after any pushes and with nothing else changing meanwhile
			room = machines.check_machine(target, t_start, t_end, [job_card] + [s["name"] for s in cascade])
			if room["blocking"] and not override:
				raise WorkstationConflict({**again, "conflicts": [_booking_json(b) for b in room["bookings"]], "needs_override": True})

	# 4. Work Order(s) by the documented policy, then verify ---------------------------------------------------------
	wo_results = []
	for wo in sorted(touched_wos):
		wo_results.append(scheduling.sync_work_order_schedule(wo))
		window = scheduling.work_order_window(wo)
		current = frappe.db.get_value("Work Order", wo, ["planned_start_date", "planned_end_date"], as_dict=True)
		if window and not (scheduling._same_instant(current.planned_start_date, window[0]) and scheduling._same_instant(current.planned_end_date, window[1])):
			raise frappe.ValidationError(_("Work Order {0} did not take the synchronised dates; the change was not applied.").format(wo))

	# 5. exactly one revision per card that changed, appended last ------------------------------------------------------
	wo_by_name = {r["work_order"]: r for r in wo_results if r}
	changed = []
	for name, previous, proposed, accepted, source, machine in pending:
		wo_name = frappe.db.get_value("Job Card", name, "work_order")
		doc = revisions.record_revision(
			name, previous=previous, proposed=proposed, accepted=accepted,
			reason=reason if machine else _("Moved to make room for {0} on {1}").format(job_card, target) if source == "Cascade" and push else (reason or (_("Cascade shift") if source == "Cascade" else None)),
			source=source, correlation_id=correlation, wo_change=wo_by_name.get(wo_name), machine_change=machine,
		)
		changed.append({"job_card": name, "old_start": previous[0], "old_end": previous[1], "new_start": accepted[0], "new_end": accepted[1], "revision": doc.revision_no})
	return changed, wo_results, conflicts_seen
