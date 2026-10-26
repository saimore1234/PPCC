"""Disposable fixtures for the PPC browser test (tests/e2e/ppc_e2e.js).

Unlike the unit tests (which roll back), a browser session can only see COMMITTED data, so these helpers commit
and `cleanup` removes everything again. Run them with bench, e.g.

    bench --site dev.local execute ppc_production_control.tests.e2e.fixture.setup
    bench --site dev.local execute ppc_production_control.tests.e2e.fixture.cleanup

Rules: everything created is named PPCE2E-* / "PPCE2E ..." and cleanup deletes by that name only; global settings
(Manufacturing Settings, defaults, naming series configuration) are never modified. Document numbers consumed from
the site's naming series are not returned (a harmless gap).
"""

import frappe

from ppc_production_control.tests import utils as U

P = "PPCE2E"
MANAGER = "ppc.e2e.manager@example.com"
VIEWER = "ppc.e2e.viewer@example.com"
OUTSIDER = "ppc.e2e.outsider@example.com"  # a desk user with no PPC role at all
PLANNER = "ppc.e2e.planner@example.com"  # may reschedule and change machines, but may not override a conflict
PASSWORD = "Ppc-E2E-2026!x"
ROLES = {
	MANAGER: ["PPC Manager", "Manufacturing Manager", "Manufacturing User", "Stock User", "Sales User"],
	VIEWER: ["PPC Viewer", "Manufacturing User", "Stock User", "Sales User"],
	OUTSIDER: ["Sales User"],
	PLANNER: ["Production Planner", "Manufacturing User", "Stock User", "Sales User"],
}


def _admin():
	frappe.set_user("Administrator")


def setup(company: str | None = None, dry_run: int = 0) -> dict:
	"""Masters, a submitted Sales Order and a submitted Production Plan (ERPNext's standard one) whose draft Work
	Orders PPC then creates in the background. Returns the names."""
	_admin()
	company = company or frappe.conf.get("ppc_e2e_company") or frappe.db.get_single_value("Global Defaults", "default_company")
	fg_wh = frappe.db.get_single_value("Manufacturing Settings", "default_fg_warehouse")
	if not fg_wh or not frappe.db.get_single_value("Manufacturing Settings", "default_wip_warehouse"):
		frappe.throw("Manufacturing Settings has no default WIP / FG warehouse; the e2e test will not change site settings.")
	rm_wh = U.make_warehouse(f"{P} RM", company)
	ws1, ws2 = U.make_workstation(f"{P} WS1", 1), U.make_workstation(f"{P} WS2", 1)
	op1, op2 = U.make_operation(f"{P} Cut", ws1), U.make_operation(f"{P} Assemble", ws2)
	rm1, rm2 = U.make_item(f"{P}-RM-1"), U.make_item(f"{P}-RM-2")
	U.ensure_stock(rm1, rm_wh)
	U.ensure_stock(rm2, rm_wh)
	fg = U.make_item(f"{P}-FG", manufacture=True)
	U.make_bom(fg, company, [(rm1, 2), (rm2, 1)], [
		{"operation": op1, "workstation": ws1, "time_in_mins": 120, "sequence_id": 1},
		{"operation": op2, "workstation": ws2, "time_in_mins": 180, "sequence_id": 2},
	], source_warehouse=rm_wh)
	so = U.make_sales_order(company, U.make_customer(f"{P} Customer"), [(fg, 10)], warehouse=fg_wh)
	plan = U.make_plan(company, [so.name], auto_create=True, auto_submit=False, submit=True)
	out = {"company": company, "sales_order": so.name, "plan": plan.name}
	if int(dry_run):
		frappe.db.rollback()
		out["dry_run"] = True
	else:
		frappe.db.commit()
	return out


def machines() -> dict:
	"""Extra machines for the Workstation-change tests: WS3, WS5, WS6, WS7 run one operation at a time, WS4 two at once."""
	_admin()
	out = {n: U.make_workstation(f"{P} {n}", cap) for n, cap in (("WS3", 1), ("WS4", 2), ("WS5", 1), ("WS6", 1), ("WS7", 1))}
	frappe.db.commit()
	return out


