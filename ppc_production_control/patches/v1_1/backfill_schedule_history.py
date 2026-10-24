import frappe


def execute():
	"""Give Job Cards that carry a PPC 1.0.0 baseline (`ppc_original_*`) the revision-history rows they lack.

	Adds revision 0 from the existing baseline and, if the schedule has moved since, one explanatory
	"Migration" revision. Never changes a baseline or a schedule; cards without any baseline are left to the
	preview-first "Initialize Missing Baselines" action. Idempotent: cards that already have history are skipped.

	Order matters: patches run *before* the `after_migrate` hook that creates PPC's Job Card fields, so the
	fields this patch reads and writes would not exist yet on a site upgraded from 1.0.0. The patch therefore
	creates them itself (idempotent) before touching anything."""
	from ppc_production_control.install import ensure_custom_fields

	if not frappe.db.table_exists("PPC Schedule Revision"):  # DocType not synced yet: nothing can be done
		return
	ensure_custom_fields()
	names = frappe.db.sql(
		"""select jc.name from `tabJob Card` jc
		where jc.ppc_original_start is not null and jc.docstatus < 2
		and not exists (select 1 from `tabPPC Schedule Revision` r where r.job_card = jc.name)""",
		pluck=True,
	)
	added = backfill(names)
	if added:
		print(f"PPC: added schedule history to {added} Job Card(s)")


def backfill(job_cards) -> int:
	"""The work of the patch for an explicit list of Job Cards (tests use this so they only touch their own data)."""
	from ppc_production_control.services import revisions

	return sum(1 for name in job_cards if revisions.backfill_history(name))
