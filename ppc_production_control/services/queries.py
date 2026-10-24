"""Read models for the PPC screen: Job Card table, Work Order table, Gantt and dashboard.

One filter dictionary drives all of them, so the table, the Gantt chart and every KPI always
describe the same set of records.

Security notes
--------------
* Every user value reaches SQL as a *bound parameter*. SQL text is assembled only from constant
  fragments in this file; sort columns come from a whitelist.
* Row-level permissions (role + User Permissions + permission_query_conditions hooks) are applied
  with Frappe's own `DatabaseQuery.build_match_conditions`, on the primary doctype of each query.
* Cancelled documents are excluded unless explicitly requested.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe import _
from frappe.model.db_query import DatabaseQuery
from frappe.utils import cint, flt, getdate, now_datetime

from ppc_production_control.services import revisions, scheduling
from ppc_production_control.services.constants import JC_DONE_STATUSES, JC_RESCHEDULABLE_STATUSES
from ppc_production_control.services.permissions import check_doctype_permission
from ppc_production_control.services.utils import get_settings

JC = "`tabJob Card`"
WO = "`tabWork Order`"
SO = "`tabSales Order`"
PLAN = "`tabProduction Plan`"
WOP = "`tabWork Order Operation`"

TEXT_FILTERS = (
	"plan", "sales_order", "sales_order_like", "customer", "company", "item_code", "component_item",
	"work_order", "job_card", "operation", "workstation", "planner", "employee", "status", "priority", "search",
)
DATE_FILTERS = (
	"planned_start_from", "planned_start_to", "revised_start_from", "revised_start_to",
	"delivery_from", "delivery_to",
)
FLAG_FILTERS = ("overdue_only", "conflict_only", "unassigned_only", "include_non_ppc")

STATUS_CHOICES = ("Draft", "Open", "Material Transferred", "Work In Progress", "On Hold", "Submitted", "Completed", "Cancelled")
MAX_PAGE = 200
MAX_CONFLICT_NAMES = 5000

REVISED_START = f"coalesce({JC}.expected_start_date, {WOP}.planned_start_time)"
REVISED_END = f"coalesce({JC}.expected_end_date, {WOP}.planned_end_time)"

SORTABLE = {
	"name": f"{JC}.name",
	"work_order": f"{JC}.work_order",
	"operation": f"{JC}.operation",
	"sequence_id": f"{JC}.sequence_id",
	"workstation": f"{JC}.workstation",
	"status": f"{JC}.status",
	"item_code": f"{JC}.production_item",
	"sales_order": f"{WO}.sales_order",
	"customer": f"{SO}.customer_name",
	"planned_qty": f"{JC}.for_quantity",
	"revised_start": REVISED_START,
	"revised_end": REVISED_END,
	"original_start": f"{JC}.ppc_original_start",
	"actual_start": f"{JC}.actual_start_date",
	"priority": f"{PLAN}.ppc_priority",
	"plan": f"{JC}.ppc_production_plan",
	"modified": f"{JC}.modified",
}


# =============================================================================================
# Filters
# =============================================================================================
def normalize_filters(raw: dict | None) -> dict:
	"""Whitelist and coerce the client's filter dict. Unknown keys are dropped."""
	raw = raw or {}
	out: dict = {}
	for key in TEXT_FILTERS:
		value = raw.get(key)
		if value is None or value == "":
			continue
		out[key] = str(value).strip()[:140]
	for key in DATE_FILTERS:
		value = raw.get(key)
		if value:
			try:
				out[key] = str(getdate(value))
			except Exception:
				frappe.throw(_("Invalid date for filter {0}").format(key))
	for key in FLAG_FILTERS:
		if cint(raw.get(key)):
			out[key] = 1
	if out.get("status") and out["status"] not in STATUS_CHOICES:
		frappe.throw(_("Unknown status filter {0}").format(out["status"]))
	return out


def like(text: str) -> str:
	return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Where:
	def __init__(self):
		self.parts: list[str] = []
		self.params: dict = {}
		self._n = 0

	def add(self, sql: str, **named):
		"""`sql` may contain {p} placeholders that are replaced by unique bind names."""
		self.parts.append(sql)
		self.params.update(named)

	def bind(self, value) -> str:
		self._n += 1
		key = f"p{self._n}"
		self.params[key] = value
		return f"%({key})s"

	def sql(self) -> str:
		return " and ".join(f"({p})" for p in self.parts) if self.parts else "1=1"


