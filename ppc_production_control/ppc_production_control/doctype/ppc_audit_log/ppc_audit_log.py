import frappe
from frappe import _
from frappe.model.document import Document


class PPCAuditLog(Document):
	"""Append-only audit trail. Rows are written by the PPC services (`services/audit.py`) and are
	never edited through the UI: every field is read-only and no role has write permission."""

	def before_save(self):
		if not self.is_new() and not frappe.flags.in_migrate and not self.flags.ppc_system_update:
			frappe.throw(_("Audit log entries cannot be modified."), frappe.PermissionError)
