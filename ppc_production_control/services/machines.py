"""Machine (Workstation) occupancy: who is booked on a Workstation, when, and is there room for one more?

What counts as a booking (verified against ERPNext 15.108 `Job Card.get_time_logs` / `get_overlap_for`)
---------------------------------------------------------------------------------------------------
* A Job Card's *planned* occupation is its `scheduled_time_logs` rows (`Job Card Scheduled Time`), not the
  header dates `expected_start_date` / `expected_end_date`: those are only min/max of the rows, and a card whose
  rows are split by working hours does not occupy the gap between them.
* Time that was actually booked (`time_logs`, `Job Card Time Log`) occupies the machine too. An open log (no
  `to_time`) occupies it until now.
* Cancelled Job Cards (docstatus 2) occupy nothing. Draft cards - Open, Material Transferred, On Hold and
  Work In Progress - are real bookings even though they are not submitted. A submitted card keeps only its
  actual time rows.
* A card with neither rows nor logs (capacity planning disabled) falls back to its header dates.
* Capacity: a Workstation with `production_capacity` N can run N operations at the same instant. A new operation
  conflicts when, at some instant of its interval, N other operations are already running. Intervals are
  half-open [from, to): back-to-back operations do not overlap.

The interval and slot maths are pure functions (tested without a database); the SQL is isolated in `occupancy_many`.
"""

from datetime import datetime, timedelta

import frappe
from frappe.utils import cint, get_datetime, getdate, now_datetime

MAX_SLOT_ITERATIONS = 2000
DEFAULT_HORIZON_DAYS = 120


# =====================================================================================
# Pure helpers
# =====================================================================================


def floor_second(value: datetime) -> datetime:
	"""Whole seconds, rounded down. ERPNext stores microseconds; planners, the Gantt and the history work in seconds.
	Bookings and windows are compared at second precision, so two operations that run back to back never show a
	sub-second overlap."""
	return value.replace(microsecond=0)


def ceil_second(value: datetime) -> datetime:
	"""Round up to a whole second (a slot that starts 0.97 s before the previous booking ends is a conflict)."""
	return value if value.microsecond == 0 else value.replace(microsecond=0) + timedelta(seconds=1)


def clip(intervals, start: datetime, end: datetime):
	"""The parts of `intervals` that lie inside [start, end); empty pieces are dropped."""
	out = []
	for a, b in intervals:
		if a is None or b is None:
			continue
		lo, hi = max(a, start), min(b, end)
		if hi > lo:
			out.append((lo, hi))
	return out


def merge_intervals(intervals) -> list[tuple]:
	"""Union of intervals: overlapping or touching ones become one."""
	out = []
	for a, b in sorted((a, b) for a, b in intervals if a is not None and b is not None and b > a):
		if out and a <= out[-1][1]:
			out[-1] = (out[-1][0], max(out[-1][1], b))
		else:
			out.append((a, b))
	return out


def occupied_intervals(rows: list[dict]) -> list[tuple]:
	"""The intervals that count against a machine's capacity, from raw occupancy rows.

	One Job Card is one operation: it cannot take two places on a machine at the same moment. ERPNext's own scheduler can leave a
	card with identical or overlapping `Job Card Scheduled Time` rows (seen when a card is pushed past another one), so each
	card's rows are merged before they are counted - otherwise a single operation would block a two-slot machine on its own."""
	by_card: dict[str, list] = {}
	for r in rows:
		by_card.setdefault(r["job_card"], []).append((r["from"], r["to"]))
	return [iv for ivs in by_card.values() for iv in merge_intervals(ivs)]


def peak_load(intervals) -> int:
	"""Largest number of intervals running at one instant (half-open: an end meets a start without overlap)."""
	events = []
	for a, b in intervals:
		if a is None or b is None or b <= a:
			continue
		events.append((a, 1))
		events.append((b, -1))
	events.sort(key=lambda e: (e[0], e[1]))
	current = peak = 0
	for _t, delta in events:
		current += delta
		peak = max(peak, current)
	return peak


def window_load(intervals, capacity: int, start: datetime, end: datetime) -> dict:
	"""How loaded is the machine inside [start, end) by `intervals` (the *other* bookings)?

	`blocking` is True when adding one more operation across the whole window would exceed `capacity`, i.e. when
	at some instant `capacity` other operations are already running."""
	inside = clip(intervals, start, end)
	peak = peak_load(inside)
	return {"peak_others": peak, "capacity": max(1, capacity), "blocking": peak >= max(1, capacity)}