def build_job_card_where(filters: dict, *, ignore_conflict: bool = False) -> Where:
	w = Where()
	status = filters.get("status")
	if status == "Cancelled":
		w.add(f"{JC}.docstatus = 2")
	else:
		w.add(f"{JC}.docstatus < 2")
	if status == "Draft":
		w.add(f"{JC}.docstatus = 0")
	elif status and status != "Cancelled":
		w.add(f"{JC}.status = {w.bind(status)}")

	if not filters.get("include_non_ppc"):
		w.add(f"ifnull({JC}.ppc_production_plan, '') != ''")

	if filters.get("plan"):
		w.add(f"{JC}.ppc_production_plan = {w.bind(filters['plan'])}")
	if filters.get("sales_order"):
		w.add(_so_name_clause(w, filters["sales_order"], exact=True))
	if filters.get("sales_order_like"):
		w.add(_so_name_clause(w, filters["sales_order_like"], exact=False))
	if filters.get("customer"):
		w.add(f"{SO}.customer = {w.bind(filters['customer'])}")
	if filters.get("company"):
		w.add(f"{JC}.company = {w.bind(filters['company'])}")
	if filters.get("item_code"):
		w.add(f"{JC}.production_item = {w.bind(filters['item_code'])}")
	if filters.get("component_item"):
		w.add(
			f"exists (select 1 from `tabWork Order Item` woi where woi.parent = {JC}.work_order "
			f"and woi.item_code = {w.bind(filters['component_item'])})"
		)
	if filters.get("work_order"):
		w.add(f"{JC}.work_order = {w.bind(filters['work_order'])}")
	if filters.get("job_card"):
		w.add(f"{JC}.name = {w.bind(filters['job_card'])}")
	if filters.get("operation"):
		w.add(f"{JC}.operation = {w.bind(filters['operation'])}")
	if filters.get("workstation"):
		w.add(f"{JC}.workstation = {w.bind(filters['workstation'])}")
	if filters.get("planner"):
		w.add(f"coalesce({PLAN}.ppc_planner, {PLAN}.owner) = {w.bind(filters['planner'])}")
	if filters.get("priority"):
		w.add(f"{PLAN}.ppc_priority = {w.bind(filters['priority'])}")
	if filters.get("employee"):
		w.add(
			f"exists (select 1 from `tabJob Card Time Log` tl where tl.parent = {JC}.name "
			f"and tl.employee = {w.bind(filters['employee'])})"
		)
	if filters.get("planned_start_from"):
		w.add(f"date({WO}.planned_start_date) >= {w.bind(filters['planned_start_from'])}")
	if filters.get("planned_start_to"):
		w.add(f"date({WO}.planned_start_date) <= {w.bind(filters['planned_start_to'])}")
	if filters.get("revised_start_from"):
		w.add(f"date({REVISED_START}) >= {w.bind(filters['revised_start_from'])}")
	if filters.get("revised_start_to"):
		w.add(f"date({REVISED_START}) <= {w.bind(filters['revised_start_to'])}")
	if filters.get("delivery_from"):
		w.add(f"{WO}.expected_delivery_date >= {w.bind(filters['delivery_from'])}")
	if filters.get("delivery_to"):
		w.add(f"{WO}.expected_delivery_date <= {w.bind(filters['delivery_to'])}")
	if filters.get("overdue_only"):
		w.add(overdue_sql(w))
	if filters.get("unassigned_only"):
		w.add(f"ifnull({JC}.workstation, '') = ''")
	if filters.get("search"):
		b = w.bind(like(filters["search"]))
		w.add(
			f"{JC}.name like {b} or {JC}.work_order like {b} or {JC}.operation like {b} or {JC}.production_item like {b} "
			f"or {JC}.workstation like {b} or {WO}.sales_order like {b} or {SO}.customer_name like {b}"
		)
	return w


def _so_name_clause(w: Where, value: str, exact: bool) -> str:
	b = w.bind(value if exact else like(value))
	return f"{WO}.sales_order {'=' if exact else 'like'} {b}"


def overdue_sql(w: Where) -> str:
	now = w.bind(now_datetime())
	return (
		f"{JC}.docstatus = 0 and {JC}.status not in ('Completed', 'Submitted', 'Cancelled') "
		f"and {REVISED_END} is not null and {REVISED_END} < {now}"
	)


JC_FROM = f"""from {JC}
	left join {WO} on {WO}.name = {JC}.work_order
	left join {SO} on {SO}.name = {WO}.sales_order
	left join {PLAN} on {PLAN}.name = {JC}.ppc_production_plan
	left join {WOP} on {WOP}.name = {JC}.operation_id"""

