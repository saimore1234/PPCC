// Timeline header labels of the Gantt: readable dates in every view, correct at month / year / DST boundaries,
// the same in every browser time zone (run this file under several TZ values), and tooltips with the full date.
const test = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");
const lib = require("../../public/gantt/ppc_gantt.js");
const m = lib.math;

const P = (s) => m.parse(s);
const labels = (row) => row.map((c) => c.label);
const subs = (row) => row.map((c) => c.sub);
const cells = (key, from, to, opts) => m.headerTiers(key, P(from), P(to), opts);

/* ------------------------------------------------------------------------------------------------ Day */
test("Day view: a readable date for every day and a clock time for every 2-hour column", () => {
	const t = cells("day", "2026-10-21 00:00:00", "2026-10-24 00:00:00");
	assert.deepEqual(labels(t.top), ["Wed 21 Oct 2026", "Thu 22 Oct 2026", "Fri 23 Oct 2026"]);
	assert.equal(t.top[0].title, "Wednesday, 21 October 2026");
	assert.equal(t.bottom.length, 36);
	assert.deepEqual(labels(t.bottom).slice(0, 4), ["00:00", "02:00", "04:00", "06:00"]);
	assert.equal(t.bottom[11].label, "22:00");
	assert.equal(t.bottom[11].title, "Wednesday, 21 October 2026, 22:00 – 24:00");
	assert.equal(t.bottom[12].label, "00:00");
	assert.match(t.bottom[12].title, /Thursday, 22 October 2026/);
});

test("Day view across a month end and a year end", () => {
	assert.deepEqual(labels(cells("day", "2026-10-31 00:00:00", "2026-11-02 00:00:00").top), ["Sat 31 Oct 2026", "Sun 01 Nov 2026"]);
	assert.deepEqual(labels(cells("day", "2026-12-31 00:00:00", "2027-01-02 00:00:00").top), ["Thu 31 Dec 2026", "Fri 01 Jan 2027"]);
});

/* ----------------------------------------------------------------------------------------------- Week */
test("Week view: the week shows its date range and month, the days show dates", () => {
	const t = cells("week", "2026-10-19 00:00:00", "2026-11-02 00:00:00");
	assert.deepEqual(labels(t.top), ["W43 · 19 – 25 Oct 2026", "W44 · 26 Oct – 01 Nov 2026"]);
	assert.equal(t.bottom.length, 14);
	assert.equal(t.bottom[2].label, "Wed 21 Oct");
	assert.equal(t.bottom[2].title, "Wednesday, 21 October 2026");
	assert.ok(t.bottom.every((c) => c.sub === undefined), "one-line cells at this width");
});

test("Week view at a month boundary: 30 Oct, 31 Oct, 01 Nov, 02 Nov", () => {
	const t = cells("week", "2026-10-30 00:00:00", "2026-11-03 00:00:00");
	assert.deepEqual(labels(t.bottom.slice(0, 4)), ["Fri 30 Oct", "Sat 31 Oct", "Sun 01 Nov", "Mon 02 Nov"]);
	assert.deepEqual(t.bottom.slice(0, 4).map((c) => !!c.weekend), [false, true, true, false]);
	assert.equal(t.bottom[2].title, "Sunday, 01 November 2026".replace("01", "1"));
});

test("Week view at a year boundary names both years in the week heading", () => {
	const t = cells("week", "2026-12-28 00:00:00", "2027-01-11 00:00:00");
	assert.deepEqual(labels(t.top), ["W53 · 28 Dec 2026 – 03 Jan 2027", "W1 · 04 – 10 Jan 2027"]);
	assert.deepEqual(labels(t.bottom.slice(3, 6)), ["Thu 31 Dec", "Fri 01 Jan", "Sat 02 Jan"]);
	assert.match(t.bottom[4].title, /Friday, 1 January 2027/);
});

test("Week view in a leap year has 29 Feb", () => {
	const t = cells("week", "2028-02-28 00:00:00", "2028-03-06 00:00:00");
	assert.deepEqual(labels(t.bottom.slice(0, 3)), ["Mon 28 Feb", "Tue 29 Feb", "Wed 01 Mar"]);
	assert.equal(t.top[0].label, "W9 · 28 Feb – 05 Mar 2028");
});

