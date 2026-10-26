import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from ppc_production_control.services.constants import DEFAULT_CAPABILITY_ROLES


class PPCSettings(Document):
	def validate(self):
		self.validate_capabilities()
		for field, minimum in (("bulk_batch_size", 1), ("max_gantt_tasks", 50), ("default_operation_minutes", 1)):
			if (self.get(field) or 0) < minimum:
				frappe.throw(_("{0} must be at least {1}.").format(self.meta.get_label(field), minimum))
		self.validate_recommendation()
		self.bulk_batch_size = min(self.bulk_batch_size, 200)
		self.max_gantt_tasks = min(self.max_gantt_tasks, 5000)

	def validate_recommendation(self):
		weights = ("rec_weight_completion", "rec_weight_load", "rec_weight_delivery", "rec_weight_cost", "rec_weight_efficiency")
		# A site upgraded from before 1.5.0 has nothing stored for these fields yet (a Single's defaults are not written by a migrate):
		# take the defaults instead of refusing to save the settings.
		if not any(self.get(f) for f in weights) and not self.get("rec_horizon_days") and not self.get("rec_load_window_days"):
			for field in (*weights, "rec_horizon_days", "rec_load_window_days", "rec_unavailable_tolerance_hours"):
				self.set(field, cint(self.meta.get_field(field).default))
		for field in weights:
			if (self.get(field) or 0) < 0:
				frappe.throw(_("{0} cannot be negative.").format(self.meta.get_label(field)))
		if sum((self.get(f) or 0) for f in weights) <= 0:
			frappe.throw(_("At least one recommendation weight must be above zero."))
		for field, low, high in (("rec_horizon_days", 7, 730), ("rec_load_window_days", 1, 60), ("rec_unavailable_tolerance_hours", 0, 24 * 90)):
			value = self.get(field)
			if value is None or value < low or value > high:
				frappe.throw(_("{0} must be between {1} and {2}.").format(self.meta.get_label(field), low, high))

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