JC_SELECT = f"""
	{JC}.name, {JC}.work_order, {JC}.operation, {JC}.workstation, {JC}.sequence_id, {JC}.for_quantity,
	{JC}.total_completed_qty, {JC}.status, {JC}.docstatus, {JC}.production_item as item_code,
	{JC}.expected_start_date, {JC}.expected_end_date, {JC}.actual_start_date, {JC}.actual_end_date,
	{JC}.ppc_production_plan, {JC}.modified, {JC}.company,
	{WO}.sales_order, {WO}.item_name, {WO}.status as wo_status, {WO}.docstatus as wo_docstatus,
	{WO}.expected_delivery_date, {WO}.planned_start_date as wo_planned_start, {WO}.planned_end_date as wo_planned_end,
	{WO}.qty as wo_qty, {SO}.customer, {SO}.customer_name,
	{PLAN}.ppc_priority as priority, coalesce({PLAN}.ppc_planner, {PLAN}.owner) as production_planner,
	{WOP}.planned_start_time as op_planned_start, {WOP}.planned_end_time as op_planned_end,
	{JC}.ppc_original_start as original_start_date, {JC}.ppc_original_end as original_end_date,
	{JC}.ppc_schedule_revision, {JC}.ppc_last_rescheduled_at, {JC}.ppc_last_rescheduled_by,
	{JC}.ppc_schedule_sync_status, {JC}.ppc_baseline_source, {JC}.ppc_revised_start, {JC}.ppc_revised_end,
	(select group_concat(distinct tl.employee order by tl.employee separator ', ')
		from `tabJob Card Time Log` tl where tl.parent = {JC}.name and ifnull(tl.employee, '') != '') as employees
"""


def _permission_clause(doctype: str) -> str:
	"""Row-level permission SQL for `doctype` (raises PermissionError without read access)."""
	check_doctype_permission(doctype, "read")
	return DatabaseQuery(doctype, user=frappe.session.user).build_match_conditions() or ""


def _scope_sql(filters: dict) -> tuple[str, dict]:
	w = build_job_card_where(filters)
	perm = _permission_clause("Job Card")
	if perm:
		w.add(perm)
	# Work Order permissions apply to the join too, so a restricted user cannot read around them
	check_doctype_permission("Work Order", "read")
	return w.sql(), w.params


# =============================================================================================
# Conflict detection (same rules as scheduling.validate_schedule, for many cards at once)
# =============================================================================================
def compute_conflicts(rows: list[dict]) -> dict[str, list[str]]:
	"""{job_card: [reasons]} for the given rows. Needs name, workstation, work_order, sequence_id,
	status, docstatus and start/end (`expected_*` or the keys `start`/`end`)."""
	active = [r for r in rows if r.get("docstatus") == 0 and r.get("status") not in JC_DONE_STATUSES]
	flagged: dict[str, list[str]] = defaultdict(list)

	def span(r):
		return scheduling.as_dt(r.get("start") or r.get("expected_start_date")), scheduling.as_dt(r.get("end") or r.get("expected_end_date"))

	dated = [(r, *span(r)) for r in active]
	dated = [(r, s, e) for r, s, e in dated if s and e and e > s]
	if not dated:
		return {}

	# --- workstation capacity ---
	stations = {r["workstation"] for r, _s, _e in dated if r.get("workstation")}
	if stations:
		lo = min(s for _r, s, _e in dated)
		hi = max(e for _r, _s, e in dated)
		others = frappe.db.sql(
			f"""select name, workstation, expected_start_date as s, expected_end_date as e from {JC}
			where workstation in %(ws)s and docstatus = 0 and status not in ('Completed', 'Submitted', 'Cancelled')
				and expected_start_date < %(hi)s and expected_end_date > %(lo)s""",
			{"ws": tuple(stations), "lo": lo, "hi": hi},
			as_dict=True,
		)
		capacity = {
			d.name: max(1, cint(d.production_capacity))
			for d in frappe.get_all("Workstation", filters={"name": ["in", list(stations)]}, fields=["name", "production_capacity"])
		}
		by_ws = defaultdict(list)
		for o in others:
			by_ws[o.workstation].append(o)
		for r, s, e in dated:
			if not r.get("workstation"):
				continue
			overlapping = [(o.s, o.e) for o in by_ws.get(r["workstation"], []) if o.name != r["name"] and o.s < e and o.e > s]
			if overlapping and scheduling.peak_concurrency([(s, e)] + overlapping) > capacity.get(r["workstation"], 1):
				flagged[r["name"]].append(_("Workstation {0} is over-booked").format(r["workstation"]))

	# --- operation sequence ---
	work_orders = {r["work_order"] for r, _s, _e in dated if r.get("work_order")}
	if work_orders:
		siblings = frappe.get_all(
			"Job Card",
			filters={"work_order": ["in", list(work_orders)], "docstatus": ["<", 2]},
			fields=["name", "work_order", "sequence_id", "expected_start_date", "expected_end_date"],
			limit_page_length=0,
		)
		by_wo = defaultdict(list)
		for sib in siblings:
			by_wo[sib.work_order].append(sib)
		for r, s, _e in dated:
			preds, _succs = scheduling.dependency_neighbours(by_wo.get(r.get("work_order"), []), r["name"])
			pred_end = max((scheduling.as_dt(p["expected_end_date"]) for p in preds if p["expected_end_date"]), default=None)
			if pred_end and s < pred_end:
				flagged[r["name"]].append(_("Starts before the preceding operation ends"))
	return dict(flagged)


