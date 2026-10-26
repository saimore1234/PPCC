// Pure-logic tests for the Control Center's UI library: no DOM, no Frappe.   Run: node --test ppc_production_control/tests/js/
const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../../public/js/ppc_ui_lib.js");

test("navigation lists the nine pages once, in order, with unique keys and routes", () => {
	assert.deepEqual(ui.PAGES.map((p) => p.key), ["dashboard", "plans", "workorders", "jobcards", "gantt", "workload", "history", "exceptions", "settings"]);
	assert.equal(new Set(ui.PAGES.map((p) => p.key)).size, ui.PAGES.length);
	const routes = ui.PAGES.filter((p) => p.route).map((p) => p.route);
	assert.equal(new Set(routes).size, routes.length);
	assert.ok(routes.every((r) => /^ppc-[a-z-]+$/.test(r)), "page names cannot collide with the Workspace 'ppc-control-center'");
	assert.ok(!routes.includes("ppc-control-center"));
	assert.equal(ui.pageByKey("settings").form, "PPC Settings");
	assert.equal(ui.pageByKey("nope"), null);
});

test("old bookmarks (ppc-production-control?tab=) land on the page that replaced the tab", () => {
	assert.equal(ui.legacyTarget("gantt").key, "gantt");
	assert.equal(ui.legacyTarget("jobcards").key, "jobcards");
	assert.equal(ui.legacyTarget("audit").key, "history");
	assert.equal(ui.legacyTarget("workorders").key, "workorders");
	assert.equal(ui.legacyTarget(undefined).key, "dashboard");
	assert.equal(ui.legacyTarget("something-else").key, "dashboard");
});

test("cleanFilters drops empty values and turns ticked checks into 1", () => {
	assert.deepEqual(ui.cleanFilters({ a: "", b: null, c: undefined, d: 0, e: false, f: "  ", g: " x ", h: true, i: 3 }), { g: "x", h: 1, i: 3 });
	assert.deepEqual(ui.cleanFilters(null), {});
	assert.ok(ui.sameFilters({ a: "1", b: "" }, { a: "1" }));
	assert.ok(ui.sameFilters({ a: "1", b: "2" }, { b: "2", a: "1" }));
	assert.ok(!ui.sameFilters({ a: "1" }, { a: "2" }));
});

test("there are exactly ten KPIs and every one opens a filtered list", () => {
	assert.equal(ui.KPIS.length, 10);
	for (const k of ui.KPIS) {
		const t = ui.kpiTarget(k.key, {});
		assert.ok(t && ui.pageByKey(t.page), `${k.key} has a target page`);
	}
	assert.equal(ui.kpiTarget("unknown", {}), null);
});

test("a KPI carries the dashboard filters; the date range means posting date for plans and schedule overlap for the rest", () => {
	const f = { company: "C1", planner: "a@b.c", date_from: "2026-10-01", date_to: "2026-10-31" };
	assert.deepEqual(ui.kpiTarget("total_plans", f), { page: "plans", filters: { company: "C1", planner: "a@b.c", posting_from: "2026-10-01", posting_to: "2026-10-31" } });
	assert.deepEqual(ui.kpiTarget("overdue_operations", f), { page: "jobcards", filters: { company: "C1", planner: "a@b.c", window_from: "2026-10-01", window_to: "2026-10-31", overdue_only: 1 } });
	assert.deepEqual(ui.kpiTarget("draft_work_orders", {}), { page: "workorders", filters: { wo_status: "Draft" } });
	assert.deepEqual(ui.kpiTarget("open_work_orders", {}), { page: "workorders", filters: { wo_status: "Open" } });
	assert.deepEqual(ui.kpiTarget("failed_syncs", {}), { page: "jobcards", filters: { sync_status: "Failed" } });
	assert.deepEqual(ui.kpiTarget("scheduling_conflicts", {}), { page: "jobcards", filters: { conflict_only: 1 } });
	assert.deepEqual(ui.kpiTarget("work_in_progress", {}), { page: "jobcards", filters: { status: "Work In Progress" } });
	assert.deepEqual(ui.kpiTarget("completed_job_cards", {}), { page: "jobcards", filters: { status: "Completed" } });
	assert.deepEqual(ui.kpiTarget("total_job_cards", { company: "" }), { page: "jobcards", filters: {} });
});

test("utilisation bands: unknown is never shown as fine", () => {
	assert.equal(ui.utilClass(null), "na");
	assert.equal(ui.utilClass(undefined), "na");
	assert.equal(ui.utilClass(NaN), "na");
	assert.equal(ui.utilClass(0), "ok");
	assert.equal(ui.utilClass(84.9), "ok");
	assert.equal(ui.utilClass(85), "warn");
	assert.equal(ui.utilClass(100), "warn");
	assert.equal(ui.utilClass(100.1), "bad");
});