def second_plan() -> dict:
	"""Another submitted plan for the same finished good (its draft Work Order is made by the background job, like the first)."""
	_admin()
	company = frappe.conf.get("ppc_e2e_company") or frappe.db.get_single_value("Global Defaults", "default_company")
	fg_wh = frappe.db.get_single_value("Manufacturing Settings", "default_fg_warehouse")
	so = U.make_sales_order(company, U.make_customer(f"{P} Customer"), [(f"{P}-FG", 10)], warehouse=fg_wh)
	plan = U.make_plan(company, [so.name], auto_create=True, auto_submit=False, submit=True)
	frappe.db.commit()
	return {"sales_order": so.name, "plan": plan.name}


def place(job_card: str, workstation: str, start: str, end: str) -> dict:
	"""Put a Job Card on a machine at an exact interval (test set-up: how a booking got there does not matter)."""
	_admin()
	from ppc_production_control.services import scheduling

	frappe.db.set_value("Job Card", job_card, "workstation", workstation)
	doc = frappe.get_doc("Job Card", job_card)
	scheduling.apply_dates(doc, scheduling.as_dt(start), scheduling.as_dt(end), "resize")
	frappe.db.commit()
	return frappe.db.get_value("Job Card", job_card, ["workstation", "expected_start_date", "expected_end_date"], as_dict=True) and {"ok": True}


def bookings(workstation: str, start: str = "2000-01-01", end: str = "2100-01-01") -> list:
	"""What the availability check sees on a machine (raw rows), for diagnosing a surprising conflict."""
	_admin()
	from ppc_production_control.services import machines, scheduling

	rows = machines.occupancy(workstation, scheduling.as_dt(start), scheduling.as_dt(end), [])
	return _plain([{k: v for k, v in r.items() if k in ("job_card", "from", "to", "source", "status", "docstatus")} for r in rows])


def site_report() -> dict:
	"""Read-only: what a `bench migrate` would still have to run on this site, and which PPC pieces it already has."""
	from frappe.modules.patch_handler import get_all_patches

	done = set(frappe.get_all("Patch Log", pluck="patch"))
	pending = [p for p in get_all_patches() if p not in done]
	return _plain({
		"pending_patches": pending,
		"apps": frappe.get_installed_apps(),
		"has_machine_fields": bool(frappe.db.has_column("PPC Schedule Revision", "previous_workstation")),
		"ppc_pages": frappe.get_all("Page", filters={"name": ["like", "ppc-%"]}, pluck="name"),
		"workspaces": frappe.get_all("Workspace", filters={"name": ["like", "PPC%"]}, pluck="name"),
	})


def rows(job_card: str) -> list:
	"""The stored scheduled time rows of a Job Card (diagnostics)."""
	_admin()
	return _plain(frappe.db.sql(
		"select name, idx, from_time, to_time, time_in_mins from `tabJob Card Scheduled Time` where parent = %s order by idx", (job_card,), as_dict=True))


def _work_orders():
	return frappe.get_all("Work Order", filters={"production_item": f"{P}-FG"}, pluck="name", order_by="creation asc")


