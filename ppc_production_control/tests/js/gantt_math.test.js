// Pure-logic tests for the Gantt: no DOM needed.   Run: node --test ppc_production_control/tests/js/
const test = require("node:test");
const assert = require("node:assert/strict");
const lib = require("../../public/gantt/ppc_gantt.js");
const m = lib.math;

test("parse/format round-trips exactly, independent of the process time zone", () => {
	for (const s of ["2027-03-28 01:30:00", "2027-03-28 02:30:00", "2027-10-31 02:30:00", "2028-02-29 23:59:59", "2027-12-31 00:00:00"]) {
		assert.equal(m.format(m.parse(s)), s, s); // includes non-existent / ambiguous local times in many zones
	}
	assert.equal(m.format(m.parse("2027-03-01")), "2027-03-01 00:00:00");
	assert.equal(m.format(m.parse("2027-03-01T08:15")), "2027-03-01 08:15:00");
	assert.equal(m.parse(""), null);
	assert.equal(m.parse(null), null);
	assert.equal(m.parse("garbage"), null);
});

test("a whole-day shift keeps the wall-clock time (no DST drift)", () => {
	const a = m.parse("2027-03-27 09:00:00");
	assert.equal(m.format(a + m.DAY), "2027-03-28 09:00:00");
	assert.equal(m.format(a + 2 * m.DAY), "2027-03-29 09:00:00");
});

test("snapping", () => {
	const t = m.parse("2027-03-01 08:07:00");
	assert.equal(m.format(m.snap(t, 15)), "2027-03-01 08:00:00");
	assert.equal(m.format(m.snap(m.parse("2027-03-01 08:08:00"), 15)), "2027-03-01 08:15:00");
	assert.equal(m.format(m.snap(m.parse("2027-03-01 17:00:00"), 1440)), "2027-03-02 00:00:00");
});

test("move keeps the duration, resize respects the minimum and the other edge", () => {
	const s = m.parse("2027-03-01 08:00:00");
	const e = m.parse("2027-03-01 12:00:00");
	let r = m.applyDrag("move", s, e, 3.4 * m.HOUR, 60);
	assert.equal(m.format(r.start), "2027-03-01 11:00:00");
	assert.equal(r.end - r.start, 4 * m.HOUR);

	r = m.applyDrag("resize-right", s, e, 2 * m.HOUR, 60);
	assert.equal(m.format(r.end), "2027-03-01 14:00:00");
	assert.equal(r.start, s);

	// dragging the right edge past the left edge cannot invert the bar
	r = m.applyDrag("resize-right", s, e, -10 * m.HOUR, 60);
	assert.equal(r.end - r.start, m.HOUR);
	// dragging the left edge past the right edge cannot invert it either
	r = m.applyDrag("resize-left", s, e, 10 * m.HOUR, 60);
	assert.equal(r.end - r.start, m.HOUR);
	assert.equal(r.end, e);
});

test("pixel <-> time conversion", () => {
	const t0 = m.parse("2027-03-01 00:00:00");
	assert.equal(m.xOf(t0 + 12 * m.HOUR, t0, 100), 50);
	assert.equal(m.timeOf(50, t0, 100), t0 + 12 * m.HOUR);
});

test("ISO week numbers", () => {
	assert.deepEqual(m.isoWeek(m.parse("2027-01-04")), { week: 1, year: 2027 });
	assert.deepEqual(m.isoWeek(m.parse("2026-12-31")), { week: 53, year: 2026 });
	assert.deepEqual(m.isoWeek(m.parse("2027-03-01")), { week: 9, year: 2027 });
});

test("header tiers cover the whole range for every scale", () => {
	const a = m.parse("2027-02-20 00:00:00");
	const b = m.parse("2027-04-10 00:00:00");
	for (const key of lib.SCALE_ORDER) {
		const tiers = m.headerTiers(key, a, b);
		for (const row of [tiers.top, tiers.bottom]) {
			assert.ok(row.length > 0, key);
			assert.ok(row[0].start <= a, `${key} first cell must reach the range start`);
			assert.ok(row[row.length - 1].end >= b, `${key} last cell must reach the range end`);
			for (let i = 1; i < row.length; i++) assert.equal(row[i].start, row[i - 1].end, `${key}: contiguous cells`);
		}
	}
	const q = m.headerTiers("quarter", a, b);
	assert.equal(q.top[0].label, "Q1 2027");
	assert.equal(q.bottom[0].label, "Feb");
	const w = m.headerTiers("week", a, b);
	assert.ok(w.bottom.some((c) => c.weekend));
	const month = m.headerTiers("month", a, b);
	assert.equal(month.top[0].label, "February 2027");
});

test("range is padded and includes original dates", () => {
	const tasks = [
		{ revised_start: "2027-03-10 08:00:00", revised_end: "2027-03-10 12:00:00", original_start: "2027-03-08 08:00:00", original_end: "2027-03-08 12:00:00" },
		{ revised_start: "2027-03-12 08:00:00", revised_end: "2027-03-13 18:00:00" },
	];
	const r = m.computeRange(tasks, 2);
	assert.equal(m.format(r.start), "2027-03-06 00:00:00");
	assert.equal(m.format(r.end), "2027-03-16 00:00:00");
	assert.equal(m.computeRange([], 2), null);
});

test("grouping, ordering and collapsing", () => {
	const tasks = [
		{ name: "A", workstation: "WS2" }, { name: "B", workstation: "WS1" }, { name: "C", workstation: "WS2" }, { name: "D" },
	];
	const flat = m.layoutRows(tasks, "none", {}, 34, 28);
	assert.equal(flat.rows.length, 4);
	assert.equal(flat.height, 4 * 34);

	const g = m.layoutRows(tasks, "workstation", {}, 34, 28);
	const labels = g.rows.filter((r) => r.type === "group").map((r) => r.key);
	assert.deepEqual(labels, ["(No workstation)", "WS1", "WS2"]);
	assert.equal(g.height, 3 * 28 + 4 * 34);
	// y positions are strictly increasing and gap-free
	let y = 0;
	for (const r of g.rows) {
		assert.equal(r.y, y);
		y += r.h;
	}
	const c = m.layoutRows(tasks, "workstation", { WS2: true }, 34, 28);
	assert.equal(c.rows.filter((r) => r.type === "task").length, 2);
	assert.equal(c.rows.find((r) => r.type === "group" && r.key === "WS2").collapsed, true);
});

test("dependency connector: normal elbow and a visible detour when the order is violated", () => {
	assert.equal(m.linkPath(100, 20, 200, 60), "M100,20 H110 V60 H200");
	const bad = m.linkPath(200, 20, 150, 60);
	assert.ok(bad.startsWith("M200,20 H210 V34"), bad);
	assert.ok(bad.endsWith("H150"), bad);
});

test("html escaping", () => {
	assert.equal(m.esc(`<img src=x onerror="a">&'`), "&lt;img src=x onerror=&quot;a&quot;&gt;&amp;&#39;");
	assert.equal(m.esc(null), "");
});

test("durations are readable", () => {
	assert.equal(m.formatDuration(90 * m.MIN), "1h 30m");
	assert.equal(m.formatDuration(3 * m.DAY + 2 * m.HOUR), "3d 2h");
	assert.equal(m.formatDuration(45 * m.MIN), "45m");
});
