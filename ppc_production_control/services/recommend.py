"""Best-machine recommendation for a Sales Order line, and its hand-over to the Production Plan and the Work Order.

What it uses (all of it ERPNext's own configuration - nothing is inferred from names)
------------------------------------------------------------------------------------
* the item's BOM and its operations: Operation, sequence, `time_in_mins`, `fixed_time`, `batch_size`, BOM quantity - with ERPNext's own
  scaling: minutes = time / BOM quantity x order quantity / batch size (a fixed-time operation is not scaled);
* which Workstations may perform an operation: the union of (1) the BOM operation's *Workstation Type* (every Workstation of that
  type), (2) the Workstations listed on the Operation in *PPC Compatible Workstations* and (3) the BOM operation's own Workstation;
  a row switched off on the Operation list removes that machine. With none of these, there is NO compatible machine and nothing is
  recommended - a machine is never assumed to fit because it is free or because of its name;
* each machine's capacity, working hours and holiday list, and what is already booked on it: the Job Cards' real time rows
  (services/machines.py), optionally the draft Work Orders' placeholder times (off by default);
* the Sales Order's delivery date.

What it never does: invent a duration, a capacity, an efficiency or a cost (missing data becomes a warning and drops that factor);
reserve a machine; submit anything; change any schedule. A recommendation is a planning aid. The PPC scheduling engine and ERPNext
validate the real schedule when Work Orders and Job Cards are created, and the machine is checked again then.
"""

import hashlib
import json
from datetime import datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime

from ppc_production_control.services import audit, machines
from ppc_production_control.services.constants import A_APPLY_MACHINE_PLAN, CAP_VIEW, R_SUCCESS, R_WARNING
from ppc_production_control.services.permissions import has_capability
from ppc_production_control.services.utils import dt_str, get_settings

# a machine in one of these states is not offered (safety), whatever its free capacity
UNUSABLE_STATES = ("Off", "Problem", "Maintenance")
DEFAULTS = {"completion": 45, "load": 25, "delivery": 20, "cost": 5, "efficiency": 5, "horizon": 120, "load_window": 7, "tolerance": 24, "single": 0, "drafts": 0}
STATUS_RECOMMENDED, STATUS_SELECTED, STATUS_STALE, STATUS_INCOMPLETE, STATUS_UNAVAILABLE, STATUS_NONE = (
	"Recommended", "Selected", "Stale", "Incomplete Data", "Unavailable", "Not Calculated")
TIGHT_DAYS = 1.0  # less than this much slack before the delivery date counts as "tight"


def disclaimer() -> str:
	return _("This is a planning recommendation, not a reservation. The PPC scheduling engine and ERPNext validate the real schedule when Work Orders and Job Cards are created, and the machine's availability is checked again then.")


# =====================================================================================
# Pure helpers (tested without a database)
# =====================================================================================


def operation_minutes(time_in_mins, fixed_time, batch_size, bom_quantity, qty) -> float | None:
	"""Minutes an operation needs for `qty`, the way ERPNext's Work Order computes them. None when the BOM holds no time for it."""
	t = flt(time_in_mins)
	if t <= 0:
		return None
	if cint(fixed_time):
		return t
	return t / (flt(bom_quantity) or 1.0) * flt(qty) / (flt(batch_size) or 1.0)


def group_operations(ops: list[dict]) -> list[list[dict]]:
	"""Operations that start together share a non-zero `sequence_id` with the one before; every other operation starts after the previous group."""
	groups: list[list[dict]] = []
	for op in ops:
		seq = cint(op.get("sequence_id"))
		if groups and seq and cint(groups[-1][0].get("sequence_id")) == seq:
			groups[-1].append(op)
		else:
			groups.append([op])
	return groups


def basis_hash(item_code, qty, uom, bom_no, delivery_date) -> str:
	"""What a recommendation was calculated for; when it changes, the stored recommendation is stale."""
	raw = "|".join(str(x or "") for x in (item_code, flt(qty), uom, bom_no, str(delivery_date or "")[:10]))
	return hashlib.sha1(raw.encode()).hexdigest()[:16]


def weighted_score(factors: list[tuple]) -> float | None:
	"""[(weight, 0..1 score)] -> 0..100. Only factors with a positive weight and a value count; None when nothing can be judged."""
	live = [(w, s) for w, s in factors if w > 0 and s is not None]
	total = sum(w for w, _s in live)
	if total <= 0:
		return None
	return round(100.0 * sum(w * s for w, s in live) / total, 1)


def spread_score(value, lo, hi, higher_is_better: bool = False) -> float:
	"""1 for the best value among the candidates, 0 for the worst; all-equal is 1."""
	if hi is None or lo is None or hi - lo <= 1e-12:
		return 1.0
	s = (value - lo) / (hi - lo)
	return s if higher_is_better else 1.0 - s


def delivery_risk(finish: datetime | None, delivery_date) -> dict:
	"""Compare an estimated finish with the promised date. The delivery date counts to the end of that day."""
	if not delivery_date:
		return {"state": "unknown", "slack_days": None}
	if finish is None:
		return {"state": "unknown", "slack_days": None}
	deadline = datetime.combine(getdate(delivery_date), datetime.max.time().replace(microsecond=0))
	slack = (deadline - finish).total_seconds() / 86400.0
	state = "misses" if slack < 0 else "tight" if slack < TIGHT_DAYS else "meets"
	return {"state": state, "slack_days": round(slack, 2)}


# =====================================================================================
# BOM, operations, compatibility
# =====================================================================================


