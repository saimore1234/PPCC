import frappe
from frappe import _
from frappe.model.document import Document

# The only fields an audited correction may touch.
CORRECTION_FIELDS = ("corrected", "correction_reason", "corrected_by")


class PPCScheduleRevision(Document):
	"""One accepted schedule of a Job Card (revision 0 is the baseline).

	The history is append-only: rows are created by the scheduling service in the same transaction
	as the schedule change they describe, and normal users and APIs can neither edit nor delete them.
	The single exception is the audited correction process (`services/revisions.correct_revision`),
	which may only annotate a row, never change a date.
	"""

	def validate(self):
		if self.is_new():
			if not self.flags.ppc_revision_write:
				frappe.throw(
					_("Schedule revisions are created by PPC when a schedule is accepted, not by hand."),
					frappe.PermissionError,
				)
			return
		if self.flags.ppc_audited_correction:
			before = self.get_doc_before_save()
			changed = [
				df.fieldname
				for df in self.meta.fields
				if df.fieldtype not in ("Section Break", "Column Break")
				and df.fieldname not in CORRECTION_FIELDS
				and before is not None
				and self.get(df.fieldname) != before.get(df.fieldname)
			]
			if changed:
				frappe.throw(_("A correction may only annotate a revision; these fields cannot change: {0}").format(", ".join(changed)))
			return
		frappe.throw(_("Schedule revisions cannot be modified."), frappe.PermissionError)

	def on_trash(self):
		if not self.flags.ppc_audited_correction:
			frappe.throw(_("Schedule revisions cannot be deleted."), frappe.PermissionError)
