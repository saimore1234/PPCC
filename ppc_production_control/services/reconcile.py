"""Schedule reconciliation: find, preview and (safely) repair disagreements between

* a Job Card's real ERPNext schedule and the last schedule PPC accepted for it,
* a Work Order's planned dates and the aggregate of its Job Cards (PPC's documented policy),
* baselines / revision history that are missing or inconsistent,
* synchronisations that failed and were never retried.

Rules: preview by default; every repair is idempotent, permission-controlled and audited; completed,
submitted, started or cancelled work and actual execution dates are never touched - such cases are
reported for manual handling instead.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint

from ppc_production_control.services import audit, revisions, scheduling
from ppc_production_control.services.constants import (
	A_INIT_BASELINE,
	A_RECONCILE,
	A_RESCHEDULE,
	CAP_RECONCILE,
	CAP_RESCHEDULE,
	CAP_RETRY,
	JC_RESCHEDULABLE_STATUSES,
	R_FAILED,
	R_SUCCESS,
	WO_LOCKED_STATUSES,
)
from ppc_production_control.services.permissions import has_capability, require_capability
from ppc_production_control.services.utils import chunks, error_text, savepoint

MAX_CARDS = 5000

ISSUES = {
	"missing_baseline": ("No baseline recorded", True),
	"missing_history": ("Baseline recorded by an earlier version; its history rows are missing", True),
	"schedule_drift": ("Job Card schedule differs from the last accepted PPC schedule", True),
	"work_order_drift": ("Work Order dates disagree with its Job Cards", True),
	"failed_sync": ("The last synchronisation failed and was not retried", True),
	"revision_mismatch": ("Revision history is inconsistent", False),
	"orphan_work_order": ("Work Order is missing or cancelled but the Job Card is active", False),
}

_CARD_FIELDS = [
	"name", "work_order", "docstatus", "status", "ppc_production_plan", "expected_start_date", "expected_end_date",
	"ppc_original_start", "ppc_revised_start", "ppc_revised_end", "ppc_schedule_revision", "ppc_schedule_sync_status",
]


def _cards(plan: str | None) -> tuple[list, bool]:
	filters = {"docstatus": ["<", 2], "ppc_production_plan": plan if plan else ["is", "set"]}
	rows = frappe.get_all("Job Card", filters=filters, fields=_CARD_FIELDS, limit_page_length=MAX_CARDS + 1, order_by="work_order, sequence_id, name")
	return rows[:MAX_CARDS], len(rows) > MAX_CARDS


def _same(a, b) -> bool:
	return scheduling._same_instant(a, b)


def _movable(card) -> bool:
	return card.docstatus == 0 and card.status in JC_RESCHEDULABLE_STATUSES


def check_consistency(plan: str | None = None) -> dict:
	"""Read-only. Returns every discrepancy with what a repair would do."""
	cards, truncated = _cards(plan)
	issues = []

	def add(kind, card=None, wo=None, message="", action="", repairable=None, movable=True):
		repairable_default = ISSUES[kind][1]
		issues.append(
			{
				"id": f"{kind}::{card.name if card else wo}",
				"type": kind,
				"title": _(ISSUES[kind][0]),
				"job_card": card.name if card else None,
				"work_order": wo or (card.work_order if card else None),
				"plan": card.ppc_production_plan if card else None,
				"message": message,
				"repairable": repairable_default if repairable is None else repairable,
				"action": action,
			}
		)

	stats = {}
	latest = {}
	for batch in chunks([c.name for c in cards], 500):
		for r in frappe.db.sql(
			"""select job_card, count(*) n, count(distinct revision_no) d, min(revision_no) lo, max(revision_no) hi
			from `tabPPC Schedule Revision` where job_card in %(n)s group by job_card""",
			{"n": tuple(batch)}, as_dict=True,
		):
			stats[r.job_card] = r
		for r in frappe.db.sql(
			"""select r.job_card, r.accepted_start, r.accepted_end from `tabPPC Schedule Revision` r
			join (select job_card, max(revision_no) m from `tabPPC Schedule Revision` where job_card in %(n)s group by job_card) x
			on x.job_card = r.job_card and x.m = r.revision_no""",
			{"n": tuple(batch)}, as_dict=True,
		):
			latest[r.job_card] = r

	wo_names = sorted({c.work_order for c in cards if c.work_order})
	wos = {
		w.name: w
		for w in frappe.get_all(
			"Work Order", filters={"name": ["in", wo_names or [""]]},
			fields=["name", "docstatus", "status", "planned_start_date", "planned_end_date"], limit_page_length=0,
		)
	}

	for c in cards:
		wo = wos.get(c.work_order)
		if not wo or wo.docstatus == 2:
			add("orphan_work_order", c, message=_("Work Order {0} is {1}.").format(c.work_order, _("missing") if not wo else _("cancelled")),
				action=_("Resolve manually: cancel or re-link the Job Card."))
		has_dates = bool(c.expected_start_date and c.expected_end_date)
		if has_dates and not c.ppc_original_start:
			add("missing_baseline", c, message=_("The card has a schedule but no recorded baseline."),
				action=_("Record a baseline labelled 'initialized from existing schedule'."))
		if c.ppc_original_start:
			st = stats.get(c.name)
			rev = cint(c.ppc_schedule_revision)
			if not st:
				add("missing_history", c, message=_("The card has a baseline but no history rows (recorded before revision history existed)."),
					action=_("Add revision 0 from the existing baseline (and one explanatory revision if the schedule has moved)."))
			elif st.lo != 0:
				add("revision_mismatch", c, message=_("Baseline fields exist but revision 0 is missing from the history."), repairable=False)
			elif st.n != st.d:
				add("revision_mismatch", c, message=_("Duplicated revision numbers in the history."), repairable=False)
			elif st.hi != rev:
				add("revision_mismatch", c, message=_("The card says revision {0} but the history ends at {1}.").format(rev, st.hi), repairable=False)
			else:
				lr = latest.get(c.name)
				if lr and c.ppc_revised_start and not (_same(lr.accepted_start, c.ppc_revised_start) and _same(lr.accepted_end, c.ppc_revised_end)):
					add("revision_mismatch", c, message=_("The latest revision does not match the accepted schedule stored on the card."), repairable=False)
		if c.ppc_revised_start and has_dates and not (_same(c.ppc_revised_start, c.expected_start_date) and _same(c.ppc_revised_end, c.expected_end_date)):
			movable = _movable(c)
			add("schedule_drift", c,
				message=_("ERPNext holds {0} - {1} but PPC last accepted {2} - {3}.").format(
					c.expected_start_date, c.expected_end_date, c.ppc_revised_start, c.ppc_revised_end),
				action=_("Re-apply the last accepted schedule (as a new revision).") if movable else _("Not changed: the card is started, submitted or finished."),
				repairable=movable)
		if c.ppc_schedule_sync_status == "Failed":
			add("failed_sync", c, message=_("A reschedule was rolled back and has not been retried."),
				action=_("Retry the stored change, or clear the flag if nothing is pending."))

	for name, wo in wos.items():
		if wo.docstatus != 1 or wo.status in WO_LOCKED_STATUSES:
			continue
		window = scheduling.work_order_window(name)
		full = scheduling.work_order_full_window(name)  # still spanning finished operations is not a defect
		if window and not any(w and _same(wo.planned_start_date, w[0]) and _same(wo.planned_end_date, w[1]) for w in (window, full)):
			add("work_order_drift", wo=name,
				message=_("Work Order {0}-{1}; its Job Cards say {2}-{3}.").format(wo.planned_start_date, wo.planned_end_date, window[0], window[1]),
				action=_("Synchronise the Work Order dates from its Job Cards."))
	counts = {k: sum(1 for i in issues if i["type"] == k) for k in ISSUES}
	return {
		"issues": issues,
		"counts": {k: v for k, v in counts.items() if v},
		"total": len(issues),
		"checked_cards": len(cards),
		"checked_work_orders": len(wos),
		"truncated": truncated,
	}


# --------------------------------------------------------------------------------------- baselines
def initialize_missing_baselines(plan: str | None = None, dry_run: bool = True) -> dict:
	"""Record a baseline for cards that have a schedule but none recorded. The baseline is *labelled*
	as initialized from the existing schedule - the true original is unknowable at this point."""
	require_capability(CAP_RECONCILE)
	found = [i for i in check_consistency(plan)["issues"] if i["type"] in ("missing_baseline", "missing_history")]
	if dry_run:
		return {"dry_run": True, "candidates": found, "initialized": 0, "history_added": 0}
	done = history = 0
	for issue in found:
		with savepoint():
			if issue["type"] == "missing_history":
				history += 1 if revisions.backfill_history(issue["job_card"]) else 0
			elif revisions.capture_baseline(issue["job_card"], revisions.BASELINE_INITIALIZED):
				done += 1
	if done or history:
		audit.log(A_INIT_BASELINE, R_SUCCESS, plan=plan, details={"initialized": done, "history_added": history, "label": revisions.BASELINE_NOTES[revisions.BASELINE_INITIALIZED]})
	return {"dry_run": False, "candidates": found, "initialized": done, "history_added": history}


# --------------------------------------------------------------------------------------- repairs
def repair(plan: str | None = None, types: list[str] | None = None, dry_run: bool = True) -> dict:
	"""Preview (default) or apply safe repairs. Idempotent: a second run finds nothing left to do."""
	require_capability(CAP_RECONCILE)
	report = check_consistency(plan)
	todo = [i for i in report["issues"] if (not types or i["type"] in types)]
	results = []
	for issue in todo:
		entry = {"id": issue["id"], "type": issue["type"], "job_card": issue["job_card"], "work_order": issue["work_order"],
			"action": issue["action"], "applied": False, "message": ""}
		if not issue["repairable"]:
			entry["message"] = _("Needs manual attention.")
			results.append(entry)
			continue
		if dry_run:
			entry["message"] = _("Would repair: {0}").format(issue["action"])
			results.append(entry)
			continue
		try:
			entry["applied"], entry["message"] = _repair_one(issue)
		except Exception as exc:
			entry["message"] = error_text(exc)
			audit.log(A_RECONCILE, R_FAILED, plan=issue["plan"], work_order=issue["work_order"], job_card=issue["job_card"],
				error=entry["message"], details={"issue": issue["type"]})
		results.append(entry)
	return {"dry_run": dry_run, "results": results, "repaired": sum(1 for r in results if r["applied"]), "issues": report["total"]}


def _repair_one(issue: dict) -> tuple[bool, str]:
	kind = issue["type"]
	if kind == "missing_baseline":
		with savepoint():
			ok = revisions.capture_baseline(issue["job_card"], revisions.BASELINE_INITIALIZED)
		message = _("Baseline recorded (initialized from existing schedule).") if ok else _("Already had a baseline.")
	elif kind == "missing_history":
		with savepoint():
			ok = revisions.backfill_history(issue["job_card"])
		message = _("History rows added from the existing baseline.") if ok else _("Already had history.")
	elif kind == "work_order_drift":
		with savepoint():
			change = scheduling.sync_work_order_schedule(issue["work_order"], reason=_("Reconciliation"))
		ok, message = bool(change), _("Work Order dates synchronised.") if change else _("Already consistent.")
	elif kind == "schedule_drift":
		card = frappe.db.get_value("Job Card", issue["job_card"], ["ppc_revised_start", "ppc_revised_end", "modified"], as_dict=True)
		res = scheduling.reschedule_job_card(
			issue["job_card"], card.ppc_revised_start, card.ppc_revised_end, mode="resize", confirmed=True,
			reason=_("Reconciliation: restored the last accepted schedule"), source="Reconciliation",
		)
		ok = bool(res.get("ok"))
		message = _("Last accepted schedule re-applied as a new revision.") if ok else "; ".join(res.get("errors") or [_("Conflicts need confirmation; not applied.")])
	elif kind == "failed_sync":
		out = retry_failed_synchronizations(job_card=issue["job_card"])
		if out["retried"]:
			ok, message = out["succeeded"] > 0, _("Retried the stored change.")
		else:  # nothing pending to retry: the flag is stale
			frappe.db.set_value("Job Card", issue["job_card"], "ppc_schedule_sync_status", "Synced", update_modified=False)
			ok, message = True, _("Nothing was pending; the failed flag was cleared.")
	else:
		return False, _("Needs manual attention.")
	if ok:
		audit.log(A_RECONCILE, R_SUCCESS, plan=issue["plan"], work_order=issue["work_order"], job_card=issue["job_card"],
			reason=message, details={"issue": kind})
	return ok, message


# --------------------------------------------------------------------------------------- retry
def retry_failed_synchronizations(plan: str | None = None, job_card: str | None = None) -> dict:
	"""Re-run reschedules that were rolled back, using the proposal stored with the failure.

	Only the newest pending failure of each Job Card is replayed (older proposals are superseded).
	Runs with the *current* user's permissions and re-validates everything, so a retry can never do
	more than the user could do by hand."""
	require_capability(CAP_RETRY)
	require_capability(CAP_RESCHEDULE)
	filters = {"action": A_RESCHEDULE, "result": R_FAILED, "retry_status": "Pending"}
	if plan:
		filters["ppc_plan"] = plan
	if job_card:
		filters["job_card"] = job_card
	rows = frappe.get_all("PPC Audit Log", filters=filters, fields=["name", "job_card", "details", "creation"], order_by="creation desc", limit_page_length=500)
	newest, superseded = {}, []
	for r in rows:
		if r.job_card in newest:
			superseded.append(r.name)
		else:
			newest[r.job_card] = r

	for name in superseded:
		frappe.db.set_value("PPC Audit Log", name, "retry_status", "Retried", update_modified=False)
	results, succeeded = [], 0
	for card, row in newest.items():
		try:
			proposal = (json.loads(row.details or "{}") or {}).get("proposal")
		except ValueError:
			proposal = None
		entry = {"job_card": card, "ok": False, "message": ""}
		if not proposal:
			entry["message"] = _("No stored proposal; reschedule it again from the Gantt.")
			frappe.db.set_value("PPC Audit Log", row.name, "retry_status", "Retried", update_modified=False)
			results.append(entry)
			continue
		res = scheduling.reschedule_job_card(
			card, proposal["start"], proposal["end"], reason=proposal.get("reason"), mode=proposal.get("mode") or "move",
			confirmed=True, shift_dependents=bool(proposal.get("shift_dependents")),
			override_conflicts=bool(proposal.get("override_conflicts")) and has_capability("Override Conflicts"),
			source="Retry",
		)
		entry["ok"] = bool(res.get("ok"))
		entry["message"] = _("Retried successfully.") if entry["ok"] else "; ".join(res.get("errors") or [_("Needs confirmation or override.")])
		if entry["ok"]:
			succeeded += 1
			for pending in frappe.get_all("PPC Audit Log", filters={**filters, "job_card": card}, pluck="name"):
				frappe.db.set_value("PPC Audit Log", pending, "retry_status", "Resolved", update_modified=False)
			frappe.db.set_value("Job Card", card, "ppc_schedule_sync_status", "Synced", update_modified=False)
		else:
			frappe.db.set_value("PPC Audit Log", row.name, "retry_status", "Retried", update_modified=False)
		results.append(entry)
	return {"retried": len(newest), "succeeded": succeeded, "superseded": len(superseded), "results": results}
