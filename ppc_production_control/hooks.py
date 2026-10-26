app_name = "ppc_production_control"
app_title = "PPC Production Control"
app_publisher = "PPC Production Control"
app_description = (
	"Production Planning & Control for ERPNext v15: works on top of the standard Production Plan - "
	"automatic draft Work Orders, Job Card tracking and an interactive Gantt schedule."
)
app_email = "admin@example.com"
app_license = "MIT"

# Apps
# ------------------
required_apps = ["erpnext"]

add_to_apps_screen = [
	{
		"name": "ppc_production_control",
		"logo": "/assets/ppc_production_control/images/ppc-logo.svg",
		"title": "PPC Production Control",
		"route": "/app/ppc-control-center",
		"has_permission": "ppc_production_control.install.has_app_permission",
	}
]

# Colour theme of the workspace and the settings form (every rule is scoped to those two routes)
app_include_css = ["ppc_theme.bundle.css"]

# Installation
# ------------
before_install = "ppc_production_control.install.before_install"
after_install = "ppc_production_control.install.after_install"
after_migrate = ["ppc_production_control.install.after_migrate"]
before_uninstall = "ppc_production_control.install.before_uninstall"

# Standard Production Plan form: PPC buttons and the embedded control panel
doctype_js = {"Production Plan": "public/js/ppc_production_plan.js"}

# "Production Control" group in the plan's Connections panel (Job Cards, audit log)
override_doctype_dashboards = {
	"Production Plan": "ppc_production_control.overrides.production_plan_dashboard.get_data",
}

# Document Events
# ---------------
# `doc_events` (not `override_doctype_class`) is used deliberately: other apps on the same site
# commonly override Work Order / Job Card classes, and only one override can win.
doc_events = {
	"Production Plan": {
		"on_submit": "ppc_production_control.events.production_plan_submitted",
	},
	"Work Order": {
		"before_insert": "ppc_production_control.events.work_order_before_insert",
		"on_submit": "ppc_production_control.events.work_order_changed",
		"on_cancel": "ppc_production_control.events.work_order_changed",
		"on_update_after_submit": "ppc_production_control.events.work_order_changed",
	},
	"Job Card": {
		"before_insert": "ppc_production_control.events.job_card_before_insert",
		"after_insert": "ppc_production_control.events.job_card_after_insert",
		"validate": "ppc_production_control.events.job_card_validate",
		"on_update": "ppc_production_control.events.job_card_changed",
		"on_submit": "ppc_production_control.events.job_card_changed",
		"on_cancel": "ppc_production_control.events.job_card_changed",
	},
}

# The audit log is append-only history: it must outlive the documents it describes, so links
# from it never block deleting a Work Order / Job Card / Production Plan.
ignore_links_on_delete = ["PPC Audit Log", "PPC Schedule Revision"]

# Scheduled Tasks
# ---------------
scheduler_events = {
	"hourly": [
		"ppc_production_control.tasks.refresh_active_plans",
	],
}

# Fixtures: roles, custom fields and default settings are created idempotently in install.py
# (before_install / after_install / after_migrate), so no fixture files need to be exported.
fixtures = []