def state() -> dict:
	"""Everything the test asserts on, straight from the database."""
	out = {"work_orders": []}
	card_names = []
	for wo in _work_orders():
		w = frappe.db.get_value("Work Order", wo, ["name", "docstatus", "status", "planned_start_date", "planned_end_date"], as_dict=True)
		cards = frappe.get_all(
			"Job Card", filters={"work_order": wo, "docstatus": ["<", 2]}, order_by="sequence_id asc",
			fields=["name", "operation", "sequence_id", "docstatus", "status", "workstation", "modified", "expected_start_date", "expected_end_date",
				"ppc_original_start", "ppc_original_end", "ppc_revised_start", "ppc_revised_end", "ppc_schedule_revision",
				"ppc_schedule_sync_status", "ppc_baseline_source"],
		)
		for c in cards:
			c["time_rows"] = frappe.db.count("Job Card Scheduled Time", {"parent": c.name})
			c["revisions"] = frappe.get_all(
				"PPC Schedule Revision", filters={"job_card": c.name}, order_by="revision_no asc",
				fields=["revision_no", "revision_type", "change_source", "change_reason", "correlation_id", "accepted_start", "accepted_end", "changed_by", "sync_status",
					"previous_workstation", "new_workstation", "override_used", "override_reason", "conflict_details", "workstation"],
			)
			card_names.append(c.name)
		w["job_cards"] = cards
		out["work_orders"].append(w)
	out["failed_audit_rows"] = frappe.db.count("PPC Audit Log", {"job_card": ["in", card_names or [""]], "result": "Failed"})
	out["plan"] = frappe.db.get_value("Production Plan Item", {"item_code": f"{P}-FG"}, "parent")
	return _plain(out)


def _plain(value):
	"""bench execute prints JSON; make datetimes strings."""
	import json

	return json.loads(json.dumps(value, default=str))


def submit_work_orders() -> list:
	"""Submit the fixture's draft Work Orders (what the PPC screen's bulk action does) so the browser test can start from Job Cards."""
	_admin()
	done = []
	for wo in frappe.get_all("Work Order", filters={"production_item": f"{P}-FG", "docstatus": 0}, pluck="name"):
		frappe.get_doc("Work Order", wo).submit()
		done.append(wo)
	frappe.db.commit()
	return done


def disturb(job_card: str) -> str:
	"""Another user touches the card: its `modified` moves, so the browser's concurrency token goes stale."""
	_admin()
	frappe.db.set_value("Job Card", job_card, "remarks", "changed behind the browser's back")
	frappe.db.commit()
	return "disturbed " + job_card


def drift(job_card: str, hours: int = 3) -> str:
	"""Simulate an out-of-band edit (e.g. a data import): ERPNext's schedule moves, PPC's accepted schedule does not."""
	_admin()
	start = frappe.db.get_value("Job Card", job_card, "expected_start_date")
	frappe.db.set_value("Job Card", job_card, "expected_start_date", frappe.utils.add_to_date(start, hours=int(hours)), update_modified=False)
	frappe.db.commit()
	return "drifted " + job_card


def users(action: str = "create") -> dict:
	_admin()
	for email in (MANAGER, VIEWER, OUTSIDER, PLANNER):
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		if action == "create":
			u = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "PPC E2E " + email.split(".")[2].split("@")[0].title(), "send_welcome_email": 0,
				"user_type": "System User", "new_password": PASSWORD, "roles": [{"role": r} for r in ROLES[email]],
			})
			u.flags.no_welcome_mail = True
			u.insert(ignore_permissions=True)
	frappe.db.commit()
	return {"action": action, "users": {e: bool(frappe.db.exists("User", e)) for e in (MANAGER, VIEWER, OUTSIDER, PLANNER)}}


# ---- Sales Order machine recommendation (ppc_recommend_e2e.js) ---------------------------------------------------------------------
REC = f"{P}-REC"


