"""Install / migrate hooks.

Everything here is idempotent: `after_migrate` runs on every `bench migrate` (and therefore on
every Frappe Cloud deploy), so re-creating the same roles / custom fields is a no-op.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ppc_production_control.services.constants import (
	DEFAULT_CAPABILITY_ROLES,
	PPC_ROLES,
	SETTINGS_DT,
)

CUSTOM_FIELDS = {
	"Production Plan": [
		{
			"fieldname": "ppc_section",
			"label": "PPC Production Control",
			"fieldtype": "Section Break",
			"insert_after": "total_produced_qty",
			"collapsible": 1,
		},
		{
			"fieldname": "ppc_auto_create_work_orders",
			"label": "Auto Create Draft Work Orders on Submit",
			"fieldtype": "Check",
			"default": "1",
			"insert_after": "ppc_section",
			"no_copy": 1,
			"description": "When the plan is submitted, Work Orders are created automatically as drafts "
			"(the same Work Orders the 'Work Order / Subcontract PO' button would make).",
		},
		{
			"fieldname": "ppc_auto_submit_work_orders",
			"label": "Auto Submit Work Orders",
			"fieldtype": "Check",
			"default": "0",
			"insert_after": "ppc_auto_create_work_orders",
			"no_copy": 1,
			"description": "Also submit them (ERPNext then generates the Job Cards).",
		},
		{
			"fieldname": "ppc_column_break",
			"fieldtype": "Column Break",
			"insert_after": "ppc_auto_submit_work_orders",
		},
		{
			"fieldname": "ppc_planner",
			"label": "Production Planner",
			"fieldtype": "Link",
			"options": "User",
			"insert_after": "ppc_column_break",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "ppc_priority",
			"label": "Priority",
			"fieldtype": "Select",
			"options": "\nLow\nMedium\nHigh\nUrgent",
			"default": "Medium",
			"insert_after": "ppc_planner",
			"allow_on_submit": 1,
		},
		{
			"fieldname": "ppc_last_scheduling_sync",
			"label": "Last Scheduling Sync",
			"fieldtype": "Datetime",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "ppc_priority",
		},
	],
	"Job Card": [
		{
			"fieldname": "ppc_production_plan",
			"label": "Production Plan",
			"fieldtype": "Link",
			"options": "Production Plan",
			"insert_after": "work_order",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"search_index": 1,
			"description": "Set by PPC Production Control from the Work Order. Do not edit.",
		},
		{
			"fieldname": "ppc_original_start",
			"label": "Original Start (PPC)",
			"fieldtype": "Datetime",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "expected_end_date",
			"description": "First schedule PPC saw for this Job Card; the current schedule is Expected Start/End.",
		},
		{
			"fieldname": "ppc_original_end",
			"label": "Original End (PPC)",
			"fieldtype": "Datetime",
			"read_only": 1,
			"no_copy": 1,
			"allow_on_submit": 1,
			"insert_after": "ppc_original_start",
		},
	],
}


def _job_card_schedule_fields() -> list[dict]:
	"""Read-only bookkeeping of the baseline / revision model (see services/revisions.py)."""
	spec = [
		("ppc_baseline_source", "Baseline Source (PPC)", "Select", {"options": "\nCreated\nInitialized from existing schedule\nAuto-scheduled"}),
		("ppc_baseline_captured_at", "Baseline Captured At (PPC)", "Datetime", {}),
		("ppc_baseline_captured_by", "Baseline Captured By (PPC)", "Link", {"options": "User"}),
		("ppc_revised_start", "Accepted Start (PPC)", "Datetime", {"description": "Last schedule accepted through PPC; compared with Expected Start to detect manual drift."}),
		("ppc_revised_end", "Accepted End (PPC)", "Datetime", {}),
		("ppc_schedule_revision", "Schedule Revision (PPC)", "Int", {"default": "0"}),
		("ppc_last_rescheduled_at", "Last Rescheduled At (PPC)", "Datetime", {}),
		("ppc_last_rescheduled_by", "Last Rescheduled By (PPC)", "Link", {"options": "User"}),
		("ppc_schedule_sync_status", "Schedule Sync Status (PPC)", "Select", {"options": "\nSynced\nFailed"}),
	]
	out, previous = [], "ppc_original_end"
	for fieldname, label, fieldtype, extra in spec:
		out.append(
			{
				"fieldname": fieldname,
				"label": label,
				"fieldtype": fieldtype,
				"insert_after": previous,
				"read_only": 1,
				"no_copy": 1,
				"allow_on_submit": 1,
				**extra,
			}
		)
		previous = fieldname
	return out


CUSTOM_FIELDS["Job Card"].extend(_job_card_schedule_fields())


def _recommendation_fields() -> dict:
	"""Machine recommendation: where a Sales Order line keeps its machine choice, how it travels to the Production Plan and the Work Order,
	and the explicit list of Workstations an Operation may run on (used only when ERPNext's own Workstation Type is not enough)."""
	return {
		"Operation": [
			{"fieldname": "ppc_compat_section", "label": "PPC Machine Recommendation", "fieldtype": "Section Break", "insert_after": "total_operation_time", "collapsible": 1},
			{
				"fieldname": "ppc_workstations", "label": "PPC Compatible Workstations", "fieldtype": "Table", "options": "PPC Operation Workstation",
				"insert_after": "ppc_compat_section",
				"description": "Optional. The Workstations that can perform this operation. When the list is empty, PPC uses the BOM operation's Workstation Type "
				"(and its own Workstation). A machine is never assumed to be compatible from its name.",
			},
		],
		"Sales Order Item": [
			{"fieldname": "ppc_machine_section", "label": "Machine Recommendation", "fieldtype": "Section Break", "insert_after": "project", "collapsible": 0},
			{"fieldname": "ppc_find_best_machine", "label": "Find Best Machine", "fieldtype": "Button", "insert_after": "ppc_machine_section", "allow_on_submit": 1},
			{
				"fieldname": "ppc_machine_preference", "label": "Machine Preference", "fieldtype": "Select", "options": "\nRecommended\nManual\nNo Preference",
				"insert_after": "ppc_find_best_machine", "allow_on_submit": 1, "no_copy": 1,
				"description": "Recommended: the machine PPC suggested. Manual: another compatible machine was chosen. A planning preference, not a reservation.",
			},
			{"fieldname": "ppc_preferred_workstation", "label": "Selected / Preferred Workstation", "fieldtype": "Link", "options": "Workstation", "insert_after": "ppc_machine_preference",
			 "allow_on_submit": 1, "no_copy": 1},
			{"fieldname": "ppc_machine_col", "fieldtype": "Column Break", "insert_after": "ppc_preferred_workstation"},
			{"fieldname": "ppc_est_start", "label": "Estimated Production Start", "fieldtype": "Datetime", "read_only": 1, "allow_on_submit": 1, "no_copy": 1, "insert_after": "ppc_machine_col"},
			{"fieldname": "ppc_est_completion", "label": "Estimated Production Completion", "fieldtype": "Datetime", "read_only": 1, "allow_on_submit": 1, "no_copy": 1, "insert_after": "ppc_est_start",
			 "description": "An estimate from the BOM times and the current schedule. Not the delivery date and not a commitment."},
			{
				"fieldname": "ppc_recommendation_status", "label": "Recommendation Status", "fieldtype": "Select", "read_only": 1, "allow_on_submit": 1, "no_copy": 1,
				"options": "\nNot Calculated\nRecommended\nSelected\nStale\nIncomplete Data\nUnavailable", "insert_after": "ppc_est_completion",
			},
			{"fieldname": "ppc_recommendation_updated_at", "label": "Recommendation Updated At", "fieldtype": "Datetime", "read_only": 1, "allow_on_submit": 1, "no_copy": 1,
			 "insert_after": "ppc_recommendation_status"},
			{"fieldname": "ppc_machine_plan", "label": "Machine Plan (JSON)", "fieldtype": "Long Text", "hidden": 1, "allow_on_submit": 1, "no_copy": 1, "insert_after": "ppc_recommendation_updated_at"},
		],
		"Production Plan Item": [
			{"fieldname": "ppc_machine_section", "label": "Machine Preference (from the Sales Order)", "fieldtype": "Section Break", "insert_after": "temporary_name", "collapsible": 1},
			{"fieldname": "ppc_preferred_workstation", "label": "Preferred Workstation", "fieldtype": "Link", "options": "Workstation", "insert_after": "ppc_machine_section", "no_copy": 1},
			{"fieldname": "ppc_machine_preference", "label": "Machine Preference", "fieldtype": "Select", "options": "\nRecommended\nManual\nNo Preference", "read_only": 1, "no_copy": 1,
			 "insert_after": "ppc_preferred_workstation"},
			{"fieldname": "ppc_machine_plan", "label": "Machine Plan (JSON)", "fieldtype": "Long Text", "hidden": 1, "no_copy": 1, "insert_after": "ppc_machine_preference"},
		],
		"Work Order": [
			{"fieldname": "ppc_machine_note", "label": "Machine Plan (PPC)", "fieldtype": "Small Text", "read_only": 1, "no_copy": 1, "allow_on_submit": 1, "insert_after": "production_plan_item",
			 "description": "Which preferred machines from the Sales Order were applied to the operations, and which were not (and why)."},
		],
	}


CUSTOM_FIELDS.update(_recommendation_fields())

# Fields of earlier iterations of this app; removed on migrate so no orphan columns remain.
OBSOLETE_CUSTOM_FIELDS = {
	"Work Order": ["ppc_production_plan", "ppc_plan_item", "ppc_idempotency_key"],
	"Job Card": ["ppc_plan_item"],
}


def has_app_permission() -> bool:
	"""Show the app on the apps screen only to users that hold a PPC role."""
	from ppc_production_control.services.permissions import any_ppc_role

	return any_ppc_role()


def before_install():
	ensure_roles()


def after_install():
	ensure_roles()
	ensure_custom_fields()
	ensure_settings()


def after_migrate():
	ensure_roles()
	ensure_custom_fields()
	ensure_settings()


def before_uninstall():
	"""Remove the columns this app added to ERPNext doctypes. Roles are kept (they may be assigned)."""
	for dt, fields in CUSTOM_FIELDS.items():
		for df in fields:
			name = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": df["fieldname"]})
			if name:
				frappe.delete_doc("Custom Field", name, force=True)
	frappe.clear_cache()


def ensure_roles():
	for role in PPC_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{"doctype": "Role", "role_name": role, "desk_access": 1, "is_custom": 1}
			).insert(ignore_permissions=True)


def ensure_custom_fields():
	remove_obsolete_custom_fields()
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)


def remove_obsolete_custom_fields():
	for dt, fieldnames in OBSOLETE_CUSTOM_FIELDS.items():
		for fieldname in fieldnames:
			name = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname})
			if name:
				frappe.delete_doc("Custom Field", name, force=True)


def ensure_settings():
	"""Seed the capability table once; later edits by administrators are never overwritten."""
	settings = frappe.get_single(SETTINGS_DT)
	if settings.get("capability_roles"):
		return
	for capability, roles in DEFAULT_CAPABILITY_ROLES.items():
		for role in roles:
			settings.append("capability_roles", {"capability": capability, "role": role})
	settings.flags.ignore_permissions = True
	settings.save()
