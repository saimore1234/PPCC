/* Extends the standard ERPNext Production Plan form with PPC Production Control.
 *
 * - "Production Control" group in Connections (Job Cards, audit log) comes from the server
 *   (override_doctype_dashboards).
 * - Buttons and an embedded control panel (Work Orders, Job Cards, Gantt) appear once the plan is submitted.
 */
frappe.ui.form.on("Production Plan", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.docstatus === 0) return;
		frappe.require(["ppc.bundle.css", "ppc_control.bundle.js"], () => frm.trigger("ppc_render"));
	},

	ppc_render(frm) {
		const ppc = window.ppc;
		if (!ppc || frm.doc.docstatus !== 1) return;
		ppc.call("get_capabilities").then((c) => {
			const caps = c.capabilities || {};
			if (!caps["View"]) return; // user has no PPC role: the standard form stays untouched
			frm._ppc_caps = caps;
			const reload = () => frm.reload_doc();
			const group = __("Production Control");
			const open = !["Closed", "Cancelled", "Completed"].includes(frm.doc.status);

			if (open && caps["Create Work Orders"]) {
				frm.add_custom_button(__("Create Draft Work Orders"), () => {
					ppc.call("create_work_orders", { plan: frm.doc.name }, { freeze: true, freeze_message: __("Creating Work Orders…") }).then((r) => {
						if (r.queued) return frappe.show_alert({ message: __("Running in the background."), indicator: "blue" });
						const failed = r.failed || [];
						frappe.show_alert({ message: __("{0} created, {1} already existed, {2} failed", [(r.created || []).length, (r.skipped || []).length, failed.length]), indicator: failed.length ? "orange" : "green" });
						if (failed.length) frappe.msgprint({ title: __("Some rows failed"), indicator: "red", message: ppc.listHtml(failed.map((f) => (f.item_code || "") + ": " + f.message)) });
						reload();
					}).catch(() => {});
				}, group);
			}
			if (caps["Retry Failed Operations"]) {
				frm.add_custom_button(__("Retry Failed"), () => ppc.call("retry_failed_operations", { plan: frm.doc.name }, { freeze: true }).then(reload).catch(() => {}), group);
			}
			if (caps["Create Work Orders"] || caps["Submit Job Cards"] || caps["Manage Plans"]) {
				frm.add_custom_button(__("Refresh Job Cards"), () => ppc.call("refresh_job_cards", { plan: frm.doc.name }, { freeze: true }).then(() => { frappe.show_alert({ message: __("Job Cards synchronised"), indicator: "green" }); reload(); }).catch(() => {}), group);
			}
			if (caps["Create Work Orders"]) {
				frm.add_custom_button(__("Generate Missing Job Cards"), () => ppc.call("generate_job_cards", { plan: frm.doc.name }, { freeze: true }).then((r) => {
					const made = r.work_orders.reduce((n, w) => n + (w.created || 0), 0);
					frappe.show_alert({ message: __("{0} Job Card(s) created", [made]), indicator: "green" });
					reload();
				}).catch(() => {}), group);
			}
			if (caps["Reschedule"]) {
				frm.add_custom_button(__("Schedule Unscheduled Operations"), () => ppc.call("schedule_unscheduled", { plan: frm.doc.name }, { freeze: true }).then((r) => {
					frappe.show_alert({ message: __("{0} operation(s) scheduled", [r.scheduled.length]), indicator: "green" });
					reload();
				}).catch(() => {}), group);
			}
			frm.add_custom_button(__("Open Gantt"), () => {
				frappe.route_options = { plan: frm.doc.name, tab: "gantt" };
				frappe.set_route("ppc-production-control");
			}, group);
			frm.add_custom_button(__("Open Production Control"), () => {
				frappe.route_options = { plan: frm.doc.name, tab: "jobcards" };
				frappe.set_route("ppc-production-control");
			}, group);

			// embedded panel (Work Orders / Job Cards / Gantt) right under the connections
			if (!frm.dashboard || !frm.dashboard.add_section) return;
			const $section = frm.dashboard.add_section('<div class="ppc-embed ppc"></div>', __("Production Control"));
			const $host = $section.find ? $section.find(".ppc-embed") : null;
			if (!$host || !$host.length) return;
			frm._ppc_panel = new ppc.ControlPanel($host, {
				showFilters: false, fixedFilters: { plan: frm.doc.name }, caps: caps, batchSize: c.bulk_batch_size, maxTasks: c.max_gantt_tasks,
			});
			frm._ppc_panel.reload();
			if (!frm._ppc_listening) {
				frm._ppc_listening = true;
				frappe.realtime.on("ppc_progress", (data) => {
					if (data && data.plan === frm.doc.name && data.finished) {
						frappe.show_alert({ message: __("Draft Work Orders created: {0}, failed: {1}", [data.created, data.failed]), indicator: data.failed ? "orange" : "green" });
						frm.reload_doc();
					}
				});
			}
		}).catch(() => {});
	},
});