def rec_setup(company: str | None = None) -> dict:
	"""Three lathes (one the BOM names), a press and a milling machine, an item whose BOM has one Turn operation on the lathe type,
	and a DRAFT Sales Order for 10 of it. Returns the names."""
	_admin()
	company = company or frappe.conf.get("ppc_e2e_company") or frappe.db.get_single_value("Global Defaults", "default_company")
	fg_wh = frappe.db.get_single_value("Manufacturing Settings", "default_fg_warehouse")
	rm_wh = U.make_warehouse(f"{P} RM", company)
	types = {}
	for tag in ("Lathe", "Press"):
		name = f"{P} {tag}"
		if not frappe.db.exists("Workstation Type", name):
			frappe.get_doc({"doctype": "Workstation Type", "workstation_type": name}).insert(ignore_permissions=True)
		types[tag] = name
	ws = {}
	for tag, wtype in (("R1", "Lathe"), ("R2", "Lathe"), ("R3", "Lathe"), ("RP", "Press")):
		name = U.make_workstation(f"{P} {tag}", 1)
		frappe.db.set_value("Workstation", name, {"workstation_type": types[wtype], "status": "Production"})
		ws[tag] = name
	frappe.db.sql("delete from `tabWorkstation Working Hour` where parent like %s", f"{P} R%")
	op = U.make_operation(f"{P} Turn", ws["R1"])
	rm = U.make_item(f"{P}-RM-1")
	U.ensure_stock(rm, rm_wh)
	item = U.make_item(REC, manufacture=True)
	U.make_bom(item, company, [(rm, 1)], [{"operation": op, "workstation": ws["R1"], "workstation_type": types["Lathe"], "time_in_mins": 30, "sequence_id": 1}], source_warehouse=rm_wh)
	so = U.make_sales_order(company, U.make_customer(f"{P} Customer"), [(item, 10)], warehouse=fg_wh, submit=False)
	frappe.db.commit()
	return {"sales_order": so.name, "item": item, "operation": op, "workstations": ws, "company": company}


def rec_state(sales_order: str) -> dict:
	"""The machine fields of the order's lines, straight from the database."""
	_admin()
	rows = frappe.get_all(
		"Sales Order Item", filters={"parent": sales_order}, order_by="idx asc", fields=[
			"name", "idx", "item_code", "qty", "ppc_preferred_workstation", "ppc_machine_preference", "ppc_recommendation_status", "ppc_est_start", "ppc_est_completion",
			"ppc_recommendation_updated_at", "ppc_machine_plan"])
	return _plain({"docstatus": frappe.db.get_value("Sales Order", sales_order, "docstatus"), "rows": rows})


def rec_submit(sales_order: str) -> dict:
	_admin()
	frappe.get_doc("Sales Order", sales_order).submit()
	frappe.db.commit()
	return {"submitted": sales_order}


def rec_plan(sales_order: str) -> dict:
	"""A submitted Production Plan for the order, with its Work Order created (and submitted, so the Job Cards exist) by the standard flow."""
	_admin()
	company = frappe.conf.get("ppc_e2e_company") or frappe.db.get_single_value("Global Defaults", "default_company")
	plan = U.make_plan(company, [sales_order], auto_create=True, auto_submit=True, submit=True)
	frappe.db.commit()
	import time

	wos = []
	for _i in range(40):
		frappe.db.commit()  # a new snapshot: the background worker commits in another process
		wos = frappe.get_all("Work Order", filters={"production_plan": plan.name, "docstatus": 1}, pluck="name")
		if wos and frappe.db.count("Job Card", {"work_order": ["in", wos], "docstatus": ["<", 2]}):
			break
		time.sleep(3)
	out = []
	for wo in wos:
		out.append({
			"name": wo, "docstatus": frappe.db.get_value("Work Order", wo, "docstatus"), "note": frappe.db.get_value("Work Order", wo, "ppc_machine_note"),
			"operations": frappe.get_all("Work Order Operation", filters={"parent": wo}, fields=["operation", "workstation"], order_by="idx asc"),
			"job_cards": frappe.get_all("Job Card", filters={"work_order": wo, "docstatus": ["<", 2]}, fields=["name", "workstation"]),
		})
	return _plain({
		"plan": plan.name, "work_orders": out,
		"plan_items": frappe.get_all("Production Plan Item", filters={"parent": plan.name}, fields=["sales_order_item", "ppc_preferred_workstation", "ppc_machine_preference"]),
	})


def rec_workstation_status(workstation: str, status: str) -> str:
	_admin()
	frappe.db.set_value("Workstation", workstation, "status", status)
	frappe.db.commit()
	return f"{workstation} is now {status}"


def _try(label, fn, problems):
	try:
		fn()
		frappe.db.commit()
	except Exception as exc:  # keep going: cleanup must be as thorough as possible
		problems.append(f"{label}: {str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__}")
		frappe.db.rollback()


