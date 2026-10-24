"""Snapshot of what matters for the 1.0.0 -> 1.1.0 migration. usage (bench python, run from the bench sites folder): snapshot_site.py <site> <label> [out.json]"""
import hashlib
import json
import sys

import frappe

site, label = sys.argv[1], sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else None
frappe.init(site=site)
frappe.connect()
db = frappe.db

res = {"site": site, "label": label}
res["revision_doctype"] = bool(db.exists("DocType", "PPC Schedule Revision"))
res["revision_table"] = bool(db.table_exists("PPC Schedule Revision"))
res["revision_rows"] = db.count("PPC Schedule Revision") if res["revision_table"] else None
cols = [c[0] for c in db.sql("show columns from `tabJob Card`") if c[0].startswith("ppc_")]
res["job_card_ppc_columns"] = sorted(cols)
res["custom_fields_job_card_ppc"] = sorted(db.get_all("Custom Field", filters={"dt": "Job Card", "fieldname": ["like", "ppc_%"]}, pluck="fieldname"))
res["job_cards_total"] = db.count("Job Card")
legacy = db.sql(
    "select name, ppc_original_start, ppc_original_end, expected_start_date, expected_end_date, docstatus, status from `tabJob Card` "
    "where ppc_original_start is not null order by name", as_dict=True)
res["legacy_cards"] = len(legacy)
h = hashlib.sha256()
for r in legacy:
    h.update(f"{r.name}|{r.ppc_original_start}|{r.ppc_original_end}\n".encode())
res["original_dates_sha256"] = h.hexdigest()
res["legacy_moved_since_baseline"] = sum(1 for r in legacy if r.expected_start_date and r.ppc_original_start and str(r.expected_start_date)[:19] != str(r.ppc_original_start)[:19]
    or r.expected_end_date and r.ppc_original_end and str(r.expected_end_date)[:19] != str(r.ppc_original_end)[:19])
res["legacy_by_state"] = {f"docstatus{r.docstatus}": 0 for r in legacy}
for r in legacy:
    res["legacy_by_state"][f"docstatus{r.docstatus}"] += 1
res["patch_log_ppc"] = sorted(db.get_all("Patch Log", filters={"patch": ["like", "ppc_production_control%"]}, pluck="patch"))
res["capability_rows"] = sorted(db.get_all("PPC Capability Role", filters={"parent": "PPC Settings"}, pluck="capability", distinct=True)) if db.table_exists("PPC Capability Role") else None
res["audit_rows"] = db.count("PPC Audit Log")
res["installed_apps"] = frappe.get_installed_apps()
res["frappe_version"] = frappe.__version__
res["erpnext_version"] = frappe.get_attr("erpnext.__version__")
print(json.dumps(res, indent=1, default=str))
if out:
    with open(out, "w") as f:
        json.dump(res, f, indent=1, default=str)
frappe.destroy()
