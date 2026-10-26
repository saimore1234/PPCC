/* Extends the standard ERPNext Production Plan form with PPC.
 *
 * - "Production Control" group in Connections (Job Cards, audit log) comes from the server (override_doctype_dashboards).
 * - Once the plan is submitted: a "Production Control" button group (create/retry/refresh actions and shortcuts into the
 *   PPC Control Center) and a short summary line with links. The dashboard, tables and Gantt live in the Control Center
 *   pages, not on this form.
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
			const center = __("Open in PPC Control Center");
			[["workorders", __("Work Orders")], ["jobcards", __("Job Cards")], ["gantt", __("Production Gantt")], ["exceptions", __("Exceptions")], ["history", __("Schedule History")]].forEach((p) => {
				frm.add_custom_button(p[1], () => ppc.go(p[0], { plan: frm.doc.name }), center);
			});
			frm.add_custom_button(__("Dashboard"), () => ppc.go("dashboard"), center);

			// a short summary with links (no embedded dashboard, tables or Gantt)
			if (frm.dashboard && frm.dashboard.add_section) {
				ppc.call("get_plan_overview", { plan: frm.doc.name }).then((o) => {
					const p = o.plan;
					const stat = (v, l, extra) => '<span class="ppc-ps-stat"><b>' + ppc.esc(v) + "</b> " + ppc.esc(l) + (extra ? ' <span class="text-muted">' + extra + "</span>" : "") + "</span>";
					const link = (page, label, n) => '<a href="#" class="ppc-ps-link" data-page="' + page + '">' + label + (n != null ? " (" + n + ")" : "") + " →</a>";
					const html = '<div class="ppc ppc-plan-summary">' +
						'<div class="ppc-ps-stats">' +
						stat(p.total_work_orders, __("Work Orders"), p.draft_work_orders ? p.draft_work_orders + " " + __("draft") : "") +
						stat(p.total_job_cards, __("Job Cards")) + stat(ppc.num(p.total_planned_qty), __("planned qty")) +
						(p.last_scheduling_sync ? stat(ppc.dt(p.last_scheduling_sync), __("last sync")) : "") +
						(o.has_pending ? '<span class="ppc-flag warn">' + __("Some rows have no Work Order yet") + "</span>" : "") +
						(o.has_failures ? '<span class="ppc-flag bad">⚠ ' + __("failures – see Exceptions") + "</span>" : "") +
						"</div>" +
						'<div class="ppc-ps-links">' + link("workorders", __("Work Orders"), p.total_work_orders) + link("jobcards", __("Job Cards"), p.total_job_cards) + link("gantt", __("Production Gantt")) + link("exceptions", __("Exceptions")) + "</div></div>";
					const $section = frm.dashboard.add_section(html, __("Production Control"));
					$section.on("click", ".ppc-ps-link", (e) => { e.preventDefault(); ppc.go($(e.currentTarget).data("page"), { plan: frm.doc.name }); });
				}).catch(() => {});
			}
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
