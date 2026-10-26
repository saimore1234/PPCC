"""Read models and pre-checks for the PPC Control Center pages.

Everything here is *read-only* (the pre-check runs ERPNext's own validations inside a savepoint that is always rolled
back). The Job Card / Work Order / Gantt tables live in `queries.py`; this module adds what the other pages need:
the Production Plan list, the dashboard overview, workstation workload and capacity, the schedule-change log and the
exceptions feed.

Rules shared with `queries.py`: every user value is a bound parameter, sort columns come from whitelists, row-level
permissions are applied with Frappe's own match conditions, and nothing is invented - when ERPNext holds no working
hours / shift / holiday data for a workstation the answer is "unavailable", not a guess.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate

from ppc_production_control.services import queries, reconcile
from ppc_production_control.services.constants import JC_DONE_STATUSES, WO_LOCKED_STATUSES
from ppc_production_control.services.permissions import check_doctype_permission
from ppc_production_control.services.queries import (
	JC, JC_FROM, PLAN, REVISED_END, SO, WO, Where, _permission_clause, _s, _scope_sql, like, normalize_filters,
)
from ppc_production_control.services.utils import error_text

MAX_PAGE = 200
DONE_SQL = "('Completed', 'Submitted', 'Cancelled')"
REVISION_DT = "PPC Schedule Revision"


def _paging(page, page_length) -> tuple[int, int]:
	return max(1, cint(page)), min(max(1, cint(page_length) or 50), MAX_PAGE)


def _page_result(rows, total, page, page_length) -> dict:
	return {"rows": rows, "total": total, "page": page, "page_length": page_length, "pages": max(1, -(-total // page_length))}


# =============================================================================================
# Production Plans
# =============================================================================================
PLAN_SORTABLE = {
	"name": f"{PLAN}.name",
	"company": f"{PLAN}.company",
	"posting_date": f"{PLAN}.posting_date",
	"status": f"{PLAN}.status",
	"planner": "planner",
	"planned_qty": "planned_qty",
	"work_orders": "work_orders",
	"job_cards": "job_cards",
	"modified": f"{PLAN}.modified",
}

STAGES = {
	"draft": "Draft",
	"cancelled": "Cancelled",
	"attention": "Needs attention",
	"awaiting": "Awaiting Work Orders",
	"completed": "Completed",
	"drafted": "Work Orders drafted",
	"partial": "Partly submitted",
	"submitted": "Work Orders submitted",
	"production": "In production",
}


def plan_stage(r) -> str:
	"""Where the plan is in the PPC flow, derived from real document counts (never stored, so never stale)."""
	if r.docstatus == 0:
		return STAGES["draft"]
	if r.docstatus == 2 or r.status == "Cancelled":
		return STAGES["cancelled"]
	if cint(r.pending_failures):
		return STAGES["attention"]
	if not cint(r.work_orders):
		return STAGES["awaiting"]
	if flt(r.planned_qty) > 0 and flt(r.produced_qty) >= flt(r.planned_qty):
		return STAGES["completed"]
	if cint(r.draft_work_orders) == cint(r.work_orders):
		return STAGES["drafted"]
	if cint(r.draft_work_orders):
		return STAGES["partial"]
	if not cint(r.job_cards):
		return STAGES["submitted"]
	return STAGES["production"]


def get_plans(filters: dict | None, page: int = 1, page_length: int = 50, sort_by: str = "posting_date", sort_order: str = "desc") -> dict:
	filters = normalize_filters(filters)
	page, page_length = _paging(page, page_length)
	order_col = PLAN_SORTABLE.get(sort_by) or PLAN_SORTABLE["posting_date"]
	direction = "asc" if str(sort_order).lower() == "asc" else "desc"

	w = Where()
	if filters.get("plan_status") == "Cancelled":
		w.add(f"{PLAN}.docstatus = 2")
	else:
		w.add(f"{PLAN}.docstatus < 2")
		if filters.get("plan_status") == "Draft":
			w.add(f"{PLAN}.docstatus = 0")
		elif filters.get("plan_status"):
			w.add(f"{PLAN}.status = {w.bind(filters['plan_status'])}")
	if filters.get("plan"):
		w.add(f"{PLAN}.name = {w.bind(filters['plan'])}")
	if filters.get("company"):
		w.add(f"{PLAN}.company = {w.bind(filters['company'])}")
	if filters.get("planner"):
		w.add(f"coalesce({PLAN}.ppc_planner, {PLAN}.owner) = {w.bind(filters['planner'])}")
	if filters.get("priority"):
		w.add(f"{PLAN}.ppc_priority = {w.bind(filters['priority'])}")
	if filters.get("posting_from"):
		w.add(f"{PLAN}.posting_date >= {w.bind(filters['posting_from'])}")
	if filters.get("posting_to"):
		w.add(f"{PLAN}.posting_date <= {w.bind(filters['posting_to'])}")
	if filters.get("item_code"):
		w.add(f"exists (select 1 from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name and pi.item_code = {w.bind(filters['item_code'])})")
	if filters.get("sales_order"):
		b = w.bind(filters["sales_order"])
		w.add(f"exists (select 1 from `tabProduction Plan Sales Order` ps where ps.parent = {PLAN}.name and ps.sales_order = {b}) "
			f"or exists (select 1 from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name and pi.sales_order = {b})")
	if filters.get("sales_order_like"):
		b = w.bind(like(filters["sales_order_like"]))
		w.add(f"exists (select 1 from `tabProduction Plan Sales Order` ps where ps.parent = {PLAN}.name and ps.sales_order like {b}) "
			f"or exists (select 1 from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name and pi.sales_order like {b})")
	if filters.get("customer"):  # through the Sales Order itself: the plan's own customer column is not always filled
		w.add(
			f"exists (select 1 from {SO} so where so.customer = {w.bind(filters['customer'])} and (so.name in "
			f"(select ps.sales_order from `tabProduction Plan Sales Order` ps where ps.parent = {PLAN}.name) or so.name in "
			f"(select pi.sales_order from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name)))"
		)
	for key, op in (("delivery_from", ">="), ("delivery_to", "<=")):
		if filters.get(key):
			w.add(
				f"exists (select 1 from `tabProduction Plan Sales Order` ps join {SO} so on so.name = ps.sales_order "
				f"where ps.parent = {PLAN}.name and so.delivery_date {op} {w.bind(filters[key])})"
			)
	if filters.get("search"):
		b = w.bind(like(filters["search"]))
		w.add(
			f"{PLAN}.name like {b} or exists (select 1 from `tabProduction Plan Sales Order` ps where ps.parent = {PLAN}.name and ps.sales_order like {b}) "
			f"or exists (select 1 from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name and (pi.sales_order like {b} or pi.item_code like {b}))"
		)
	perm = _permission_clause("Production Plan")
	if perm:
		w.add(perm)
	where = w.sql()

	total = frappe.db.sql(f"select count(*) from {PLAN} where {where}", w.params)[0][0]
	rows = frappe.db.sql(
		f"""select {PLAN}.name, {PLAN}.company, {PLAN}.posting_date, {PLAN}.status, {PLAN}.docstatus, {PLAN}.modified,
			coalesce({PLAN}.ppc_planner, {PLAN}.owner) as planner, {PLAN}.ppc_priority as priority,
			(select count(distinct pi.item_code) from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name) as fg_items,
			(select ifnull(sum(pi.planned_qty), 0) from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name) as planned_qty,
			(select ifnull(sum(pi.produced_qty), 0) from `tabProduction Plan Item` pi where pi.parent = {PLAN}.name) as produced_qty,
			(select count(*) from {WO} where {WO}.production_plan = {PLAN}.name and {WO}.docstatus < 2) as work_orders,
			(select count(*) from {WO} where {WO}.production_plan = {PLAN}.name and {WO}.docstatus = 0) as draft_work_orders,
			(select count(*) from {JC} where {JC}.ppc_production_plan = {PLAN}.name and {JC}.docstatus < 2) as job_cards,
			(select count(*) from `tabPPC Audit Log` a where a.ppc_plan = {PLAN}.name and a.result = 'Failed' and a.retry_status = 'Pending') as pending_failures
		from {PLAN} where {where}
		order by ({order_col} is null), {order_col} {direction}, {PLAN}.name desc
		limit %(limit)s offset %(offset)s""",
		{**w.params, "limit": page_length, "offset": (page - 1) * page_length},
		as_dict=True,
	)

	orders = _sales_orders_of([r.name for r in rows])
	for r in rows:
		r.posting_date = str(r.posting_date) if r.posting_date else None
		r.modified = _s(r.modified)
		r.stage = plan_stage(r)
		r.planned_qty, r.produced_qty = flt(r.planned_qty), flt(r.produced_qty)
		r.sales_orders, r.sales_order_count = orders.get(r.name, ([], 0))
	return _page_result(rows, total, page, page_length)


def _sales_orders_of(plans: list[str]) -> dict:
	"""{plan: ([first three Sales Orders], count)} in one query (no per-row lookups)."""
	if not plans:
		return {}
	rows = frappe.db.sql(
		"""select parent, count(distinct sales_order) as n,
			substring_index(group_concat(distinct sales_order order by sales_order separator ','), ',', 3) as first
		from (
			select parent, sales_order from `tabProduction Plan Sales Order`
			union
			select parent, sales_order from `tabProduction Plan Item` where ifnull(sales_order, '') != ''
		) x where parent in %(p)s and ifnull(sales_order, '') != '' group by parent""",
		{"p": tuple(plans)},
		as_dict=True,
	)
	return {r.parent: ([s for s in (r["first"] or "").split(",") if s], cint(r.n)) for r in rows}


# =============================================================================================
# Dashboard overview
# =============================================================================================
def _scope(filters: dict) -> dict:
	"""One filter dict for every query on the dashboard. The date range means: Production Plans by posting date,
	Work Orders and Job Cards by schedule overlap (a card that starts before the range but runs into it counts)."""
	out = {k: v for k, v in filters.items() if k not in ("date_from", "date_to")}
	if filters.get("date_from"):
		out["posting_from"] = out["window_from"] = filters["date_from"]
	if filters.get("date_to"):
		out["posting_to"] = out["window_to"] = filters["date_to"]
	return out


def get_overview(filters: dict | None) -> dict:
	"""One response for the whole dashboard (one round trip, aggregated SQL, no per-row lookups)."""
	raw = dict(filters or {})
	date_from, date_to = raw.pop("date_from", None), raw.pop("date_to", None)
	filters = normalize_filters(raw)
	for key, value in (("date_from", date_from), ("date_to", date_to)):
		if value:
			filters[key] = str(getdate(value))
	jc_filters = _scope(filters)

	dash = queries.get_dashboard(jc_filters)
	k = dash["kpis"]
	kpis = {
		"total_plans": k.get("total_ppc_plans", 0),
		"pending_qty": k.get("pending_production_qty", 0),
		"draft_work_orders": k.get("draft_work_orders", 0),
		"open_work_orders": k.get("open_work_orders", 0),
		"total_job_cards": k.get("total_job_cards", 0),
		"work_in_progress": k.get("work_in_progress", 0),
		"completed_job_cards": k.get("completed_job_cards", 0),
		"overdue_operations": k.get("overdue_operations", 0),
		"scheduling_conflicts": k.get("scheduling_conflicts", 0),
		"failed_syncs": k.get("failed_syncs", 0),
	}

	overdue = queries.get_job_cards({**jc_filters, "overdue_only": 1}, 1, 8, "revised_end", "asc")
	failed = queries.get_job_cards({**jc_filters, "sync_status": "Failed"}, 1, 8, "modified", "desc")
	return {
		"as_of": dash["as_of"],
		"kpis": kpis,
		"schedule": dash["schedule"],
		"workload": dash["workload"][:8],
		"delivery_risks": delivery_risks(filters),
		"overdue": {"rows": overdue["rows"], "total": overdue["total"]},
		"failed": {"rows": failed["rows"], "total": failed["total"]},
		"recent_revisions": get_revision_log({k: v for k, v in filters.items() if k in ("plan", "work_order")}, 1, 8)["rows"],
	}


def delivery_risks(filters: dict, horizon_days: int = 14, limit: int = 10) -> list[dict]:
	"""Work Orders (not finished) whose Sales Order delivery is close, past, or earlier than the planned end."""
	w = Where()
	w.add(f"{WO}.docstatus < 2")
	w.add(f"{WO}.status not in ('Completed', 'Stopped', 'Closed', 'Cancelled')")
	w.add(f"{WO}.expected_delivery_date is not null")
	w.add(f"({WO}.expected_delivery_date <= {w.bind(add_days(nowdate(), horizon_days))} or {WO}.planned_end_date > {WO}.expected_delivery_date)")
	w.add(f"ifnull({WO}.production_plan, '') != ''")
	for key, col in (("plan", f"{WO}.production_plan"), ("company", f"{WO}.company"), ("customer", f"{SO}.customer"), ("item_code", f"{WO}.production_item")):
		if filters.get(key):
			w.add(f"{col} = {w.bind(filters[key])}")
	if filters.get("planner"):
		w.add(f"coalesce({PLAN}.ppc_planner, {PLAN}.owner) = {w.bind(filters['planner'])}")
	perm = _permission_clause("Work Order")
	if perm:
		w.add(perm)
	rows = frappe.db.sql(
		f"""select {WO}.name as work_order, {WO}.sales_order, {SO}.customer_name, {WO}.production_item as item_code, {WO}.item_name,
			{WO}.expected_delivery_date as delivery, {WO}.planned_end_date as planned_end, {WO}.qty, {WO}.produced_qty,
			{WO}.docstatus, {WO}.production_plan as ppc_plan
		from {WO} left join {SO} on {SO}.name = {WO}.sales_order left join {PLAN} on {PLAN}.name = {WO}.production_plan
		where {w.sql()} order by {WO}.expected_delivery_date, {WO}.name limit %(limit)s""",
		{**w.params, "limit": limit},
		as_dict=True,
	)
	today = getdate()
	for r in rows:
		delivery = getdate(r.delivery)
		days = (delivery - today).days
		late = bool(r.planned_end and getdate(r.planned_end) > delivery)
		r["risk"] = "Overdue" if days < 0 else ("Planned after delivery" if late else "Due soon")
		r["days_left"] = days
		r["delivery"], r["planned_end"] = str(r.delivery), _s(r.planned_end)
		r["draft"] = r.docstatus == 0
	return rows


# =============================================================================================
# Schedule-change log
# =============================================================================================
REVISION_FIELDS = [
	"name", "job_card", "work_order", "ppc_plan", "sales_order", "operation", "workstation", "revision_no", "revision_type",
	"previous_start", "previous_end", "accepted_start", "accepted_end", "change_reason", "baseline_note", "change_source",
	"changed_by", "changed_at", "sync_status", "correlation_id", "corrected",
]
REVISION_SORT = {
	"changed_at": "changed_at", "job_card": "job_card", "work_order": "work_order", "revision_no": "revision_no",
	"changed_by": "changed_by", "change_source": "change_source",
}


def get_revision_log(filters: dict | None, page: int = 1, page_length: int = 50, sort_by: str = "changed_at", sort_order: str = "desc") -> dict:
	"""Schedule revisions across all Job Cards (permission-checked by Frappe as for any list)."""
	raw = filters or {}
	page, page_length = _paging(page, page_length)
	check_doctype_permission(REVISION_DT, "read")
	cond: dict = {}
	for src, field in (("job_card", "job_card"), ("work_order", "work_order"), ("plan", "ppc_plan"), ("operation", "operation"),
			("workstation", "workstation"), ("user", "changed_by"), ("source", "change_source"), ("sync_status", "sync_status"), ("sales_order", "sales_order")):
		if raw.get(src):
			cond[field] = str(raw[src])[:140]
	kind = raw.get("revision_type") or "Revision"  # baselines are rows too; "All" shows both
	if kind in ("Revision", "Baseline"):
		cond["revision_type"] = kind
	if raw.get("sales_order_like"):
		cond["sales_order"] = ["like", like(str(raw["sales_order_like"])[:140])]
	lo, hi = raw.get("from"), raw.get("to")
	if lo and hi:
		cond["changed_at"] = ["between", [str(getdate(lo)), str(getdate(hi))]]  # Frappe widens a date-only end to the end of that day
	elif lo:
		cond["changed_at"] = [">=", str(getdate(lo))]
	elif hi:
		cond["changed_at"] = ["<", str(add_days(getdate(hi), 1))]
	or_filters = None
	if raw.get("search"):
		b = ["like", like(str(raw["search"])[:140])]
		or_filters = {"job_card": b, "work_order": b, "operation": b, "change_reason": b}
	order = f"{REVISION_SORT.get(sort_by) or 'changed_at'} {'asc' if str(sort_order).lower() == 'asc' else 'desc'}, name desc"
	total = frappe.get_list(REVISION_DT, filters=cond, or_filters=or_filters, fields=["count(name) as n"])[0]["n"]
	rows = frappe.get_list(
		REVISION_DT, filters=cond, or_filters=or_filters, fields=REVISION_FIELDS, order_by=order,
		limit_start=(page - 1) * page_length, limit_page_length=page_length,
	)
	for r in rows:
		for key in ("previous_start", "previous_end", "accepted_start", "accepted_end", "changed_at"):
			r[key] = _s(r[key])
		r["note"] = r.baseline_note if r.revision_type == "Baseline" else r.change_reason
	return _page_result(rows, cint(total), page, page_length)


# =============================================================================================
# Workload and capacity
# =============================================================================================
def _hours_between(a: datetime, b: datetime) -> float:
	return max(0.0, (b - a).total_seconds() / 3600)


def get_workload(filters: dict | None, from_date=None, to_date=None, include_idle: bool = True) -> dict:
	"""Per workstation: available hours (from the workstation's own working hours and holiday list), scheduled hours
	(from the Job Cards' scheduled time rows), remaining capacity, utilisation and overloaded periods.

	"Available" is only reported when ERPNext holds working hours for the workstation; otherwise it is `None` and the
	page says "unavailable". A weekly-off / holiday list is honoured when set."""
	filters = normalize_filters({k: v for k, v in (filters or {}).items() if k not in ("window_from", "window_to")})
	start = getdate(from_date) if from_date else getdate()
	end = getdate(to_date) if to_date else add_days(start, 27)
	if end < start:
		start, end = end, start
	span = min((end - start).days + 1, 92)
	end = add_days(start, span - 1)
	days = [add_days(start, i) for i in range(span)]
	lo, hi = datetime.combine(start, time.min), datetime.combine(add_days(end, 1), time.min)

	where, params = _scope_sql(filters)
	active = f"{JC}.docstatus = 0 and {JC}.status not in {DONE_SQL}"
	span_rows = frappe.db.sql(
		f"""select {JC}.name, {JC}.workstation, st.from_time as s, st.to_time as e
		{JC_FROM} join `tabJob Card Scheduled Time` st on st.parent = {JC}.name
		where {where} and {active} and st.from_time < %(hi)s and st.to_time > %(lo)s""",
		{**params, "lo": lo, "hi": hi},
		as_dict=True,
	) + frappe.db.sql(
		f"""select {JC}.name, {JC}.workstation, {JC}.expected_start_date as s, {JC}.expected_end_date as e
		{JC_FROM} where {where} and {active} and {JC}.expected_start_date < %(hi)s and {JC}.expected_end_date > %(lo)s
			and not exists (select 1 from `tabJob Card Scheduled Time` st where st.parent = {JC}.name)""",
		{**params, "lo": lo, "hi": hi},
		as_dict=True,
	)
	unassigned = frappe.db.sql(
		f"select count(*) {JC_FROM} where {where} and {active} and ifnull({JC}.workstation, '') = ''", params
	)[0][0]

	by_ws: dict[str, list] = defaultdict(list)
	cards_by_ws: dict[str, set] = defaultdict(set)
	for r in span_rows:
		if not r.workstation or not r.s or not r.e:
			continue
		by_ws[r.workstation].append((r.s, r.e))
		cards_by_ws[r.workstation].add(r.name)

	names = set(by_ws)
	if filters.get("workstation"):
		names.add(filters["workstation"])
	idle = []
	if include_idle:
		has_disabled = frappe.get_meta("Workstation").has_field("disabled")
		idle_filters = {"disabled": 0} if has_disabled else {}
		idle = [n for n in frappe.get_all("Workstation", filters=idle_filters, pluck="name", limit_page_length=300, order_by="name") if n not in names]
		if filters.get("workstation"):
			idle = []
	info = _workstation_info(sorted(names | set(idle)), start, end)

	out = []
	for name in sorted(names) + idle:
		ws = info.get(name, {})
		capacity = max(1, cint(ws.get("production_capacity")))
		shift = ws.get("shift_hours")  # None = ERPNext holds no working hours for this workstation
		scheduled = [0.0] * span
		for s, e in by_ws.get(name, []):
			s, e = max(s, lo), min(e, hi)
			day = max(start, s.date())
			while day <= end and datetime.combine(day, time.min) < e:
				d0 = datetime.combine(day, time.min)
				scheduled[(day - start).days] += _hours_between(max(s, d0), min(e, d0 + timedelta(days=1)))
				day = add_days(day, 1)
		holidays = ws.get("holidays", set())
		available = None if shift is None else [0.0 if d in holidays else shift * capacity for d in days]
		total_sched = round(sum(scheduled), 2)
		total_avail = None if available is None else round(sum(available), 2)
		periods, run = [], None
		if available is not None:
			for i, d in enumerate(days):
				if scheduled[i] > available[i] + 1e-9:
					if run and run["to"] == days[i - 1]:
						run["to"], run["peak"] = d, max(run["peak"], round(scheduled[i], 2))
					else:
						run = {"from": d, "to": d, "peak": round(scheduled[i], 2)}
						periods.append(run)
		out.append(
			{
				"workstation": name,
				"capacity": capacity,
				"shift_hours": shift,
				"holiday_list": ws.get("holiday_list"),
				"available_known": available is not None,
				"available_hours": total_avail,
				"scheduled_hours": total_sched,
				"remaining_hours": None if total_avail is None else round(max(total_avail - total_sched, 0), 2),
				"utilization": None if not total_avail else round(100 * total_sched / total_avail, 1),
				"overloaded_days": sum(1 for p in periods for _i in range((p["to"] - p["from"]).days + 1)),
				"periods": [{"from": str(p["from"]), "to": str(p["to"]), "peak_hours": p["peak"]} for p in periods],
				"job_cards": len(cards_by_ws.get(name, ())),
				"daily": [round(x, 2) for x in scheduled],
				"daily_available": None if available is None else [round(x, 2) for x in available],
			}
		)
	notes = []
	if any(not r["available_known"] for r in out):
		notes.append(_("Some workstations have no working hours in ERPNext, so their available hours and utilisation cannot be calculated."))
	return {
		"from": str(start), "to": str(end), "days": [str(d) for d in days], "rows": out,
		"unassigned_operations": cint(unassigned), "notes": notes, "as_of": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
	}


def _workstation_info(names: list[str], start, end) -> dict:
	if not names:
		return {}
	meta = frappe.get_meta("Workstation")
	fields = ["name", "production_capacity"] + (["holiday_list"] if meta.has_field("holiday_list") else [])
	info = {d.name: dict(d) for d in frappe.get_all("Workstation", filters={"name": ["in", names]}, fields=fields, limit_page_length=0)}
	if frappe.db.table_exists("Workstation Working Hour"):
		child = frappe.get_meta("Workstation Working Hour")
		wf = ["parent", "start_time", "end_time"] + (["hours"] if child.has_field("hours") else []) + (["enabled"] if child.has_field("enabled") else [])
		shifts = defaultdict(float)
		seen = set()
		for r in frappe.get_all("Workstation Working Hour", filters={"parent": ["in", names], "parenttype": "Workstation"}, fields=wf, limit_page_length=0):
			if "enabled" in r and not cint(r.enabled):
				continue
			hours = flt(r.get("hours")) if r.get("hours") else (_td_hours(r.end_time) - _td_hours(r.start_time) if r.start_time and r.end_time else 0)
			seen.add(r.parent)
			shifts[r.parent] += max(0.0, hours)
		for n in names:
			if n in info:
				info[n]["shift_hours"] = round(shifts[n], 4) if n in seen and shifts[n] > 0 else None
	lists = {i["holiday_list"] for i in info.values() if i.get("holiday_list")}
	if lists:
		holidays = defaultdict(set)
		for h in frappe.get_all("Holiday", filters={"parent": ["in", list(lists)], "holiday_date": ["between", [start, end]]}, fields=["parent", "holiday_date"], limit_page_length=0):
			holidays[h.parent].add(getdate(h.holiday_date))
		for i in info.values():
			i["holidays"] = holidays.get(i.get("holiday_list"), set())
	return info


def _td_hours(value) -> float:
	if isinstance(value, timedelta):
		return value.total_seconds() / 3600
	return value.hour + value.minute / 60 if hasattr(value, "hour") else 0.0


# =============================================================================================
# Exceptions
# =============================================================================================
EXCEPTION_TYPES = {
	# (title, severity) - titles are translated when used, never at import time
	"failed_sync": ("Failed synchronization", "high"),
	"schedule_drift": ("Schedule drift", "medium"),
	"work_order_drift": ("Work Order out of step", "medium"),
	"workstation_conflict": ("Workstation conflict", "medium"),
	"dependency_conflict": ("Dependency conflict", "medium"),
	"overdue_operation": ("Overdue operation", "medium"),
	"orphan_work_order": ("Missing or invalid Work Order", "high"),
	"revision_mismatch": ("Revision history mismatch", "high"),
	"missing_baseline": ("Missing baseline", "low"),
	"missing_history": ("Missing history", "low"),
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
MAX_EXCEPTIONS = 5000


def get_exceptions(filters: dict | None, types=None, page: int = 1, page_length: int = 50) -> dict:
	"""Everything that needs a planner's attention, merged into one list with counts per type."""
	filters = normalize_filters(filters)
	page, page_length = _paging(page, page_length)
	wanted = {t for t in (types or []) if t in EXCEPTION_TYPES}
	items: list[dict] = []

	def add(kind, *, job_card=None, work_order=None, plan=None, operation=None, workstation=None, message="", action="", repairable=False, extra=None):
		title, severity = EXCEPTION_TYPES[kind]
		items.append({
			"id": f"{kind}::{job_card or work_order}", "type": kind, "title": _(title), "severity": severity, "job_card": job_card,
			"work_order": work_order, "plan": plan, "operation": operation, "workstation": workstation, "message": message,
			"action": action, "repairable": bool(repairable), **(extra or {}),
		})

	report = reconcile.check_consistency(filters.get("plan"))
	for i in report["issues"]:
		add(i["type"], job_card=i["job_card"], work_order=i["work_order"], plan=i["plan"], message=i["message"], action=i["action"], repairable=i["repairable"])

	where, params = _scope_sql(filters)
	rows = frappe.db.sql(
		f"""select {JC}.name, {JC}.workstation, {JC}.work_order, {JC}.sequence_id, {JC}.status, {JC}.docstatus, {JC}.operation,
			{JC}.expected_start_date, {JC}.expected_end_date, {JC}.ppc_production_plan as ppc_plan, {REVISED_END} as revised_end
		{JC_FROM} where {where} limit {MAX_EXCEPTIONS}""",
		params,
		as_dict=True,
	)
	now = now_datetime()
	by_name = {r.name: r for r in rows}
	for name, pairs in queries.compute_conflict_kinds(rows).items():
		r = by_name[name]
		for kind, text in pairs:
			add(kind, job_card=name, work_order=r.work_order, plan=r.ppc_plan, operation=r.operation, workstation=r.workstation, message=text,
				action=_("Move one of the operations in the Production Gantt."))
	for r in rows:
		if r.docstatus == 0 and r.status not in JC_DONE_STATUSES and r.revised_end and r.revised_end < now:
			add("overdue_operation", job_card=r.name, work_order=r.work_order, plan=r.ppc_plan, operation=r.operation, workstation=r.workstation,
				message=_("Should have ended on {0}.").format(frappe.format(r.revised_end, {"fieldtype": "Datetime"})), action=_("Reschedule it or complete the operation."))

	# the consistency issues carry no workstation/operation: fill them from the rows we already have, then apply the filters
	for it in items:
		src = by_name.get(it["job_card"])
		if src and not it["operation"]:
			it["operation"], it["workstation"] = src.operation, src.workstation
	for key, field in (("work_order", "work_order"), ("job_card", "job_card"), ("workstation", "workstation"), ("operation", "operation")):
		if filters.get(key):
			items = [it for it in items if it[field] == filters[key]]
	if filters.get("search"):
		needle = filters["search"].lower()
		items = [it for it in items if any(needle in str(it[f] or "").lower() for f in ("job_card", "work_order", "plan", "operation", "workstation", "message", "title"))]

	counts = defaultdict(int)
	for it in items:
		counts[it["type"]] += 1
	shown = [it for it in items if not wanted or it["type"] in wanted]
	shown.sort(key=lambda it: (SEVERITY_ORDER[it["severity"]], it["type"], it["job_card"] or "", it["work_order"] or ""))
	total = len(shown)
	chunk = shown[(page - 1) * page_length: page * page_length]
	return {
		**_page_result(chunk, total, page, page_length),
		"counts": {k: counts.get(k, 0) for k in EXCEPTION_TYPES},
		"types": [{"type": k, "title": _(v[0]), "severity": v[1]} for k, v in EXCEPTION_TYPES.items()],
		"truncated": report.get("truncated", False) or len(rows) >= MAX_EXCEPTIONS,
	}


# =============================================================================================
# Work Order pre-submit check
# =============================================================================================
MAX_PRECHECK = 100


def precheck_work_orders(names: list[str], plan: str | None = None) -> dict:
	"""For each draft Work Order: would ERPNext's own validation and mandatory-field checks pass if it were submitted now?

	Nothing is saved: the validation runs in memory inside a savepoint that is always rolled back. Rules that are only
	enforced at the moment of submission (some Server Scripts, stock checks) cannot be known here; they are still reported
	per record by the real submission."""
	results = []
	seen = set()
	for name in [n for n in (names or []) if n and not (n in seen or seen.add(n))][:MAX_PRECHECK]:
		row = {"name": name, "ok": False, "blockers": [], "item_code": None, "plan": None}
		info = frappe.db.get_value("Work Order", name, ["docstatus", "status", "production_plan", "production_item", "bom_no", "qty"], as_dict=True)
		if not info:
			row["blockers"].append(_("Work Order does not exist."))
			results.append(row)
			continue
		row["item_code"], row["plan"] = info.production_item, info.production_plan
		if plan and info.production_plan != plan:
			row["blockers"].append(_("Belongs to a different Production Plan."))
		elif not frappe.has_permission("Work Order", "submit", doc=name):
			row["blockers"].append(_("You may not submit this Work Order."))
		elif info.docstatus != 0:
			row["blockers"].append(_("Already {0}.").format(_("submitted") if info.docstatus == 1 else _("cancelled")))
		else:
			row["blockers"].extend(_dry_run_validation(name))
		row["ok"] = not row["blockers"]
		results.append(row)
	return {"results": results, "ok_count": sum(r["ok"] for r in results), "blocked_count": sum(not r["ok"] for r in results), "limit": MAX_PRECHECK}


def _dry_run_validation(name: str) -> list[str]:
	"""ERPNext's own `validate` + mandatory check on the stored document, then everything is rolled back."""
	blockers: list[str] = []
	save_point = f"ppc_precheck_{abs(hash(name)) % 10**8}"
	frappe.db.savepoint(save_point)
	try:
		doc = frappe.get_doc("Work Order", name)
		doc._action = "save"  # set by Frappe's own save() before the save-time checks run
		# the same sequence a real save runs: snapshot of the stored document, before_validate / validate / before_save
		# (controller and hooks), then Frappe's own field checks (mandatory, links, selects, ...)
		for step in (doc.load_doc_before_save, doc.run_before_save_methods, doc._validate):
			try:
				step()
			except Exception as exc:
				blockers.append(error_text(exc))
				if step == doc.load_doc_before_save:
					break
	finally:
		frappe.db.rollback(save_point=save_point)
		frappe.clear_messages()
	return [b for i, b in enumerate(blockers) if b and b not in blockers[:i]]