/* ---------------------------------------------------------------------------------------------- Month */
test("Month view: the month heading, then each day's number over its month - never a single-letter weekday", () => {
	const t = cells("month", "2026-10-29 00:00:00", "2026-11-03 00:00:00");
	assert.deepEqual(labels(t.top), ["October 2026", "November 2026"]);
	assert.deepEqual(labels(t.bottom.slice(0, 5)), ["29", "30", "31", "01", "02"]);
	assert.deepEqual(subs(t.bottom.slice(0, 5)), ["Oct", "Oct", "Oct", "Nov", "Nov"]);
	assert.equal(t.bottom[3].title, "Sunday, 1 November 2026");
});

test("Month view across the new year and a leap day", () => {
	const t = cells("month", "2026-12-30 00:00:00", "2027-01-03 00:00:00");
	assert.deepEqual(labels(t.top), ["December 2026", "January 2027"]);
	assert.deepEqual(t.bottom.slice(0, 4).map((c) => c.label + " " + c.sub), ["30 Dec", "31 Dec", "01 Jan", "02 Jan"]);
	const leap = cells("month", "2028-02-28 00:00:00", "2028-03-02 00:00:00");
	assert.deepEqual(leap.bottom.slice(0, 3).map((c) => c.label + " " + c.sub), ["28 Feb", "29 Feb", "01 Mar"]);
	assert.deepEqual(labels(leap.top), ["February 2028", "March 2028"]);
	const noLeap = cells("month", "2027-02-27 00:00:00", "2027-03-02 00:00:00");
	assert.deepEqual(noLeap.bottom.slice(0, 3).map((c) => c.label + " " + c.sub), ["27 Feb", "28 Feb", "01 Mar"]);
});

/* -------------------------------------------------------------------------------------------- Quarter */
test("Quarter view keeps quarter / month grouping and adds readable weekly columns", () => {
	const t = cells("quarter", "2026-10-19 00:00:00", "2027-01-20 00:00:00");
	assert.deepEqual(labels(t.top), ["Q4 2026", "Q1 2027"]);
	assert.deepEqual(labels(t.mid), ["October 2026", "November 2026", "December 2026", "January 2027"]);
	assert.deepEqual(t.bottom.slice(0, 3).map((c) => c.label + "/" + c.sub), ["19 Oct/W43", "26 Oct/W44", "02 Nov/W45"]);
	const dec28 = t.bottom.find((c) => c.label === "28 Dec");
	assert.equal(dec28.sub, "W53");
	assert.equal(dec28.title, "Week 53, 28 Dec 2026 – 03 Jan 2027");
	assert.equal(t.bottom.find((c) => c.label === "04 Jan").sub, "W1");
});

/* ---------------------------------------------------------------------------------------- invariants */
test("every scale: contiguous rows covering the range, a title with the year on every cell, and no single-letter labels", () => {
	const a = P("2026-12-20 00:00:00");
	const b = P("2027-03-10 00:00:00");
	for (const key of lib.SCALE_ORDER) {
		const t = m.headerTiers(key, a, b);
		for (const [name, row] of Object.entries(t)) {
			assert.ok(row.length > 0, `${key}.${name}`);
			assert.ok(row[0].start <= a, `${key}.${name} reaches the start`);
			assert.ok(row[row.length - 1].end >= b, `${key}.${name} reaches the end`);
			row.forEach((c, i) => {
				if (i) assert.equal(c.start, row[i - 1].end, `${key}.${name}: contiguous`);
				assert.match(c.title, /\b20\d\d\b/, `${key}.${name} "${c.label}": the tooltip names the year`);
				assert.doesNotMatch(c.label, /^[A-Za-z]\s?\d{1,2}$/, `${key}.${name}: "${c.label}" is a single-letter weekday label`);
				assert.doesNotMatch(c.sub || "", /^[A-Za-z]$/, `${key}.${name}: sub label`);
			});
		}
		assert.equal(!!t.mid, key === "quarter", "only the quarter view has a middle tier");
	}
});