def utilization_pct(intervals, capacity: int, start: datetime, end: datetime) -> float | None:
	"""Booked machine-minutes in the window as a share of its capacity (None for an empty window)."""
	span = (end - start).total_seconds()
	if span <= 0:
		return None
	booked = sum((b - a).total_seconds() for a, b in clip(intervals, start, end))
	return round(100.0 * booked / (span * max(1, capacity)), 1)


def find_slot(intervals, capacity: int, earliest: datetime, minutes: float, horizon_end: datetime, skip=None) -> tuple | None:
	"""First (start, end) at or after `earliest`, `minutes` long, where the machine has room for one more operation.

	`skip(dt) -> dt` may move a candidate start forward (holidays, non-working time). Returns None when nothing fits
	before `horizon_end`. The search only ever jumps to the next moment a booking ends, so it terminates."""
	length = timedelta(minutes=max(1.0, float(minutes)))
	capacity = max(1, capacity)
	cursor = ceil_second(skip(earliest) if skip else earliest)
	for _i in range(MAX_SLOT_ITERATIONS):
		if cursor >= horizon_end:
			return None
		end = cursor + length
		inside = clip(intervals, cursor, end)
		if peak_load(inside) < capacity:
			return cursor, end
		# the earliest moment inside the window at which a booking ends: nothing before it can fit
		next_start = min(b for _a, b in inside)
		cursor = ceil_second(skip(next_start) if skip else next_start)
	return None


def group_bookings(rows: list[dict]) -> list[dict]:
	"""One entry per Job Card (with all of its intervals) from raw occupancy rows."""
	by_card: dict[str, dict] = {}
	for r in rows:
		entry = by_card.get(r["job_card"])
		if entry is None:
			entry = by_card[r["job_card"]] = {
				"job_card": r["job_card"], "work_order": r.get("work_order"), "operation": r.get("operation"),
				"status": r.get("status"), "docstatus": r.get("docstatus"), "quantity": r.get("quantity"),
				"workstation": r.get("workstation"), "source": r["source"], "intervals": [],
			}
		entry["intervals"].append((r["from"], r["to"]))
	for entry in by_card.values():
		entry["from"] = min(a for a, _b in entry["intervals"])
		entry["to"] = max(b for _a, b in entry["intervals"])
	return sorted(by_card.values(), key=lambda e: (e["from"], e["job_card"]))


def as_time(value):
	"""A Frappe Time value (a timedelta from the database, a string, or a time) as a datetime.time."""
	if isinstance(value, timedelta):
		return (datetime.min + value).time()
	if isinstance(value, str):
		return get_datetime("2000-01-01 " + value).time()
	return value


def layout_minutes(earliest: datetime, minutes: float, working_hours, holidays, *, allow_overtime: bool = False, max_days: int = 400) -> list[tuple] | None:
	"""The pieces of working time that `minutes` of operation time occupy when started at `earliest`.

	Follows ERPNext's capacity planning: a machine with no working hours (or a site that allows overtime) works around the
	clock in one piece; otherwise the time is laid into the machine's daily working-hour slots, skipping its holidays and
	carrying over to the next day. Returns [(from, to), ...], or None when it does not fit within `max_days`."""
	minutes = float(minutes or 0)
	if minutes <= 0:
		return []
	if not working_hours or allow_overtime:
		return [(earliest, earliest + timedelta(minutes=minutes))]
	slots = sorted((as_time(a), as_time(b)) for a, b in working_hours if a is not None and b is not None and as_time(b) > as_time(a))
	if not slots:
		return [(earliest, earliest + timedelta(minutes=minutes))]
	rows, remaining, cursor, day = [], minutes, earliest, earliest.date()
	for _i in range(max_days):
		if day not in holidays:
			for a, b in slots:
				slot_start, slot_end = datetime.combine(day, a), datetime.combine(day, b)
				start = max(cursor, slot_start)
				if start >= slot_end:
					continue
				take = min(remaining, (slot_end - start).total_seconds() / 60.0)
				rows.append((start, start + timedelta(minutes=take)))
				cursor, remaining = start + timedelta(minutes=take), remaining - take
				if remaining <= 1e-9:
					return rows
		day = day + timedelta(days=1)
		cursor = max(cursor, datetime.combine(day, datetime.min.time()))
	return None