def _config() -> dict:
	s = get_settings()

	def pick(field, default, minimum=0):
		v = s.get(field)
		return default if v is None or v < minimum else v

	weights = {k: pick("rec_weight_" + k, DEFAULTS[k]) for k in ("completion", "load", "delivery", "cost", "efficiency")}
	if sum(weights.values()) <= 0:
		weights = {k: DEFAULTS[k] for k in weights}
	return {
		"weights": weights, "horizon": pick("rec_horizon_days", DEFAULTS["horizon"], 7), "load_window": pick("rec_load_window_days", DEFAULTS["load_window"], 1),
		"tolerance": pick("rec_unavailable_tolerance_hours", DEFAULTS["tolerance"]), "single": cint(s.get("rec_single_machine")), "drafts": cint(s.get("rec_include_draft_wo")),
	}


def resolve_bom(item_code: str, bom_no: str | None = None) -> tuple[dict | None, str | None, list[str]]:
	"""(BOM, problem, notes). The named BOM, else the item's default active BOM, else (with a note) its newest active one."""
	fields = ["name", "item", "quantity", "is_active", "docstatus", "with_operations", "uom"]
	notes: list[str] = []
	if bom_no:
		row = frappe.db.get_value("BOM", bom_no, fields, as_dict=True)
		if not row:
			return None, _("BOM {0} does not exist.").format(bom_no), notes
		if row.item != item_code:
			return None, _("BOM {0} is for {1}, not {2}.").format(bom_no, row.item, item_code), notes
		if row.docstatus != 1 or not cint(row.is_active):
			return None, _("BOM {0} is not an active, submitted BOM.").format(bom_no), notes
		return row, None, notes
	name = frappe.db.get_value("BOM", {"item": item_code, "is_active": 1, "is_default": 1, "docstatus": 1}, "name")
	if not name:
		name = frappe.db.get_value("BOM", {"item": item_code, "is_active": 1, "docstatus": 1}, "name", order_by="creation desc")
		if name:
			notes.append(_("{0} has no default BOM; the newest active BOM {1} was used.").format(item_code, name))
	if not name:
		return None, _("{0} has no active BOM, so its operations are unknown.").format(item_code), notes
	return frappe.db.get_value("BOM", name, fields, as_dict=True), None, notes


def bom_operations(bom_name: str) -> list[dict]:
	return frappe.get_all(
		"BOM Operation", filters={"parent": bom_name, "parenttype": "BOM"}, order_by="idx asc",
		fields=["idx", "operation", "workstation", "workstation_type", "time_in_mins", "fixed_time", "batch_size", "sequence_id", "base_hour_rate as hour_rate"],
	)


def compatible_workstations(operation: str, workstation_type: str | None, bom_workstation: str | None) -> dict:
	"""{"machines": {ws: [why]}, "efficiency": {ws: pct}, "excluded": {ws: why}} from ERPNext's configuration only (see the module docstring)."""
	found: dict[str, list[str]] = {}
	efficiency: dict[str, float] = {}
	switched_off: set[str] = set()
	if frappe.db.table_exists("PPC Operation Workstation"):
		for r in frappe.get_all("PPC Operation Workstation", filters={"parent": operation, "parenttype": "Operation"}, fields=["workstation", "enabled", "efficiency_pct"]):
			if cint(r.enabled):
				found.setdefault(r.workstation, []).append(_("listed for the Operation"))
				if flt(r.efficiency_pct) > 0:
					efficiency[r.workstation] = flt(r.efficiency_pct)
			else:
				switched_off.add(r.workstation)
	if workstation_type:
		for ws in frappe.get_all("Workstation", filters={"workstation_type": workstation_type}, pluck="name"):
			found.setdefault(ws, []).append(_("Workstation Type {0}").format(workstation_type))
	if bom_workstation:
		found.setdefault(bom_workstation, []).append(_("the BOM's own machine"))
	excluded = {ws: _("switched off for this operation") for ws in switched_off if ws in found}
	for ws in excluded:
		found.pop(ws, None)
	return {"machines": found, "efficiency": efficiency, "excluded": excluded}


def _info(ws: str) -> dict | None:
	info = machines.workstation_info(ws)
	if info is not None:
		info["capacity_configured"] = bool(cint(frappe.db.get_value("Workstation", ws, "production_capacity")))
		info["hour_rate"] = flt(frappe.db.get_value("Workstation", ws, "hour_rate"))
	return info


def _stock_qty(item_code: str, qty: float, uom: str | None, stock_uom: str | None) -> tuple[float, str | None]:
	"""(quantity in the item's stock UOM, problem). Production quantities are always in the stock UOM."""
	if not uom or uom == stock_uom:
		return flt(qty), None
	factor = frappe.db.get_value("UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor")
	if not flt(factor):
		return flt(qty), _("{0} has no conversion from {1} to {2}; the quantity was used as it is.").format(item_code, uom, stock_uom)
	return flt(qty) * flt(factor), None


# =====================================================================================
# The recommendation
# =====================================================================================