test("hour formatting", () => {
	assert.equal(ui.fmtHours(8), "8 h");
	assert.equal(ui.fmtHours(7.5), "7.5 h");
	assert.equal(ui.fmtHours(7.25), "7.3 h");
	assert.equal(ui.fmtHours(0), "0 h");
	assert.equal(ui.fmtHours(null), "");
	assert.equal(ui.fmtHours(undefined), "");
	assert.equal(ui.fmtHours(2.04, 2), "2.04 h");
});

test("pagination range text", () => {
	assert.deepEqual(ui.pageRange(1, 50, 0), [0, 0]);
	assert.deepEqual(ui.pageRange(1, 50, 20), [1, 20]);
	assert.deepEqual(ui.pageRange(2, 50, 120), [51, 100]);
	assert.deepEqual(ui.pageRange(3, 50, 120), [101, 120]);
});

test("debounce: one trailing call with the last arguments, cancel and pending work", () => {
	let now = 0;
	const jobs = new Map();
	let next = 1;
	const timers = { set: (f, ms) => { jobs.set(next, { f, at: now + ms }); return next++; }, clear: (id) => jobs.delete(id) };
	const tick = (ms) => { now += ms; [...jobs].filter(([, j]) => j.at <= now).forEach(([id, j]) => { jobs.delete(id); j.f(); }); };
	const calls = [];
	const d = ui.debounce((x) => calls.push(x), 300, timers);
	d("a"); tick(100); d("b"); tick(100); d("c");
	assert.ok(d.pending());
	tick(299);
	assert.deepEqual(calls, []);
	tick(1);
	assert.deepEqual(calls, ["c"]);
	assert.ok(!d.pending());
	d("x"); d.cancel(); tick(1000);
	assert.deepEqual(calls, ["c"]);
});

test("debounce works with the real timer functions (no 'Illegal invocation')", async () => {
	const seen = [];
	const d = ui.debounce((v) => seen.push(v), 5);
	d(1); d(2);
	await new Promise((r) => setTimeout(r, 40));
	assert.deepEqual(seen, [2]);
});

test("date arithmetic is independent of the process time zone", () => {
	assert.equal(ui.addDays("2027-03-27", 1), "2027-03-28");
	assert.equal(ui.addDays("2027-03-28", 1), "2027-03-29"); // spring-forward day in Europe
	assert.equal(ui.addDays("2027-10-31", 1), "2027-11-01");
	assert.equal(ui.addDays("2028-02-28", 1), "2028-02-29");
	assert.equal(ui.addDays("2027-01-01", -1), "2026-12-31");
});

test("period presets are relative to the server's today", () => {
	assert.deepEqual(ui.periodRange("next7", "2026-10-26"), { date_from: "2026-10-26", date_to: "2026-11-01" });
	assert.deepEqual(ui.periodRange("next30", "2026-10-26"), { date_from: "2026-10-26", date_to: "2026-11-24" });
	assert.deepEqual(ui.periodRange("last30", "2026-10-26"), { date_from: "2026-09-27", date_to: "2026-10-26" });
	assert.deepEqual(ui.periodRange("month", "2026-10-26"), { date_from: "2026-10-01", date_to: "2026-10-31" });
	assert.deepEqual(ui.periodRange("month", "2028-02-10"), { date_from: "2028-02-01", date_to: "2028-02-29" });
	assert.deepEqual(ui.periodRange("month", "2026-12-05"), { date_from: "2026-12-01", date_to: "2026-12-31" });
	assert.deepEqual(ui.periodRange("", "2026-10-26"), { date_from: "", date_to: "" });
});

test("a page only accepts the route options it understands", () => {
	const allowed = ["plan", "company"];
	assert.deepEqual(ui.routeFilters({ plan: "P1", company: "", evil: "<script>", x: 1 }, allowed), { plan: "P1" });
	assert.deepEqual(ui.routeFilters(null, allowed), {});
	assert.deepEqual(ui.routeFilters({ plan: null, company: undefined }, allowed), {});
});

test("bulk selections are grouped by Production Plan and only include what was selected", () => {
	const rows = [{ name: "WO1", ppc_plan: "PP1" }, { name: "WO2", ppc_plan: "PP2" }, { name: "WO3", ppc_plan: "PP1" }, { name: "WO4", ppc_plan: "PP1" }, { name: "WO5" }];
	assert.deepEqual(ui.groupByPlan(rows, ["WO1", "WO3", "WO2", "WO5"]), { PP1: ["WO1", "WO3"], PP2: ["WO2"], "": ["WO5"] });
	assert.deepEqual(ui.groupByPlan(rows, []), {});
	assert.deepEqual(ui.groupByPlan(rows, ["NOPE"]), {});
});
