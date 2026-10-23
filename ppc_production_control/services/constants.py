"""Shared constants for PPC Production Control."""

# The plan is ERPNext's own Production Plan; PPC adds fields, connections and a control screen to it.
PLAN_DT = "Production Plan"
AUDIT_DT = "PPC Audit Log"
SETTINGS_DT = "PPC Settings"

ROLE_MANAGER = "PPC Manager"
ROLE_PLANNER = "Production Planner"
ROLE_SUPERVISOR = "Production Supervisor"
ROLE_VIEWER = "PPC Viewer"
PPC_ROLES = [ROLE_MANAGER, ROLE_PLANNER, ROLE_SUPERVISOR, ROLE_VIEWER]

# Capability names are the option values of the "PPC Capability Role" doctype.
CAP_VIEW = "View"
CAP_MANAGE_PLANS = "Manage Plans"
CAP_CREATE_WO = "Create Work Orders"
CAP_SUBMIT_WO = "Submit Work Orders"
CAP_SUBMIT_JC = "Submit Job Cards"
CAP_RESCHEDULE = "Reschedule"
CAP_OVERRIDE = "Override Conflicts"
CAP_RETRY = "Retry Failed Operations"

DEFAULT_CAPABILITY_ROLES = {
	CAP_VIEW: PPC_ROLES,
	CAP_MANAGE_PLANS: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_CREATE_WO: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_SUBMIT_WO: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_SUBMIT_JC: [ROLE_MANAGER, ROLE_PLANNER, ROLE_SUPERVISOR],
	CAP_RESCHEDULE: [ROLE_MANAGER, ROLE_PLANNER],
	CAP_OVERRIDE: [ROLE_MANAGER],
	CAP_RETRY: [ROLE_MANAGER, ROLE_PLANNER],
}

# Work Order statuses ---------------------------------------------------------------------------
WO_LOCKED_STATUSES = ("Completed", "Stopped", "Closed", "Cancelled")

# Job Card statuses ---------------------------------------------------------------------------
JC_DONE_STATUSES = ("Completed", "Submitted", "Cancelled")
# A Job Card can only be re-scheduled while ERPNext has not started or finished it.
JC_RESCHEDULABLE_STATUSES = ("Open", "Material Transferred", "On Hold")

# Audit ---------------------------------------------------------------------------------------
A_CREATE_WO = "Create Work Order"
A_SUBMIT_WO = "Submit Work Order"
A_GENERATE_JC = "Generate Job Cards"
A_SYNC_JC = "Sync Job Cards"
A_SUBMIT_JC = "Submit Job Card"
A_RESCHEDULE = "Reschedule Job Card"
A_SYNC_WO_SCHEDULE = "Sync Work Order Schedule"
A_AUTO_SCHEDULE = "Auto Schedule"
A_RETRY = "Retry"

R_SUCCESS = "Success"
R_FAILED = "Failed"
R_SKIPPED = "Skipped"
R_WARNING = "Warning"

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