def recommend(
	item_code: str,
	qty,
	*,
	uom: str | None = None,
	bom_no: str | None = None,
	delivery_date=None,
	forced: dict | None = None,
	exclude=(),
	now: datetime | None = None,
) -> dict:
	"""Rank the compatible Workstations for every operation of the item's BOM. See the module docstring for the rules.

	`forced` ({operation: workstation}) pins the machine of some operations (the planner's own choice): the operations after it are
	then timed from its estimated finish. `exclude` lists Job Cards not to count as bookings."""
	cfg = _config()
	now = now or now_datetime()
	earliest = machines.ceil_second(now.replace(second=0, microsecond=0) + timedelta(minutes=1))
	out = {
		"ok": True, "item_code": item_code, "qty": flt(qty), "uom": uom, "status": STATUS_NONE, "operations": [], "warnings": [], "data_gaps": [],
		"overall": {"start": None, "completion": None, "delivery_date": str(getdate(delivery_date)) if delivery_date else None, "risk": "unknown", "slack_days": None},
		"weights": cfg["weights"], "calculated_at": dt_str(now), "disclaimer": disclaimer(), "bom": None, "recommended_workstation": None, "single_machine": bool(cfg["single"]),
	}
	item = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom", "disabled"], as_dict=True)
	if not item:
		out.update(ok=False, status=STATUS_INCOMPLETE)
		out["warnings"].append(_("Item {0} does not exist.").format(item_code))
		return out
	out.update(item_name=item.item_name, stock_uom=item.stock_uom)
	if flt(qty) <= 0:
		out.update(ok=False, status=STATUS_INCOMPLETE)
		out["warnings"].append(_("Enter the order quantity first."))
		return out
	stock_qty, problem = _stock_qty(item_code, flt(qty), uom, item.stock_uom)
	if problem:
		out["warnings"].append(problem)
	out["stock_qty"] = stock_qty

	bom, problem, notes = resolve_bom(item_code, bom_no)
	out["warnings"].extend(notes)
	if not bom:
		out["status"] = STATUS_INCOMPLETE
		out["warnings"].append(problem)
		out["data_gaps"].append("bom")
		return out
	out["bom"] = {"name": bom.name, "quantity": flt(bom.quantity), "with_operations": bool(cint(bom.with_operations)), "uom": bom.uom}
	ops = bom_operations(bom.name) if cint(bom.with_operations) else []
	if not ops:
		out["status"] = STATUS_INCOMPLETE
		out["warnings"].append(_("BOM {0} has no operations, so there is no machine to recommend.").format(bom.name))
		out["data_gaps"].append("operations")
		return out
	if frappe.db.sql(
		"""select 1 from `tabBOM Item` bi join `tabBOM` b on b.name = bi.bom_no join `tabBOM Operation` o on o.parent = b.name
		where bi.parent = %s and bi.bom_no is not null limit 1""", bom.name,
	):
		out["warnings"].append(_("Some components are sub-assemblies with their own operations; only the operations of {0} itself are recommended here.").format(bom.name))

	mfg = frappe.get_cached_doc("Manufacturing Settings")
	if cint(mfg.get("disable_capacity_planning")):
		out["warnings"].append(_("Capacity planning is disabled in Manufacturing Settings: ERPNext will not time-schedule the Job Cards, so these dates are estimates only."))
	flags = {
		"allow_overtime": bool(cint(mfg.get("allow_overtime"))), "holidays_off": bool(cint(mfg.get("allow_production_on_holidays"))),
		"gap": max(0, cint(mfg.get("mins_between_operations")) if mfg.get("mins_between_operations") is not None else 10),
	}

	horizon_end = earliest + timedelta(days=cfg["horizon"])
	op_ctx = []
	all_ws: set[str] = set()
	for op in ops:
		compat = compatible_workstations(op.operation, op.workstation_type, op.workstation)
		op_ctx.append({
			"op": op, "minutes": operation_minutes(op.time_in_mins, op.fixed_time, op.batch_size, bom.quantity, stock_qty), "compat": compat,
		})
		all_ws |= set(compat["machines"]) | set(compat["excluded"])
	infos = {ws: _info(ws) for ws in all_ws}
	book = machines.intervals_by_workstation(sorted(w for w, i in infos.items() if i), earliest, horizon_end + timedelta(days=30), exclude, include_draft_work_orders=bool(cfg["drafts"]))
	if not cfg["drafts"]:
		out["data_gaps"].append("draft_work_orders_not_counted")

	holiday_cache: dict[str, set] = {}

	def holidays_of(ws: str) -> set:
		if flags["holidays_off"] or flags["allow_overtime"] or not (infos[ws] or {}).get("working_hours"):
			return set()  # ERPNext only skips holidays when it lays work into working hours
		if ws not in holiday_cache:
			holiday_cache[ws] = machines._holiday_dates(infos[ws], earliest.date(), horizon_end.date() + timedelta(days=30))
		return holiday_cache[ws]

	def run(pins: dict) -> dict:
		"""One pass over the BOM in sequence, each operation after the previous group; `pins` fixes machines."""
		virtual: dict[str, list] = {ws: [] for ws in infos}
		results, warnings = [], []
		chain_known, group_start = True, earliest
		groups = group_operations([c["op"] for c in op_ctx])
		by_idx = {c["op"].idx: c for c in op_ctx}
		# minutes still to come after each group (for the delivery projection)
		later_minutes = []
		for gi in range(len(groups)):
			rest = groups[gi + 1:]
			mins = [max([by_idx[o.idx]["minutes"] or 0 for o in g] or [0]) + flags["gap"] for g in rest]
			known = all(by_idx[o.idx]["minutes"] for g in rest for o in g)
			later_minutes.append(sum(mins) if known else None)
		overall_start, overall_end = None, None
		for gi, group in enumerate(groups):
			group_end = None
			for op in group:
				ctx = by_idx[op.idx]
				res = _rank_operation(ctx, infos, book, virtual, holidays_of, group_start if chain_known else earliest, chain_known, horizon_end, cfg, flags, delivery_date, later_minutes[gi], pins.get(op.operation))
				results.append(res)
				warnings.extend(res["warnings"])
				picked = res["selected_candidate"]
				if picked and picked.get("rows"):
					virtual[picked["workstation"]].extend(picked["rows"])
				if picked and picked.get("end") and chain_known and ctx["minutes"]:
					group_end = max(group_end, picked["end"]) if group_end else picked["end"]
					overall_start = min(overall_start, picked["start"]) if overall_start else picked["start"]
					overall_end = max(overall_end, picked["end"]) if overall_end else picked["end"]
				else:
					chain_known = False
			if chain_known and group_end:
				group_start = group_end + timedelta(minutes=flags["gap"])
			else:
				chain_known = False
		return {"results": results, "warnings": warnings, "start": overall_start if chain_known else (overall_start or None), "end": overall_end if chain_known else None, "chain_known": chain_known}

	pins = {k: v for k, v in (forced or {}).items() if v}
	single = None
	if cfg["single"] and len(op_ctx) > 1:
		common = None
		for c in op_ctx:
			usable = {ws for ws in c["compat"]["machines"] if infos.get(ws)}
			common = usable if common is None else common & usable
		if not common:
			out["warnings"].append(_("No single machine is compatible with all operations of {0}; the operations are recommended separately.").format(bom.name))
		else:
			routes = []
			for ws in sorted(common):
				r = run({c["op"].operation: ws for c in op_ctx})
				picked = [x["selected_candidate"] for x in r["results"]]
				if all(picked):
					routes.append((ws, r))
			if routes:
				best = min(routes, key=lambda t: (t[1]["end"] or datetime.max, t[0]))
				pins = {c["op"].operation: best[0] for c in op_ctx} if not pins else pins
				out["single_machine_options"] = [{"workstation": w, "completion": dt_str(r["end"]) if r["end"] else None} for w, r in sorted(routes, key=lambda t: (t[1]["end"] or datetime.max, t[0]))]

	final = run(pins)
	out["operations"] = [_public(r) for r in final["results"]]
	out["warnings"].extend(w for w in final["warnings"] if w not in out["warnings"])
	picks = [r["selected"] for r in out["operations"]]
	out["recommended_workstation"] = picks[0] if picks and picks[0] else None
	out["overall"]["start"] = dt_str(final["start"]) if final["start"] else None
	out["overall"]["completion"] = dt_str(final["end"]) if final["end"] else None
	risk = delivery_risk(final["end"], delivery_date)
	out["overall"].update(risk=risk["state"], slack_days=risk["slack_days"])
	if risk["state"] == "misses":
		out["warnings"].append(_("The estimated completion ({0}) is after the delivery date ({1}).").format(dt_str(final["end"]), getdate(delivery_date)))
	elif risk["state"] == "tight":
		out["warnings"].append(_("The estimated completion leaves less than a day before the delivery date ({0}).").format(getdate(delivery_date)))
	if not delivery_date:
		out["data_gaps"].append("delivery_date")
	if not final["chain_known"]:
		out["data_gaps"].append("durations")
	if any(r["selected"] is None for r in out["operations"]):
		out["status"] = STATUS_UNAVAILABLE
	elif out["data_gaps"] and any(g in ("durations", "bom", "operations") for g in out["data_gaps"]):
		out["status"] = STATUS_INCOMPLETE
	else:
		out["status"] = STATUS_RECOMMENDED
	out["basis"] = basis_hash(item_code, qty, uom, bom.name, delivery_date)
	out["manual"] = any(r["selected"] and r["recommended"] and r["selected"] != r["recommended"] for r in out["operations"])
	return out


