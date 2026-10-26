import frappe
from frappe import _
from frappe.model.document import Document


class PPCOperationWorkstation(Document):
	"""One Workstation that may perform the Operation this row belongs to (the explicit compatibility list used by the machine recommendation)."""

	def validate(self):
		if self.workstation and not frappe.db.exists("Workstation", self.workstation):
			frappe.throw(_("Workstation {0} does not exist.").format(self.workstation))
