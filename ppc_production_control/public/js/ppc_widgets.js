/* PPC Control Center - reschedule flow, table column definitions, bulk runner and preview-first repair actions.
 * (The pages themselves are in ppc_pages.js; shared components in ppc_shell.js.) */
(function () {
	"use strict";
	const ppc = window.ppc;

	/* ======================================================================= tooltip html (Gantt bars) */
	ppc.taskTooltip = function (t, h) {
		const row = (k, v) => (v ? "<tr><td>" + k + "</td><td>" + v + "</td></tr>" : "");
		const e = h.esc;
		return (
			"<b>" + e(t.name) + "</b> " + ppc.badge(t.status) + "<table>" +
			row(__("Operation"), e(t.operation) + (t.sequence_id ? " (#" + e(t.sequence_id) + ")" : "")) +
			row(__("Finished good"), e(t.item_code) + (t.item_name ? " – " + e(t.item_name) : "")) +
			row(__("Work Order"), e(t.work_order)) + row(__("Sales Order"), e(t.sales_order)) + row(__("Customer"), e(t.customer_name)) +
			row(__("Workstation"), e(t.workstation || __("(none)"))) +
			row(__("Planned qty"), ppc.num(t.planned_qty) + " (" + __("done") + " " + ppc.num(t.completed_qty) + ")") +
			row(__("Scheduled"), ppc.dt(t.revised_start) + " → " + ppc.dt(t.revised_end) + " (" + h.formatDuration(window.PPCGanttLib.math.parse(t.revised_end) - window.PPCGanttLib.math.parse(t.revised_start)) + ")") +
			(t.original_start && (t.original_start !== t.revised_start || t.original_end !== t.revised_end) ? row(__("Original"), ppc.dt(t.original_start) + " → " + ppc.dt(t.original_end) + (t.baseline_label ? ' <span class="text-muted">(' + e(t.baseline_label) + ")</span>" : "")) : "") +
			(t.actual_start ? row(__("Actual"), ppc.dt(t.actual_start) + " → " + (t.actual_end ? ppc.dt(t.actual_end) : __("in progress"))) : "") +
			(t.revision > 0 ? row(__("Revisions"), e(t.revision) + (t.last_rescheduled_by ? " – " + __("last by {0}", [e(t.last_rescheduled_by)]) : "")) : "") +
			(t.difference ? row(__("Change"), e(t.difference)) : "") +
			(t.sync_status && t.sync_status !== "Synced" ? row(__("Sync"), e(t.sync_status)) : "") +
			(t.is_estimated ? row(__("Dates"), "<i>" + __("estimated – not saved yet") + "</i>") : "") +
			row(__("Priority"), e(t.priority)) +
			(t.conflict ? row(__("Conflict"), '<span class="ppc-flag bad">' + e(t.conflict_reasons.join("; ")) + "</span>") : "") +
			(t.overdue ? row(__("Overdue"), '<span class="ppc-flag bad">' + __("past its end date") + "</span>") : "") +
			(!t.movable ? row(__("Locked"), e(t.lock_reason || "")) : "") +
			"</table>"
		);
	};

	/* ======================================================================= reschedule flow */
	/**
	 * Save a new schedule. Walks the user through warnings/overrides; resolves {ok, start, end, modified, stale}.
	 */
	ppc.reschedule = async function (task, start, end, mode, preset) {
		const args = Object.assign(
			{ job_card: task.name, start: start, end: end, mode: mode, expected_modified: task.modified, confirmed: 0, override_conflicts: 0, shift_dependents: 0 },
			preset || {}
		);
		for (let round = 0; round < 3; round++) {
			let r;
			try { r = await ppc.call("reschedule_job_card", args, { freeze: true, freeze_message: __("Saving schedule…") }); } catch (e) { return { ok: false }; }
			if (r.ok) {
				frappe.show_alert({ message: __("Rescheduled {0}", [task.name]) + (r.changed.length > 1 ? " (+" + (r.changed.length - 1) + " " + __("following operations") + ")" : ""), indicator: "green" });
				const mine = r.changed.find((c) => c.job_card === task.name) || {};
				return { ok: true, start: mine.new_start || start, end: mine.new_end || end, others: r.changed.length > 1, modified: r.modified };
			}
			if (r.errors && r.errors.length) {
				frappe.msgprint({ title: r.stale ? __("Schedule changed by someone else") : __("Cannot reschedule"), indicator: "red", message: ppc.listHtml(r.errors) });
				return { ok: false, stale: !!r.stale };
			}
			if (!r.needs_confirmation) return { ok: false };
			const choice = await ppc.askSchedule(r, args);
			if (!choice) return { ok: false };
			Object.assign(args, choice, { confirmed: 1 });
		}
		return { ok: false };
	};

	ppc.askSchedule = function (r, args) {
		return new Promise(function (resolve) {
			const hasSuccessor = r.warnings.some((w) => w.code === "successor" || w.code === "cascade");
			const fields = [
				{ fieldtype: "HTML", fieldname: "list" },
				{ fieldtype: "Check", fieldname: "shift_dependents", label: __("Shift following operations to keep the sequence"), default: hasSuccessor ? 1 : 0, hidden: hasSuccessor ? 0 : 1 },
			];
			if (r.needs_override && r.can_override) {
				fields.push({ fieldtype: "Check", fieldname: "override_conflicts", label: __("I accept these conflicts and want to save anyway (override)") });
			} else if (r.needs_override) {
				fields.push({ fieldtype: "HTML", fieldname: "no_override", options: '<p class="text-danger">' + __("You are not allowed to override scheduling conflicts. Choose other dates, shift the following operations, or ask a PPC Manager.") + "</p>" });
			}
			fields.push({ fieldtype: "Small Text", fieldname: "reason", label: __("Reason for the change"), default: args.reason || "" });
			const d = new frappe.ui.Dialog({
				title: __("Confirm schedule change"),
				fields: fields,
				primary_action_label: __("Save"),
				primary_action: function (v) {
					d.hide();
					resolve({ shift_dependents: v.shift_dependents ? 1 : 0, override_conflicts: v.override_conflicts ? 1 : 0, reason: v.reason || "" });
				},
				secondary_action_label: __("Cancel"),
				secondary_action: function () { d.hide(); resolve(null); },
			});
			d.fields_dict.list.$wrapper.html(
				"<ul class='ppc-warnlist'>" + r.warnings.map((w) => "<li class='" + (w.needs_override ? "must" : "") + "'>" + ppc.esc(w.message) + "</li>").join("") + "</ul>"
			);
			d.$wrapper.on("hidden.bs.modal", function () { resolve(null); }); // closing the dialog = cancel
			ppc.skin(d); d.show();
		});
	};

	/** Dialog to type dates (keyboard-accessible alternative to dragging). */
	ppc.editDates = function (task, onDone) {
		const d = new frappe.ui.Dialog({
			title: __("Reschedule {0}", [task.name]),
			fields: [
				{ fieldtype: "HTML", fieldname: "info", options: "<p class='text-muted'>" + ppc.esc(task.operation) + " · " + ppc.esc(task.item_code || "") + " · " + ppc.esc(task.workstation || "") + "</p>" },
				{ fieldtype: "Datetime", fieldname: "start", label: __("Start"), reqd: 1, default: task.revised_start },
				{ fieldtype: "Datetime", fieldname: "end", label: __("End"), reqd: 1, default: task.revised_end },
				{ fieldtype: "Small Text", fieldname: "reason", label: __("Reason") },
			],
			primary_action_label: __("Save"),
			primary_action: async function (v) {
				d.hide();
				const res = await ppc.reschedule(task, v.start, v.end, "resize", { reason: v.reason || "", source: "Date Dialog" });
				if (res.ok && onDone) onDone(res);
			},
		});
		ppc.skin(d); d.show();
	};

	/* ======================================================================= table columns */
	const two = (a, b) => (a ? '<span class="ppc-2l">' + ppc.dt(a) + '</span><span class="ppc-2l">' + (b ? ppc.dt(b) : "…") + "</span>" : "");
	const STAGE_CLASS = {
		"Draft": "st-done", "Cancelled": "bad", "Needs attention": "bad", "Awaiting Work Orders": "warn", "Work Orders drafted": "st-open",
		"Partly submitted": "st-hold", "Work Orders submitted": "st-material", "In production": "st-wip", "Completed": "ok",
	};
	ppc.stageBadge = (s) => (s ? ppc.badge(s, STAGE_CLASS[s] || "") : "");

	ppc.columns = {
		plans: () => [
			{ key: "name", label: __("Production Plan"), sort: "name", html: (r) => ppc.link("Production Plan", r.name) },
			{ key: "company", label: __("Company"), sort: "company" },
			{ key: "posting_date", label: __("Posting Date"), sort: "posting_date", html: (r) => ppc.dt(r.posting_date) },
			{ key: "status", label: __("Status"), sort: "status", html: (r) => ppc.badge(r.status) },
			{ key: "planner", label: __("Planner"), sort: "planner" },
			{ key: "sales_orders", label: __("Sales Orders"), html: (r) => (r.sales_orders || []).map((s) => ppc.link("Sales Order", s)).join(", ") + (r.sales_order_count > r.sales_orders.length ? " +" + (r.sales_order_count - r.sales_orders.length) : "") },
			{ key: "fg_items", label: __("Finished Goods"), align: "num" },
			{ key: "planned_qty", label: __("Planned Qty"), sort: "planned_qty", align: "num", html: (r) => ppc.num(r.planned_qty) + (r.produced_qty ? ' <span class="text-muted">(' + ppc.num(r.produced_qty) + " " + __("made") + ")</span>" : "") },
			{ key: "work_orders", label: __("Work Orders"), sort: "work_orders", align: "num", html: (r) => r.work_orders ? '<a href="#" class="ppc-golink" data-page="workorders" data-plan="' + ppc.esc(r.name) + '">' + r.work_orders + "</a>" + (r.draft_work_orders ? ' <span class="text-muted">(' + r.draft_work_orders + " " + __("draft") + ")</span>" : "") : "0" },
			{ key: "job_cards", label: __("Job Cards"), sort: "job_cards", align: "num", html: (r) => r.job_cards ? '<a href="#" class="ppc-golink" data-page="jobcards" data-plan="' + ppc.esc(r.name) + '">' + r.job_cards + "</a>" : "0" },
			{ key: "stage", label: __("Planning Status"), html: (r) => ppc.stageBadge(r.stage) },
			{ key: "priority", label: __("Priority"), hidden: true },
			{ key: "modified", label: __("Last Updated"), sort: "modified", html: (r) => ppc.dt(r.modified) },
		],

		workOrders: () => [
			{ key: "name", label: __("Work Order"), sort: "name", html: (r) => ppc.link("Work Order", r.name) },
			{ key: "ppc_plan", label: __("Production Plan"), sort: "plan", html: (r) => ppc.link("Production Plan", r.ppc_plan) },
			{ key: "sales_order", label: __("Sales Order"), sort: "sales_order", html: (r) => ppc.link("Sales Order", r.sales_order) },
			{ key: "customer_name", label: __("Customer"), sort: "customer" },
			{ key: "item_code", label: __("Finished Good"), sort: "item_code", html: (r) => ppc.esc(r.item_code) + (r.item_name ? ' <span class="text-muted">' + ppc.esc(r.item_name) + "</span>" : "") },
			{ key: "bom_no", label: __("BOM"), html: (r) => ppc.link("BOM", r.bom_no) },
			{ key: "qty", label: __("Planned Qty"), sort: "qty", align: "num", html: (r) => ppc.num(r.qty) },
			{ key: "produced_qty", label: __("Produced"), sort: "produced_qty", align: "num", html: (r) => ppc.num(r.produced_qty) },
			{ key: "warehouses", label: __("Source → Target Warehouse"), html: (r) => ppc.esc(r.source_warehouse || r.wip_warehouse || "–") + " → " + ppc.esc(r.fg_warehouse || "–") },
			{ key: "planned", label: __("Planned Start / End"), sort: "planned_start", html: (r) => two(r.planned_start_date, r.planned_end_date) },
			{ key: "expected_delivery_date", label: __("Delivery"), sort: "delivery", hidden: true, html: (r) => ppc.dt(r.expected_delivery_date) },
			{ key: "status", label: __("Status"), sort: "status", html: (r) => ppc.badge(r.status) },
			{ key: "job_cards", label: __("Job Cards"), align: "num", html: (r) => r.job_cards ? '<a href="#" class="ppc-golink" data-page="jobcards" data-work-order="' + ppc.esc(r.name) + '">' + r.job_cards + "</a>" : "0" },
			{ key: "sync_status", label: __("Sync Status"), html: (r) => ppc.syncBadge(r.sync_status) },
			{ key: "error_message", label: __("Last Error"), wrap: true, hidden: true, html: (r) => r.error_message ? '<span class="ppc-flag bad">' + ppc.esc(r.error_message) + "</span>" : "" },
		],

		jobCards: (canReschedule) => [
			{ key: "name", label: __("Job Card"), sort: "name", html: (r) => ppc.link("Job Card", r.name) },
			{ key: "ppc_plan", label: __("Production Plan"), sort: "plan", html: (r) => ppc.link("Production Plan", r.ppc_plan) },
			{ key: "sales_order", label: __("Sales Order"), sort: "sales_order", html: (r) => ppc.link("Sales Order", r.sales_order) },
			{ key: "customer_name", label: __("Customer"), sort: "customer" },
			{ key: "item_code", label: __("Finished Good"), sort: "item_code", html: (r) => ppc.esc(r.item_code) + (r.item_name ? ' <span class="text-muted">' + ppc.esc(r.item_name) + "</span>" : "") },
			{ key: "work_order", label: __("Work Order"), sort: "work_order", html: (r) => ppc.link("Work Order", r.work_order) },
			{ key: "operation", label: __("Operation"), sort: "operation", html: (r) => ppc.esc(r.operation) + (r.sequence_id ? ' <span class="text-muted">#' + ppc.esc(r.sequence_id) + "</span>" : "") },
			{ key: "workstation", label: __("Workstation"), sort: "workstation", html: (r) => r.workstation ? ppc.esc(r.workstation) : '<span class="ppc-flag warn">' + __("unassigned") + "</span>" },
			{ key: "planned_qty", label: __("Planned Qty"), sort: "planned_qty", align: "num", html: (r) => ppc.num(r.planned_qty) },
			{ key: "completed_qty", label: __("Completed Qty"), align: "num", html: (r) => ppc.num(r.completed_qty) },
			{ key: "status", label: __("Status"), sort: "status", html: (r) => ppc.badge(r.status) + (r.overdue ? ' <span class="ppc-flag bad" title="' + __("Overdue") + '">⚠</span>' : "") + (r.conflict ? ' <span class="ppc-flag bad" title="' + ppc.esc((r.conflict_reasons || []).join("; ")) + '">⛔</span>' : "") },
			{ key: "original", label: __("Original Start / End"), sort: "original_start", html: (r) => (r.has_baseline === false ? '<span class="text-muted" title="' + __("No baseline recorded yet") + '">–</span>' : '<span title="' + ppc.esc(r.baseline_label || "") + '">' + two(r.original_start, r.original_end) + "</span>") },
			{ key: "revised", label: __("Current Start / End"), sort: "revised_start", html: (r) => two(r.revised_start, r.revised_end) + (r.is_estimated ? ' <span class="text-muted" title="' + __("estimated") + '">~</span>' : "") },
			{ key: "actual", label: __("Actual Start / End"), sort: "actual_start", html: (r) => two(r.actual_start, r.actual_end) },
			{ key: "revision", label: __("Revisions"), align: "num", html: (r) => (r.has_baseline === false ? "" : ppc.esc(r.revision || 0)) },
			{ key: "difference", label: __("Change vs Original"), wrap: true, hidden: true, html: (r) => ppc.esc(r.difference || "") },
			{ key: "last_rescheduled_by", label: __("Last Rescheduled By"), html: (r) => (r.last_rescheduled_by ? ppc.esc(r.last_rescheduled_by) + '<span class="ppc-2l text-muted">' + ppc.dt(r.last_rescheduled_at) + "</span>" : "") },
			{ key: "priority", label: __("Priority"), sort: "priority", hidden: true },
			{ key: "employees", label: __("Assigned Employee"), hidden: true },
			{ key: "sync_status", label: __("Sync Status"), html: (r) => ppc.syncBadge(r.sync_status) },
			{ key: "act", label: "", html: (r) => '<button type="button" class="btn btn-xs btn-default ppc-history" data-name="' + ppc.esc(r.name) + '">' + __("History") + "</button>" + (r.movable && canReschedule ? ' <button type="button" class="btn btn-xs btn-default ppc-edit" data-name="' + ppc.esc(r.name) + '" title="' + __("Reschedule") + '">✎</button>' : "") },
		],
	};

	/* ======================================================================= bulk run over several plans */
	/**
	 * Submit ids in server-sized batches, one plan at a time (the server validates each plan's records together),
	 * with one progress bar and one per-record result at the end. groups: {plan: [ids]}.
	 */
	ppc.runGroups = async function (opts) {
		const plans = Object.keys(opts.groups).filter((p) => opts.groups[p].length);
		const total = plans.reduce((n, p) => n + opts.groups[p].length, 0);
		const size = Math.max(1, opts.batchSize || 25);
		const all = [];
		let done = 0;
		frappe.show_progress(opts.title, 0, total, __("Starting…"), true);
		try {
			for (const plan of plans) {
				const ids = opts.groups[plan];
				for (let i = 0; i < ids.length; i += size) {
					const chunk = ids.slice(i, i + size);
					const res = await ppc.call(opts.method, { plan: plan, [opts.argName]: chunk });
					all.push(...(res.results || []));
					done += chunk.length;
					frappe.show_progress(opts.title, done, total, __("{0} of {1} processed", [done, total]), true);
					await new Promise((r) => setTimeout(r, 0));
				}
			}
		} catch (e) {
			all.push({ name: __("(stopped)"), ok: false, message: __("The request failed; processing stopped. Records already processed are listed.") });
		} finally {
			frappe.hide_progress();
		}
		ppc.showResults(opts.title, all, opts.doctype);
		if (opts.onDone) opts.onDone(all);
		return all;
	};

	/* ======================================================================= preview-first repair actions */
	ppc.integrity = {
		/** Preview, then (on confirmation, with the Reconcile capability) apply the safe repairs. */
		async previewRepairs(opts) {
			const r = await ppc.call("repair_schedule_inconsistencies", { plan: opts.plan || null, types: opts.types || [], dry_run: 1 }, { freeze: true, freeze_message: __("Checking…") }).catch(() => null);
			if (!r) return;
			const fixable = r.results.filter((x) => !/manual/i.test(x.message));
			const d = new frappe.ui.Dialog({
				title: __("Repair preview – nothing has been changed yet"),
				size: "large",
				fields: [{ fieldtype: "HTML", fieldname: "body" }],
				primary_action_label: opts.canApply && fixable.length ? __("Apply {0} repair(s)", [fixable.length]) : null,
				primary_action: async () => {
					d.hide();
					const done = await ppc.call("repair_schedule_inconsistencies", { plan: opts.plan || null, types: opts.types || [], dry_run: 0 }, { freeze: true, freeze_message: __("Repairing…") }).catch(() => null);
					if (done) {
						frappe.show_alert({ message: __("{0} repair(s) applied", [done.repaired]), indicator: done.repaired ? "green" : "orange" });
						if (opts.onDone) opts.onDone();
					}
				},
			});
			d.fields_dict.body.$wrapper.html(r.results.length
				? ppc.listHtml(r.results.map((x) => (x.job_card || x.work_order || "") + " – " + x.type.replace(/_/g, " ") + ": " + x.message))
				: "<p>" + __("Nothing to repair.") + "</p>");
			ppc.skin(d); d.show();
		},

		async initializeBaselines(opts) {
			const r = await ppc.call("initialize_missing_baselines", { plan: opts.plan || null, dry_run: 1 }, { freeze: true }).catch(() => null);
			if (!r) return;
			if (!r.candidates.length) return frappe.show_alert({ message: __("Every scheduled Job Card already has a baseline."), indicator: "green" });
			frappe.confirm(
				__("{0} Job Card(s) have a schedule but no recorded baseline or history. Their current schedule will be recorded as the baseline and labelled 'initialized from existing schedule' (their true original plan is unknown). Existing baselines are never overwritten. Continue?", [r.candidates.length]) +
				ppc.listHtml(r.candidates.slice(0, 15).map((c) => c.job_card)),
				async () => {
					const done = await ppc.call("initialize_missing_baselines", { plan: opts.plan || null, dry_run: 0 }, { freeze: true }).catch(() => null);
					if (done) { frappe.show_alert({ message: __("{0} baseline(s) recorded", [done.initialized + (done.history_added || 0)]), indicator: "green" }); if (opts.onDone) opts.onDone(); }
				}
			);
		},

		retryFailed(opts) {
			frappe.confirm(__("Replay the schedule changes that were rolled back? Each change is re-validated with your permissions."), async () => {
				const r = await ppc.call("retry_failed_synchronizations", { plan: opts.plan || null }, { freeze: true, freeze_message: __("Retrying…") }).catch(() => null);
				if (!r) return;
				frappe.show_alert({ message: __("{0} retried, {1} succeeded", [r.retried, r.succeeded]), indicator: r.retried && r.succeeded < r.retried ? "orange" : "green" });
				const failed = r.results.filter((x) => !x.ok);
				if (failed.length) frappe.msgprint({ title: __("Not retried successfully"), indicator: "red", message: ppc.listHtml(failed.map((f) => f.job_card + ": " + f.message)) });
				if (opts.onDone) opts.onDone();
			});
		},
	};
})();