def _public(r: dict) -> dict:
	"""The part of an operation's result that leaves the server (no internal objects)."""
	keep = {k: v for k, v in r.items() if k not in ("selected_candidate",)}
	keep["candidates"] = [
		{**{k: v for k, v in c.items() if k != "rows"}, "start": dt_str(c["start"]) if c["start"] else None, "end": dt_str(c["end"]) if c["end"] else None}
		for c in r["candidates"]
	]
	return keep


def _rank_operation(ctx, infos, book, virtual, holidays_of, earliest, timing_known, horizon_end, cfg, flags, delivery_date, later_minutes, pinned) -> dict:
	"""Evaluate, score and rank the compatible machines of one operation."""
	op, minutes, compat = ctx["op"], ctx["minutes"], ctx["compat"]
	res = {
		"operation": op.operation, "idx": op.idx, "sequence_id": cint(op.sequence_id) or None, "minutes": round(minutes, 2) if minutes else None, "duration_known": bool(minutes),
		"bom_workstation": op.workstation, "workstation_type": op.workstation_type, "candidates": [], "excluded": [], "warnings": [], "recommended": None, "selected": None,
		"selected_candidate": None,
	}
	for ws, why in compat["excluded"].items():
		res["excluded"].append({"workstation": ws, "reason": why})
	if not compat["machines"]:
		res["warnings"].append(_("No compatible Workstation is configured for operation {0}: set a Workstation Type on the BOM operation, or list the machines on the Operation. A machine is never assumed to fit.").format(op.operation))
		return res
	if not minutes:
		res["warnings"].append(_("Operation {0} has no time in the BOM, so no duration, start or completion is estimated for it.").format(op.operation))

	cands = []
	for ws, why in compat["machines"].items():
		info = infos.get(ws)
		if not info:
			res["excluded"].append({"workstation": ws, "reason": _("Workstation does not exist")})
			continue
		if info["status"] in UNUSABLE_STATES:
			res["excluded"].append({"workstation": ws, "reason": _("Workstation is {0}").format(info["status"])})
			continue
		intervals = book.get(ws, []) + virtual.get(ws, [])
		warns = []
		if not info.get("capacity_configured"):
			warns.append(_("Capacity is not configured on {0}; ERPNext's default of 1 is used.").format(ws))
		util = machines.utilization_pct(intervals, info["capacity"], earliest, earliest + timedelta(days=cfg["load_window"]))
		cand = {
			"workstation": ws, "compatibility": {"state": "compatible", "sources": why}, "capacity": info["capacity"], "workstation_type": info["workstation_type"],
			"utilization": util, "hour_rate": info.get("hour_rate") or None, "efficiency": compat["efficiency"].get(ws), "status": info["status"], "warnings": warns,
			"start": None, "end": None, "feasible": False, "rows": None,
		}
		if timing_known:
			plan = machines.plan_operation(intervals, info["capacity"], earliest, minutes or 0, info["working_hours"], holidays_of(ws), horizon_end, allow_overtime=flags["allow_overtime"])
			if plan:
				cand.update(start=plan["start"], end=plan["end"] if minutes else None, rows=plan["rows"] if minutes else [], feasible=True)
				at_start = machines.peak_load(machines.clip(intervals, plan["start"], plan["start"] + timedelta(seconds=1)))
				cand["running_at_start"] = at_start
				cand["available_now"] = plan["start"] <= earliest + timedelta(minutes=1)
				cand["elapsed_minutes"] = round((plan["end"] - plan["start"]).total_seconds() / 60.0, 1) if minutes else None
			else:
				cand["warnings"].append(_("No free slot within the look-ahead period."))
		else:
			cand["feasible"] = True  # ranked on workload only: the start is unknown because an earlier operation has no duration
		cands.append(cand)

	feasible = [c for c in cands if c["feasible"]]
	_score(feasible, cfg, delivery_date, later_minutes, minutes, timing_known)
	feasible.sort(key=lambda c: (-(c["score"] if c["score"] is not None else -1), c["end"] or datetime.max, c["workstation"]))
	for i, c in enumerate(feasible, 1):
		c["rank"] = i
	for c in cands:
		if not c["feasible"]:
			c["rank"], c["score"] = None, None
			res["excluded"].append({"workstation": c["workstation"], "reason": _("No free slot within {0} days").format(cfg["horizon"])})
	res["candidates"] = feasible
	_explain(feasible, delivery_date)
	if feasible:
		res["recommended"] = feasible[0]["workstation"]
		chosen = next((c for c in feasible if c["workstation"] == pinned), None) if pinned else None
		if pinned and not chosen:
			res["warnings"].append(_("{0} cannot be used for {1} (not compatible or not available); the recommendation was used instead.").format(pinned, op.operation))
		chosen = chosen or feasible[0]
		res["selected"] = chosen["workstation"]
		res["selected_candidate"] = chosen
		chosen["selected"] = True
	else:
		res["warnings"].append(_("No compatible machine has a free slot for {0}.").format(op.operation))
	return res