test("the time zone is appended to every tooltip when one is given", () => {
	const t = m.headerTiers("week", P("2026-10-19 00:00:00"), P("2026-10-26 00:00:00"), { tz: "Asia/Kolkata" });
	for (const c of [...t.top, ...t.bottom]) assert.match(c.title, / · Asia\/Kolkata$/);
});

test("adaptive day labels: no single-letter weekday at any width", () => {
	const d = P("2026-10-21 00:00:00");
	assert.deepEqual(m.dayCell(d, 96), { label: "Wed 21 Oct" });
	assert.deepEqual(m.dayCell(d, 84), { label: "Wed 21 Oct" });
	assert.deepEqual(m.dayCell(d, 60), { label: "21 Oct" });
	assert.deepEqual(m.dayCell(d, 32), { label: "21", sub: "Oct" });
	assert.deepEqual(m.dayCell(d, 20), { label: "21" });
});

test("formatting helpers", () => {
	assert.equal(m.formatFull(P("2026-10-21 08:05:00")), "Wed 21 Oct 2026 08:05");
	assert.equal(m.fullDate(P("2027-01-01 00:00:00")), "Friday, 1 January 2027");
	assert.equal(m.dateRange(P("2026-10-19 00:00:00"), P("2026-10-25 00:00:00")), "19 – 25 Oct 2026");
	assert.equal(m.dateRange(P("2026-10-26 00:00:00"), P("2026-11-01 00:00:00")), "26 Oct – 01 Nov 2026");
	assert.equal(m.dateRange(P("2026-12-28 00:00:00"), P("2027-01-03 00:00:00")), "28 Dec 2026 – 03 Jan 2027");
});

/* ------------------------------------------------------------------------------ time zones and DST */
test("daylight-saving days are exactly 24 hours and keep their dates (spring forward / fall back, any browser time zone)", () => {
	for (const [from, to, expected] of [
		["2027-03-27 00:00:00", "2027-03-31 00:00:00", ["Sat 27 Mar", "Sun 28 Mar", "Mon 29 Mar", "Tue 30 Mar"]], // Europe spring forward
		["2027-10-30 00:00:00", "2027-11-03 00:00:00", ["Sat 30 Oct", "Sun 31 Oct", "Mon 01 Nov", "Tue 02 Nov"]], // Europe fall back
		["2027-03-13 00:00:00", "2027-03-17 00:00:00", ["Sat 13 Mar", "Sun 14 Mar", "Mon 15 Mar", "Tue 16 Mar"]], // US spring forward
		["2027-11-06 00:00:00", "2027-11-10 00:00:00", ["Sat 06 Nov", "Sun 07 Nov", "Mon 08 Nov", "Tue 09 Nov"]], // US fall back
		["2027-09-24 00:00:00", "2027-09-28 00:00:00", ["Fri 24 Sep", "Sat 25 Sep", "Sun 26 Sep", "Mon 27 Sep"]], // New Zealand spring forward
	]) {
		const t = m.headerTiers("week", P(from), P(to));
		assert.deepEqual(labels(t.bottom), expected);
		t.bottom.forEach((c) => assert.equal(c.end - c.start, m.DAY, `${c.label} is 24 h long`));
		const day = m.headerTiers("day", P(from), P(to));
		assert.equal(day.bottom.length, expected.length * 12, "twelve 2-hour columns a day, whatever the clock did");
	}
});

test("the 'today' cell follows the server's now, not the browser's clock", () => {
	const nowMs = P("2027-03-28 23:30:00");
	for (const key of lib.SCALE_ORDER) {
		const t = m.headerTiers(key, P("2027-03-20 00:00:00"), P("2027-04-10 00:00:00"), { nowMs });
		for (const [name, row] of Object.entries(t)) {
			const today = row.filter((c) => c.today);
			assert.equal(today.length, 1, `${key}.${name}: exactly one current cell`);
			assert.ok(today[0].start <= nowMs && nowMs < today[0].end);
		}
	}
	const week = m.headerTiers("week", P("2027-03-20 00:00:00"), P("2027-04-10 00:00:00"), { nowMs });
	assert.equal(week.bottom.find((c) => c.today).label, "Sun 28 Mar");
	const none = m.headerTiers("week", P("2027-03-20 00:00:00"), P("2027-04-10 00:00:00"));
	assert.ok(![...none.top, ...none.bottom].some((c) => c.today));
});

