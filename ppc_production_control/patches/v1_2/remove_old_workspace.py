import frappe


def execute():
	"""The workspace "PPC" became "PPC Control Center". Remove the old one so users do not see two PPC entries.

	Only the standard workspace this app shipped is removed (public, from this app's module); a workspace a user created
	with the same name by hand is left alone. Idempotent."""
	if not frappe.db.exists("Workspace", "PPC") or not frappe.db.exists("Workspace", "PPC Control Center"):
		return  # nothing to replace, or the new one is not there yet: never leave a site without a PPC workspace
	old = frappe.db.get_value("Workspace", "PPC", ["module", "public"], as_dict=True)
	if old.module == "PPC Production Control" and old.public:
		frappe.delete_doc("Workspace", "PPC", force=True, ignore_permissions=True)
		print("PPC: removed the old 'PPC' workspace (replaced by 'PPC Control Center')")