def _score(cands, cfg, delivery_date, later_minutes, minutes, timing_known):
	"""Weighted 0-100 score. Factors without reliable data are dropped, not guessed."""
	if not cands:
		return
	w = cfg["weights"]
	ends = [c["end"] for c in cands if c["end"]]
	lo_end, hi_end = (min(ends), max(ends)) if ends and len(ends) == len(cands) else (None, None)
	utils = [c["utilization"] for c in cands if c["utilization"] is not None]
	costs = [c["hour_rate"] * (minutes or 0) / 60.0 for c in cands] if minutes and all(c["hour_rate"] for c in cands) else None
	effs = [c["efficiency"] for c in cands] if all(c["efficiency"] for c in cands) else None
	for i, c in enumerate(cands):
		f = []
		c["factors"] = {}
		if lo_end is not None and timing_known:
			s = spread_score(c["end"].timestamp(), lo_end.timestamp(), hi_end.timestamp())
			f.append((w["completion"], s))
			c["factors"]["completion"] = s
		if len(utils) == len(cands):
			s = spread_score(c["utilization"], min(utils), max(utils)) if max(utils) - min(utils) > 1e-9 else 1.0 - min(1.0, c["utilization"] / 100.0)
			f.append((w["load"], s))
			c["factors"]["load"] = s
		if delivery_date and c["end"] and later_minutes is not None and minutes:
			projected = c["end"] + timedelta(minutes=later_minutes)
			risk = delivery_risk(projected, delivery_date)
			s = 1.0 if risk["state"] in ("meets", "tight") else 1.0 / (1.0 + abs(risk["slack_days"] or 0))
			f.append((w["delivery"], s))
			c["factors"]["delivery"] = s
			c["delivery_risk"] = risk["state"]
			c["delivery_slack_days"] = risk["slack_days"]
			c["projected_completion"] = dt_str(projected)
		if costs:
			s = spread_score(costs[i], min(costs), max(costs))
			f.append((w["cost"], s))
			c["factors"]["cost"] = s
			c["cost_estimate"] = round(costs[i], 2)
		if effs:
			s = spread_score(effs[i], min(effs), max(effs), higher_is_better=True)
			f.append((w["efficiency"], s))
			c["factors"]["efficiency"] = s
		c["score"] = weighted_score(f)