/* ------------------------------------------------------------------------------------------- the DOM */
function mount(options = {}, scale = "week") {
	const dom = new JSDOM('<!doctype html><body><div id="g"></div></body>', { pretendToBeVisual: true });
	global.window = dom.window;
	global.document = dom.window.document;
	delete require.cache[require.resolve("../../public/gantt/ppc_gantt.js")];
	const L = require("../../public/gantt/ppc_gantt.js");
	const el = document.getElementById("g");
	const g = new L.PPCGantt(el, Object.assign({ scale, groupBy: "workstation", now: "2026-10-30 08:00:00" }, options));
	const base = { sales_order: "SO-1", item_code: "FG-1", work_order: "WO-1", status: "Open", movable: true, conflict: false, overdue: false };
	g.setData({
		tasks: [
			{ ...base, name: "JC-1", operation: "Cut", workstation: "WS-MACH-01", revised_start: "2026-10-30 08:00:00", revised_end: "2026-11-02 13:00:00", original_start: "2026-10-29 08:00:00", original_end: "2026-11-01 13:00:00" },
			{ ...base, name: "JC-2", operation: "Pack", workstation: "WS-MACH-02", revised_start: "2026-12-30 08:00:00", revised_end: "2027-01-02 10:00:00" },
		],
		links: [],
	});
	return { g, el, window: dom.window };
}

const texts = (el, sel) => [...el.querySelectorAll(sel)].map((c) => c.textContent);

test("DOM: each view renders its readable labels with a full-date tooltip, and the Today column is marked", () => {
	const { g, el } = mount();
	assert.ok(texts(el, ".ppc-hbottom .ppc-hcell").includes("Fri 30 Oct"));
	assert.ok(texts(el, ".ppc-hbottom .ppc-hcell").includes("Sun 01 Nov"));
	assert.ok(texts(el, ".ppc-htop .ppc-hcell").some((t) => /^W44 · 26 Oct – 01 Nov 2026$/.test(t)));
	const today = el.querySelector(".ppc-hbottom .ppc-hcell.today");
	assert.equal(today.textContent, "Fri 30 Oct");
	assert.equal(today.getAttribute("title"), "Friday, 30 October 2026");

	g.setScale("month");
	assert.deepEqual(texts(el, ".ppc-htop .ppc-hcell").slice(0, 2), ["October 2026", "November 2026"]);
	const monthCells = [...el.querySelectorAll(".ppc-hbottom .ppc-hcell")];
	assert.ok(monthCells.every((c) => c.classList.contains("two")), "two-line cells: number over month");
	assert.equal(monthCells.find((c) => c.textContent === "01Nov").getAttribute("title").slice(0, 6), "Sunday");
	assert.equal(el.querySelector(".ppc-hcell.today .sub").textContent, "Oct");

	g.setScale("quarter");
	assert.ok(texts(el, ".ppc-htop .ppc-hcell").includes("Q4 2026") && texts(el, ".ppc-htop .ppc-hcell").includes("Q1 2027"));
	assert.ok(texts(el, ".ppc-hmid .ppc-hcell").includes("December 2026"));
	assert.ok(texts(el, ".ppc-hbottom .ppc-hcell").includes("28 DecW53"));

	g.setScale("day");
	assert.ok(texts(el, ".ppc-htop .ppc-hcell").includes("Fri 30 Oct 2026"));
	assert.ok(texts(el, ".ppc-hbottom .ppc-hcell").includes("08:00"));
});

