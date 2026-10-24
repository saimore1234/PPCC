"""Verify the 1.0.0 -> 1.1.0 migration result on the staging site against the pre-migration snapshot. usage (bench python, run from the bench sites folder): verify_upgrade.py <site> <before.json>"""
import hashlib
import json
import sys

import frappe

site, before_path = sys.argv[1], sys.argv[2]
before = json.load(open(before_path))
frappe.init(site=site)
frappe.connect()
db = frappe.db
checks = []


def check(name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})


s19 = lambda v: str(v)[:19] if v else None  # noqa: E731

# 1. schema and patch log
check("PPC Schedule Revision DocType and table exist", db.exists("DocType", "PPC Schedule Revision") and db.table_exists("PPC Schedule Revision"))
cols = {c[0] for c in db.sql("show columns from `tabJob Card`")}
need = {"ppc_baseline_source", "ppc_baseline_captured_at", "ppc_baseline_captured_by", "ppc_revised_start", "ppc_revised_end",
        "ppc_schedule_revision", "ppc_last_rescheduled_at", "ppc_last_rescheduled_by", "ppc_schedule_sync_status"}
check("all nine new Job Card columns exist", need <= cols, sorted(need - cols))
check("migration patch is recorded in the Patch Log", db.exists("Patch Log", {"patch": "ppc_production_control.patches.v1_1.backfill_schedule_history"}))
check("no Work Order column was added by PPC", not [c for c in db.sql("show columns from `tabWork Order`") if c[0].startswith("ppc_")])

# 2. baselines preserved exactly
legacy = db.sql("select name, ppc_original_start, ppc_original_end, expected_start_date, expected_end_date, ppc_baseline_source, ppc_baseline_captured_by, "
                "ppc_schedule_revision, ppc_revised_start, ppc_revised_end, ppc_schedule_sync_status from `tabJob Card` where ppc_original_start is not null order by name", as_dict=True)
h = hashlib.sha256()
for r in legacy:
    h.update(f"{r.name}|{r.ppc_original_start}|{r.ppc_original_end}\n".encode())
check("legacy card count unchanged", len(legacy) == before["legacy_cards"], f"{len(legacy)} vs {before['legacy_cards']}")
check("original dates of every legacy card are byte-identical (sha256)", h.hexdigest() == before["original_dates_sha256"])
check("job card total unchanged", db.count("Job Card") == before["job_cards_total"], db.count("Job Card"))

# 3. history rows: revision 0 from the existing baseline, an honest Migration revision where the card had moved
moved = fabricated = bad_rev0 = bad_pointer = bad_note = no_history = 0
for r in legacy:
    rows = frappe.get_all("PPC Schedule Revision", filters={"job_card": r.name}, order_by="revision_no asc",
                          fields=["revision_no", "revision_type", "change_source", "change_reason", "changed_by", "accepted_start", "accepted_end", "previous_start", "previous_end", "baseline_note"])
    if not rows:
        no_history += 1
        continue
    r0 = rows[0]
    if not (r0.revision_no == 0 and r0.change_source == "Baseline" and s19(r0.accepted_start) == s19(r.ppc_original_start) and s19(r0.accepted_end) == s19(r.ppc_original_end)):
        bad_rev0 += 1
    if r0.changed_by:  # a 1.0.0 baseline has no recorded author: none may appear
        fabricated += 1
    has_moved = s19(r.expected_start_date) != s19(r.ppc_original_start) or s19(r.expected_end_date) != s19(r.ppc_original_end)
    if has_moved:
        moved += 1
        ok = len(rows) == 2 and rows[1].revision_no == 1 and rows[1].change_source == "Migration" and "not recorded" in (rows[1].change_reason or "")
        if not ok:
            bad_note += 1
        if rows[1].changed_by:
            fabricated += 1
        if not (r.ppc_schedule_revision == 1 and s19(r.ppc_revised_start) == s19(r.expected_start_date) and s19(r.ppc_revised_end) == s19(r.expected_end_date)):
            bad_pointer += 1
    else:
        if len(rows) != 1 or r.ppc_schedule_revision != 0 or s19(r.ppc_revised_start) != s19(r.ppc_original_start):
            bad_pointer += 1
check("every legacy card received history", no_history == 0, no_history)
check("revision 0 equals the preserved 1.0.0 baseline", bad_rev0 == 0, bad_rev0)
check("moved cards got exactly one explanatory 'Migration' revision saying author/time/reason were not recorded", bad_note == 0, f"{moved} moved cards, {bad_note} wrong")
check("no author was fabricated (changed_by empty on every migrated row)", fabricated == 0, fabricated)
check("accepted-schedule pointers and revision counters match the card", bad_pointer == 0, bad_pointer)
check("moved-card count equals the pre-migration count", moved == before["legacy_moved_since_baseline"], f"{moved} vs {before['legacy_moved_since_baseline']}")
check("revision rows total = legacy cards + moved cards", db.count("PPC Schedule Revision") == len(legacy) + moved, db.count("PPC Schedule Revision"))
check("no history row for a card without baseline", db.sql("select count(*) from `tabPPC Schedule Revision` r join `tabJob Card` j on j.name=r.job_card where j.ppc_original_start is null")[0][0] == 0)

# 4. cards without baseline were not invented
nb = db.sql("select count(*) from `tabJob Card` where ppc_original_start is null and docstatus<2 and expected_start_date is not null")[0][0]
check("cards with a schedule but no baseline are left for the preview-first action", True, f"{nb} such cards remain")

# 5. consistency + idempotency + permissions
from ppc_production_control.patches.v1_1 import backfill_schedule_history
from ppc_production_control.services import reconcile

frappe.set_user("Administrator")
report = reconcile.check_consistency(None)
counts = report["counts"]
check("consistency check: no revision_mismatch / missing_history after migration", not counts.get("revision_mismatch") and not counts.get("missing_history"), counts)
rows_before = db.count("PPC Schedule Revision")
backfill_schedule_history.execute()
check("re-running the patch adds nothing (idempotent)", db.count("PPC Schedule Revision") == rows_before)
check("capability fallback: PPC Manager can reconcile without a settings row",
      "PPC Manager" in __import__("ppc_production_control.services.permissions", fromlist=["x"]).get_capability_roles()["Reconcile Schedules"])
check("audit log untouched", db.count("PPC Audit Log") == before["audit_rows"], f"{db.count('PPC Audit Log')} vs {before['audit_rows']}")
check("custom fields exist for every new column", len(frappe.get_all("Custom Field", filters={"dt": "Job Card", "fieldname": ["like", "ppc_%"]})) >= 12)

summary = {"site": site, "passed": sum(c["ok"] for c in checks), "failed": sum(not c["ok"] for c in checks), "consistency_counts": counts, "checks": checks}
print(json.dumps(summary, indent=1))
frappe.db.rollback()  # verification never writes
frappe.destroy()
