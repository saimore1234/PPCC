/*
 * PPC Control Center - pure UI logic (no DOM, no Frappe): navigation model, filter clean-up, KPI -> page mapping,
 * utilisation classes, formatting helpers. Kept separate so it can be unit-tested in Node in several time zones.
 */
(function (root) {
	"use strict";

	/** The Control Center's pages, in navigation order. `route` is the Frappe page name; `form` opens a standard form. */
	const PAGES = [
		{ key: "dashboard", route: "ppc-dashboard", label: "Dashboard" },
		{ key: "plans", route: "ppc-production-plans", label: "Production Plans" },
		{ key: "workorders", route: "ppc-work-orders", label: "Work Orders" },
		{ key: "jobcards", route: "ppc-job-cards", label: "Job Cards" },
		{ key: "gantt", route: "ppc-gantt", label: "Production Gantt" },
		{ key: "workload", route: "ppc-workload", label: "Workload & Capacity" },
		{ key: "history", route: "ppc-schedule-history", label: "Schedule History" },
		{ key: "exceptions", route: "ppc-exceptions", label: "Exceptions" },
		{ key: "settings", form: "PPC Settings", label: "PPC Settings" },
	];

	function pageByKey(key) {
		return PAGES.find(function (p) { return p.key === key; }) || null;
	}

	/** Old bookmarks and the plan form used `ppc-production-control` with a `tab`; map the tab to its new page. */
	const LEGACY_TABS = { jobcards: "jobcards", workorders: "workorders", gantt: "gantt", workload: "workload", audit: "history" };
	function legacyTarget(tab) {
		return pageByKey(LEGACY_TABS[tab] || "dashboard");
	}

	/** Drop empty values so that "no filter" never reaches the server as "".  Booleans/checks become 1. */
	function cleanFilters(values) {
		const out = {};
		Object.keys(values || {}).forEach(function (k) {
			let v = values[k];
			if (v === null || v === undefined || v === "" || v === 0 || v === false) return;
			if (v === true) v = 1;
			if (typeof v === "string") v = v.trim();
			if (v === "") return;
			out[k] = v;
		});
		return out;
	}

	function sameFilters(a, b) {
		return JSON.stringify(sortKeys(cleanFilters(a))) === JSON.stringify(sortKeys(cleanFilters(b)));
	}

	function sortKeys(o) {
		const r = {};
		Object.keys(o).sort().forEach(function (k) { r[k] = o[k]; });
		return r;
	}

	/**
	 * Which page a KPI opens, and with which filters. `f` are the dashboard's filters
	 * (company, planner, date_from, date_to); the date range keeps its meaning on every page:
	 * plans by posting date, Work Orders and Job Cards by schedule overlap.
	 */
	function kpiTarget(kpi, f) {
		f = cleanFilters(f);
		const base = {};
		if (f.company) base.company = f.company;
		if (f.planner) base.planner = f.planner;
		if (f.plan) base.plan = f.plan;
		const scheduled = Object.assign({}, base);
		if (f.date_from) scheduled.window_from = f.date_from;
		if (f.date_to) scheduled.window_to = f.date_to;
		const plans = Object.assign({}, base);
		if (f.date_from) plans.posting_from = f.date_from;
		if (f.date_to) plans.posting_to = f.date_to;
		const map = {
			total_plans: ["plans", plans],
			pending_qty: ["plans", plans],
			draft_work_orders: ["workorders", Object.assign({}, scheduled, { wo_status: "Draft" })],
			open_work_orders: ["workorders", Object.assign({}, scheduled, { wo_status: "Open" })],
			total_job_cards: ["jobcards", scheduled],
			work_in_progress: ["jobcards", Object.assign({}, scheduled, { status: "Work In Progress" })],
			completed_job_cards: ["jobcards", Object.assign({}, scheduled, { status: "Completed" })],
			overdue_operations: ["jobcards", Object.assign({}, scheduled, { overdue_only: 1 })],
			scheduling_conflicts: ["jobcards", Object.assign({}, scheduled, { conflict_only: 1 })],
			failed_syncs: ["jobcards", Object.assign({}, scheduled, { sync_status: "Failed" })],
		};
		const hit = map[kpi];
		return hit ? { page: hit[0], filters: hit[1] } : null;
	}

	/** The ten dashboard KPIs: key, label, tone when non-zero. */
	const KPIS = [
		{ key: "total_plans", label: "Production Plans" },
		{ key: "pending_qty", label: "Pending Production Qty", decimals: true },
		{ key: "draft_work_orders", label: "Draft Work Orders" },
		{ key: "open_work_orders", label: "Submitted / Open Work Orders" },
		{ key: "total_job_cards", label: "Job Cards" },
		{ key: "work_in_progress", label: "Work in Progress" },
		{ key: "completed_job_cards", label: "Completed Operations" },
		{ key: "overdue_operations", label: "Overdue Operations", tone: "bad" },
		{ key: "scheduling_conflicts", label: "Scheduling Conflicts", tone: "bad" },
		{ key: "failed_syncs", label: "Failed Synchronizations", tone: "bad" },
	];

	/** Utilisation colour band; `null` (unknown) is never coloured as if it were fine. */
	function utilClass(pct) {
		if (pct === null || pct === undefined || isNaN(pct)) return "na";
		if (pct > 100) return "bad";
		if (pct >= 85) return "warn";
		return "ok";
	}

	function fmtHours(h, digits) {
		if (h === null || h === undefined || isNaN(h)) return "";
		const d = digits === undefined ? 1 : digits;
		return Number(h).toFixed(d).replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1") + " h";
	}

	function pageRange(page, pageLength, total) {
		if (!total) return [0, 0];
		return [(page - 1) * pageLength + 1, Math.min(total, page * pageLength)];
	}

	/** Trailing-edge debounce; `timers` is injectable so tests need no real clock. */
	function debounce(fn, ms, timers) {
		const t = timers || { set: (f, m) => setTimeout(f, m), clear: (h) => clearTimeout(h) }; // wrapped: a bare setTimeout called as t.set() throws "Illegal invocation" in browsers
		let id = null;
		const wrapped = function () {
			const args = arguments;
			const self = this;
			if (id !== null) t.clear(id);
			id = t.set(function () { id = null; fn.apply(self, args); }, ms);
		};
		wrapped.cancel = function () { if (id !== null) { t.clear(id); id = null; } };
		wrapped.pending = function () { return id !== null; };
		return wrapped;
	}

	/** `YYYY-MM-DD` of `d` plus `days`, computed on UTC fields so the browser's time zone cannot shift it. */
	function addDays(isoDate, days) {
		const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(isoDate);
		const t = Date.UTC(+m[1], +m[2] - 1, +m[3]) + days * 86400000;
		const d = new Date(t);
		const p = function (n) { return n < 10 ? "0" + n : "" + n; };
		return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate());
	}

	/** Date-range presets for the dashboard filter, relative to the server's "today" (a `YYYY-MM-DD` string). */
	function periodRange(preset, today) {
		switch (preset) {
			case "next7": return { date_from: today, date_to: addDays(today, 6) };
			case "next30": return { date_from: today, date_to: addDays(today, 29) };
			case "last30": return { date_from: addDays(today, -29), date_to: today };
			case "month": {
				const first = today.slice(0, 8) + "01";
				const nextMonth = new Date(Date.UTC(+today.slice(0, 4), +today.slice(5, 7), 1));
				const last = addDays(nextMonth.toISOString().slice(0, 10), -1);
				return { date_from: first, date_to: last };
			}
			default: return { date_from: "", date_to: "" };
		}
	}

	/** Keep only the keys a page understands out of route options (never trust arbitrary route state). */
	function routeFilters(options, allowed) {
		const out = {};
		Object.keys(options || {}).forEach(function (k) {
			if (allowed.indexOf(k) !== -1 && options[k] !== undefined && options[k] !== null && options[k] !== "") out[k] = options[k];
		});
		return out;
	}

	/** Group Work Order rows by their Production Plan: bulk submission is validated per plan. */
	function groupByPlan(rows, selected) {
		const want = new Set(selected);
		const groups = {};
		rows.forEach(function (r) {
			if (!want.has(r.name)) return;
			const key = r.ppc_plan || "";
			(groups[key] = groups[key] || []).push(r.name);
		});
		return groups;
	}

	const api = {
		PAGES: PAGES, KPIS: KPIS, pageByKey: pageByKey, legacyTarget: legacyTarget, cleanFilters: cleanFilters, sameFilters: sameFilters,
		kpiTarget: kpiTarget, utilClass: utilClass, fmtHours: fmtHours, pageRange: pageRange, debounce: debounce, addDays: addDays,
		periodRange: periodRange, routeFilters: routeFilters, groupByPlan: groupByPlan,
	};
	if (typeof module !== "undefined" && module.exports) module.exports = api;
	if (root) root.PPCUiLib = api;
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);
