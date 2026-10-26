/* PPC Control Center - the pages. Each is a Frappe Page (page/<name>/<name>.js) that calls ppc.pages.<key>.mount(wrapper). */
(function () {
	"use strict";
	const ppc = window.ppc;
	const lib = window.PPCUiLib;
	ppc.pages = ppc.pages || {};

	const LIST_PAGE_SIZE = 50;
	const today = () => frappe.datetime.get_today();

	/** Register a page class under `key`; `mount` is what the Frappe Page script calls. */
	function register(key, PageClass) {
		ppc.pages[key] = {
			mount(wrapper) {
				if (wrapper.ppc_instance) return wrapper.ppc_instance;
				wrapper.ppc_instance = new PageClass(wrapper);
				return wrapper.ppc_instance;
			},
			show(wrapper) {
				if (!wrapper.ppc_instance) return;
				const held = wrapper.ppc_route || {}; // taken by the page script while Frappe was showing the route
				wrapper.ppc_route = null;
				return wrapper.ppc_instance.show(held);
			},
		};
	}

	/* =========================================================================================== list page base */
	/**
	 * A server-paged table with a filter toolbar. Subclasses define: method (API), defs() -> {primary, more}, columns(),
	 * tableOptions(), and may override args(filters), afterLoad(res), afterInit().
	 */
	class ListPage extends ppc.Page {
		constructor(wrapper, key, opts) {
			super(wrapper, key, opts);
			this.pageNo = 1;
			this.pageLength = LIST_PAGE_SIZE;
			this.sortState = { key: null, dir: "asc" };
			this._req = 0;
		}

		defaultSort() { return { key: null, dir: "asc" }; }
		tableOptions() { return {}; }
		afterInit() {}
		afterLoad() {}
		emptyText() { return __("No records match the filters."); }

		init() {
			const defs = this.defs();
			this.sortState = this.defaultSort();
			this.filters = new ppc.FilterToolbar(this.$content, {
				primary: defs.primary, more: defs.more,
				onChange: () => { this.pageNo = 1; this.reload(true); },
			});
			this.$actions = $('<div class="ppc-actionbar"></div>').appendTo(this.$content);
			this.$table = $('<div class="ppc-tablehost"></div>').appendTo(this.$content);
			this.drawer = new ppc.Drawer(this.$root);
			this.table = new ppc.DataTable(this.$table, this.columns(), Object.assign({
				idKey: "name", storageKey: this.key, pageLength: this.pageLength, sortKey: this.sortState.key, sortDir: this.sortState.dir,
				emptyText: this.emptyText(),
				onSort: (k, d) => { this.sortState = { key: k, dir: d }; this.pageNo = 1; this.reload(true); },
				onPage: (p) => { this.pageNo = p; this.reload(true); },
				onPageSize: (n) => { this.pageLength = n; this.table.opts.pageLength = n; this.pageNo = 1; this.reload(true); },
			}, this.tableOptions()));
			this.$table.on("click", ".ppc-golink", (e) => {
				e.preventDefault();
				const d = $(e.currentTarget).data();
				ppc.go(d.page, { plan: d.plan || undefined, work_order: d.workOrder || undefined });
			});
			this.$table.on("click", ".ppc-history", (e) => {
				e.preventDefault();
				this.openHistory($(e.currentTarget).data("name"));
			});
			this.afterInit();
		}

		args() {
			return { filters: this.filters.get(), page: this.pageNo, page_length: this.pageLength, sort_by: this.sortState.key, sort_order: this.sortState.dir };
		}

		load() {
			const id = ++this._req;
			return ppc.call(this.method, this.args()).then((res) => {
				if (id !== this._req) return; // a newer request is on its way: this answer is already out of date
				this.table.sort = { key: this.sortState.key, dir: this.sortState.dir };
				this.table.setData(res);
				this.afterLoad(res);
			}).catch(() => {
				if (id !== this._req) return;
				this.$table.find(".ppc-empty").first().text(__("The list could not be loaded."));
			});
		}

		openHistory(jobCard) {
			return ppc.openHistory(this.drawer, jobCard, {
				task: this.taskFor ? this.taskFor(jobCard) : null, canReschedule: this.can("Reschedule"),
				onChanged: () => this.reload(true), busy: (p) => this.busyWhile(p),
			});
		}
	}

	const COMPANY = { fieldname: "company", label: "Company", fieldtype: "Link", options: "Company" };
	const SEARCH = (label) => ({ fieldname: "search", label: label || "Search", fieldtype: "Data", width: 2 });

	/* =========================================================================================== Production Plans */
	class PlansPage extends ListPage {
		constructor(wrapper) {
			super(wrapper, "plans", {
				description: __("Standard ERPNext Production Plans with their Work Orders, Job Cards and planning stage."),
				primary: { label: __("New Production Plan"), action: () => frappe.new_doc("Production Plan") },
				routeKeys: ["plan", "company", "planner", "plan_status", "posting_from", "posting_to", "search", "sales_order", "sales_order_like", "customer", "item_code", "delivery_from", "delivery_to"],
			});
			this.method = "get_production_plans";
		}

		defaultSort() { return { key: "posting_date", dir: "desc" }; }
		emptyText() { return __("No Production Plans match the filters."); }
		defs() {
			return {
				primary: [
					SEARCH(__("Search plan or Sales Order")), COMPANY,
					{ fieldname: "plan_status", label: "Status", fieldtype: "Select", options: "\nDraft\nSubmitted\nNot Started\nIn Process\nCompleted\nClosed\nCancelled" },
					{ fieldname: "planner", label: "Planner", fieldtype: "Link", options: "User" },
					{ fieldname: "posting_from", label: "Posted from", fieldtype: "Date" }, { fieldname: "posting_to", label: "Posted to", fieldtype: "Date" },
				],
				more: [
					{ fieldname: "sales_order_like", label: "Sales Order (partial)", fieldtype: "Data" },
					{ fieldname: "customer", label: "Customer", fieldtype: "Link", options: "Customer" },
					{ fieldname: "item_code", label: "Finished Good", fieldtype: "Link", options: "Item" },
					{ fieldname: "delivery_from", label: "Delivery from", fieldtype: "Date" }, { fieldname: "delivery_to", label: "Delivery to", fieldtype: "Date" },
					{ fieldname: "priority", label: "Priority", fieldtype: "Select", options: "\nLow\nMedium\nHigh\nUrgent" },
				],
			};
		}

		columns() { return ppc.columns.plans(); }
	}
	register("plans", PlansPage);

	/* =========================================================================================== Work Orders */
	class WorkOrdersPage extends ListPage {
		constructor(wrapper) {
			super(wrapper, "workorders", {
				description: __("Work Orders of your Production Plans. Draft Work Orders can be checked and submitted in bulk; ERPNext validates every one."),
				routeKeys: ["plan", "company", "planner", "wo_status", "search", "sales_order", "sales_order_like", "customer", "item_code", "work_order", "warehouse",
					"planned_start_from", "planned_start_to", "delivery_from", "delivery_to", "window_from", "window_to"],
			});
			this.method = "get_work_orders";
		}

		defaultSort() { return { key: "planned_start", dir: "asc" }; }
		emptyText() { return __("No Work Orders match the filters."); }
		defs() {
			return {
				primary: [
					SEARCH(__("Search Work Order, item, Sales Order or customer")),
					{ fieldname: "plan", label: "Production Plan", fieldtype: "Link", options: "Production Plan" },
					{ fieldname: "wo_status", label: "Status", fieldtype: "Select", options: "\nDraft\nOpen\nNot Started\nIn Process\nCompleted\nStopped\nClosed\nCancelled" },
					{ fieldname: "sales_order_like", label: "Sales Order (partial)", fieldtype: "Data" },
					{ fieldname: "item_code", label: "Finished Good", fieldtype: "Link", options: "Item" }, COMPANY,
				],
				more: [
					{ fieldname: "customer", label: "Customer", fieldtype: "Link", options: "Customer" },
					{ fieldname: "work_order", label: "Work Order", fieldtype: "Link", options: "Work Order" },
					{ fieldname: "warehouse", label: "Warehouse", fieldtype: "Link", options: "Warehouse" },
					{ fieldname: "planner", label: "Planner", fieldtype: "Link", options: "User" },
					{ fieldname: "planned_start_from", label: "Planned start from", fieldtype: "Date" }, { fieldname: "planned_start_to", label: "Planned start to", fieldtype: "Date" },
					{ fieldname: "window_from", label: "Scheduled between", fieldtype: "Date" }, { fieldname: "window_to", label: "and", fieldtype: "Date" },
					{ fieldname: "delivery_from", label: "Delivery from", fieldtype: "Date" }, { fieldname: "delivery_to", label: "Delivery to", fieldtype: "Date" },
				],
			};
		}

		columns() { return ppc.columns.workOrders(); }
		tableOptions() {
			return {
				selectable: this.can("Submit Work Orders"), isEligible: (r) => r.can_submit,
				selectAllLabel: __("Select all draft Work Orders matching the filters"),
				onSelectAllMatching: () => this.fetchAllDrafts(),
				onSelectionChange: (sel) => this.$submit && this.$submit.text(__("Submit Selected Work Orders") + (sel.length ? " (" + sel.length + ")" : "")),
			};
		}

		afterInit() {
			if (this.can("Submit Work Orders")) {
				this.$submit = $('<button type="button" class="btn btn-primary btn-sm ppc-submit-wo">' + __("Submit Selected Work Orders") + "</button>").appendTo(this.$actions).on("click", () => this.submitSelected());
			}
			$('<span class="text-muted ppc-hint"></span>').text(__("Tick draft Work Orders, then submit. Each is checked first and you see any problem before anything is submitted.")).appendTo(this.$actions);
		}

		afterLoad(res) {
			this.$table.find("th").first().attr("title", __("Only draft Work Orders can be selected"));
			if (res.rows.some((r) => r.error_message)) this.table.columns.find((c) => c.key === "error_message").hidden = false, this.table.render();
		}

		async fetchAllDrafts() {
			const names = [];
			for (let page = 1; page <= 50; page++) {
				const r = await ppc.call("get_work_orders", { filters: this.filters.get(), page: page, page_length: 200 });
				r.rows.forEach((row) => { if (row.can_submit) names.push(row.name); });
				if (page >= r.pages) break;
			}
			return names;
		}

		async submitSelected() {
			const sel = this.table.selection();
			if (!sel.length) return frappe.show_alert({ message: __("Select at least one Work Order."), indicator: "orange" });
			const results = [];
			for (let i = 0; i < sel.length; i += 50) {
				const r = await ppc.call("precheck_work_orders", { work_orders: sel.slice(i, i + 50) }, { freeze: true, freeze_message: __("Checking Work Orders…") }).catch(() => null);
				if (!r) return;
				results.push(...r.results);
			}
			this.showPrecheck(results);
		}

		showPrecheck(results) {
			const ready = results.filter((r) => r.ok);
			const blocked = results.length - ready.length;
			const rows = results.map((r) => "<tr><td>" + ppc.link("Work Order", r.name) + "</td><td>" + ppc.esc(r.item_code || "") + "</td><td>" + ppc.link("Production Plan", r.plan) + "</td><td>" +
				(r.ok ? '<span class="ppc-result-ok">✔ ' + __("Ready") + "</span>" : '<span class="ppc-result-fail">✖ ' + __("Blocked") + "</span>") + '</td><td class="wrap">' + (r.blockers || []).map(ppc.esc).join("<br>") + "</td></tr>").join("");
			const d = new frappe.ui.Dialog({
				title: __("Check before submitting"), size: "large",
				fields: [{ fieldtype: "HTML", fieldname: "body" }],
				primary_action_label: ready.length ? __("Submit {0} Work Order(s)", [ready.length]) : null,
				primary_action: () => {
					d.hide();
					ppc.runGroups({
						title: __("Submit Work Orders"), method: "submit_selected_work_orders", argName: "work_orders", doctype: "Work Order",
						groups: lib.groupByPlan(ready.map((r) => ({ name: r.name, ppc_plan: r.plan })), ready.map((r) => r.name)), batchSize: this.settings.bulk_batch_size || 25,
						onDone: () => { this.table.clearSelection(); this.reload(true); },
					});
				},
				secondary_action_label: __("Cancel"), secondary_action: () => d.hide(),
			});
			d.fields_dict.body.$wrapper.html(
				'<p><span class="ppc-result-ok">' + __("{0} ready", [ready.length]) + '</span> · <span class="ppc-result-fail">' + __("{0} blocked", [blocked]) + "</span></p>" +
				'<div class="ppc-results"><table class="ppc-table"><thead><tr><th>' + [__("Work Order"), __("Finished Good"), __("Production Plan"), __("Check"), __("Problems")].join("</th><th>") + "</th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
				'<p class="text-muted ppc-note">' + __("This runs ERPNext's own validation and mandatory-field checks without saving anything. Rules enforced only at the moment of submission are still reported per record afterwards; nothing is bypassed.") + "</p>"
			);
			d.show();
		}
	}
	register("workorders", WorkOrdersPage);

	/* =========================================================================================== Job Cards */
	class JobCardsPage extends ListPage {
		constructor(wrapper) {
			super(wrapper, "jobcards", {
				description: __("Operations of your Work Orders with original, current and actual dates. Dates are changed on the Production Gantt or with the date dialog."),
				routeKeys: ["plan", "company", "planner", "status", "search", "sales_order", "sales_order_like", "customer", "item_code", "work_order", "job_card", "operation",
					"workstation", "employee", "priority", "sync_status", "overdue_only", "conflict_only", "unassigned_only", "include_non_ppc", "window_from", "window_to",
					"planned_start_from", "planned_start_to", "revised_start_from", "revised_start_to", "delivery_from", "delivery_to"],
			});
			this.method = "get_job_cards";
		}

		defaultSort() { return { key: "revised_start", dir: "asc" }; }
		emptyText() { return __("No Job Cards match the filters. Job Cards appear after a Work Order of a plan is submitted."); }
		defs() {
			return {
				primary: [
					SEARCH(__("Search Job Card, Work Order, item or customer")),
					{ fieldname: "sales_order_like", label: "Sales Order (partial)", fieldtype: "Data" },
					{ fieldname: "plan", label: "Production Plan", fieldtype: "Link", options: "Production Plan" },
					{ fieldname: "work_order", label: "Work Order", fieldtype: "Link", options: "Work Order" },
					{ fieldname: "status", label: "Status", fieldtype: "Select", options: "\nDraft\nOpen\nMaterial Transferred\nWork In Progress\nOn Hold\nSubmitted\nCompleted\nCancelled" },
					{ fieldname: "workstation", label: "Workstation", fieldtype: "Link", options: "Workstation" },
				],
				more: [
					COMPANY, { fieldname: "customer", label: "Customer", fieldtype: "Link", options: "Customer" },
					{ fieldname: "item_code", label: "Finished Good", fieldtype: "Link", options: "Item" },
					{ fieldname: "operation", label: "Operation", fieldtype: "Link", options: "Operation" },
					{ fieldname: "employee", label: "Assigned Employee", fieldtype: "Link", options: "Employee" },
					{ fieldname: "planner", label: "Planner", fieldtype: "Link", options: "User" },
					{ fieldname: "priority", label: "Priority", fieldtype: "Select", options: "\nLow\nMedium\nHigh\nUrgent" },
					{ fieldname: "sync_status", label: "Sync status", fieldtype: "Select", options: "\nFailed\nDrift\nNo baseline\nRescheduled\nSynced\nEstimated" },
					{ fieldname: "window_from", label: "Scheduled between", fieldtype: "Date" }, { fieldname: "window_to", label: "and", fieldtype: "Date" },
					{ fieldname: "revised_start_from", label: "Current start from", fieldtype: "Date" }, { fieldname: "revised_start_to", label: "Current start to", fieldtype: "Date" },
					{ fieldname: "planned_start_from", label: "Work Order start from", fieldtype: "Date" }, { fieldname: "planned_start_to", label: "Work Order start to", fieldtype: "Date" },
					{ fieldname: "delivery_from", label: "Delivery from", fieldtype: "Date" }, { fieldname: "delivery_to", label: "Delivery to", fieldtype: "Date" },
					{ fieldname: "overdue_only", label: "Overdue only", fieldtype: "Check" }, { fieldname: "conflict_only", label: "Scheduling conflict only", fieldtype: "Check" },
					{ fieldname: "unassigned_only", label: "Unassigned workstation only", fieldtype: "Check" },
					{ fieldname: "include_non_ppc", label: "Include Job Cards without a plan", fieldtype: "Check" },
				],
			};
		}

		columns() { return ppc.columns.jobCards(this.can("Reschedule")); }
		tableOptions() {
			return {
				selectable: this.can("Submit Job Cards"), isEligible: (r) => r.docstatus === 0, rowClass: (r) => (r.movable ? "" : "locked"),
				selectAllLabel: __("Select all draft Job Cards matching the filters"),
				onSelectAllMatching: () => this.fetchAllDrafts(),
				onSelectionChange: (sel) => this.$submit && this.$submit.text(__("Submit Selected Job Cards") + (sel.length ? " (" + sel.length + ")" : "")),
			};
		}

		afterInit() {
			if (this.can("Submit Job Cards")) {
				this.$submit = $('<button type="button" class="btn btn-primary btn-sm ppc-submit-jc">' + __("Submit Selected Job Cards") + "</button>").appendTo(this.$actions).on("click", () => this.submitSelected());
			}
			this.$table.on("click", ".ppc-edit", (e) => {
				e.preventDefault();
				const row = this.table.rows.find((r) => r.name === $(e.currentTarget).data("name"));
				if (row) ppc.editDates(row, () => this.reload(true));
			});
		}

		taskFor(name) { return this.table.rows.find((r) => r.name === name) || null; }

		async fetchAllDrafts() {
			const names = [];
			for (let page = 1; page <= 50; page++) {
				const r = await ppc.call("get_job_cards", { filters: this.filters.get(), page: page, page_length: 200, sort_by: "name", sort_order: "asc" });
				r.rows.forEach((row) => { if (row.docstatus === 0) names.push(row.name); });
				if (page >= r.pages) break;
			}
			return names;
		}

		submitSelected() {
			const sel = this.table.selection();
			if (!sel.length) return frappe.show_alert({ message: __("Select at least one Job Card."), indicator: "orange" });
			const known = new Map(this.table.rows.map((r) => [r.name, r.ppc_plan]));
			if (sel.some((n) => !known.has(n))) return this.submitAcrossPages(sel);
			this.confirmSubmit(sel, lib.groupByPlan(this.table.rows.map((r) => ({ name: r.name, ppc_plan: r.ppc_plan })), sel));
		}

		/** Picks made on other pages: look up their plans first (one query per 200). */
		async submitAcrossPages(sel) {
			const plans = new Map();
			for (let page = 1; page <= 50; page++) {
				const r = await ppc.call("get_job_cards", { filters: this.filters.get(), page: page, page_length: 200, sort_by: "name", sort_order: "asc" });
				r.rows.forEach((row) => plans.set(row.name, row.ppc_plan));
				if (page >= r.pages) break;
			}
			this.confirmSubmit(sel, lib.groupByPlan(sel.map((n) => ({ name: n, ppc_plan: plans.get(n) })), sel));
		}

		confirmSubmit(sel, groups) {
			frappe.confirm(
				__("Submit {0} Job Card(s)?", [sel.length]) + "<br><span class='text-muted'>" + __("Earlier operations are submitted first. Each Job Card is validated by ERPNext; failures are listed afterwards and nothing is bypassed.") + "</span>",
				() => ppc.runGroups({
					title: __("Submit Job Cards"), method: "submit_selected_job_cards", argName: "job_cards", doctype: "Job Card", groups: groups,
					batchSize: this.settings.bulk_batch_size || 25, onDone: () => { this.table.clearSelection(); this.reload(true); },
				})
			);
		}
	}
	register("jobcards", JobCardsPage);

	/* =========================================================================================== Schedule History */
	class HistoryPage extends ListPage {
		constructor(wrapper) {
			super(wrapper, "history", {
				description: __("Every accepted schedule change: what it was, what it became, who changed it and why. Entries are append-only."),
				routeKeys: ["plan", "work_order", "job_card", "user", "source", "revision_type", "from", "to", "search", "sync_status", "operation", "workstation", "sales_order_like"],
			});
			this.method = "get_revision_log";
		}

		defaultSort() { return { key: "changed_at", dir: "desc" }; }
		emptyText() { return __("No schedule changes match the filters."); }
		defs() {
			return {
				primary: [
					SEARCH(__("Search Job Card, Work Order, operation or reason")),
					{ fieldname: "job_card", label: "Job Card", fieldtype: "Link", options: "Job Card" },
					{ fieldname: "work_order", label: "Work Order", fieldtype: "Link", options: "Work Order" },
					{ fieldname: "plan", label: "Production Plan", fieldtype: "Link", options: "Production Plan" },
					{ fieldname: "user", label: "Changed by", fieldtype: "Link", options: "User" },
					{ fieldname: "revision_type", label: "Show", fieldtype: "Select", options: "Revision\nBaseline\nAll", default: "Revision" },
					{ fieldname: "from", label: "From", fieldtype: "Date" }, { fieldname: "to", label: "To", fieldtype: "Date" },
				],
				more: [
					{ fieldname: "source", label: "Source", fieldtype: "Select", options: "\nGantt Drag\nGantt Resize\nDate Dialog\nCascade\nWorkstation Change\nAuto Schedule\nReconciliation\nRetry\nMigration\nAPI" },
					{ fieldname: "sync_status", label: "Sync status", fieldtype: "Select", options: "\nSynced\nFailed" },
					{ fieldname: "operation", label: "Operation", fieldtype: "Link", options: "Operation" },
					{ fieldname: "workstation", label: "Workstation", fieldtype: "Link", options: "Workstation" },
					{ fieldname: "sales_order_like", label: "Sales Order (partial)", fieldtype: "Data" },
				],
			};
		}

		columns() {
			const two = (a, b) => (a ? '<span class="ppc-2l">' + ppc.dt(a) + '</span><span class="ppc-2l">' + (b ? ppc.dt(b) : "…") + "</span>" : "–");
			return [
				{ key: "changed_at", label: __("Timestamp"), sort: "changed_at", html: (r) => ppc.dt(r.changed_at) },
				{ key: "job_card", label: __("Job Card"), sort: "job_card", html: (r) => ppc.link("Job Card", r.job_card) + '<span class="ppc-2l text-muted">' + ppc.esc(r.operation || "") + "</span>" },
				{ key: "work_order", label: __("Work Order"), sort: "work_order", html: (r) => ppc.link("Work Order", r.work_order) },
				{ key: "machine", label: __("Workstation"), html: (r) => (r.new_workstation ? ppc.machineChange(r) : ppc.esc(r.workstation || "")) },
				{ key: "revision_no", label: __("Revision"), sort: "revision_no", align: "num", html: (r) => ppc.esc(r.revision_no) + (r.revision_type === "Baseline" ? ' <span class="text-muted">(' + __("baseline") + ")</span>" : "") },
				{ key: "previous", label: __("Previous Start / End"), html: (r) => two(r.previous_start, r.previous_end) },
				{ key: "accepted", label: __("New Start / End"), html: (r) => two(r.accepted_start, r.accepted_end) },
				{ key: "note", label: __("Change Reason / Note"), wrap: true, html: (r) => ppc.esc(r.note || "") },
				{ key: "changed_by", label: __("User"), sort: "changed_by", html: (r) => ppc.esc(r.changed_by || "") },
				{ key: "change_source", label: __("Source"), sort: "change_source" },
				{ key: "sync_status", label: __("Sync Status"), html: (r) => ppc.syncBadge(r.sync_status) },
				{ key: "act", label: "", html: (r) => '<button type="button" class="btn btn-xs btn-default ppc-history" data-name="' + ppc.esc(r.job_card) + '">' + __("History") + "</button>" },
			];
		}
	}
	register("history", HistoryPage);

	/* =========================================================================================== Exceptions */
	class ExceptionsPage extends ListPage {
		constructor(wrapper) {
			super(wrapper, "exceptions", {
				description: __("Everything that needs a planner's attention. Repairs are always previewed first and are never applied to started or finished work."),
				routeKeys: ["plan", "work_order", "job_card", "workstation", "operation", "search", "types"],
			});
			this.method = "get_exceptions";
			this.types = new Set();
		}

		defaultSort() { return { key: null, dir: "asc" }; }
		emptyText() { return __("No exceptions. Everything the checks look at is consistent."); }
		defs() {
			return {
				primary: [
					SEARCH(__("Search Job Card, Work Order or message")),
					{ fieldname: "plan", label: "Production Plan", fieldtype: "Link", options: "Production Plan" },
					{ fieldname: "work_order", label: "Work Order", fieldtype: "Link", options: "Work Order" },
					{ fieldname: "job_card", label: "Job Card", fieldtype: "Link", options: "Job Card" },
					{ fieldname: "workstation", label: "Workstation", fieldtype: "Link", options: "Workstation" },
				],
				more: [],
			};
		}

		args() {
			return { filters: this.filters.get(), types: Array.from(this.types), page: this.pageNo, page_length: this.pageLength };
		}

		applyRoute(filters, raw) {
			const t = (raw && raw.types) || filters.types;
			this.types = new Set(Array.isArray(t) ? t : String(t || "").split(",").filter(Boolean));
			delete filters.types;
			return super.applyRoute(filters);
		}

		columns() {
			const sev = { high: "bad", medium: "warn", low: "" };
			return [
				{ key: "title", label: __("Exception"), html: (r) => ppc.badge(r.title, sev[r.severity] || "") },
				{ key: "job_card", label: __("Job Card"), html: (r) => ppc.link("Job Card", r.job_card) + (r.operation ? '<span class="ppc-2l text-muted">' + ppc.esc(r.operation) + "</span>" : "") },
				{ key: "work_order", label: __("Work Order"), html: (r) => ppc.link("Work Order", r.work_order) },
				{ key: "plan", label: __("Production Plan"), html: (r) => ppc.link("Production Plan", r.plan) },
				{ key: "workstation", label: __("Workstation"), html: (r) => ppc.esc(r.workstation || "") },
				{ key: "message", label: __("Details"), wrap: true, html: (r) => ppc.esc(r.message) },
				{ key: "action", label: __("What to do"), wrap: true, html: (r) => ppc.esc(r.action || "") },
				{ key: "repairable", label: __("Automatic repair"), html: (r) => (r.repairable ? '<span class="ppc-flag ok">' + __("Preview available") + "</span>" : '<span class="text-muted">' + __("Manual") + "</span>") },
				{ key: "act", label: "", html: (r) => (r.job_card ? '<button type="button" class="btn btn-xs btn-default ppc-history" data-name="' + ppc.esc(r.job_card) + '">' + __("History") + '</button> <button type="button" class="btn btn-xs btn-default ppc-ingantt" data-name="' + ppc.esc(r.job_card) + '">' + __("Gantt") + "</button>" : "") },
			];
		}

		afterInit() {
			this.$chips = $('<div class="ppc-chips" role="group" aria-label="' + __("Exception types") + '"></div>').insertBefore(this.$actions);
			const done = () => this.reload(true);
			if (this.can("Reconcile Schedules")) {
				$('<button type="button" class="btn btn-default btn-sm ppc-preview-repair">' + __("Preview repairs") + "</button>").appendTo(this.$actions)
					.on("click", () => ppc.integrity.previewRepairs({ plan: this.filters.get().plan, types: Array.from(this.types).filter((t) => this.repairable(t)), canApply: true, onDone: done }));
				$('<button type="button" class="btn btn-default btn-sm ppc-init-baselines">' + __("Initialize Missing Baselines") + "</button>").appendTo(this.$actions)
					.on("click", () => ppc.integrity.initializeBaselines({ plan: this.filters.get().plan, onDone: done }));
			}
			if (this.can("Retry Failed Operations")) {
				$('<button type="button" class="btn btn-default btn-sm ppc-retry-sync">' + __("Retry Failed Synchronizations") + "</button>").appendTo(this.$actions)
					.on("click", () => ppc.integrity.retryFailed({ plan: this.filters.get().plan, onDone: done }));
			}
			this.$table.on("click", ".ppc-ingantt", (e) => {
				e.preventDefault();
				ppc.go("gantt", { job_card: $(e.currentTarget).data("name") });
			});
		}

		repairable(type) { return ["missing_baseline", "missing_history", "schedule_drift", "work_order_drift", "failed_sync"].includes(type); }
		taskFor() { return null; }

		afterLoad(res) {
			this.$chips.empty();
			(res.types || []).forEach((t) => {
				const n = res.counts[t.type] || 0;
				const on = this.types.has(t.type);
				const $c = $('<button type="button" class="ppc-chip ' + (on ? "on " : "") + (n && t.severity === "high" ? "high" : "") + '" aria-pressed="' + on + '" data-type="' + t.type + '"></button>').appendTo(this.$chips);
				$c.append('<span class="n">' + n + "</span><span>" + ppc.esc(t.title) + "</span>");
				$c.on("click", () => { on ? this.types.delete(t.type) : this.types.add(t.type); this.pageNo = 1; this.reload(true); });
			});
			if (res.truncated) frappe.show_alert({ message: __("The check was limited; narrow the filters to see everything."), indicator: "orange" });
		}
	}
	register("exceptions", ExceptionsPage);

	/* =========================================================================================== Workload & Capacity */
	class WorkloadPage extends ppc.Page {
		constructor(wrapper) {
			super(wrapper, "workload", {
				description: __("Scheduled hours against the working hours ERPNext holds for each workstation. Where a workstation has no working hours, availability is shown as unavailable - never guessed."),
				routeKeys: ["plan", "company", "planner", "workstation", "from_date", "to_date", "customer", "item_code"],
			});
			this._req = 0;
		}

		init() {
			this.filters = new ppc.FilterToolbar(this.$content, {
				primary: [
					COMPANY, { fieldname: "workstation", label: "Workstation", fieldtype: "Link", options: "Workstation" },
					{ fieldname: "plan", label: "Production Plan", fieldtype: "Link", options: "Production Plan" },
					{ fieldname: "planner", label: "Planner", fieldtype: "Link", options: "User" },
					{ fieldname: "from_date", label: "From", fieldtype: "Date", default: today() }, { fieldname: "to_date", label: "To", fieldtype: "Date", default: frappe.datetime.add_days(today(), 27) },
					{ fieldname: "include_idle", label: "Include idle workstations", fieldtype: "Check", default: 1 },
				],
				more: [],
				onChange: () => this.reload(true),
			});
			this.$summary = $('<div class="ppc-summary"></div>').appendTo(this.$content);
			this.$note = $('<div class="ppc-note-banner" hidden></div>').appendTo(this.$content);
			this.$table = $('<div class="ppc-tablehost"></div>').appendTo(this.$content);
			this.$table.on("click", ".ppc-golink", (e) => {
				e.preventDefault();
				const d = $(e.currentTarget).data();
				ppc.go("jobcards", { workstation: d.workstation, window_from: d.from, window_to: d.to });
			});
		}

		applyRoute(filters) {
			const f = Object.assign({}, filters);
			this.filters.set(f, false, true);
			return this.reload(true);
		}

		args() {
			const f = this.filters.get();
			const range = { from_date: f.from_date, to_date: f.to_date, include_idle: f.include_idle ? 1 : 0 };
			delete f.from_date; delete f.to_date; delete f.include_idle;
			return Object.assign({ filters: f }, range);
		}

		load() {
			const id = ++this._req;
			this.$table.html('<div class="ppc-skeleton">' + __("Loading…") + "</div>");
			return ppc.call("get_workload", this.args()).then((r) => { if (id === this._req) this.render(r); this.stamp(r.as_of); })
				.catch(() => { if (id === this._req) this.$table.html('<div class="ppc-error" role="alert">' + __("The workload could not be loaded.") + "</div>"); });
		}

		render(r) {
			const over = r.rows.filter((x) => x.overloaded_days).length;
			this.$summary.html(
				'<div class="ppc-stat"><span class="v">' + r.rows.length + '</span><span class="l">' + __("Workstations") + "</span></div>" +
				'<div class="ppc-stat ' + (over ? "bad" : "") + '"><span class="v">' + over + '</span><span class="l">' + __("Overloaded") + "</span></div>" +
				'<a href="#" class="ppc-stat ' + (r.unassigned_operations ? "warn" : "") + ' ppc-unassigned"><span class="v">' + r.unassigned_operations + '</span><span class="l">' + __("Unassigned operations") + "</span></a>" +
				'<div class="ppc-stat plain"><span class="v sm">' + ppc.dt(r.from) + " → " + ppc.dt(r.to) + '</span><span class="l">' + __("{0} days", [r.days.length]) + "</span></div>"
			);
			this.$summary.find(".ppc-unassigned").on("click", (e) => { e.preventDefault(); ppc.go("jobcards", { unassigned_only: 1 }); });
			this.$note.prop("hidden", !r.notes.length).text(r.notes.join(" "));
			if (!r.rows.length) return this.$table.html('<div class="ppc-empty">' + __("No workstation has scheduled work in this period.") + "</div>");
			let h = '<div class="ppc-table-wrap"><table class="ppc-table ppc-workload"><thead><tr>' +
				[__("Workstation"), __("Capacity"), __("Available"), __("Scheduled"), __("Remaining"), __("Utilization"), __("Overloaded periods"), __("Job Cards"), __("Daily load")].map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
			r.rows.forEach((x) => {
				const cls = lib.utilClass(x.utilization);
				const bar = x.utilization === null
					? '<span class="text-muted">' + (x.available_known ? "–" : __("unavailable")) + "</span>"
					: '<span class="ppc-util ' + cls + '"><span style="width:' + Math.min(100, x.utilization) + '%"></span></span> <b>' + x.utilization + "%</b>";
				const periods = x.periods.length
					? x.periods.map((p) => '<span class="ppc-flag bad">' + ppc.dt(p.from) + (p.to !== p.from ? " → " + ppc.dt(p.to) : "") + " (" + lib.fmtHours(p.peak_hours) + ")</span>").join("<br>")
					: (x.available_known ? '<span class="text-muted">–</span>' : '<span class="text-muted">' + __("unavailable") + "</span>");
				const strip = x.daily.map((s, i) => {
					const a = x.daily_available ? x.daily_available[i] : null;
					const ratio = a === null ? (s ? 0.5 : 0) : (a ? s / a : (s ? 2 : 0));
					const c = a === null ? (s ? "na" : "") : (ratio > 1 ? "bad" : ratio >= 0.85 ? "warn" : ratio > 0 ? "ok" : "");
					return '<i class="' + c + '" title="' + ppc.esc(r.days[i] + ": " + lib.fmtHours(s) + (a === null ? "" : " / " + lib.fmtHours(a))) + '"></i>';
				}).join("");
				h += "<tr><td>" + ppc.esc(x.workstation) + (x.holiday_list ? '<span class="ppc-2l text-muted">' + ppc.esc(x.holiday_list) + "</span>" : "") + '</td><td class="num">' + x.capacity + '</td><td class="num">' +
					(x.available_known ? lib.fmtHours(x.available_hours) : '<span class="text-muted">' + __("unavailable") + "</span>") + '</td><td class="num">' + lib.fmtHours(x.scheduled_hours, 1) +
					'</td><td class="num">' + (x.remaining_hours === null ? '<span class="text-muted">–</span>' : lib.fmtHours(x.remaining_hours)) + "</td><td>" + bar + "</td><td>" + periods + '</td><td class="num">' +
					(x.job_cards ? '<a href="#" class="ppc-golink" data-workstation="' + ppc.esc(x.workstation) + '" data-from="' + r.from + '" data-to="' + r.to + '">' + x.job_cards + "</a>" : "0") + '</td><td><span class="ppc-strip">' + strip + "</span></td></tr>";
			});
			this.$table.html(h + "</tbody></table></div>");
		}
	}
	register("workload", WorkloadPage);

	/* =========================================================================================== Production Gantt */
	class GanttPage extends ppc.Page {
		constructor(wrapper) {
			super(wrapper, "gantt", {
				description: __("Drag a bar to move an operation, drag its edges to change its length. Every change is validated and saved by the server; if it is refused the bar goes back."),
				routeKeys: ["plan", "work_order", "job_card", "workstation", "operation", "status", "search", "sales_order_like", "item_code", "window_from", "window_to",
					"overdue_only", "conflict_only", "sync_status", "company", "planner"],
				staleSeconds: 120,
			});
			this._req = 0;
		}

		init() {
			this.filters = new ppc.FilterToolbar(this.$content, {
				primary: [
					SEARCH(__("Search Job Card, Sales Order or item")),
					{ fieldname: "plan", label: "Production Plan", fieldtype: "Link", options: "Production Plan" },
					{ fieldname: "workstation", label: "Workstation", fieldtype: "Link", options: "Workstation" },
					{ fieldname: "status", label: "Status", fieldtype: "Select", options: "\nOpen\nMaterial Transferred\nWork In Progress\nOn Hold\nCompleted\nSubmitted" },
					{ fieldname: "window_from", label: "From", fieldtype: "Date", default: frappe.datetime.add_days(today(), -7) },
					{ fieldname: "window_to", label: "To", fieldtype: "Date", default: frappe.datetime.add_days(today(), 45) },
				],
				more: [
					{ fieldname: "work_order", label: "Work Order", fieldtype: "Link", options: "Work Order" },
					{ fieldname: "operation", label: "Operation", fieldtype: "Link", options: "Operation" },
					{ fieldname: "item_code", label: "Finished Good", fieldtype: "Link", options: "Item" },
					{ fieldname: "sales_order_like", label: "Sales Order (partial)", fieldtype: "Data" },
					{ fieldname: "sync_status", label: "Sync status", fieldtype: "Select", options: "\nFailed\nDrift\nNo baseline\nRescheduled\nSynced\nEstimated" },
					COMPANY, { fieldname: "overdue_only", label: "Overdue only", fieldtype: "Check" }, { fieldname: "conflict_only", label: "Conflicts only", fieldtype: "Check" },
				],
				onChange: () => this.reload(true),
			});
			this.$actions = $('<div class="ppc-actionbar"></div>').appendTo(this.$content);
			$('<button type="button" class="btn btn-default btn-sm ppc-fit-all">' + __("Show all dates") + "</button>").appendTo(this.$actions).on("click", () => { this.filters.set({ window_from: "", window_to: "" }, false, true); this.reload(true); });
			$('<button type="button" class="btn btn-default btn-sm ppc-reset-window">' + __("Reset to today ± window") + "</button>").appendTo(this.$actions)
				.on("click", () => { this.filters.set({ window_from: frappe.datetime.add_days(today(), -7), window_to: frappe.datetime.add_days(today(), 45) }, false, true); this.reload(true); });
			this.$machines = $('<label class="ppc-inline-check"><input type="checkbox" class="ppc-all-machines"> ' + __("Show all machines") + "</label>").appendTo(this.$actions);
			this.$machines.find("input").prop("checked", !!ppc.pref.get("gantt_all_machines", false)).on("change", (e) => {
				ppc.pref.set("gantt_all_machines", e.target.checked);
				if (this.gantt) this.gantt.setShowAllMachines(e.target.checked);
			});
			this.$count = $('<span class="text-muted ppc-count"></span>').appendTo(this.$actions);
			this.$tzNote = $('<span class="text-muted ppc-tznote"></span>').appendTo(this.$actions);
			this.$ganttHost = $('<div class="ppc-ganttpage"></div>').appendTo(this.$content);
			this.drawer = new ppc.Drawer(this.$root);
			this.gantt = null;
		}

		applyRoute(filters) {
			// arriving from another page with explicit filters: do not silently keep the default date window
			const f = Object.assign({}, filters);
			if (!("window_from" in f) && !("window_to" in f)) { f.window_from = ""; f.window_to = ""; }
			this.filters.set(f, true, true);
			return this.reload(true);
		}

		taskFor(name) { return this.gantt ? this.gantt.taskByName(name) : null; }

		ensureGantt() {
			if (this.gantt) return;
			const lib2 = window.PPCGanttLib;
			this.gantt = new lib2.PPCGantt(this.$ganttHost[0], {
				height: 620, scale: ppc.pref.get("gantt_scale", "week"), groupBy: ppc.pref.get("gantt_group", "workstation"), now: null,
				canEdit: this.can("Reschedule"), canChangeWorkstation: this.can("Reschedule"), timezoneLabel: ppc.siteTimeZone(),
				gridMode: ppc.pref.get("gantt_cols", "compact"), tooltip: ppc.taskTooltip,
				onChange: (evt) => this.onChange(evt),
				onEdit: (task) => (this.can("Reschedule") ? ppc.editDates(task, () => this.reload(true)) : frappe.set_route("Form", "Job Card", task.name)),
				onSelect: (task) => { if (task && !(this.drawer.isOpen() && this.drawer.current === task.name)) this.openHistory(task.name); },
			});
		}

		openHistory(name) {
			return ppc.openHistory(this.drawer, name, { task: this.taskFor(name), canReschedule: this.can("Reschedule"), onChanged: () => this.reload(true), busy: (p) => this.busyWhile(p) });
		}

		load() {
			const id = ++this._req;
			return ppc.call("get_gantt_data", { filters: this.filters.get(), limit: this.settings.max_gantt_tasks }).then((r) => {
				if (id !== this._req) return;
				this.ensureGantt();
				if (r.as_of) this.gantt.opts.now = r.as_of; // the Gantt's "today" follows the site clock, not the browser's
				this.gantt.showAllMachines = !!ppc.pref.get("gantt_all_machines", false);
				this.gantt.setData(r);
				this.$tzNote.text(ppc.timeZoneNote());
				ppc.pref.set("gantt_scale", this.gantt.scale);
				ppc.pref.set("gantt_group", this.gantt.groupBy);
				ppc.pref.set("gantt_cols", this.gantt.gridMode);
				this.$count.text(r.truncated ? __("Showing the first {0} of {1} operations - narrow the filters or the dates.", [r.tasks.length, r.total]) : __("{0} operation(s)", [r.total]));
				if (!r.tasks.length) this.$ganttHost.addClass("ppc-gantt-empty");
				else this.$ganttHost.removeClass("ppc-gantt-empty");
				if (this.drawer.isOpen() && this.drawer.current) return this.openHistory(this.drawer.current);
			}).catch(() => {
				if (id === this._req) frappe.show_alert({ message: __("The Gantt data could not be loaded."), indicator: "red" });
			});
		}

		async onChange(evt) {
			if (evt.workstation) { // dropped on another machine's row (maybe with new dates too)
				const moved = await ppc.changeWorkstation(evt.task, evt.workstation, { start: evt.start, end: evt.end });
				if (moved.ok || moved.stale) this.reload(true);
				return moved;
			}
			const res = await ppc.reschedule(evt.task, evt.start, evt.end, evt.mode, { source: evt.mode === "move" ? "Gantt Drag" : "Gantt Resize" });
			// refresh bars, tables and the open history after a success (cascades move other bars) and after a stale refusal
			if (res.ok || res.stale) this.reload(true);
			return res;
		}
	}
	register("gantt", GanttPage);

	/* =========================================================================================== Dashboard */
	const PERIODS = { "": "", "All dates": "", "Next 7 days": "next7", "Next 30 days": "next30", "This month": "month", "Last 30 days": "last30" };

	class DashboardPage extends ppc.Page {
		constructor(wrapper) {
			super(wrapper, "dashboard", {
				description: __("Production planning and control at a glance. Every number is counted from live ERPNext records and opens the matching list."),
				primary: { label: __("Create Production Plan"), action: () => frappe.new_doc("Production Plan") },
				routeKeys: ["company", "planner", "date_from", "date_to"],
			});
			this._req = 0;
			this._period = "";
		}

		init() {
			this.filters = new ppc.FilterToolbar(this.$content, {
				primary: [
					COMPANY, { fieldname: "planner", label: "Planner", fieldtype: "Link", options: "User" },
					{ fieldname: "period", label: "Period", fieldtype: "Select", options: "All dates\nNext 7 days\nNext 30 days\nThis month\nLast 30 days", default: "All dates" },
					{ fieldname: "date_from", label: "From", fieldtype: "Date" }, { fieldname: "date_to", label: "To", fieldtype: "Date" },
				],
				more: [],
				onChange: (f) => this.onFilter(f),
			});
			this.kpis = new ppc.KpiGrid(this.$content, (key) => this.openKpi(key));
			this.$grid = $('<div class="ppc-dash-grid"></div>').appendTo(this.$content);
			const go = (key, f) => () => ppc.go(key, f || {});
			this.panels = {
				schedule: new ppc.Panel(this.$grid, { title: "Production schedule overview", cls: "wide", actions: [{ label: __("Open Gantt"), onClick: go("gantt") }] }),
				risks: new ppc.Panel(this.$grid, { title: "Upcoming delivery risks", actions: [{ label: __("Work Orders"), onClick: go("workorders", { wo_status: "Open" }) }] }),
				overdue: new ppc.Panel(this.$grid, { title: "Overdue Job Cards", actions: [{ label: __("View all"), onClick: () => this.openKpi("overdue_operations") }] }),
				workload: new ppc.Panel(this.$grid, { title: "Workstation workload", actions: [{ label: __("Capacity"), onClick: go("workload") }] }),
				failed: new ppc.Panel(this.$grid, { title: "Failed synchronizations", actions: [{ label: __("Exceptions"), onClick: go("exceptions", { types: "failed_sync" }) }] }),
				recent: new ppc.Panel(this.$grid, { title: "Recent schedule revisions", actions: [{ label: __("All history"), onClick: go("history") }] }),
				quick: new ppc.Panel(this.$grid, { title: "Quick actions" }),
			};
			this.renderQuick();
		}

		onFilter(f) {
			if ((f.period || "") !== this._period) {
				this._period = f.period || "";
				if (PERIODS[this._period] !== undefined) this.filters.set(lib.periodRange(PERIODS[this._period], today()), false, true);
			}
			this.reload(true);
		}

		apiFilters() {
			const f = this.filters.get();
			delete f.period;
			return f;
		}

		openKpi(key) {
			const target = lib.kpiTarget(key, this.apiFilters());
			if (target) ppc.go(target.page, target.filters);
		}

		renderQuick() {
			const $b = this.panels.quick.html('<div class="ppc-quick"></div>');
			const add = (label, fn, cls) => $('<button type="button" class="btn btn-sm ' + (cls || "btn-default") + '"></button>').text(label).appendTo($b.find(".ppc-quick")).on("click", fn);
			add(__("Create Production Plan"), () => frappe.new_doc("Production Plan"), "btn-primary");
			add(__("Open Production Gantt"), () => ppc.go("gantt"));
			add(__("Review exceptions"), () => ppc.go("exceptions"));
			add(__("Submit draft Work Orders"), () => ppc.go("workorders", { wo_status: "Draft" }));
			add(__("Workload & Capacity"), () => ppc.go("workload"));
			if (this.can("Retry Failed Operations")) add(__("Retry failed synchronizations"), () => ppc.go("exceptions", { types: "failed_sync" }));
			add(__("PPC Settings"), () => ppc.go("settings"));
		}

		load() {
			const id = ++this._req;
			this.kpis.loading();
			["schedule", "risks", "overdue", "workload", "failed", "recent"].forEach((k) => this.panels[k].loading());
			return ppc.call("get_dashboard_overview", { filters: this.apiFilters() }).then((o) => {
				if (id !== this._req) return;
				this.kpis.render(o.kpis);
				this.renderSchedule(o);
				this.renderRisks(o.delivery_risks);
				this.renderOverdue(o.overdue);
				this.renderWorkload(o.workload);
				this.renderFailed(o.failed);
				this.renderRecent(o.recent_revisions);
				this.stamp(o.as_of);
			}).catch(() => {
				if (id !== this._req) return;
				const retry = () => this.reload(true);
				this.$content.find(".ppc-kpis2").html('<div class="ppc-error" role="alert">' + __("The dashboard could not be loaded.") + ' <button type="button" class="btn btn-xs btn-default ppc-retry">' + __("Try again") + "</button></div>").find(".ppc-retry").on("click", retry);
				["schedule", "risks", "overdue", "workload", "failed", "recent"].forEach((k) => this.panels[k].error(__("This section could not be loaded."), retry));
			});
		}

		renderSchedule(o) {
			const f = this.apiFilters();
			const lo = f.date_from || frappe.datetime.add_days(today(), -3);
			const hi = f.date_to || frappe.datetime.add_days(today(), 30);
			const days = (o.schedule || []).filter((s) => s.day >= lo && s.day <= hi);
			if (!days.length) return this.panels.schedule.empty(__("No scheduled operations between {0} and {1}.", [ppc.dt(lo), ppc.dt(hi)]));
			const $c = this.panels.schedule.html('<div class="ppc-chart" role="img" aria-label="' + __("Scheduled hours per start day") + '"></div>').find(".ppc-chart");
			if (window.frappe && frappe.Chart) {
				new frappe.Chart($c[0], { data: { labels: days.map((s) => s.day.slice(5)), datasets: [{ name: __("Scheduled hours"), values: days.map((s) => s.hours) }] }, type: "bar", height: 200, colors: ["#2563eb"], axisOptions: { xIsSeries: true }, barOptions: { spaceRatio: 0.4 } });
			}
			this.panels.schedule.$body.append('<div class="text-muted ppc-caption">' + __("Hours of operations starting each day, between {0} and {1}.", [ppc.dt(lo), ppc.dt(hi)]) + "</div>");
		}

		renderRisks(rows) {
			if (!rows.length) return this.panels.risks.empty(__("No Work Order is due soon or planned after its delivery date."));
			this.panels.risks.html(ppc.miniTable([
				{ label: __("Delivery"), html: (r) => ppc.dt(r.delivery) },
				{ label: __("Risk"), html: (r) => ppc.badge(__(r.risk), r.risk === "Due soon" ? "warn" : "bad") },
				{ label: __("Work Order"), html: (r) => ppc.link("Work Order", r.work_order) + (r.draft ? ' <span class="text-muted">(' + __("draft") + ")</span>" : "") },
				{ label: __("Customer"), html: (r) => ppc.esc(r.customer_name || r.sales_order || "") },
				{ label: __("Item"), html: (r) => ppc.esc(r.item_code) },
				{ label: __("Planned end"), html: (r) => ppc.dt(r.planned_end) },
			], rows));
		}

		renderOverdue(res) {
			if (!res.rows.length) return this.panels.overdue.empty(__("No overdue operations."));
			this.panels.overdue.html(ppc.miniTable([
				{ label: __("Job Card"), html: (r) => ppc.link("Job Card", r.name) },
				{ label: __("Operation"), html: (r) => ppc.esc(r.operation) },
				{ label: __("Workstation"), html: (r) => ppc.esc(r.workstation || "–") },
				{ label: __("Was due"), html: (r) => ppc.dt(r.revised_end) },
				{ label: __("Status"), html: (r) => ppc.badge(r.status) },
			], res.rows));
			if (res.total > res.rows.length) this.panels.overdue.$body.append('<div class="text-muted ppc-caption">' + __("Showing {0} of {1}.", [res.rows.length, res.total]) + "</div>");
		}

		renderWorkload(rows) {
			if (!rows.length) return this.panels.workload.empty(__("No open operations are scheduled."));
			const max = Math.max.apply(null, rows.map((w) => w.scheduled_hours).concat([1]));
			this.panels.workload.html('<div class="ppc-bars">' + rows.map((w) =>
				'<div class="ppc-bar-row"><div title="' + ppc.esc(w.workstation) + '"><b>' + ppc.esc(w.workstation) + "</b></div>" +
				'<div class="ppc-meter" role="img" aria-label="' + w.scheduled_hours + ' h"><span class="' + (w.overdue_cards ? "over" : "") + '" style="width:' + Math.round((100 * w.scheduled_hours) / max) + '%"></span></div>' +
				"<div>" + lib.fmtHours(w.scheduled_hours) + " · " + w.job_cards + (w.overdue_cards ? ' · <span class="ppc-flag bad">' + w.overdue_cards + " " + __("overdue") + "</span>" : "") + "</div></div>").join("") + "</div>");
		}

		renderFailed(res) {
			if (!res.rows.length) return this.panels.failed.empty(__("No failed synchronizations."));
			this.panels.failed.html(ppc.miniTable([
				{ label: __("Job Card"), html: (r) => ppc.link("Job Card", r.name) },
				{ label: __("Operation"), html: (r) => ppc.esc(r.operation) },
				{ label: __("Work Order"), html: (r) => ppc.link("Work Order", r.work_order) },
				{ label: __("Last change by"), html: (r) => ppc.esc(r.last_rescheduled_by || "–") },
			], res.rows));
		}

		renderRecent(rows) {
			if (!rows.length) return this.panels.recent.empty(__("No schedule has been changed yet."));
			this.panels.recent.html(ppc.miniTable([
				{ label: __("When"), html: (r) => ppc.dt(r.changed_at) },
				{ label: __("Job Card"), html: (r) => ppc.link("Job Card", r.job_card) },
				{ label: __("Rev"), cls: "num", html: (r) => ppc.esc(r.revision_no) },
				{ label: __("New start"), html: (r) => ppc.dt(r.accepted_start) },
				{ label: __("By"), html: (r) => ppc.esc(r.changed_by || "") },
			], rows));
		}
	}
	register("dashboard", DashboardPage);
})();