def conflict_names_in_scope(filters: dict) -> list[str]:
	"""Names of every Job Card in the (conflict-free) filter scope that has a conflict."""
	scoped = dict(filters)
	scoped.pop("conflict_only", None)
	where, params = _scope_sql(scoped)
	rows = frappe.db.sql(
		f"""select {JC}.name, {JC}.workstation, {JC}.work_order, {JC}.sequence_id, {JC}.status, {JC}.docstatus,
			{JC}.expected_start_date, {JC}.expected_end_date
		{JC_FROM} where {where}""",
		params,
		as_dict=True,
	)
	return sorted(compute_conflicts(rows))[:MAX_CONFLICT_NAMES]


# =============================================================================================
# Job Card table
# =============================================================================================
def get_job_cards(filters: dict | None, page: int = 1, page_length: int = 50, sort_by: str = "revised_start", sort_order: str = "asc") -> dict:
	filters = normalize_filters(filters)
	page = max(1, cint(page))
	page_length = min(max(1, cint(page_length) or 50), MAX_PAGE)
	order_col = SORTABLE.get(sort_by) or SORTABLE["revised_start"]
	direction = "desc" if str(sort_order).lower() == "desc" else "asc"

	conflict_filter = None
	if filters.get("conflict_only"):
		conflict_filter = conflict_names_in_scope(filters)
	where, params = _scope_sql({k: v for k, v in filters.items() if k != "conflict_only"})
	if conflict_filter is not None:
		if not conflict_filter:
			return _empty_page(page, page_length)
		where += f" and {JC}.name in %(conflict_names)s"
		params["conflict_names"] = tuple(conflict_filter)

	total = frappe.db.sql(f"select count(*) {JC_FROM} where {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""select {JC_SELECT} {JC_FROM} where {where}
		order by ({order_col} is null), {order_col} {direction}, {JC}.name
		limit %(limit)s offset %(offset)s""",
		{**params, "limit": page_length, "offset": (page - 1) * page_length},
		as_dict=True,
	)
	conflicts = compute_conflicts(rows)
	return {
		"rows": [present_job_card(r, conflicts.get(r.name)) for r in rows],
		"total": total,
		"page": page,
		"page_length": page_length,
		"pages": max(1, -(-total // page_length)),
	}


def _empty_page(page, page_length):
	return {"rows": [], "total": 0, "page": page, "page_length": page_length, "pages": 1}


def present_job_card(r, conflict_reasons=None) -> dict:
	revised_start = r.expected_start_date or r.op_planned_start
	revised_end = r.expected_end_date or r.op_planned_end
	pending = max(0.0, flt(r.for_quantity) - flt(r.total_completed_qty))
	movable = r.docstatus == 0 and r.status in JC_RESCHEDULABLE_STATUSES and r.wo_docstatus == 1 and r.wo_status not in (
		"Stopped", "Closed", "Completed", "Cancelled")
	now = now_datetime()
	overdue = bool(
		r.docstatus == 0 and r.status not in JC_DONE_STATUSES and revised_end and revised_end < now
	)
	return {
		"name": r.name,
		"ppc_plan": r.ppc_production_plan,
		"sales_order": r.sales_order,
		"customer": r.customer,
		"customer_name": r.customer_name,
		"item_code": r.item_code,
		"item_name": r.item_name,
		"work_order": r.work_order,
		"sequence_id": r.sequence_id,
		"operation": r.operation,
		"workstation": r.workstation,
		"planned_qty": flt(r.for_quantity),
		"completed_qty": flt(r.total_completed_qty),
		"pending_qty": pending,
		"original_start": _s(r.original_start_date),
		"original_end": _s(r.original_end_date),
		"revised_start": _s(revised_start),
		"revised_end": _s(revised_end),
		"is_estimated": not (r.expected_start_date and r.expected_end_date),
		"actual_start": _s(r.actual_start_date),
		"actual_end": _s(r.actual_end_date),
		"status": r.status,
		"docstatus": r.docstatus,
		"priority": r.priority,
		"employees": r.employees,
		"sync_status": _schedule_state(r),
		"revision": cint(r.ppc_schedule_revision),
		"last_rescheduled_by": r.ppc_last_rescheduled_by,
		"last_rescheduled_at": _s(r.ppc_last_rescheduled_at),
		"has_baseline": bool(r.original_start_date),
		"baseline_label": revisions.BASELINE_NOTES.get(r.ppc_baseline_source) if r.original_start_date else None,
		"difference": revisions.describe_difference(
			(r.original_start_date, r.original_end_date), (r.expected_start_date, r.expected_end_date), cint(r.ppc_schedule_revision)
		)["text"] if r.original_start_date and r.expected_start_date else "",
		"expected_delivery_date": _s(r.expected_delivery_date),
		"modified": str(r.modified),
		"movable": bool(movable),
		"overdue": overdue,
		"conflict": bool(conflict_reasons),
		"conflict_reasons": conflict_reasons or [],
	}


def _schedule_state(r) -> str:
	"""Failed / Drift / No baseline / Estimated / Rescheduled / Synced - the most important wins."""
	if r.docstatus == 2:
		return "Cancelled"
	if r.ppc_schedule_sync_status == "Failed":
		return "Failed"
	if not (r.expected_start_date and r.expected_end_date):
		return "Estimated"
	if r.ppc_revised_start and not (
		revisions_same(r.ppc_revised_start, r.expected_start_date) and revisions_same(r.ppc_revised_end, r.expected_end_date)
	):
		return "Drift"
	if not r.original_start_date:
		return "No baseline"
	if r.original_start_date != r.expected_start_date or r.original_end_date != r.expected_end_date:
		return "Rescheduled"
	return "Synced"


def revisions_same(a, b) -> bool:
	return scheduling._same_instant(a, b)


def _s(value) -> str | None:
	return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") and value else (str(value) if value else None)


# =============================================================================================
# Work Order table
# =============================================================================================
def get_work_orders(filters: dict | None, page: int = 1, page_length: int = 50) -> dict:
	filters = normalize_filters(filters)
	page = max(1, cint(page))
	page_length = min(max(1, cint(page_length) or 50), MAX_PAGE)

	w = Where()
	w.add(f"{WO}.docstatus < 2")
	if not filters.get("include_non_ppc"):
		w.add(f"ifnull({WO}.production_plan, '') != ''")
	if filters.get("plan"):
		w.add(f"{WO}.production_plan = {w.bind(filters['plan'])}")
	if filters.get("work_order"):
		w.add(f"{WO}.name = {w.bind(filters['work_order'])}")
	if filters.get("sales_order"):
		w.add(f"{WO}.sales_order = {w.bind(filters['sales_order'])}")
	if filters.get("sales_order_like"):
		w.add(f"{WO}.sales_order like {w.bind(like(filters['sales_order_like']))}")
	if filters.get("company"):
		w.add(f"{WO}.company = {w.bind(filters['company'])}")
	if filters.get("item_code"):
		w.add(f"{WO}.production_item = {w.bind(filters['item_code'])}")
	if filters.get("customer"):
		w.add(f"{SO}.customer = {w.bind(filters['customer'])}")
	if filters.get("planner"):
		w.add(f"coalesce({PLAN}.ppc_planner, {PLAN}.owner) = {w.bind(filters['planner'])}")
	if filters.get("priority"):
		w.add(f"{PLAN}.ppc_priority = {w.bind(filters['priority'])}")
	if filters.get("planned_start_from"):
		w.add(f"date({WO}.planned_start_date) >= {w.bind(filters['planned_start_from'])}")
	if filters.get("planned_start_to"):
		w.add(f"date({WO}.planned_start_date) <= {w.bind(filters['planned_start_to'])}")
	if filters.get("delivery_from"):
		w.add(f"{WO}.expected_delivery_date >= {w.bind(filters['delivery_from'])}")
	if filters.get("delivery_to"):
		w.add(f"{WO}.expected_delivery_date <= {w.bind(filters['delivery_to'])}")
	if filters.get("component_item"):
		w.add(
			f"exists (select 1 from `tabWork Order Item` woi where woi.parent = {WO}.name "
			f"and woi.item_code = {w.bind(filters['component_item'])})"
		)
	if filters.get("search"):
		b = w.bind(like(filters["search"]))
		w.add(f"{WO}.name like {b} or {WO}.production_item like {b} or {WO}.sales_order like {b} or {WO}.item_name like {b}")
	perm = _permission_clause("Work Order")
	if perm:
		w.add(perm)

	frm = f"""from {WO}
		left join {SO} on {SO}.name = {WO}.sales_order
		left join {PLAN} on {PLAN}.name = {WO}.production_plan"""
	where = w.sql()
	total = frappe.db.sql(f"select count(*) {frm} where {where}", w.params)[0][0]
	rows = frappe.db.sql(
		f"""select {WO}.name, {WO}.docstatus, {WO}.status, {WO}.production_item as item_code, {WO}.item_name,
			{WO}.qty, {WO}.produced_qty, {WO}.sales_order, {SO}.customer_name, {WO}.planned_start_date,
			{WO}.planned_end_date, {WO}.expected_delivery_date, {WO}.production_plan as ppc_plan,
			(select a.error_details from `tabPPC Audit Log` a where a.work_order = {WO}.name and a.result = 'Failed'
				and a.retry_status = 'Pending' order by a.creation desc limit 1) as error_message,
			(select ifnull(sum({JC}.ppc_schedule_revision), 0) from {JC} where {JC}.work_order = {WO}.name and {JC}.docstatus < 2) as schedule_revisions,
			(select count(*) from {JC} where {JC}.work_order = {WO}.name and {JC}.docstatus < 2) as job_cards
		{frm} where {where}
		order by {WO}.planned_start_date, {WO}.name limit %(limit)s offset %(offset)s""",
		{**w.params, "limit": page_length, "offset": (page - 1) * page_length},
		as_dict=True,
	)
	for r in rows:
		r.planned_start_date, r.planned_end_date = _s(r.planned_start_date), _s(r.planned_end_date)
		r.expected_delivery_date = _s(r.expected_delivery_date)
		r.status = "Draft" if r.docstatus == 0 else r.status
		r.can_submit = r.docstatus == 0
	return {"rows": rows, "total": total, "page": page, "page_length": page_length, "pages": max(1, -(-total // page_length))}


# =============================================================================================
# Gantt
# =============================================================================================
def get_gantt_data(filters: dict | None, limit: int | None = None) -> dict:
	filters = normalize_filters(filters)
	settings = get_settings()
	cap = min(max(50, cint(limit) or cint(settings.max_gantt_tasks) or 500), 5000)

	conflict_filter = None
	if filters.get("conflict_only"):
		conflict_filter = conflict_names_in_scope(filters)
	where, params = _scope_sql({k: v for k, v in filters.items() if k != "conflict_only"})
	if conflict_filter is not None:
		if not conflict_filter:
			return {"tasks": [], "links": [], "total": 0, "truncated": False, "range": None}
		where += f" and {JC}.name in %(conflict_names)s"
		params["conflict_names"] = tuple(conflict_filter)

	total = frappe.db.sql(f"select count(*) {JC_FROM} where {where}", params)[0][0]
	rows = frappe.db.sql(
		f"""select {JC_SELECT} {JC_FROM} where {where}
		order by ({REVISED_START} is null), {REVISED_START}, {JC}.work_order, {JC}.sequence_id, {JC}.name
		limit %(limit)s""",
		{**params, "limit": cap},
		as_dict=True,
	)
	conflicts = compute_conflicts(rows)
	estimates = _estimate_missing(rows)

	tasks, by_wo = [], defaultdict(list)
	for r in rows:
		task = present_job_card(r, conflicts.get(r.name))
		est = estimates.get(r.name)
		if est and not (r.expected_start_date and r.expected_end_date):
			task["revised_start"], task["revised_end"] = _s(est[0]), _s(est[1])
		if not task["revised_start"] or not task["revised_end"]:
			continue  # nothing to draw; still listed in the table
		task["progress"] = round(100 * flt(r.total_completed_qty) / flt(r.for_quantity), 1) if flt(r.for_quantity) else 0
		task["lock_reason"] = None if task["movable"] else _lock_reason(r)
		tasks.append(task)
		by_wo[r.work_order].append(task)

	links = []
	for jobs in by_wo.values():
		groups = scheduling.group_by_sequence(jobs)
		for a, b in zip(groups, groups[1:]):
			links.extend({"from": x["name"], "to": y["name"]} for x in a for y in b)

	rng = None
	if tasks:
		rng = {"start": min(t["revised_start"] for t in tasks), "end": max(t["revised_end"] for t in tasks)}
	return {"tasks": tasks, "links": links, "total": total, "truncated": total > cap, "range": rng}


def _lock_reason(r) -> str:
	if r.docstatus == 1:
		return _("Submitted")
	if r.status not in JC_RESCHEDULABLE_STATUSES:
		return _("{0}").format(r.status)
	if r.wo_docstatus != 1:
		return _("Work Order is not submitted")
	return _("Work Order is {0}").format(r.wo_status)


def _estimate_missing(rows) -> dict:
	"""Display-only dates for Job Cards that have no schedule at all (never written to the database)."""
	need = {r.work_order for r in rows if not (r.expected_start_date and r.expected_end_date) and not (r.op_planned_start and r.op_planned_end)}
	out = {}
	for wo_name in need:
		wo = frappe.db.get_value("Work Order", wo_name, ["name", "qty", "planned_start_date"], as_dict=True)
		if not wo:
			continue
		ops = {o.name: o for o in frappe.get_all("Work Order Operation", filters={"parent": wo_name}, fields=["name", "time_in_mins"])}
		jobs = scheduling.get_work_order_jobs(wo_name)
		anchor = scheduling.as_dt(wo.planned_start_date) or now_datetime()
		plan = scheduling.plan_sequential_schedule(
			scheduling.group_by_sequence(jobs), anchor, lambda j: scheduling.operation_minutes(j, wo, ops)[0]
		)
		for item in plan:
			out[item["name"]] = (item["start"], item["end"])
	return out


# =============================================================================================
# Dashboard
# =============================================================================================
def get_dashboard(filters: dict | None) -> dict:
	filters = normalize_filters(filters)
	where, params = _scope_sql({k: v for k, v in filters.items() if k not in ("conflict_only",)})
	now = now_datetime()

	row = frappe.db.sql(
		f"""select count(*) as total,
			sum({JC}.docstatus = 0) as draft,
			sum({JC}.status = 'Open' and {JC}.docstatus = 0) as open,
			sum({JC}.status = 'Work In Progress') as wip,
			sum({JC}.status = 'Completed') as completed,
			sum({JC}.docstatus = 0 and {JC}.status not in ('Completed', 'Submitted', 'Cancelled')
				and {REVISED_END} is not null and {REVISED_END} < %(now)s) as overdue,
			sum(ifnull({JC}.workstation, '') = '' and {JC}.docstatus = 0
				and {JC}.status not in ('Completed', 'Submitted', 'Cancelled')) as unassigned
		{JC_FROM} where {where}""",
		{**params, "now": now},
		as_dict=True,
	)[0]

	scope_rows = frappe.db.sql(
		f"""select {JC}.name, {JC}.workstation, {JC}.work_order, {JC}.sequence_id, {JC}.status, {JC}.docstatus,
			{JC}.expected_start_date, {JC}.expected_end_date {JC_FROM} where {where}""",
		params,
		as_dict=True,
	)
	conflicts = len(compute_conflicts(scope_rows))
	if filters.get("conflict_only"):
		pass  # the KPI counts conflicts within the same scope; the filter only narrows the tables

	kpis = {
		"total_job_cards": cint(row.total),
		"draft_job_cards": cint(row.draft),
		"open_job_cards": cint(row.open),
		"work_in_progress": cint(row.wip),
		"completed_job_cards": cint(row.completed),
		"overdue_operations": cint(row.overdue),
		"unassigned_operations": cint(row.unassigned),
		"scheduling_conflicts": conflicts,
	}
	kpis.update(_work_order_kpis(filters))
	kpis.update(_plan_kpis(filters))

	workload = frappe.db.sql(
		f"""select ifnull(nullif({JC}.workstation, ''), %(unassigned)s) as workstation,
			count(*) as job_cards,
			sum({JC}.status = 'Open') as open_cards,
			sum({JC}.status = 'Work In Progress') as wip_cards,
			sum({JC}.docstatus = 0 and {JC}.status not in ('Completed', 'Submitted', 'Cancelled')
				and {REVISED_END} is not null and {REVISED_END} < %(now)s) as overdue_cards,
			round(sum(ifnull(timestampdiff(minute, {REVISED_START}, {REVISED_END}), 0)) / 60, 1) as scheduled_hours,
			sum(greatest({JC}.for_quantity - ifnull({JC}.total_completed_qty, 0), 0)) as pending_qty
		{JC_FROM} where {where} and {JC}.docstatus = 0 and {JC}.status not in ('Completed', 'Submitted', 'Cancelled')
		group by ifnull(nullif({JC}.workstation, ''), %(unassigned)s)
		order by scheduled_hours desc limit 100""",
		{**params, "now": now, "unassigned": _("(Unassigned)")},
		as_dict=True,
	)
	names = [w.workstation for w in workload]
	capacity = {
		d.name: max(1, cint(d.production_capacity))
		for d in frappe.get_all(
			"Workstation", filters={"name": ["in", names or [""]]}, fields=["name", "production_capacity"]
		)
	}
	for w in workload:
		w["capacity"] = capacity.get(w.workstation)
		w["scheduled_hours"] = flt(w.scheduled_hours)
		w["pending_qty"] = flt(w.pending_qty, 3)

	schedule = frappe.db.sql(
		f"""select date({REVISED_START}) as day, count(*) as job_cards,
			round(sum(ifnull(timestampdiff(minute, {REVISED_START}, {REVISED_END}), 0)) / 60, 1) as hours
		{JC_FROM} where {where} and {REVISED_START} is not null
			and {JC}.docstatus = 0 and {JC}.status not in ('Completed', 'Submitted', 'Cancelled')
		group by date({REVISED_START}) order by day limit 120""",
		params,
		as_dict=True,
	)
	for s in schedule:
		s["day"] = str(s["day"])
		s["hours"] = flt(s["hours"])
	return {"kpis": kpis, "workload": workload, "schedule": schedule, "as_of": now.strftime("%Y-%m-%d %H:%M:%S")}


def _plan_where(filters: dict, w: Where, plan_alias=PLAN) -> None:
	w.add(f"{plan_alias}.docstatus = 1")
	if filters.get("plan"):
		w.add(f"{plan_alias}.name = {w.bind(filters['plan'])}")
	if filters.get("company"):
		w.add(f"{plan_alias}.company = {w.bind(filters['company'])}")
	if filters.get("planner"):
		w.add(f"coalesce({plan_alias}.ppc_planner, {plan_alias}.owner) = {w.bind(filters['planner'])}")
	if filters.get("priority"):
		w.add(f"{plan_alias}.ppc_priority = {w.bind(filters['priority'])}")
	if filters.get("sales_order"):
		w.add(f"exists (select 1 from `tabProduction Plan Sales Order` ps where ps.parent = {plan_alias}.name and ps.sales_order = {w.bind(filters['sales_order'])})")
	if filters.get("sales_order_like"):
		w.add(f"exists (select 1 from `tabProduction Plan Sales Order` ps where ps.parent = {plan_alias}.name and ps.sales_order like {w.bind(like(filters['sales_order_like']))})")
	if filters.get("customer"):
		w.add(f"exists (select 1 from `tabProduction Plan Sales Order` ps where ps.parent = {plan_alias}.name and ps.customer = {w.bind(filters['customer'])})")
	if filters.get("item_code"):
		w.add(f"exists (select 1 from `tabProduction Plan Item` pi where pi.parent = {plan_alias}.name and pi.item_code = {w.bind(filters['item_code'])})")


def _plan_kpis(filters: dict) -> dict:
	w = Where()
	_plan_where(filters, w)
	perm = _permission_clause("Production Plan")
	if perm:
		w.add(perm)
	plans = frappe.db.sql(f"select {PLAN}.name from {PLAN} where {w.sql()}", w.params, pluck="name")
	if not plans:
		return {"total_sales_orders": 0, "total_ppc_plans": 0, "pending_production_qty": 0.0}

	so_count = frappe.db.sql(
		"select count(distinct sales_order) from `tabProduction Plan Item` where parent in %(p)s and ifnull(sales_order, '') != ''",
		{"p": tuple(plans)},
	)[0][0]
	# The standard plan keeps planned and produced quantity per item row (ERPNext updates produced_qty
	# from the Work Orders), so what is still owed is simply planned - produced.
	pending = frappe.db.sql(
		"""select sum(greatest(planned_qty - ifnull(produced_qty, 0), 0))
		from `tabProduction Plan Item` where parent in %(p)s""",
		{"p": tuple(plans)},
	)[0][0]
	return {
		"total_sales_orders": cint(so_count),
		"total_ppc_plans": len(plans),
		"pending_production_qty": flt(pending, 3),
	}


def _work_order_kpis(filters: dict) -> dict:
	w = Where()
	w.add(f"{WO}.docstatus < 2")
	w.add(f"ifnull({WO}.production_plan, '') != ''" if not filters.get("include_non_ppc") else "1=1")
	if filters.get("plan"):
		w.add(f"{WO}.production_plan = {w.bind(filters['plan'])}")
	if filters.get("work_order"):
		w.add(f"{WO}.name = {w.bind(filters['work_order'])}")
	if filters.get("sales_order"):
		w.add(f"{WO}.sales_order = {w.bind(filters['sales_order'])}")
	if filters.get("sales_order_like"):
		w.add(f"{WO}.sales_order like {w.bind(like(filters['sales_order_like']))}")
	if filters.get("company"):
		w.add(f"{WO}.company = {w.bind(filters['company'])}")
	if filters.get("item_code"):
		w.add(f"{WO}.production_item = {w.bind(filters['item_code'])}")
	if filters.get("customer"):
		w.add(f"{SO}.customer = {w.bind(filters['customer'])}")
	if filters.get("planner"):
		w.add(f"coalesce({PLAN}.ppc_planner, {PLAN}.owner) = {w.bind(filters['planner'])}")
	if filters.get("priority"):
		w.add(f"{PLAN}.ppc_priority = {w.bind(filters['priority'])}")
	perm = _permission_clause("Work Order")
	if perm:
		w.add(perm)
	row = frappe.db.sql(
		f"""select count(*) as total, sum({WO}.docstatus = 0) as draft,
			sum({WO}.docstatus = 1 and {WO}.status in ('Not Started', 'In Process')) as open
		from {WO} left join {SO} on {SO}.name = {WO}.sales_order
		left join {PLAN} on {PLAN}.name = {WO}.production_plan where {w.sql()}""",
		w.params,
		as_dict=True,
	)[0]
	return {"work_orders_created": cint(row.total), "draft_work_orders": cint(row.draft), "open_work_orders": cint(row.open)}
