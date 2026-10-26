// Entry point of the PPC front-end bundle (built by `bench build`, loaded with frappe.require("ppc_control.bundle.js")).
// Order matters: each file reads what the previous ones put on `window`.
import "../gantt/ppc_gantt.js";
import "./ppc_common.js";
import "./ppc_ui_lib.js";
import "./ppc_shell.js";
import "./ppc_widgets.js";
import "./ppc_pages.js";
