/* PPC Control Center - shared page shell and components (window.ppc): navigation, page base class, filter toolbar,
 * KPI grid, panels, side drawer and the schedule-history view. Loaded through ppc_control.bundle.js. */
(function () {
	"use strict";
	const ppc = (window.ppc = window.ppc || {});
	const lib = window.PPCUiLib;

	/* ======================================================================= capabilities, routing */
	let capsPromise = null;
	/** What the user may do (cached for the session: it only decides which buttons are shown; the server re-checks everything). */
	ppc.capabilities = function () {
		if (!capsPromise) capsPromise = ppc.call("get_capabilities").catch((e) => { capsPromise = null; throw e; });
		return capsPromise;
	};

	ppc.routeFor = function (key) {
		const p = lib.pageByKey(key);
		return p.form ? "/app/" + frappe.router.slug(p.form) : "/app/" + p.route;
	};

	/** Open a Control Center page, optionally with filters (read by the page as route options). */
	ppc.go = function (key, filters) {
		const p = lib.pageByKey(key);
		frappe.route_options = Object.assign({}, filters || {});
		if (p.form) frappe.set_route("Form", p.form);
		else frappe.set_route(p.route);
	};

	/** Route options for the page being shown: Frappe's own, plus any `?x=y` query string (never anything else). */
	ppc.takeRouteOptions = function () {
		const opts = Object.assign({}, frappe.route_options || {});
		frappe.route_options = null;
		try {
			new URLSearchParams(window.location.search).forEach((v, k) => { if (!(k in opts)) opts[k] = v; });
		} catch (e) { /* old browser: route options only */ }
		return opts;
	};

	/* ======================================================================= icons (presentation only) */
	const ICON_PATHS = {
		dashboard: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
		plans: '<path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6M9 16h4"/>',
		workorders: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
		jobcards: '<path d="M12 3 3 8l9 5 9-5z"/><path d="m3 13 9 5 9-5M3 17.5l9 5 9-5" opacity=".7"/>',
		gantt: '<path d="M4 6h9M8 12h12M6 18h8"/><circle cx="4" cy="6" r="1"/><circle cx="20" cy="12" r="1"/><circle cx="14" cy="18" r="1"/>',
		workload: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
		history: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
		exceptions: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
		settings: '<path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
		qty: '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
		draft: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
		open: '<circle cx="12" cy="12" r="9"/><path d="m10 8 6 4-6 4z"/>',
		wip: '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>',
		done: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
		overdue: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2M9 2h6"/>',
		conflict: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
		failed: '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
	};
	const KPI_ICON = {
		total_plans: "plans", pending_qty: "qty", draft_work_orders: "draft", open_work_orders: "open", total_job_cards: "jobcards",
		work_in_progress: "wip", completed_job_cards: "done", overdue_operations: "overdue", scheduling_conflicts: "conflict", failed_syncs: "failed",
	};
	ppc.icon = function (name, cls) {
		return '<svg class="' + (cls || "ppc-ic") + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + (ICON_PATHS[name] || "") + "</svg>";
	};

	/* ======================================================================= navigation */
	ppc.renderNav = function ($parent, activeKey) {
		const $nav = $('<nav class="ppc-nav" aria-label="' + __("PPC Control Center") + '"></nav>').appendTo($parent);
		lib.PAGES.forEach(function (p) {
			const $a = $('<a class="ppc-nav-item"></a>').attr("href", ppc.routeFor(p.key)).html(ppc.icon(p.key) + ppc.esc(__(p.label))).appendTo($nav);
			if (p.key === activeKey) $a.addClass("active").attr("aria-current", "page");
			$a.on("click", function (e) {
				if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return; // let the browser open a new tab
				e.preventDefault();
				ppc.go(p.key);
			});
		});
		return $nav;
	};

	/* ======================================================================= page base class */
	ppc.Page = class Page {
		/**
		 * opts: {description, primary: {label, action, icon}, routeKeys: [filter keys the page accepts from route options],
		 *        staleSeconds}
		 */
		constructor(wrapper, key, opts) {
			this.wrapper = wrapper;
			this.key = key;
			this.opts = Object.assign({ routeKeys: [], staleSeconds: 90 }, opts || {});
			const def = lib.pageByKey(key);
			this.page = frappe.ui.make_app_page({ parent: wrapper, title: __(def.label), single_column: true });
			this.$root = $('<div class="ppc ppc-page" data-ppc-page="' + key + '"></div>').appendTo(this.page.main);
			ppc.renderNav(this.$root, key);
			this.$bar = $('<div class="ppc-pagebar"><div class="ppc-pagebar-desc"></div><div class="ppc-pagebar-meta"><span class="ppc-updated" aria-live="polite"></span></div></div>').appendTo(this.$root);
			this.$bar.find(".ppc-pagebar-desc").text(this.opts.description || "");
			this.$content = $('<div class="ppc-content"></div>').appendTo(this.$root);
			this.page.set_secondary_action(__("Refresh"), () => this.reload(true), "refresh");
			if (this.opts.primary) this.page.set_primary_action(this.opts.primary.label, this.opts.primary.action, this.opts.primary.icon || "add");
			this._busy = 0;
			this.loadedAt = 0;
			this.ready = ppc.capabilities().then((c) => {
				this.caps = c.capabilities || {};
				this.settings = c;
				if (!this.caps["View"]) return this.denied();
				try {
					return this.init();
				} catch (e) {
					console.error("PPC page failed to start", e); // eslint-disable-line no-console
					return this.failed();
				}
			}, () => this.denied());
		}

		can(cap) { return !!(this.caps && this.caps[cap]); }
		init() { /* build the page */ }
		load() { return Promise.resolve(); }

		reload(force) {
			// the filter controls apply values asynchronously: read them only once they have settled
			return this.ready.then(() => this.unusable ? null : this.busyWhile(
				Promise.resolve(this.filters && this.filters.whenSettled()).then(() => this.load(force)).then(() => { this.loadedAt = Date.now(); this.stamp(); })));
		}

		/** Called every time Frappe shows the page: apply route options, and refresh if the data is old. */
		show(held) {
			if (this._showing) { // a second show while the first is still starting: keep any options it brought
				this._pendingOpts = Object.assign(this._pendingOpts || {}, held || {});
				return this._showing;
			}
			this._showing = this.ready.then(() => {
				if (this.unusable) return;
				const opts = Object.assign({}, held || ppc.takeRouteOptions(), this._pendingOpts || {});
				this._pendingOpts = null;
				const filters = lib.routeFilters(opts, this.opts.routeKeys);
				if (Object.keys(filters).length || opts.__reset) return this.applyRoute(filters, opts);
				if (!this.loadedAt || Date.now() - this.loadedAt > this.opts.staleSeconds * 1000) return this.reload();
			}).finally(() => { this._showing = null; });
			return this._showing;
		}

		applyRoute(filters) {
			if (this.filters) this.filters.set(filters, true, true);
			return this.reload(true);
		}

		denied() {
			this.$content.html('<div class="ppc-empty">' + __("You do not have access to the PPC Control Center. Ask an administrator for a PPC role.") + "</div>");
			this.unusable = true;
		}

		failed() {
			this.$content.html('<div class="ppc-error" role="alert">' + __("This page could not be started. Reload it; if it happens again, tell your administrator.") + "</div>");
			this.unusable = true;
		}

		stamp(iso) {
			const when = iso ? frappe.datetime.str_to_user(iso) : frappe.datetime.str_to_user(frappe.datetime.now_datetime());
			this.$bar.find(".ppc-updated").text(__("Updated {0}", [when]));
		}

		/** `data-ppc-busy` counts loads in flight, so tests (and anything waiting for data) can wait on real state. */
		busyWhile(work) {
			this._busy += 1;
			this.$root.attr("data-ppc-busy", this._busy);
			const done = () => { this._busy -= 1; this.$root.attr("data-ppc-busy", this._busy); };
			return Promise.resolve(work).then((r) => { done(); return r; }, (e) => { done(); throw e; });
		}
	};

	/* ======================================================================= filter toolbar */
	ppc.FilterToolbar = class FilterToolbar {
		/**
		 * cfg: {primary: [df], more: [df], onChange(filters), delay}
		 * df: {fieldname, label, fieldtype, options, default, width}. Values are read back with `get()` (empty ones dropped).
		 */
		constructor($parent, cfg) {
			this.cfg = cfg;
			this.controls = {};
			this.muted = true; // starting values are not user changes
			this._settled = Promise.resolve();
			this.fire = lib.debounce(() => { if (!this.muted && this.cfg.onChange) this.cfg.onChange(this.get()); }, cfg.delay || 350);
			this.$el = $('<section class="ppc-filters2" aria-label="' + __("Filters") + '"></section>').appendTo($parent);
			this.$primary = $('<div class="ppc-fgrid"></div>').appendTo(this.$el);
			this.$tools = $('<div class="ppc-ftools"></div>').appendTo(this.$el);
			this.$more = $('<div class="ppc-fgrid ppc-fmore" hidden></div>').insertAfter(this.$primary);
			(cfg.primary || []).forEach((df) => this._add(df, this.$primary));
			(cfg.more || []).forEach((df) => this._add(df, this.$more));
			this._settle(Object.keys(this.controls).map((k) => this.controls[k]._initial).filter(Boolean));
			if ((cfg.more || []).length) {
				this.$toggle = $('<button type="button" class="btn btn-default btn-xs ppc-fmore-toggle" aria-expanded="false"></button>').appendTo(this.$tools)
					.on("click", () => this.toggleMore());
			}
			$('<button type="button" class="btn btn-default btn-xs ppc-fclear">' + __("Clear filters") + "</button>").appendTo(this.$tools).on("click", () => { this.clear(); });
			this._label();
		}

		_add(df, $into) {
			const $cell = $('<div class="ppc-fcell"></div>').appendTo($into);
			if (df.width) $cell.css("grid-column", "span " + df.width);
			const full = Object.assign({}, df, { label: __(df.label), change: () => this._changed(), _default: df.default });
			const control = frappe.ui.form.make_control({ df: full, parent: $cell, render_input: true });
			control.refresh();
			if (df.default !== undefined && df.default !== null && df.default !== "") control._initial = control.set_value(df.default);
			if (control.$input) {
				control.$input.on("keydown", (e) => { if (e.key === "Enter") { control.$input.trigger("change"); this.fire(); } });
				if (["Data", "Small Text"].includes(df.fieldtype)) control.$input.on("input", () => this.fire());
			}
			this.controls[df.fieldname] = control;
		}

		_changed() { this._label(); if (!this.muted) this.fire(); }

		/** Hold change events back until `jobs` (promises from control.set_value) have all settled. */
		_settle(jobs) {
			this.muted = true;
			const mine = (this._settled = Promise.all([this._settled].concat(jobs.map((j) => Promise.resolve(j).catch(() => null)))).then(() => {
				if (this._settled === mine) { this.muted = false; this._label(); }
			}));
			return mine;
		}

		whenSettled() { return this._settled; }

		_value(fieldname) {
			const c = this.controls[fieldname];
			if (["Data", "Small Text"].includes(c.df.fieldtype) && c.$input) return c.$input.val();
			return c.get_value();
		}

		get() {
			const out = {};
			Object.keys(this.controls).forEach((k) => { out[k] = this._value(k); });
			return lib.cleanFilters(out);
		}

		/** Replace the filters (`replace`: also clear the ones not mentioned). `silent`: do not trigger onChange. */
		set(values, replace, silent) {
			const jobs = [];
			this.muted = true;
			Object.keys(this.controls).forEach((k) => {
				const c = this.controls[k];
				if (k in (values || {})) jobs.push(c.set_value(values[k]));
				else if (replace) jobs.push(c.set_value(c.df.fieldtype === "Check" ? 0 : (c.df._default !== undefined ? c.df._default : "")));
			});
			const done = this._settle(jobs);
			if (!silent) done.then(() => this.fire());
			return done;
		}

		clear() {
			this.fire.cancel();
			return this.set({}, true, true).then(() => { if (this.cfg.onChange) this.cfg.onChange(this.get()); });
		}

		toggleMore(force) {
			const open = force === undefined ? this.$more.prop("hidden") : force;
			this.$more.prop("hidden", !open);
			if (this.$toggle) this.$toggle.attr("aria-expanded", open ? "true" : "false");
			this._label();
		}

		_label() {
			if (!this.$toggle) return;
			const active = (this.cfg.more || []).filter((df) => {
				const v = this.controls[df.fieldname] && this._value(df.fieldname);
				return v !== "" && v !== null && v !== undefined && v !== 0;
			}).length;
			this.$toggle.text((this.$more.prop("hidden") ? __("More filters") : __("Fewer filters")) + (active ? " (" + active + ")" : ""));
		}
	};

	/* ======================================================================= KPI grid, panels, tables */
	ppc.KpiGrid = class KpiGrid {
		constructor($parent, onOpen) {
			this.onOpen = onOpen;
			this.$el = $('<div class="ppc-kpis2" role="list"></div>').appendTo($parent);
		}

		render(values) {
			this.$el.empty();
			lib.KPIS.forEach((def) => {
				const v = values && values[def.key] != null ? values[def.key] : 0;
				const tone = def.tone && v ? def.tone : "";
				const $k = $('<button type="button" class="ppc-kpi2 ' + tone + '" role="listitem" data-kpi="' + def.key + '"></button>').appendTo(this.$el);
				$k.attr("aria-label", __(def.label) + ": " + v).append(
					'<span class="ppc-kpi-ic" aria-hidden="true">' + ppc.icon(KPI_ICON[def.key] || "qty") + '</span><span class="ppc-kpi-txt"><span class="v">' + ppc.num(v, def.decimals ? 3 : 0) + '</span><span class="l">' + ppc.esc(__(def.label)) + "</span></span>");
				$k.on("click", () => this.onOpen && this.onOpen(def.key));
			});
		}

		loading() { this.$el.html('<div class="ppc-skeleton">' + __("Loading…") + "</div>"); }
	};

	ppc.Panel = class Panel {
		constructor($parent, cfg) {
			this.cfg = cfg || {};
			this.$el = $('<section class="ppc-panel ' + (cfg.cls || "") + '"></section>').appendTo($parent);
			this.$head = $('<header class="ppc-panel-head"><h3></h3><span class="ppc-panel-actions"></span></header>').appendTo(this.$el);
			this.$head.find("h3").text(__(cfg.title || ""));
			(cfg.actions || []).forEach((a) => {
				$('<button type="button" class="btn btn-link btn-xs"></button>').text(a.label).appendTo(this.$head.find(".ppc-panel-actions")).on("click", a.onClick);
			});
			this.$body = $('<div class="ppc-panel-body"></div>').appendTo(this.$el);
		}

		loading() { this.$body.html('<div class="ppc-skeleton">' + __("Loading…") + "</div>"); }
		empty(msg) { this.$body.html('<div class="ppc-empty ppc-empty-sm">' + ppc.esc(msg || __("Nothing to show.")) + "</div>"); }
		html(h) { this.$body.html(h); return this.$body; }
		error(msg, retry) {
			this.$body.html('<div class="ppc-error" role="alert">' + ppc.esc(msg || __("This section could not be loaded.")) + (retry ? ' <button type="button" class="btn btn-xs btn-default ppc-retry">' + __("Try again") + "</button>" : "") + "</div>");
			if (retry) this.$body.find(".ppc-retry").on("click", retry);
		}
	};

	/** Small read-only table as HTML: columns [{label, html(row), cls}], rows. */
	ppc.miniTable = function (columns, rows) {
		let h = '<div class="ppc-table-wrap ppc-mini"><table class="ppc-table"><thead><tr>' + columns.map((c) => "<th>" + ppc.esc(c.label) + "</th>").join("") + "</tr></thead><tbody>";
		rows.forEach((r) => { h += "<tr>" + columns.map((c) => '<td class="' + (c.cls || "") + '">' + c.html(r) + "</td>").join("") + "</tr>"; });
		return h + "</tbody></table></div>";
	};

	/* ======================================================================= drawer */
	ppc.Drawer = class Drawer {
		constructor($parent) {
			this.$el = $('<aside class="ppc-drawer" role="complementary" aria-label="' + __("Details") + '" hidden><header><h3></h3><button type="button" class="ppc-drawer-close" aria-label="' + __("Close") + '">×</button></header><div class="ppc-drawer-body"></div></aside>').appendTo($parent);
			this.$el.find(".ppc-drawer-close").on("click", () => this.close());
			this.$el.on("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); this.close(); } });
			this.current = null;
		}

		isOpen() { return !this.$el.prop("hidden"); }
		open(title, key, keepFocus) {
			const wasOpen = this.isOpen();
			if (!wasOpen) this.opener = document.activeElement;
			this.current = key || null;
			this.$el.find("h3").text(title || "");
			this.$el.prop("hidden", false).attr("data-open", "1");
			this.$el.closest(".ppc").addClass("ppc-has-drawer");
			if (!(keepFocus && wasOpen)) this.$el.find(".ppc-drawer-close").trigger("focus");
			return this.$el.find(".ppc-drawer-body");
		}

		close() {
			this.$el.prop("hidden", true).removeAttr("data-open");
			this.$el.closest(".ppc").removeClass("ppc-has-drawer");
			this.current = null;
			if (this.opener && document.contains(this.opener)) $(this.opener).trigger("focus");
			this.opener = null;
			if (this.onClose) this.onClose();
		}

		body() { return this.$el.find(".ppc-drawer-body"); }
	};

	/** "WS-1 → WS-2" for a revision that moved the Job Card to another machine (with a flag when a conflict was overridden). */
	ppc.machineChange = function (r) {
		if (!r.new_workstation) return '<span class="text-muted">–</span>';
		return ppc.esc(r.previous_workstation || "–") + " → <b>" + ppc.esc(r.new_workstation) + "</b>" +
			(r.override_used ? ' <span class="ppc-flag bad" title="' + ppc.esc(r.override_reason || __("conflict overridden")) + '">' + __("override") + "</span>" : "");
	};

	/* ======================================================================= schedule history view (drawer content) */
	ppc.historyHtml = function (h, task) {
		const range = (r) => (r && r.start ? ppc.dt(r.start) + " → " + (r.end ? ppc.dt(r.end) : "…") : "–");
		const kv = (k, v) => '<div><span class="k">' + k + "</span>" + v + "</div>";
		const bad = h.sync_status === "Failed" || h.sync_status === "Drift";
		let html = '<div class="ppc-hist-head">' + ppc.badge(h.status) + " " + ppc.syncBadge(h.sync_status) + "</div>";
		html += '<div class="ppc-detail-grid">' +
			kv(__("Job Card"), ppc.link("Job Card", h.job_card)) +
			kv(__("Operation"), ppc.esc(task ? (task.operation || "") + (task.workstation ? " · " + task.workstation : "") : "") || "–") +
			kv(__("Work Order"), ppc.link("Work Order", h.work_order) || "–") +
			kv(__("Original schedule") + (h.baseline.label ? " (" + ppc.esc(h.baseline.label) + ")" : ""), h.has_baseline ? range(h.baseline) : '<span class="ppc-flag warn">' + __("not recorded yet") + "</span>") +
			kv(__("Current schedule"), range(h.current)) +
			kv(__("Actual execution"), range(h.actual)) +
			kv(__("Revisions"), ppc.esc(h.revision)) +
			kv(__("Last rescheduled"), h.last_rescheduled_at ? ppc.esc(h.last_rescheduled_by || "") + ", " + ppc.dt(h.last_rescheduled_at) : "–") +
			"</div>";
		if (h.difference && h.difference.text) html += '<div class="ppc-detail-diff' + (bad ? " bad" : "") + '">' + ppc.esc(h.difference.text) + "</div>";
		if (h.sync_status === "Failed") html += '<p class="ppc-flag bad">' + __("The last reschedule was rolled back. Use Retry Failed Synchronizations on the Exceptions page or reschedule again.") + "</p>";
		if (h.sync_status === "Drift") html += '<p class="ppc-flag bad">' + __("The Job Card no longer matches the last schedule PPC accepted. See the Exceptions page.") + "</p>";
		html += '<h4 class="ppc-sub">' + __("Revision history") + "</h4>";
		html += '<div class="ppc-table-wrap ppc-mini"><table class="ppc-table"><thead><tr>' +
			[__("Rev"), __("When"), __("By"), __("Source"), __("Workstation"), __("Previous"), __("Accepted"), __("Reason / note"), __("Work Order dates"), __("Sync")].map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>";
		h.revisions.forEach((r) => {
			const note = r.revision_type === "Baseline" ? (r.baseline_note || __("Initial schedule")) : (r.change_reason || "");
			html += "<tr><td>" + ppc.esc(r.revision_no) + "</td><td>" + ppc.dt(r.changed_at) + "</td><td>" + ppc.esc(r.changed_by) + "</td><td>" + ppc.esc(r.change_source) + "</td><td>" + ppc.machineChange(r) + "</td><td>" +
				(r.previous_start ? ppc.dt(r.previous_start) + " → " + ppc.dt(r.previous_end) : "–") + "</td><td>" + ppc.dt(r.accepted_start) + " → " + ppc.dt(r.accepted_end) +
				'</td><td class="wrap">' + ppc.esc(note) + (r.corrected ? ' <i class="text-muted">(' + __("corrected") + ": " + ppc.esc(r.correction_reason) + ")</i>" : "") + "</td><td>" +
				(r.work_order_new_end ? ppc.dt(r.work_order_new_start) + " → " + ppc.dt(r.work_order_new_end) : "–") + "</td><td>" + ppc.syncBadge(r.sync_status) + "</td></tr>";
		});
		if (!h.revisions.length) html += '<tr><td colspan="10" class="ppc-empty">' + __("No schedule history recorded for this Job Card yet.") + "</td></tr>";
		return html + "</tbody></table></div>";
	};

	/**
	 * Show a Job Card's schedule history in `drawer`. opts: {task, canReschedule, onChanged(), busy(promise)}.
	 * A late answer for a card the user has already moved away from is ignored.
	 */
	ppc.openHistory = function (drawer, jobCard, opts) {
		opts = opts || {};
		const refreshing = drawer.isOpen() && drawer.current === jobCard;
		const $body = drawer.open(jobCard, jobCard, refreshing);
		if (!refreshing) $body.html('<div class="ppc-skeleton">' + __("Loading schedule history…") + "</div>");
		const work = ppc.call("get_schedule_history", { job_card: jobCard }).then((h) => {
			if (drawer.current !== jobCard) return;
			$body.html('<div class="ppc-drawer-actions"></div>' + ppc.historyHtml(h, opts.task));
			const $a = $body.find(".ppc-drawer-actions");
			if (opts.task && opts.task.movable && opts.canReschedule) {
				$('<button type="button" class="btn btn-default btn-xs">' + __("Reschedule…") + "</button>").appendTo($a).on("click", () => ppc.editDates(opts.task, () => opts.onChanged && opts.onChanged()));
			}
			if (opts.task && opts.task.movable && opts.canReschedule && ppc.changeWorkstation) {
				$('<button type="button" class="btn btn-default btn-xs ppc-change-ws">' + __("Change Workstation") + "</button>").appendTo($a).on("click", () => {
					ppc.changeWorkstation(opts.task, null).then((res) => { if (res && (res.ok || res.stale) && opts.onChanged) opts.onChanged(res); });
				});
			}
			$('<a class="btn btn-default btn-xs" href="/app/job-card/' + encodeURIComponent(jobCard) + '">' + __("Open Job Card") + "</a>").appendTo($a);
			if (h.work_order) $('<a class="btn btn-default btn-xs" href="/app/work-order/' + encodeURIComponent(h.work_order) + '">' + __("Open Work Order") + "</a>").appendTo($a);
		}).catch(() => { if (drawer.current === jobCard) $body.html('<div class="ppc-error" role="alert">' + __("The history could not be loaded.") + "</div>"); });
		return opts.busy ? opts.busy(work) : work;
	};
})();
