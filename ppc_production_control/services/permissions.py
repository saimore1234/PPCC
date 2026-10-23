"""Role/capability checks for PPC actions.

Two independent layers are applied to every PPC API call:

1. *Capability* - may this user perform the PPC action at all (configurable in PPC Settings)?
2. *Document permission* - may this user do it on this particular ERPNext document
   (Work Order, Job Card, Sales Order ...)? That is always checked with the regular Frappe
   permission engine, so ERPNext's own role / user-permission rules continue to apply.
"""

import frappe
from frappe import _

from ppc_production_control.services.constants import (
	CAP_OVERRIDE,
	DEFAULT_CAPABILITY_ROLES,
	PPC_ROLES,
	ROLE_PLANNER,
	SETTINGS_DT,
)


def _is_admin(roles: list[str], user: str) -> bool:
	return user == "Administrator" or "System Manager" in roles


def get_capability_roles() -> dict[str, list[str]]:
	"""Capability -> roles, from PPC Settings; falls back to the shipped defaults for any
	capability that has no row configured."""
	mapping: dict[str, set[str]] = {}
	try:
		settings = frappe.get_single(SETTINGS_DT)
		for row in settings.get("capability_roles") or []:
			mapping.setdefault(row.capability, set()).add(row.role)
	except Exception:
		# Settings not migrated yet (e.g. during install): use the defaults.
		mapping = {}
	for capability, roles in DEFAULT_CAPABILITY_ROLES.items():
		mapping.setdefault(capability, set(roles))
	return {k: sorted(v) for k, v in mapping.items()}


def has_capability(capability: str, user: str | None = None) -> bool:
	user = user or frappe.session.user
	roles = frappe.get_roles(user)
	if _is_admin(roles, user):
		return True
	allowed = set(get_capability_roles().get(capability, []))
	if capability == CAP_OVERRIDE and ROLE_PLANNER in roles:
		if frappe.get_single(SETTINGS_DT).planner_can_override_conflicts:
			return True
	return bool(allowed.intersection(roles))


def require_capability(capability: str, user: str | None = None):
	if not has_capability(capability, user):
		frappe.throw(
			_("You are not permitted to perform this action ({0}). Ask an administrator to grant the PPC role."
			).format(_(capability)),
			frappe.PermissionError,
		)


def check_doc_permission(doctype: str, name: str | None, ptype: str = "read"):
	"""Standard document-level check (role + user permission + sharing)."""
	if not frappe.has_permission(doctype, ptype, doc=name):
		frappe.throw(
			_("No {0} permission on {1} {2}").format(_(ptype), _(doctype), name or ""),
			frappe.PermissionError,
		)


def check_doctype_permission(doctype: str, ptype: str = "read"):
	if not frappe.has_permission(doctype, ptype):
		frappe.throw(_("No {0} permission on {1}").format(_(ptype), _(doctype)), frappe.PermissionError)


def user_capabilities(user: str | None = None) -> dict[str, bool]:
	"""Used by the UI to hide buttons the user cannot use. The server still enforces every check."""
	return {cap: has_capability(cap, user) for cap in DEFAULT_CAPABILITY_ROLES}


def any_ppc_role(user: str | None = None) -> bool:
	roles = set(frappe.get_roles(user or frappe.session.user))
	return bool(roles.intersection(PPC_ROLES)) or "System Manager" in roles