def _explain(cands, delivery_date):
	"""Plain-language reasons for each candidate's position."""
	if not cands:
		return
	best = cands[0]
	ends = [c["end"] for c in cands if c["end"]]
	earliest_end = min(ends) if ends else None
	utils = [c["utilization"] for c in cands if c["utilization"] is not None]
	for c in cands:
		why = [_("Compatible: {0}.").format(", ".join(c["compatibility"]["sources"]))]
		if c["start"]:
			if c.get("available_now"):
				why.append(_("Available now (capacity {0}, {1} other operation(s) running).").format(c["capacity"], c.get("running_at_start", 0)))
			else:
				why.append(_("Free from {0} (capacity {1}).").format(dt_str(c["start"]), c["capacity"]))
		if c["end"] and earliest_end:
			late = (c["end"] - earliest_end).total_seconds() / 3600.0
			if late < 1e-6:
				why.append(_("Shortest feasible completion time ({0}).").format(dt_str(c["end"])))
			else:
				why.append(_("Finishes {0} h later than the earliest option.").format(round(late, 1)))
		if c["utilization"] is not None and len(utils) > 1:
			if c["utilization"] <= min(utils) and max(utils) - min(utils) >= 5:
				why.append(_("Lower workload than the alternatives ({0}% booked).").format(c["utilization"]))
			elif c["utilization"] >= max(utils) and max(utils) - min(utils) >= 5:
				why.append(_("Higher workload than the alternatives ({0}% booked).").format(c["utilization"]))
		if c.get("delivery_risk") == "meets":
			why.append(_("Meets the delivery date ({0} day(s) to spare).").format(c["delivery_slack_days"]))
		elif c.get("delivery_risk") == "tight":
			why.append(_("Tight against the delivery date ({0} day(s) to spare).").format(c["delivery_slack_days"]))
		elif c.get("delivery_risk") == "misses":
			why.append(_("Would miss the delivery date by {0} day(s).").format(abs(c["delivery_slack_days"])))
		if "cost" in c.get("factors", {}) and c is best:
			why.append(_("Lowest estimated machine cost."))
		c["explanation"] = why


# =====================================================================================
# Saving the selection on the Sales Order line, and carrying it on
# =====================================================================================


def build_machine_plan(result: dict, selection: dict | None = None) -> dict:
	"""The JSON stored on the Sales Order line: which machine was chosen for each operation, and what it was calculated for."""
	picks = []
	for op in result.get("operations", []):
		ws = (selection or {}).get(op["operation"]) or op.get("selected")
		if ws:
			picks.append({"operation": op["operation"], "sequence_id": op.get("sequence_id"), "workstation": ws})
	return {"operations": picks, "basis": result.get("basis"), "bom": (result.get("bom") or {}).get("name"), "calculated_at": result.get("calculated_at")}


def parse_plan(raw) -> dict:
	try:
		data = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
	except ValueError:
		return {}
	return data if isinstance(data, dict) else {}


def _line_key(row) -> tuple:
	return (row.get("ppc_preferred_workstation") or "", row.get("ppc_machine_preference") or "", row.get("ppc_machine_plan") or "")


def validate_line(row, sales_order) -> None:
	"""Server-side rules for a Sales Order line's machine preference (the browser's checks are never relied on).

	* the Workstation must exist and be able to perform at least one operation of the item's BOM (a preferred machine applies to every
	  operation it is compatible with); incompatibility is an error that no role can override;
	* when the preference is new or changed, the estimates (start, completion, status) are recalculated here from the stored selection -
	  whatever the browser sent for them is replaced;
	* when only the item, quantity, UOM, BOM or delivery date changed since the recommendation was made, the preference is kept
	  but marked Stale, so the planner is shown it must be recalculated."""
	ws = row.get("ppc_preferred_workstation")
	plan = parse_plan(row.get("ppc_machine_plan"))
	pref = row.get("ppc_machine_preference") or ""
	if not ws and not plan.get("operations"):
		if pref == "No Preference":
			row.ppc_recommendation_status = ""
		return
	if pref == "No Preference":
		row.ppc_preferred_workstation = None
		row.ppc_machine_plan = None
		row.ppc_est_start = row.ppc_est_completion = None
		row.ppc_recommendation_status = ""
		return
	if ws and not frappe.db.exists("Workstation", ws):
		frappe.throw(_("Row {0}: Workstation {1} does not exist.").format(row.idx, ws), title=_("Machine Preference"))
	bom, _problem, _notes = resolve_bom(row.item_code, row.get("bom_no") or None)
	ops = bom_operations(bom.name) if bom and cint(bom.with_operations) else []
	if ops:
		if ws:
			fits = [o.operation for o in ops if ws in compatible_workstations(o.operation, o.workstation_type, o.workstation)["machines"]]
			if not fits:
				frappe.throw(
					_("Row {0}: {1} cannot perform any operation of {2}. Choose a compatible Workstation (or clear the preference).").format(row.idx, ws, row.item_code),
					title=_("Machine Preference"),
				)
		for entry in plan.get("operations", []):
			allowed = next((o for o in ops if o.operation == entry.get("operation")), None)
			if not allowed:
				frappe.throw(_("Row {0}: {1} is not an operation of BOM {2}.").format(row.idx, entry.get("operation"), bom.name), title=_("Machine Preference"))
			if entry.get("workstation") not in compatible_workstations(allowed.operation, allowed.workstation_type, allowed.workstation)["machines"]:
				frappe.throw(_("Row {0}: {1} cannot perform {2}.").format(row.idx, entry.get("workstation"), entry.get("operation")), title=_("Machine Preference"))
	delivery = row.get("delivery_date") or sales_order.get("delivery_date")
	current = basis_hash(row.item_code, row.qty, row.get("uom"), bom.name if bom else None, delivery)
	before = _stored_line(sales_order, row)
	changed = before is None or _line_key(before) != _line_key(row)
	if changed:
		forced = {e["operation"]: e["workstation"] for e in plan.get("operations", []) if e.get("workstation")}
		if ws:
			for o in ops:
				if o.operation not in forced and ws in compatible_workstations(o.operation, o.workstation_type, o.workstation)["machines"]:
					forced[o.operation] = ws
		result = recommend(row.item_code, row.qty, uom=row.get("uom"), bom_no=bom.name if bom else None, delivery_date=delivery, forced=forced)
		row.ppc_est_start = result["overall"]["start"]
		row.ppc_est_completion = result["overall"]["completion"]
		row.ppc_recommendation_updated_at = now_datetime()
		stored = build_machine_plan(result, forced)
		row.ppc_machine_plan = json.dumps(stored)
		row.ppc_recommendation_status = (
			result["status"] if result["status"] in (STATUS_INCOMPLETE, STATUS_UNAVAILABLE) else (STATUS_RECOMMENDED if not result.get("manual") and pref != "Manual" else STATUS_SELECTED)
		)
		if not row.ppc_preferred_workstation:
			row.ppc_preferred_workstation = result.get("recommended_workstation")
	elif plan.get("basis") and plan["basis"] != current:
		row.ppc_recommendation_status = STATUS_STALE


