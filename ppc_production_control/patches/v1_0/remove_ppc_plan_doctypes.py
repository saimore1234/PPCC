import frappe

OBSOLETE_DOCTYPES = [
	"PPC Production Plan",
	"PPC Production Plan Item",
	"PPC Plan Sales Order",
	"PPC Plan Item Source",
	"PPC Work Order Link",
	"PPC Job Card Link",
]
# columns of the removed custom fields (Frappe leaves them behind when a Custom Field is deleted)
OBSOLETE_COLUMNS = {
	"Work Order": ["ppc_production_plan", "ppc_plan_item", "ppc_idempotency_key"],
	"Job Card": ["ppc_plan_item"],
}


def execute():
	"""Earlier iterations had their own plan document. The standard Production Plan is used now, so the
	old DocTypes, their tables, and the columns they left on Work Order / Job Card are removed.

	Nothing is dropped unless it is empty: a table with rows or a column with values is kept (and
	reported in the Error Log) so no data can be lost. Safe to re-run.
	"""
	for name in OBSOLETE_DOCTYPES:
		if frappe.db.exists("DocType", name):
			frappe.delete_doc("DocType", name, force=True, ignore_permissions=True, delete_permanently=True)
		table = f"tab{name}"
		if frappe.db.sql("show tables like %s", table):
			if frappe.db.sql(f"select 1 from `{table}` limit 1"):
				frappe.log_error(title="PPC cleanup kept a non-empty table", message=table)
			else:
				frappe.db.sql_ddl(f"drop table `{table}`")

	from ppc_production_control.install import remove_obsolete_custom_fields

	remove_obsolete_custom_fields()
	for doctype, columns in OBSOLETE_COLUMNS.items():
		table = f"tab{doctype}"
		for col in columns:
			if not frappe.db.sql(f"show columns from `{table}` like %s", col):
				continue
			if frappe.db.sql(f"select 1 from `{table}` where `{col}` is not null and `{col}` != '' limit 1"):
				frappe.log_error(title="PPC cleanup kept a column that holds data", message=f"{table}.{col}")
				continue
			frappe.db.sql_ddl(f"alter table `{table}` drop column `{col}`")
	frappe.clear_cache()
