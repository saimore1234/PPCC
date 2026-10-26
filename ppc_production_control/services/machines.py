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