def _stored_line(sales_order, row):
	"""The saved version of this Sales Order line, or None for a new document / new line."""
	before = sales_order.get_doc_before_save() if hasattr(sales_order, "get_doc_before_save") else None
	if not before or not row.get("name"):
		return None
	return next((r for r in before.get("items") or [] if r.name == row.name), None)


# ---- Production Plan and Work Order -------------------------------------------------------------------------------------------


def line_preference(sales_order_item: str | None) -> dict | None:
	"""The machine preference stored on a Sales Order line, or None."""
	if not sales_order_item or not frappe.db.exists("Sales Order Item", sales_order_item):
		return None
	row = frappe.db.get_value(
		"Sales Order Item", sales_order_item, ["ppc_preferred_workstation", "ppc_machine_preference", "ppc_machine_plan", "ppc_recommendation_status"], as_dict=True,
	)
	if not row or not row.ppc_preferred_workstation or (row.ppc_machine_preference or "") == "No Preference":
		return None
	return row


def copy_preferences_to_plan(plan_doc) -> int:
	"""Carry each Sales Order line's machine preference onto the Production Plan item made from it (matched by the line's name)."""
	copied = 0
	for item in plan_doc.get("po_items") or []:
		if not item.get("sales_order_item") or item.get("ppc_machine_plan") or item.get("ppc_preferred_workstation"):
			continue
		pref = line_preference(item.sales_order_item)
		if not pref:
			continue
		item.ppc_preferred_workstation = pref.ppc_preferred_workstation
		item.ppc_machine_preference = pref.ppc_machine_preference
		item.ppc_machine_plan = pref.ppc_machine_plan
		copied += 1
	return copied


def _planner_of(plan_name: str | None) -> str | None:
	if not plan_name:
		return None
	row = frappe.db.get_value("Production Plan", plan_name, ["ppc_planner", "owner"], as_dict=True)
	return (row.ppc_planner or row.owner) if row else None


def apply_to_work_order(wo) -> dict:
	"""Use the preferred machines on a NEW Work Order's operations - after checking, now, that each is still possible.

	Called before the Work Order is inserted (so the Job Cards ERPNext creates on submit land on the chosen machines). A machine that is no
	longer compatible or usable, or whose first free slot is later than the tolerance after the Work Order's planned start, is NOT applied:
	the BOM's own machine stays, and the planner is told (see `notify_unavailable`). Never submits, never creates stock transactions."""
	result = {"applied": [], "skipped": []}
	source = None
	if wo.get("production_plan_item"):
		source = frappe.db.get_value("Production Plan Item", wo.production_plan_item, ["ppc_preferred_workstation", "ppc_machine_plan"], as_dict=True)
	if (not source or not source.ppc_preferred_workstation) and wo.get("sales_order_item"):
		source = line_preference(wo.sales_order_item)
	if not source or not source.get("ppc_preferred_workstation"):
		return result
	entries = parse_plan(source.get("ppc_machine_plan")).get("operations") or []
	if not entries:  # a preference without a per-operation plan: the machine applies to every operation it can perform
		entries = [{"operation": r.operation, "workstation": source.ppc_preferred_workstation} for r in wo.get("operations") or []]
	cfg = _config()
	mfg = frappe.get_cached_doc("Manufacturing Settings")
	earliest = machines.ceil_second(max(get_datetime(wo.get("planned_start_date") or now_datetime()), now_datetime()))
	horizon_end = earliest + timedelta(days=cfg["horizon"])
	for entry in entries:
		row = next((r for r in wo.get("operations") or [] if r.operation == entry.get("operation")), None)
		ws = entry.get("workstation")
		if not row or not ws:
			continue
		if row.workstation == ws:
			result["applied"].append({"operation": row.operation, "workstation": ws, "note": _("already the BOM's machine")})
			continue
		problem = _usable(ws, row, earliest, horizon_end, cfg, mfg)
		if problem:
			result["skipped"].append({"operation": row.operation, "workstation": ws, "reason": problem, "kept": row.workstation})
			continue
		row.workstation = ws
		row.hour_rate = flt(frappe.db.get_value("Workstation", ws, "hour_rate")) or row.get("hour_rate")
		result["applied"].append({"operation": row.operation, "workstation": ws})
	lines = [_("{0}: {1} applied").format(a["operation"], a["workstation"]) for a in result["applied"]] + [
		_("{0}: {1} NOT applied ({2}); {3} kept").format(s["operation"], s["workstation"], s["reason"], s["kept"] or "-") for s in result["skipped"]
	]
	if lines:
		wo.ppc_machine_note = "\n".join(lines)
	return result