test("DOM: header height follows its tiers, and the task list header lines up with it", () => {
	const { g, el } = mount();
	const heights = () => ({ head: el.querySelector(".ppc-gantt-head").style.height, left: el.querySelector(".ppc-gantt-left-head").style.height });
	assert.deepEqual(heights(), { head: "44px", left: "44px" }, "week: two one-line tiers");
	g.setScale("month");
	assert.deepEqual(heights(), { head: "58px", left: "58px" }, "month: second line for the month under the day");
	g.setScale("quarter");
	assert.deepEqual(heights(), { head: "80px", left: "80px" }, "quarter: three tiers");
	g.setScale("day");
	assert.deepEqual(heights(), { head: "44px", left: "44px" });
});

test("DOM: navigating and zooming keeps the labels right", () => {
	const { g, el } = mount();
	const sequence = ["month", "quarter", "week", "day", "week", "month", "week"];
	for (const key of sequence) {
		g.setScale(key);
		assert.equal(g.scale, key);
		const bottom = texts(el, ".ppc-hbottom .ppc-hcell");
		assert.ok(bottom.length > 0);
		assert.ok(bottom.every((t) => !/^[A-Za-z]\s?\d{1,2}$/.test(t)), `${key}: no single-letter weekday label: ${bottom.slice(0, 4)}`);
		assert.equal(el.querySelectorAll(".ppc-hcell.today").length >= 1, true);
	}
	g.zoom(1); // week -> day
	assert.equal(g.scale, "day");
	g.zoom(-1);
	g.zoom(-1);
	assert.equal(g.scale, "month");
	g.goToToday();
	assert.equal(g.scale, "month");
	assert.ok(el.querySelector(".ppc-today-line"));
});

test("DOM: the time zone label reaches the tooltips; bars and ghost bars carry the full date with the year", () => {
	const { el } = mount({ timezoneLabel: "Asia/Kolkata" });
	for (const c of el.querySelectorAll(".ppc-hcell")) assert.match(c.getAttribute("title"), /Asia\/Kolkata$/);
	assert.match(el.querySelector('.ppc-bar[data-name="JC-1"]').getAttribute("aria-label"), /Fri 30 Oct 2026 08:00 to Mon 02 Nov 2026 13:00/);
	assert.match(el.querySelector(".ppc-orig").getAttribute("title"), /Thu 29 Oct 2026 08:00 → Sun 01 Nov 2026 13:00/);
});

test("DOM: every bar shows the Workstation it is on", () => {
	const { el } = mount();
	assert.match(el.querySelector('.ppc-bar[data-name="JC-1"] .ppc-bar-label').textContent, /Cut · WS-MACH-01/);
	assert.match(el.querySelector('.ppc-bar[data-name="JC-2"] .ppc-bar-label').textContent, /Pack · WS-MACH-02/);
	assert.match(el.querySelector('.ppc-bar[data-name="JC-1"]').getAttribute("aria-label"), /WS-MACH-01/);
});

test("DOM: the default tooltip names the machine and the full dates", () => {
	const { g } = mount();
	const html = g._tipHtml(g.taskByName("JC-1"));
	assert.match(html, /WS-MACH-01/);
	assert.match(html, /Fri 30 Oct 2026 08:00/);
});

test("DOM: header labels do not depend on the browser time zone (a rescheduled bar still reports the same strings)", async () => {
	const { g, el } = mount();
	const calls = [];
	g.opts.onChange = (evt) => { calls.push(evt); return Promise.resolve({ ok: true }); };
	const before = texts(el, ".ppc-hbottom .ppc-hcell").join("|");
	const bar = el.querySelector('.ppc-bar[data-name="JC-1"]');
	const ev = (type, props) => new g.el.ownerDocument.defaultView.MouseEvent(type, Object.assign({ bubbles: true, cancelable: true, button: 0 }, props));
	bar.dispatchEvent(ev("pointerdown", { clientX: 200 }));
	g.el.ownerDocument.dispatchEvent(ev("pointermove", { clientX: 296 }));
	g.el.ownerDocument.dispatchEvent(ev("pointerup", { clientX: 296 }));
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(calls.length, 1);
	assert.equal(calls[0].start, "2026-10-31 08:00:00", "the dragged dates are the same naive strings in every time zone");
	assert.equal(texts(el, ".ppc-hbottom .ppc-hcell").join("|").length > 0, true);
	assert.ok(before.includes("Fri 30 Oct"));
});
