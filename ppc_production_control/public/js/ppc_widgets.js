/* PPC Production Control - screen widgets (filters, KPIs, tables, Gantt tab, reschedule flow, control page). */
(function () {
	"use strict";
	const ppc = window.ppc;

	/* ======================================================================= filter bar */
	const FILTER_DEFS = [
		{ fieldname: "plan", label: "Production Plan", fieldtype: "Link", options: "Production Plan" },
		{ fieldname: "sales_order", label: "Sales Order", fieldtype: "Link", options: "Sales Order" },
		{ fieldname: "sales_order_like", label: "Sales Order No. (partial)", fieldtype: "Data" },
		{ fieldname: "customer", label: "Customer", fieldtype: "Link", options: "Customer" },
		{ fieldname: "company", label: "Company", fieldtype: "Link", options: "Company" },
		{ fieldname: "item_code", label: "Finished Good Item", fieldtype: "Link", options: "Item" },
		{ fieldname: "component_item", label: "Component Item", fieldtype: "Link", options: "Item" },
		{ fieldname: "work_order", label: "Work Order", fieldtype: "Link", options: "Work Order" },
		{ fieldname: "job_card", label: "Job Card", fieldtype: "Link", options: "Job Card" },
		{ fieldname: "operation", label: "Operation", fieldtype: "Link", options: "Operation" },
		{ fieldname: "workstation", label: "Workstation", fieldtype: "Link", options: "Workstation" },
		{ fieldname: "planner", label: "Production Planner", fieldtype: "Link", options: "User" },
		{ fieldname: "employee", label: "Assigned Employee", fieldtype: "Link", options: "Employee" },
		{ fieldname: "status", label: "Status", fieldtype: "Select", options: "\nDraft\nOpen\nMaterial Transferred\nWork In Progress\nOn Hold\nSubmitted\nCompleted\nCancelled" },
		{ fieldname: "priority", label: "Priority", fieldtype: "Select", options: "\nLow\nMedium\nHigh\nUrgent" },
		{ fieldname: "planned_start_from", label: "Planned Start From", fieldtype: "Date" },
		{ fieldname: "planned_start_to", label: "Planned Start To", fieldtype: "Date" },
		{ fieldname: "revised_start_from", label: "Revised Start From", fieldtype: "Date" },
		{ fieldname: "revised_start_to", label: "Revised Start To", fieldtype: "Date" },
		{ fieldname: "delivery_from", label: "Expected Delivery From", fieldtype: "Date" },
		{ fieldname: "delivery_to", label: "Expected Delivery To", fieldtype: "Date" },
		{ fieldname: "search", label: "Search", fieldtype: "Data" },
		{ fieldname: "overdue_only", label: "Overdue only", fieldtype: "Check" },
		{ fieldname: "conflict_only", label: "Scheduling conflict only", fieldtype: "Check" },
		{ fieldname: "unassigned_only", label: "Unassigned workstation only", fieldtype: "Check" },
		{ fieldname: "include_non_ppc", label: "Include Job Cards without a Production Plan", fieldtype: "Check" },
	];

	ppc.FilterBar = class FilterBar {
		constructor(parent, onApply, initial) {
			this.controls = {};
			this.onApply = onApply;
			this.$el = $('<details class="ppc-filters" open><summary>' + __("Filters") + '</summary><div class="ppc-filter-grid"></div><div class="ppc-filter-actions"></div></details>').appendTo(parent);
			const $grid = this.$el.find(".ppc-filter-grid");
			FILTER_DEFS.forEach((def) => {
				const $c = $("<div></div>").appendTo($grid);
				const df = Object.assign({}, def, { label: __(def.label), onchange: null });
				if (def.fieldname === "company") df.default = frappe.defaults.get_user_default("Company");
				this.controls[def.fieldname] = frappe.ui.form.make_control({ df: df, parent: $c, render_input: true });
				this.controls[def.fieldname].refresh();
			});
			const $a = this.$el.find(".ppc-filter-actions");
			$('<button class="btn btn-primary btn-sm">' + __("Apply Filters") + "</button>").appendTo($a).on("click", () => this.apply());
			$('<button class="btn btn-default btn-sm">' + __("Clear Filters") + "</button>").appendTo($a).on("click", () => this.clear());
			$('<button class="btn btn-default btn-sm">' + __("Refresh") + "</button>").appendTo($a).on("click", () => this.apply());
			this.$el.on("keydown", "input", (e) => { if (e.key === "Enter") this.apply(); });
			if (initial) this.set(initial);
		}

		get() {
			const out = {};
			Object.keys(this.controls).forEach((k) => {
				const v = this.controls[k].get_value();
				if (v === 0 || v === "" || v == null) return;
				out[k] = v;
			});
			return out;
		}

		set(values) {
			Object.keys(this.controls).forEach((k) => this.controls[k].set_value(values && values[k] != null ? values[k] : (this.controls[k].df.fieldtype === "Check" ? 0 : "")));
		}

		clear() { this.set({}); this.apply(); }
		apply() { if (this.onApply) this.onApply(this.get()); }
	};

	/* ======================================================================= KPI cards */
	const KPI_DEFS = [
		["total_sales_orders", "Total Sales Orders"], ["total_ppc_plans", "Total Production Plans"],
		["pending_production_qty", "Pending Production Qty"], ["work_orders_created", "Work Orders Created"],
		["draft_work_orders", "Draft Work Orders"], ["open_work_orders", "Open Work Orders"],
		["total_job_cards", "Total Job Cards"], ["draft_job_cards", "Draft Job Cards"],
		["open_job_cards", "Open Job Cards"], ["work_in_progress", "Work in Progress"],
		["completed_job_cards", "Completed Job Cards"], ["overdue_operations", "Overdue Operations", "bad", { overdue_only: 1 }],
		["unassigned_operations", "Unassigned Operations", "warn", { unassigned_only: 1 }],
		["scheduling_conflicts", "Scheduling Conflicts", "bad", { conflict_only: 1 }],
	];

	ppc.renderKpis = function ($el, kpis, onFilter) {
		$el.empty().addClass("ppc-kpis");
		KPI_DEFS.forEach(function (d) {
			const val = kpis[d[0]] || 0;
			const cls = d[2] && val ? d[2] : "";
			const $k = $('<div class="ppc-kpi ' + cls + '"' + (d[3] ? ' data-filter="1" role="button" tabindex="0"' : "") + '><div class="v">' + ppc.num(val, 3) + '</div><div class="l">' + __(d[1]) + "</div></div>").appendTo($el);
			if (d[3] && onFilter) $k.on("click", () => onFilter(d[3]));
		});
	};

	/* ======================================================================= tooltip html */
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
			(t.original_start && t.original_start !== t.revised_start ? row(__("Original"), ppc.dt(t.original_start) + " → " + ppc.dt(t.original_end)) : "") +
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
	 * Save a new schedule. Walks the user through warnings/overrides; resolves {ok, start, end, modified}.
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
				return { ok: true, start: mine.new_start || start, end: mine.new_end || end, others: r.changed.length > 1 };
			}
			if (r.errors && r.errors.length) {
				frappe.msgprint({ title: __("Cannot reschedule"), indicator: "red", message: ppc.listHtml(r.errors) });
				return { ok: false };
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
			d.show();
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
				const res = await ppc.reschedule(task, v.start, v.end, "resize", { reason: v.reason || "" });
				if (res.ok && onDone) onDone(res);
			},
		});
		d.show();
	};

	/* ======================================================================= control page */
	const JC_COLUMNS = () => [
		{ key: "name", label: __("Job Card"), sort: "name", html: (r) => ppc.link("Job Card", r.name) },
		{ key: "ppc_plan", label: __("Production Plan"), sort: "plan", html: (r) => ppc.link("Production Plan", r.ppc_plan) },
		{ key: "sales_order", label: __("Sales Order"), sort: "sales_order", html: (r) => ppc.link("Sales Order", r.sales_order) },
		{ key: "customer_name", label: __("Customer"), sort: "customer" },
		{ key: "item_code", label: __("Finished Good"), sort: "item_code", html: (r) => ppc.esc(r.item_code) + (r.item_name ? ' <span class="text-muted">' + ppc.esc(r.item_name) + "</span>" : "") },
		{ key: "work_order", label: __("Work Order"), sort: "work_order", html: (r) => ppc.link("Work Order", r.work_order) },
		{ key: "sequence_id", label: __("Seq"), sort: "sequence_id", align: "num" },
		{ key: "operation", label: __("Operation"), sort: "operation" },
		{ key: "workstation", label: __("Workstation"), sort: "workstation", html: (r) => r.workstation ? ppc.esc(r.workstation) : '<span class="ppc-flag warn">' + __("unassigned") + "</span>" },
		{ key: "planned_qty", label: __("Planned Qty"), sort: "planned_qty", align: "num", html: (r) => ppc.num(r.planned_qty) },
		{ key: "completed_qty", label: __("Completed"), align: "num", html: (r) => ppc.num(r.completed_qty) },
		{ key: "pending_qty", label: __("Pending"), align: "num", html: (r) => ppc.num(r.pending_qty) },
		{ key: "original_start", label: __("Original Start"), sort: "original_start", html: (r) => ppc.dt(r.original_start) },
		{ key: "original_end", label: __("Original End"), html: (r) => ppc.dt(r.original_end) },
		{ key: "revised_start", label: __("Revised Start"), sort: "revised_start", html: (r) => ppc.dt(r.revised_start) + (r.is_estimated ? ' <span class="text-muted" title="' + __("estimated") + '">~</span>' : "") },
		{ key: "revised_end", label: __("Revised End"), sort: "revised_end", html: (r) => ppc.dt(r.revised_end) },
		{ key: "actual_start", label: __("Actual Start"), sort: "actual_start", html: (r) => ppc.dt(r.actual_start) },
		{ key: "actual_end", label: __("Actual End"), html: (r) => ppc.dt(r.actual_end) },
		{ key: "status", label: __("Status"), sort: "status", html: (r) => ppc.badge(r.status) + (r.overdue ? ' <span class="ppc-flag bad" title="' + __("Overdue") + '">⚠</span>' : "") + (r.conflict ? ' <span class="ppc-flag bad" title="' + ppc.esc(r.conflict_reasons.join("; ")) + '">⛔</span>' : "") },
		{ key: "priority", label: __("Priority"), sort: "priority" },
		{ key: "employees", label: __("Assigned Employee") },
		{ key: "sync_status", label: __("Sync Status"), html: (r) => ppc.syncBadge(r.sync_status) },
		{ key: "act", label: "", html: (r) => r.movable ? '<a class="ppc-edit" data-name="' + ppc.esc(r.name) + '" href="#" title="' + __("Reschedule") + '">✎</a>' : "" },
	];

	const WO_COLUMNS = () => [
		{ key: "name", label: __("Work Order"), html: (r) => ppc.link("Work Order", r.name) },
		{ key: "ppc_plan", label: __("Production Plan"), html: (r) => ppc.link("Production Plan", r.ppc_plan) },
		{ key: "sales_order", label: __("Sales Order"), html: (r) => ppc.link("Sales Order", r.sales_order) },
		{ key: "customer_name", label: __("Customer") },
		{ key: "item_code", label: __("Item"), html: (r) => ppc.esc(r.item_code) + (r.item_name ? ' <span class="text-muted">' + ppc.esc(r.item_name) + "</span>" : "") },
		{ key: "qty", label: __("Qty"), align: "num", html: (r) => ppc.num(r.qty) },
		{ key: "produced_qty", label: __("Produced"), align: "num", html: (r) => ppc.num(r.produced_qty) },
		{ key: "status", label: __("Status"), html: (r) => ppc.badge(r.status) },
		{ key: "planned_start_date", label: __("Planned Start"), html: (r) => ppc.dt(r.planned_start_date) },
		{ key: "planned_end_date", label: __("Planned End"), html: (r) => ppc.dt(r.planned_end_date) },
		{ key: "expected_delivery_date", label: __("Delivery"), html: (r) => ppc.dt(r.expected_delivery_date) },
		{ key: "job_cards", label: __("Job Cards"), align: "num" },
		{ key: "sync_status", label: __("Sync Status"), html: (r) => ppc.syncBadge(r.sync_status) },
		{ key: "error_message", label: __("Last Error"), wrap: true, html: (r) => r.error_message ? '<span class="ppc-flag bad">' + ppc.esc(r.error_message) + "</span>" : "" },
	];

	ppc.ControlPanel = class ControlPanel {
		/**
		 * parent: element; opts: {fixedFilters, showFilters, plan, caps, onPlanAction}
		 * The same panel powers the PPC Production Control page and the plan form's embedded view.
		 */
		constructor(parent, opts) {
			this.$root = $(parent);
			this.opts = Object.assign({ showFilters: true, fixedFilters: {}, caps: {}, batchSize: 25, maxTasks: 500 }, opts || {});
			this.filters = {};
			this.jcSort = { key: "revised_start", dir: "asc" };
			this.jcPage = 1;
			this.pageLength = 50;
			this.woPage = 1;
			this.activeTab = ppc.pref.get("tab", "jobcards");
			this._build();
		}

		effectiveFilters() { return Object.assign({}, this.filters, this.opts.fixedFilters); }
		can(cap) { return !!(this.opts.caps && this.opts.caps[cap]); }

		_build() {
			const self = this;
			this.$root.empty().addClass("ppc");
			if (this.opts.showFilters) this.filterBar = new ppc.FilterBar(this.$root, (f) => { self.filters = f; self.reload(); });
			this.$strip = $('<div class="ppc-plan-strip" style="display:none"></div>').appendTo(this.$root);
			this.$kpis = $('<div></div>').appendTo(this.$root);
			const tabs = [["jobcards", __("Job Cards")], ["workorders", __("Work Orders")], ["gantt", __("Gantt")], ["workload", __("Workload & Schedule")], ["audit", __("Audit Log")]];
			this.$tabs = $('<div class="ppc-tabs" role="tablist"></div>').appendTo(this.$root);
			this.panes = {};
			tabs.forEach(function (t) {
				$('<button type="button" class="ppc-tab" role="tab" data-tab="' + t[0] + '">' + t[1] + "</button>").appendTo(self.$tabs).on("click", () => self.showTab(t[0]));
				self.panes[t[0]] = $('<div class="ppc-pane" role="tabpanel"></div>').appendTo(self.$root);
			});
			this._buildJobCards();
			this._buildWorkOrders();
			this._buildGantt();
			this.$workload = $('<div></div>').appendTo(this.panes.workload);
			this.$audit = $('<div></div>').appendTo(this.panes.audit);
			this.showTab(this.activeTab, true);
		}

		showTab(key, silent) {
			this.activeTab = key;
			ppc.pref.set("tab", key);
			this.$tabs.find(".ppc-tab").each(function () { $(this).toggleClass("active", $(this).data("tab") === key).attr("aria-selected", $(this).data("tab") === key); });
			Object.keys(this.panes).forEach((k) => this.panes[k].toggleClass("active", k === key));
			if (!silent) this.loadTab(key);
			else this._pendingLoad = true;
		}

		reload() {
			this.jcPage = 1;
			this.woPage = 1;
			this.loadKpis();
			this.loadPlanStrip();
			this.loadTab(this.activeTab);
		}

		loadTab(key) {
			if (key === "jobcards") this.loadJobCards();
			else if (key === "workorders") this.loadWorkOrders();
			else if (key === "gantt") this.loadGantt();
			else if (key === "workload") this.loadWorkload();
			else if (key === "audit") this.loadAudit();
		}

		/* ---------------------------------------------------------------- strip + KPIs */
		loadKpis() {
			return ppc.call("get_ppc_dashboard", { filters: this.effectiveFilters() }).then((r) => {
				this.dashboard = r;
				ppc.renderKpis(this.$kpis, r.kpis, (extra) => {
					if (this.filterBar) { this.filterBar.set(Object.assign({}, this.filters, extra)); this.filterBar.apply(); }
				});
				this.serverNow = r.as_of;
				if (this.activeTab === "workload") this.renderWorkload();
			}).catch(() => {});
		}

		loadPlanStrip() {
			const plan = this.effectiveFilters().plan;
			if (!plan) { this.$strip.hide(); this.plan = null; return Promise.resolve(); }
			return ppc.call("get_plan_overview", { plan: plan }).then((r) => {
				this.plan = r;
				const p = r.plan;
				const kv = (k, v) => '<span><span class="k">' + k + "</span>" + v + "</span>";
				const actions = [];
				if (p.docstatus === 1) {
					if (this.can("Create Work Orders") && r.has_pending) actions.push(["create", __("Create Draft Work Orders")]);
					if (this.can("Retry Failed Operations") && r.has_failures) actions.push(["retry", __("Retry Failed")]);
					if (this.can("Create Work Orders") || this.can("Submit Job Cards") || this.can("Manage Plans")) actions.push(["refresh_jc", __("Refresh Job Cards")]);
					if (this.can("Reschedule")) actions.push(["schedule", __("Schedule Unscheduled")]);
				}
				this.$strip.html(
					kv(__("Production Plan"), ppc.link("Production Plan", p.name)) + kv(__("Status"), ppc.badge(p.status)) +
					kv(__("Planner"), ppc.esc(p.production_planner)) + kv(__("Priority"), ppc.esc(p.priority || "–")) +
					kv(__("Posting date"), ppc.dt(p.posting_date)) +
					kv(__("Work Orders"), p.total_work_orders + " (" + p.draft_work_orders + " " + __("draft") + ", " + p.submitted_work_orders + " " + __("submitted") + ")") +
					kv(__("Job Cards"), p.total_job_cards) + kv(__("Planned qty"), ppc.num(p.total_planned_qty)) +
					kv(__("Last sync"), ppc.dt(p.last_scheduling_sync) || "–") +
					(r.has_failures ? '<span class="ppc-flag bad">⚠ ' + __("failures – see Audit Log") + "</span>" : "") +
					'<span class="ppc-strip-actions"></span>'
				).show();
				const $a = this.$strip.find(".ppc-strip-actions");
				actions.forEach((a) => $('<button type="button" class="btn btn-xs btn-default" style="margin-left:6px">' + a[1] + "</button>").appendTo($a).on("click", () => this.planAction(a[0], p.name)));
			}).catch(() => {});
		}

		planAction(action, plan) {
			const done = () => this.reload();
			if (action === "create") ppc.call("create_work_orders", { plan: plan }, { freeze: true }).then((r) => { this.reportCreate(r); done(); });
			else if (action === "retry") ppc.call("retry_failed_operations", { plan: plan }, { freeze: true }).then((r) => { this.reportCreate(r.work_orders); done(); });
			else if (action === "refresh_jc") ppc.call("refresh_job_cards", { plan: plan }, { freeze: true }).then(() => { frappe.show_alert({ message: __("Job Cards synchronised"), indicator: "green" }); done(); });
			else if (action === "schedule") ppc.call("schedule_unscheduled", { plan: plan }, { freeze: true }).then((r) => { frappe.show_alert({ message: __("{0} operation(s) scheduled", [r.scheduled.length]), indicator: "green" }); done(); });
		}

		reportCreate(r) {
			if (!r) return;
			if (r.queued) return frappe.show_alert({ message: __("Creating {0} Work Orders in the background…", [r.pending]), indicator: "blue" });
			const bits = [__("{0} created", [(r.created || []).length]), __("{0} already existed", [(r.skipped || []).length]), __("{0} failed", [(r.failed || []).length])];
			frappe.show_alert({ message: bits.join(" · "), indicator: (r.failed || []).length ? "orange" : "green" });
			if ((r.failed || []).length) frappe.msgprint({ title: __("Some rows failed"), indicator: "red", message: ppc.listHtml(r.failed.map((f) => (f.item_code || "") + ": " + f.message)) });
		}

		/* ---------------------------------------------------------------- job cards */
		_buildJobCards() {
			const self = this;
			const $p = this.panes.jobcards;
			this.$jcTools = $('<div class="ppc-toolbar"></div>').appendTo($p);
			this.$jcTable = $('<div></div>').appendTo($p);
			if (this.can("Submit Job Cards")) {
				this.$btnSubmitJc = $('<button type="button" class="btn btn-primary btn-sm">' + __("Submit Selected Job Cards") + "</button>").appendTo(this.$jcTools).on("click", () => this.submitJobCards());
			}
			$('<button type="button" class="btn btn-default btn-sm">' + __("Refresh") + "</button>").appendTo(this.$jcTools).on("click", () => this.reload());
			this.jcTable = new ppc.DataTable(this.$jcTable, JC_COLUMNS(), {
				idKey: "name", selectable: true, storageKey: "jobcards", pageLength: this.pageLength,
				sortKey: this.jcSort.key, sortDir: this.jcSort.dir,
				isEligible: (r) => r.docstatus === 0,
				rowClass: (r) => (r.movable ? "" : "locked"),
				emptyText: __("No Job Cards match the filters. Job Cards appear after a Work Order of a PPC plan is submitted."),
				onSort: (k, d) => { self.jcSort = { key: k, dir: d }; self.jcPage = 1; self.loadJobCards(); },
				onPage: (p) => { self.jcPage = p; self.loadJobCards(); },
				onPageSize: (n) => { self.pageLength = n; self.jcTable.opts.pageLength = n; self.jcPage = 1; self.loadJobCards(); },
				onSelectAllMatching: () => self.fetchAllDraftJobCards(),
				selectAllLabel: __("Select all Draft Job Cards matching the filters"),
				onSelectionChange: (sel) => { if (self.$btnSubmitJc) self.$btnSubmitJc.text(__("Submit Selected Job Cards") + (sel.length ? " (" + sel.length + ")" : "")); },
			});
			this.$jcTable.on("click", ".ppc-edit", function (e) {
				e.preventDefault();
				const row = self.jcTable.rows.find((r) => r.name === $(this).data("name"));
				if (row) ppc.editDates(row, () => self.reload());
			});
		}

		loadJobCards() {
			const t = this.jcTable;
			return ppc.call("get_job_cards", { filters: this.effectiveFilters(), page: this.jcPage, page_length: this.pageLength, sort_by: this.jcSort.key, sort_order: this.jcSort.dir })
				.then((r) => { t.sort = { key: this.jcSort.key, dir: this.jcSort.dir }; t.setData(r); }).catch(() => {});
		}

		async fetchAllDraftJobCards() {
			const names = [];
			let page = 1;
			const MAX = 10000;
			for (;;) {
				const r = await ppc.call("get_job_cards", { filters: this.effectiveFilters(), page: page, page_length: 200, sort_by: "name", sort_order: "asc" });
				r.rows.forEach((row) => { if (row.docstatus === 0) names.push(row.name); });
				if (page >= r.pages || names.length >= MAX) break;
				page++;
			}
			if (names.length >= MAX) frappe.show_alert({ message: __("Only the first {0} were selected; narrow the filters.", [MAX]), indicator: "orange" });
			return names;
		}

		submitJobCards() {
			const sel = this.jcTable.selection();
			const plan = this.planForSelection(this.jcTable, sel);
			if (!plan) return;
			ppc.bulk({
				title: __("Submit Job Cards"), verb: __("Submit"), ids: sel, method: "submit_selected_job_cards", argName: "job_cards",
				args: { plan: plan }, batchSize: this.opts.batchSize, doctype: "Job Card",
				onDone: () => { this.jcTable.clearSelection(); this.reload(); },
			});
		}

		/** Bulk actions are per plan; with several plans in view, run once per plan. */
		planForSelection(table, sel) {
			const plans = new Set(table.rows.filter((r) => sel.includes(r.name)).map((r) => r.ppc_plan).filter(Boolean));
			const single = this.effectiveFilters().plan;
			if (single) return single;
			if (plans.size === 1) return Array.from(plans)[0];
			if (!sel.length) { frappe.show_alert({ message: __("Select at least one record."), indicator: "orange" }); return null; }
			frappe.msgprint(__("The selection spans several PPC plans (or records outside this page). Filter by one PPC Production Plan first, then select."));
			return null;
		}

		/* ---------------------------------------------------------------- work orders */
		_buildWorkOrders() {
			const self = this;
			const $p = this.panes.workorders;
			this.$woTools = $('<div class="ppc-toolbar"></div>').appendTo($p);
			this.$woTable = $('<div></div>').appendTo($p);
			if (this.can("Submit Work Orders")) {
				this.$btnSubmitWo = $('<button type="button" class="btn btn-primary btn-sm">' + __("Submit Selected Work Orders") + "</button>").appendTo(this.$woTools).on("click", () => this.submitWorkOrders());
			}
			this.woTable = new ppc.DataTable(this.$woTable, WO_COLUMNS(), {
				idKey: "name", selectable: true, storageKey: "workorders", pageLength: 50,
				isEligible: (r) => r.can_submit,
				emptyText: __("No Work Orders yet."),
				onPage: (p) => { self.woPage = p; self.loadWorkOrders(); },
				selectAllLabel: __("Select all"),
				onSelectAllMatching: () => ppc.call("get_work_orders", { filters: self.effectiveFilters(), page: 1, page_length: 200 }).then((r) => r.rows.filter((x) => x.can_submit).map((x) => x.name)),
				onSelectionChange: (sel) => { if (self.$btnSubmitWo) self.$btnSubmitWo.text(__("Submit Selected Work Orders") + (sel.length ? " (" + sel.length + ")" : "")); },
			});
		}

		loadWorkOrders() {
			return ppc.call("get_work_orders", { filters: this.effectiveFilters(), page: this.woPage, page_length: 50 }).then((r) => this.woTable.setData(r)).catch(() => {});
		}

		submitWorkOrders() {
			const sel = this.woTable.selection();
			if (!sel.length) return frappe.show_alert({ message: __("Select at least one record."), indicator: "orange" });
			const plans = new Set(this.woTable.rows.filter((r) => sel.includes(r.name)).map((r) => r.ppc_plan).filter(Boolean));
			const p = this.effectiveFilters().plan || (plans.size === 1 ? Array.from(plans)[0] : null);
			if (!p) return frappe.msgprint(__("The selection spans several PPC plans (or records outside this page). Filter by one PPC Production Plan first, then select."));
			ppc.bulk({
				title: __("Submit Work Orders"), verb: __("Submit"), ids: sel, method: "submit_selected_work_orders", argName: "work_orders",
				args: { plan: p }, batchSize: this.opts.batchSize, doctype: "Work Order",
				hint: __("Only Draft Work Orders of this plan are submitted. ERPNext validates each one (warehouses, stock rules, capacity) and creates its Job Cards."),
				onDone: () => { this.woTable.clearSelection(); this.reload(); },
			});
		}

		/* ---------------------------------------------------------------- gantt */
		_buildGantt() {
			const $p = this.panes.gantt;
			this.$ganttHost = $('<div></div>').appendTo($p);
			this.gantt = null;
		}

		loadGantt() {
			return ppc.call("get_gantt_data", { filters: this.effectiveFilters(), limit: this.opts.maxTasks }).then((r) => {
				if (!this.gantt) {
					const lib = window.PPCGanttLib;
					this.gantt = new lib.PPCGantt(this.$ganttHost[0], {
						height: 560, scale: ppc.pref.get("gantt_scale", "week"), groupBy: ppc.pref.get("gantt_group", "workstation"),
						now: this.serverNow, canEdit: this.can("Reschedule"),
						tooltip: ppc.taskTooltip,
						onChange: (evt) => this.onGanttChange(evt),
						onEdit: (task) => this.can("Reschedule") ? ppc.editDates(task, () => this.reload()) : frappe.set_route("Form", "Job Card", task.name),
						onSelect: () => {},
					});
				}
				this.gantt.opts.now = this.serverNow || this.gantt.opts.now;
				this.gantt.setData(r);
				ppc.pref.set("gantt_scale", this.gantt.scale);
				ppc.pref.set("gantt_group", this.gantt.groupBy);
				if (r.truncated) frappe.show_alert({ message: __("Showing the first {0} of {1} operations. Narrow the filters to see the rest.", [r.tasks.length, r.total]), indicator: "orange" });
			}).catch(() => {});
		}

		async onGanttChange(evt) {
			const res = await ppc.reschedule(evt.task, evt.start, evt.end, evt.mode);
			if (res.ok) setTimeout(() => this.reload(), 50); // refresh bars, tables and KPIs (cascades move other bars)
			return res;
		}

		/* ---------------------------------------------------------------- workload */
		loadWorkload() {
			if (!this.dashboard) return this.loadKpis().then(() => this.renderWorkload());
			return this.renderWorkload();
		}

		renderWorkload() {
			const d = this.dashboard;
			if (!d) return;
			const max = Math.max.apply(null, d.workload.map((w) => w.scheduled_hours).concat([1]));
			let html = "<h5>" + __("Workload by Workstation (open operations)") + '</h5><div class="ppc-bars">';
			if (!d.workload.length) html += '<div class="ppc-empty">' + __("No open operations.") + "</div>";
			d.workload.forEach(function (w) {
				html += '<div class="ppc-bar-row"><div title="' + ppc.esc(w.workstation) + '"><b>' + ppc.esc(w.workstation) + "</b> <span class=\"text-muted\">(" + __("cap.") + " " + (w.capacity || "–") + ")</span></div>" +
					'<div class="ppc-meter" role="img" aria-label="' + w.scheduled_hours + ' h"><span class="' + (w.overdue_cards ? "over" : "") + '" style="width:' + Math.round((100 * w.scheduled_hours) / max) + '%"></span></div>' +
					"<div>" + w.scheduled_hours + " h · " + w.job_cards + " " + __("cards") + (w.overdue_cards ? ' · <span class="ppc-flag bad">' + w.overdue_cards + " " + __("overdue") + "</span>" : "") + "</div></div>";
			});
			html += '</div><h5 style="margin-top:18px">' + __("Production Schedule Overview (hours per start day)") + '</h5><div class="ppc-chart"></div>';
			this.$workload.html(html);
			if (d.schedule.length && window.frappe && frappe.Chart) {
				new frappe.Chart(this.$workload.find(".ppc-chart")[0], {
					data: { labels: d.schedule.map((s) => s.day), datasets: [{ name: __("Hours"), values: d.schedule.map((s) => s.hours) }] },
					type: "bar", height: 220, colors: ["#2563eb"], axisOptions: { xIsSeries: true }, barOptions: { spaceRatio: 0.4 },
				});
			}
		}

		/* ---------------------------------------------------------------- audit */
		loadAudit() {
			const f = this.effectiveFilters();
			return ppc.call("get_audit_log", { plan: f.plan, work_order: f.work_order, job_card: f.job_card, limit: 200 }).then((r) => {
				const rows = r.rows;
				let html = '<div class="ppc-table-wrap"><table class="ppc-table"><thead><tr>' + [__("Time"), __("Action"), __("Result"), __("Plan"), __("Work Order"), __("Job Card"), __("Old Start"), __("Old End"), __("New Start"), __("New End"), __("User"), __("Reason / Error"), __("Retry")].map((h) => "<th>" + h + "</th>").join("") + "</tr></thead><tbody>";
				if (!rows.length) html += '<tr><td colspan="13" class="ppc-empty">' + __("Nothing logged yet.") + "</td></tr>";
				rows.forEach(function (a) {
					html += "<tr><td>" + ppc.dt(a.timestamp) + "</td><td>" + ppc.esc(a.action) + '</td><td><span class="ppc-flag ' + (a.result === "Failed" ? "bad" : a.result === "Warning" ? "warn" : "") + '">' + ppc.esc(a.result) + "</span></td><td>" +
						ppc.link("Production Plan", a.ppc_plan) + "</td><td>" + ppc.link("Work Order", a.work_order) + "</td><td>" + ppc.link("Job Card", a.job_card) + "</td><td>" +
						ppc.dt(a.old_start_date) + "</td><td>" + ppc.dt(a.old_end_date) + "</td><td>" + ppc.dt(a.new_start_date) + "</td><td>" + ppc.dt(a.new_end_date) + "</td><td>" + ppc.esc(a.user) +
						'</td><td class="wrap">' + ppc.esc(a.error_details || a.reason || "") + "</td><td>" + ppc.esc(a.retry_status) + "</td></tr>";
				});
				this.$audit.html(html + "</tbody></table></div>");
			}).catch(() => {});
		}
	};
})();