def _usable(ws: str, row, earliest: datetime, horizon_end: datetime, cfg: dict, mfg) -> str | None:
	"""Why `ws` cannot be used for this Work Order operation right now (None when it can)."""
	info = _info(ws)
	if not info:
		return _("the Workstation no longer exists")
	if info["status"] in UNUSABLE_STATES:
		return _("the Workstation is {0}").format(info["status"])
	if ws not in compatible_workstations(row.operation, row.get("workstation_type"), row.get("workstation"))["machines"]:
		return _("it is no longer compatible with this operation")
	minutes = flt(row.get("time_in_mins"))
	allow_overtime = bool(cint(mfg.get("allow_overtime")))
	holidays = set()
	if info["working_hours"] and not allow_overtime and not cint(mfg.get("allow_production_on_holidays")):
		holidays = machines._holiday_dates(info, earliest.date(), horizon_end.date() + timedelta(days=30))
	book = machines.intervals_by_workstation([ws], earliest, horizon_end + timedelta(days=30), include_draft_work_orders=bool(cfg["drafts"]))
	plan = machines.plan_operation(book.get(ws, []), info["capacity"], earliest, minutes, info["working_hours"], holidays, horizon_end, allow_overtime=allow_overtime)
	if not plan:
		return _("it has no free slot within {0} days").format(cfg["horizon"])
	wait_h = (plan["start"] - earliest).total_seconds() / 3600.0
	if wait_h > cfg["tolerance"]:
		return _("it is busy until {0}, {1} h after the planned start").format(dt_str(plan["start"]), round(wait_h))
	return None


def notify_unavailable(wo, result: dict) -> None:
	"""After the Work Order exists: audit the outcome and, for machines that could not be applied, tell the planner and attach the alternatives."""
	if not result.get("applied") and not result.get("skipped"):
		return
	plan_name = wo.get("production_plan")
	alternatives = {}
	for s in result["skipped"]:
		row = next((r for r in wo.get("operations") or [] if r.operation == s["operation"]), None)
		if row:
			try:
				alternatives[s["operation"]] = _alternatives_for(row)
			except Exception:
				frappe.log_error(title="PPC: recalculating machine alternatives failed", message=frappe.get_traceback())
	audit.log(
		A_APPLY_MACHINE_PLAN, R_WARNING if result["skipped"] else R_SUCCESS, plan=plan_name, work_order=wo.name,
		reason=_("Preferred machines from the Sales Order were checked when the Work Order was created"),
		details={"applied": result["applied"], "not_applied": result["skipped"], "alternatives": alternatives},
	)
	if result["skipped"]:
		user = _planner_of(plan_name) or wo.owner
		lines = "<br>".join(frappe.utils.escape_html(f"{s['operation']}: {s['workstation']} - {s['reason']}") for s in result["skipped"])
		alt = "<br>".join(frappe.utils.escape_html(f"{op}: " + ", ".join(a["workstation"] for a in alts[:3])) for op, alts in alternatives.items() if alts)
		frappe.get_doc({
			"doctype": "Notification Log", "type": "Alert", "for_user": user, "from_user": frappe.session.user,
			"subject": _("Preferred machine not available for Work Order {0}").format(wo.name), "document_type": "Work Order", "document_name": wo.name,
			"email_content": lines + ("<br><br>" + _("Alternatives (ranked):") + "<br>" + alt if alt else ""),
		}).insert(ignore_permissions=True)


def _alternatives_for(row) -> list[dict]:
	"""Compatible machines for a Work Order operation, best first (free from, and how busy), for the planner's notification."""
	cfg = _config()
	earliest = machines.ceil_second(now_datetime())
	compat = compatible_workstations(row.operation, row.get("workstation_type"), row.get("workstation"))
	out = []
	for ws in compat["machines"]:
		info = _info(ws)
		if not info or info["status"] in UNUSABLE_STATES:
			continue
		book = machines.intervals_by_workstation([ws], earliest, earliest + timedelta(days=cfg["horizon"] + 30))
		plan = machines.plan_operation(book.get(ws, []), info["capacity"], earliest, flt(row.get("time_in_mins")), info["working_hours"], set(), earliest + timedelta(days=cfg["horizon"]))
		if plan:
			out.append({"workstation": ws, "start": dt_str(plan["start"]), "end": dt_str(plan["end"])})
	return sorted(out, key=lambda a: (a["end"], a["workstation"]))


# ---- permissions ----------------------------------------------------------------------------------------------------------------------------------


def may_recommend() -> bool:
	"""Whoever may create or edit Sales Orders, or holds the PPC View capability, may ask for a recommendation."""
	return bool(
		frappe.has_permission("Sales Order", "create") or frappe.has_permission("Sales Order", "write") or has_capability(CAP_VIEW)
	)