def plan_operation(intervals, capacity: int, earliest: datetime, minutes: float, working_hours, holidays, horizon_end: datetime, *, allow_overtime: bool = False) -> dict | None:
	"""The first start at or after `earliest` where `minutes` of operation time fit on a machine that already has `intervals` booked.

	Every piece of working time must find room: at no instant may `capacity` other operations be running. Returns
	{"start", "end", "rows"} or None when nothing fits before `horizon_end`. `minutes` of 0 means "start as soon as the machine is free"."""
	capacity = max(1, capacity)
	cursor = ceil_second(earliest)
	for _i in range(MAX_SLOT_ITERATIONS):
		if cursor >= horizon_end:
			return None
		rows = layout_minutes(cursor, minutes, working_hours, holidays, allow_overtime=allow_overtime) if minutes > 0 else [(cursor, cursor + timedelta(seconds=1))]
		if rows is None:
			return None
		blocked_until = None
		for a, b in rows:
			inside = clip(intervals, a, b)
			if peak_load(inside) >= capacity:
				# the next start that can be free is when the first of the bookings overlapping this piece ends (not the piece's own end:
				# a booking longer than the piece would otherwise be crept past one piece-length at a time)
				blocked_until = min(y for x, y in intervals if x < b and y > a)
				break
		if blocked_until is None:
			return {"start": rows[0][0], "end": rows[-1][1], "rows": rows}
		cursor = ceil_second(max(blocked_until, cursor + timedelta(seconds=1)))
	return None


# =====================================================================================
# Database
# =====================================================================================


def workstation_info(workstation: str) -> dict | None:
	"""Capacity, calendar and state of a Workstation; None when it does not exist.

	ERPNext's Workstation has no "disabled" flag. It has a `status` (Production / Off / Idle / Problem /
	Maintenance / Setup), which is reported, not interpreted as eligibility."""
	row = frappe.db.get_value(
		"Workstation", workstation,
		["name", "workstation_type", "production_capacity", "holiday_list", "status", "description"], as_dict=True,
	)
	if not row:
		return None
	hours = frappe.get_all(
		"Workstation Working Hour", filters={"parent": workstation, "parenttype": "Workstation"},
		fields=["start_time", "end_time"], order_by="start_time asc", limit_page_length=0,
	)
	return {
		"name": row.name, "workstation_type": row.workstation_type, "capacity": max(1, cint(row.production_capacity) or 1),
		"holiday_list": row.holiday_list, "status": row.status, "description": row.description,
		"working_hours": [(h.start_time, h.end_time) for h in hours],
	}


def occupancy_many(workstations: list[str], start: datetime, end: datetime, exclude=()) -> list[dict]:
	"""Raw bookings of the given Workstations that touch [start, end): one row per interval."""
	if not workstations:
		return []
	args = {"ws": tuple(workstations), "start": start, "end": end, "ex": tuple(exclude) or ("",), "now": now_datetime()}
	rows = []

	# planned time of cards that are still drafts
	for r in frappe.db.sql(
		"""select jc.name job_card, jc.workstation, jc.work_order, jc.operation, jc.status, jc.docstatus, jc.for_quantity quantity,
			s.from_time `from`, s.to_time `to`
		from `tabJob Card Scheduled Time` s join `tabJob Card` jc on jc.name = s.parent
		where jc.workstation in %(ws)s and jc.docstatus = 0 and jc.name not in %(ex)s
			and s.from_time < %(end)s and s.to_time > %(start)s""",
		args, as_dict=True,
	):
		rows.append({**r, "source": "scheduled"})

	# time that was actually booked (an unfinished log runs until now)
	for r in frappe.db.sql(
		"""select jc.name job_card, jc.workstation, jc.work_order, jc.operation, jc.status, jc.docstatus, jc.for_quantity quantity,
			t.from_time `from`, t.to_time `to`
		from `tabJob Card Time Log` t join `tabJob Card` jc on jc.name = t.parent
		where jc.workstation in %(ws)s and jc.docstatus < 2 and jc.name not in %(ex)s and t.from_time is not null
			and t.from_time < %(end)s and (t.to_time is null or t.to_time > %(start)s)""",
		args, as_dict=True,
	):
		to = r["to"] or max(args["now"], r["from"] + timedelta(minutes=1))
		rows.append({**r, "to": to, "source": "actual"})

	# cards with no planned rows at all (capacity planning off): their header dates are all there is
	for r in frappe.db.sql(
		"""select jc.name job_card, jc.workstation, jc.work_order, jc.operation, jc.status, jc.docstatus, jc.for_quantity quantity,
			jc.expected_start_date `from`, jc.expected_end_date `to`
		from `tabJob Card` jc
		where jc.workstation in %(ws)s and jc.docstatus = 0 and jc.name not in %(ex)s
			and jc.expected_start_date < %(end)s and jc.expected_end_date > %(start)s
			and not exists (select 1 from `tabJob Card Scheduled Time` s where s.parent = jc.name)""",
		args, as_dict=True,
	):
		rows.append({**r, "source": "expected"})

	out = []
	for r in rows:
		a, b = floor_second(get_datetime(r["from"])), floor_second(get_datetime(r["to"]))
		if b > a:
			out.append({**r, "from": a, "to": b})
	return out