def _cancel_delete(doctype, name, problems):
	def run():
		doc = frappe.get_doc(doctype, name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	_try(f"{doctype} {name}", run, problems)


def cleanup() -> dict:
	"""Delete everything the fixture (and the browser test) created. Idempotent. Returns what is left (should be {})."""
	_admin()
	problems = []
	users("delete")
	wos = sorted(set(_work_orders() + frappe.get_all("Work Order", filters={"production_item": REC}, pluck="name")))
	plans = sorted(set(frappe.get_all("Production Plan Item", filters={"item_code": ["in", [f"{P}-FG", REC]]}, pluck="parent")))
	sos = sorted(set(frappe.get_all("Sales Order Item", filters={"item_code": ["in", [f"{P}-FG", REC]]}, pluck="parent")))
	cards = frappe.get_all("Job Card", filters={"work_order": ["in", wos or [""]]}, pluck="name")
	# append-only logs of the disposable cards: removed directly (test data created minutes ago, never real history)
	frappe.db.delete("PPC Schedule Revision", {"job_card": ["in", cards or [""]]})
	frappe.db.delete("PPC Audit Log", {"job_card": ["in", cards or [""]]})
	frappe.db.delete("PPC Audit Log", {"ppc_plan": ["in", plans or [""]]})
	frappe.db.commit()
	for jc in cards:
		_cancel_delete("Job Card", jc, problems)
	for wo in wos:
		_cancel_delete("Work Order", wo, problems)
	for pp in plans:
		_cancel_delete("Production Plan", pp, problems)
	for so in sos:
		_cancel_delete("Sales Order", so, problems)
	for bom in frappe.get_all("BOM", filters={"item": ["in", [f"{P}-FG", REC]]}, pluck="name"):
		_cancel_delete("BOM", bom, problems)
	frappe.db.delete("Item Price", {"item_code": ["like", f"{P}-%"]})
	frappe.db.delete("Bin", {"item_code": ["like", f"{P}-%"]})
	frappe.db.commit()
	for doctype, filters in (
		("Item", {"item_code": ["like", f"{P}-%"]}), ("Operation", {"name": ["like", f"{P} %"]}), ("Workstation", {"name": ["like", f"{P} %"]}),
		("Workstation Type", {"name": ["like", f"{P} %"]}), ("Warehouse", {"warehouse_name": ["like", f"{P} %"]}), ("Customer", {"customer_name": ["like", f"{P} %"]}),
	):
		for name in frappe.get_all(doctype, filters=filters, pluck="name"):
			_try(f"{doctype} {name}", lambda doctype=doctype, name=name: frappe.delete_doc(doctype, name, force=True, ignore_permissions=True), problems)
	left = {
		doctype: frappe.db.count(doctype, filters)
		for doctype, filters in (
			("Item", {"item_code": ["like", f"{P}-%"]}), ("BOM", {"item": f"{P}-FG"}), ("Work Order", {"production_item": f"{P}-FG"}),
			("Job Card", {"work_order": ["in", wos or [""]]}), ("Production Plan Item", {"item_code": f"{P}-FG"}),
			("Sales Order Item", {"item_code": ["in", [f"{P}-FG", REC]]}), ("Workstation Type", {"name": ["like", f"{P} %"]}), ("Operation", {"name": ["like", f"{P} %"]}),
			("Workstation", {"name": ["like", f"{P} %"]}), ("Warehouse", {"warehouse_name": ["like", f"{P} %"]}),
			("Customer", {"customer_name": ["like", f"{P} %"]}), ("Bin", {"item_code": ["like", f"{P}-%"]}),
			("User", {"name": ["in", [MANAGER, VIEWER]]}), ("PPC Schedule Revision", {"job_card": ["in", cards or [""]]}),
		)
	}
	return {"left": {k: v for k, v in left.items() if v}, "clean": not any(left.values()), "problems": problems}
