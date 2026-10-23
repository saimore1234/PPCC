import frappe
from frappe import _
from frappe.model.document import Document

from ppc_production_control.services.constants import DEFAULT_CAPABILITY_ROLES


class PPCSettings(Document):
	def validate(self):
		self.validate_capabilities()
		for field, minimum in (("bulk_batch_size", 1), ("max_gantt_tasks", 50), ("default_operation_minutes", 1)):
			if (self.get(field) or 0) < minimum:
				frappe.throw(_("{0} must be at least {1}.").format(self.meta.get_label(field), minimum))
		self.bulk_batch_size = min(self.bulk_batch_size, 200)
		self.max_gantt_tasks = min(self.max_gantt_tasks, 5000)

	def validate_capabilities(self):
		seen = set()
		for row in self.capability_roles:
			if row.capability not in DEFAULT_CAPABILITY_ROLES:
				frappe.throw(_("Row {0}: unknown capability {1}").format(row.idx, row.capability))
			key = (row.capability, row.role)
			if key in seen:
				frappe.throw(_("Row {0}: {1} is already assigned to {2}.").format(row.idx, row.role, row.capability))
			seen.add(key)
		# A site that can no longer view or manage anything would lock every non-admin out.
		configured = {row.capability for row in self.capability_roles}
		if self.capability_roles and "View" not in configured:
			frappe.throw(_("At least one role must keep the 'View' capability."))