def intervals_by_workstation(workstations: list[str], start: datetime, end: datetime, exclude=(), *, include_draft_work_orders: bool = False) -> dict[str, list[tuple]]:
	"""{workstation: [(from, to), ...]} - what is booked on each machine in [start, end), one Job Card counted once per piece of time.

	Submitted Work Orders are covered by their Job Cards. Draft Work Orders hold only placeholder times and are left out unless asked for."""
	rows = occupancy_many(workstations, start, end, exclude)
	if include_draft_work_orders and workstations:
		for r in frappe.db.sql(
			"""select wo.name job_card, o.workstation, o.planned_start_time `from`, o.planned_end_time `to`
			from `tabWork Order Operation` o join `tabWork Order` wo on wo.name = o.parent
			where wo.docstatus = 0 and o.workstation in %(ws)s and o.planned_start_time is not null and o.planned_end_time is not null
				and o.planned_start_time < %(end)s and o.planned_end_time > %(start)s""",
			{"ws": tuple(workstations), "start": start, "end": end}, as_dict=True,
		):
			a, b = floor_second(get_datetime(r["from"])), floor_second(get_datetime(r["to"]))
			if b > a:
				rows.append({"job_card": "WO:" + r["job_card"], "workstation": r["workstation"], "from": a, "to": b})
	by_ws: dict[str, list[dict]] = {w: [] for w in workstations}
	for r in rows:
		by_ws.setdefault(r["workstation"], []).append(r)
	return {w: occupied_intervals(rs) for w, rs in by_ws.items()}


def occupancy(workstation: str, start: datetime, end: datetime, exclude=()) -> list[dict]:
	return occupancy_many([workstation], start, end, exclude)


def check_machine(workstation: str, start: datetime, end: datetime, exclude=(), info: dict | None = None) -> dict:
	"""Is there room on `workstation` for one more operation over [start, end)?

	Returns {"blocking", "capacity", "peak_others", "bookings": [per Job Card, only those overlapping the window],
	"utilization"}. `bookings` lists every overlapping card (not only the one that tips the capacity over)."""
	info = info or workstation_info(workstation)
	capacity = info["capacity"] if info else 1
	start, end = floor_second(start), floor_second(end)
	raw = occupancy(workstation, start, end, exclude)
	intervals = occupied_intervals(raw)
	load = window_load(intervals, capacity, start, end)
	return {
		"blocking": load["blocking"], "capacity": capacity, "peak_others": load["peak_others"],
		"utilization": utilization_pct(intervals, capacity, start, end),
		"bookings": group_bookings(raw),
	}


def _holiday_dates(info: dict, first, last) -> set:
	if not info or not info.get("holiday_list"):
		return set()
	if cint(frappe.db.get_single_value("Manufacturing Settings", "allow_production_on_holidays")):
		return set()
	return {
		getdate(d) for d in frappe.get_all(
			"Holiday", filters={"parent": info["holiday_list"], "holiday_date": ["between", [first, last]]}, pluck="holiday_date"
		)
	}


def next_free_slot(workstation: str, earliest: datetime, minutes: float, exclude=(), horizon_days: int = DEFAULT_HORIZON_DAYS, info=None) -> dict | None:
	"""The next contiguous slot of `minutes` on `workstation` at or after `earliest` where it has room.

	The slot is contiguous elapsed time - the same thing a drag on the Gantt produces. A start that falls on one of the
	Workstation's holidays is moved to the next day; working-hour shifts are reported by the caller as a warning,
	not used to cut the slot into pieces."""
	info = info or workstation_info(workstation)
	if not info:
		return None
	horizon_end = earliest + timedelta(days=horizon_days)
	raw = occupancy(workstation, earliest, horizon_end + timedelta(minutes=minutes), exclude)
	holidays = _holiday_dates(info, getdate(earliest), getdate(horizon_end))

	def skip(dt: datetime) -> datetime:
		moved = dt
		while getdate(moved) in holidays:
			moved = datetime.combine(getdate(moved) + timedelta(days=1), moved.time())
		return moved

	slot = find_slot(occupied_intervals(raw), info["capacity"], earliest, minutes, horizon_end, skip)
	if not slot:
		return None
	return {"start": slot[0], "end": slot[1]}
