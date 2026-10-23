"""Adds a "Production Control" group to the Connections panel of the standard Production Plan."""

from frappe import _


def get_data(data):
	"""`override_doctype_dashboards` hook: receives ERPNext's dashboard dict and returns it extended.

	Work Orders already appear under "Transactions" (they carry `production_plan`). Job Cards reach
	the plan through the field PPC maintains on them (`ppc_production_plan`).
	"""
	data = data or {}
	data.setdefault("non_standard_fieldnames", {}).update(
		{"Job Card": "ppc_production_plan", "PPC Audit Log": "ppc_plan"}
	)
	data.setdefault("transactions", []).append(
		{"label": _("Production Control"), "items": ["Job Card", "PPC Audit Log"]}
	)
	return data
