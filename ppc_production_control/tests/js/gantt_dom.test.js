// DOM-level tests of the Gantt component with synthetic pointer events (jsdom, dev-only).
const test = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

function setup(options = {}, data) {
	const dom = new JSDOM('<!doctype html><body><div id="g"></div></body>', { pretendToBeVisual: true });
	global.window = dom.window;
	global.document = dom.window.document;
	delete require.cache[require.resolve("../../public/gantt/ppc_gantt.js")];
	const lib = require("../../public/gantt/ppc_gantt.js");
	const el = document.getElementById("g");
	const calls = [];
	const g = new lib.PPCGantt(el, Object.assign({
		scale: "week", groupBy: "workstation", now: "2027-03-02 08:00:00",
		onChange: (evt) => { calls.push(evt); return Promise.resolve(g._reply ? g._reply(evt) : { ok: true }); },
	}, options));
	g.setData(data || sample());
	return { g, el, lib, calls, window: dom.window };
}

function sample() {
	const base = { sales_order: "SO-1", item_code: "FG-1", item_name: "Finished <b>good</b>", work_order: "WO-1", status: "Open", movable: true, conflict: false, overdue: false };
	return {
		tasks: [
			{ ...base, name: "JC-1", operation: "Cut", workstation: "WS1", revised_start: "2027-03-02 08:00:00", revised_end: "2027-03-02 13:00:00", original_start: "2027-03-01 08:00:00", original_end: "2027-03-01 13:00:00" },
			{ ...base, name: "JC-2", operation: "Assemble", workstation: "WS2", revised_start: "2027-03-02 13:10:00", revised_end: "2027-03-02 23:10:00", conflict: true },
			{ ...base, name: "JC-3", operation: "Pack", workstation: "WS2", status: "Completed", movable: false, revised_start: "2027-03-04 08:00:00", revised_end: "2027-03-04 10:00:00", is_estimated: true },
		],
		links: [{ from: "JC-1", to: "JC-2" }],
	};
}

const ev = (win, type, props) => new win.MouseEvent(type, Object.assign({ bubbles: true, cancelable: true, button: 0 }, props));
const px = (bar) => parseFloat(bar.style.left);
const wd = (bar) => parseFloat(bar.style.width);
const bar = (el, name) => el.querySelector(`.ppc-bar[data-name="${name}"]`);

async function drag(window, target, dx, { up = true } = {}) {
	target.dispatchEvent(ev(window, "pointerdown", { clientX: 200 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 200 + dx }));
	if (up) window.document.dispatchEvent(ev(window, "pointerup", { clientX: 200 + dx }));
	await new Promise((r) => setTimeout(r, 0));
}

test("renders groups, bars, header, today marker, dependency and original-plan ghost", () => {
	const { el } = setup();
	assert.equal(el.querySelectorAll(".ppc-bar").length, 3);
	assert.equal(el.querySelectorAll(".ppc-lrow.group").length, 2);
	assert.deepEqual([...el.querySelectorAll(".ppc-lrow.group")].map((g) => g.dataset.group), ["WS1", "WS2"]);
	assert.ok(el.querySelector(".ppc-hcell.top"));
	assert.ok(el.querySelector(".ppc-today-line"));
	assert.equal(el.querySelectorAll("path.ppc-link").length, 1);
	assert.equal(el.querySelectorAll(".ppc-orig").length, 1, "only JC-1 differs from its original plan");
	assert.ok(bar(el, "JC-3").classList.contains("locked"));
	assert.ok(bar(el, "JC-3").classList.contains("estimated"));
	assert.ok(bar(el, "JC-2").classList.contains("conflict"));
	assert.equal(bar(el, "JC-3").querySelectorAll(".ppc-handle").length, 0, "locked bars have no resize handles");
	assert.equal(bar(el, "JC-1").querySelectorAll(".ppc-handle").length, 2);
	assert.match(bar(el, "JC-1").getAttribute("aria-label"), /Cut, JC-1, Open/);
	assert.match(el.querySelector(".ppc-gantt-count").textContent, /3 bars/);
});

test("the four scales change bar width proportionally", () => {
	const { g, el, lib } = setup();
	const widths = {};
	for (const key of ["quarter", "month", "week", "day"]) {
		g.setScale(key);
		widths[key] = wd(bar(el, "JC-1"));
		assert.ok(el.querySelector(`.ppc-scale[data-scale="${key}"]`).classList.contains("active"));
	}
	assert.ok(widths.quarter < widths.month && widths.month < widths.week && widths.week < widths.day, JSON.stringify(widths));
	// 5 hours = 5/24 day
	assert.ok(Math.abs(widths.week - (5 / 24) * lib.SCALES.week.pxPerDay) < 0.5);
	// zoom buttons walk the scales and stop at the ends
	g.setScale("day"); g.zoom(1);
	assert.equal(g.scale, "day");
	g.setScale("quarter"); g.zoom(-1);
	assert.equal(g.scale, "quarter");
	g.zoom(1);
	assert.equal(g.scale, "month");
});

test("dragging a bar moves it by the snapped distance and reports ISO strings", async () => {
	const { window, el, calls } = setup();
	const b = bar(el, "JC-1");
	const left0 = px(b);
	await drag(window, b, 24); // week scale: 4px per hour => +6h
	assert.equal(calls.length, 1);
	assert.equal(calls[0].mode, "move");
	assert.equal(calls[0].start, "2027-03-02 14:00:00");
	assert.equal(calls[0].end, "2027-03-02 19:00:00");
	assert.deepEqual(calls[0].previous, { start: "2027-03-02 08:00:00", end: "2027-03-02 13:00:00" });
	assert.ok(px(bar(el, "JC-1")) > left0, "accepted: bar stays at the new place");
	assert.equal(calls[0].task.name, "JC-1");
});

test("resizing from the right and from the left", async () => {
	const { window, el, calls } = setup();
	await drag(window, bar(el, "JC-1").querySelector('[data-handle="resize-right"]'), 8); // +2h
	assert.equal(calls[0].mode, "resize");
	assert.equal(calls[0].start, "2027-03-02 08:00:00");
	assert.equal(calls[0].end, "2027-03-02 15:00:00");

	await drag(window, bar(el, "JC-1").querySelector('[data-handle="resize-left"]'), -12); // -3h
	assert.equal(calls[1].start, "2027-03-02 05:00:00");
	assert.equal(calls[1].end, "2027-03-02 15:00:00");
});

test("a rejected change snaps the bar back to its stored dates", async () => {
	const { window, el, g, calls } = setup();
	g._reply = () => ({ ok: false });
	const left0 = px(bar(el, "JC-1"));
	await drag(window, bar(el, "JC-1"), 40);
	assert.equal(calls.length, 1);
	assert.equal(px(bar(el, "JC-1")), left0);
	assert.equal(g.taskByName("JC-1").revised_start, "2027-03-02 08:00:00");
});

test("a failing handler also reverts", async () => {
	const { window, el, g } = setup({ onChange: () => Promise.reject(new Error("boom")) });
	const left0 = px(bar(el, "JC-1"));
	await drag(window, bar(el, "JC-1"), 40);
	assert.equal(px(bar(el, "JC-1")), left0);
});

test("the server's adjusted dates win over the dragged ones", async () => {
	const { window, el, g } = setup();
	g._reply = () => ({ ok: true, start: "2027-03-02 09:00:00", end: "2027-03-02 14:00:00", modified: "m2" });
	await drag(window, bar(el, "JC-1"), 40);
	const t = g.taskByName("JC-1");
	assert.equal(t.revised_start, "2027-03-02 09:00:00");
	assert.equal(t.modified, "m2");
});

test("escape cancels an in-flight drag; a zero move or a sub-snap move calls nothing", async () => {
	const { window, el, calls } = setup();
	const b = bar(el, "JC-1");
	b.dispatchEvent(ev(window, "pointerdown", { clientX: 200 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 260 }));
	window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
	window.document.dispatchEvent(ev(window, "pointerup", { clientX: 260 }));
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(calls.length, 0);

	await drag(window, bar(el, "JC-1"), 0);
	await drag(window, bar(el, "JC-1"), 1); // 1px = 15 min < half a snap step (30 min) -> rounds to 0
	assert.equal(calls.length, 0);
});

test("locked bars cannot be dragged; read-only mode locks everything", async () => {
	const a = setup();
	await drag(a.window, bar(a.el, "JC-3"), 50);
	assert.equal(a.calls.length, 0);

	const b = setup({ canEdit: false });
	assert.equal(b.el.querySelectorAll(".ppc-handle").length, 0);
	await drag(b.window, bar(b.el, "JC-1"), 50);
	assert.equal(b.calls.length, 0);
});

test("only one change can be in flight per drag", async () => {
	let resolve;
	const calls = [];
	const { window, el } = setup({ onChange: (e) => { calls.push(e); return new Promise((r) => (resolve = r)); } });
	const b = bar(el, "JC-1");
	b.dispatchEvent(ev(window, "pointerdown", { clientX: 200 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 230 }));
	window.document.dispatchEvent(ev(window, "pointerup", { clientX: 230 }));
	window.document.dispatchEvent(ev(window, "pointerup", { clientX: 230 }));
	assert.equal(calls.length, 1);
	assert.ok(b.classList.contains("pending"));
	resolve({ ok: true });
});

test("group collapse, search, grouping switch and double-click edit", () => {
	const edits = [];
	const { g, el } = setup({ onEdit: (t) => edits.push(t.name) });
	el.querySelector('.ppc-lrow.group[data-group="WS2"]').click();
	assert.equal(el.querySelectorAll(".ppc-bar").length, 1);
	el.querySelector('.ppc-lrow.group[data-group="WS2"]').click();
	assert.equal(el.querySelectorAll(".ppc-bar").length, 3);

	g.search = "assem"; g.render();
	assert.deepEqual([...el.querySelectorAll(".ppc-bar")].map((b) => b.dataset.name), ["JC-2"]);
	g.search = ""; g.render();

	g.setGroupBy("none");
	assert.equal(el.querySelectorAll(".ppc-lrow.group").length, 0);
	g.setGroupBy("work_order");
	assert.deepEqual([...el.querySelectorAll(".ppc-lrow.group")].map((x) => x.dataset.group), ["WO-1"]);

	bar(el, "JC-2").dispatchEvent(new window.MouseEvent("dblclick", { bubbles: true }));
	assert.deepEqual(edits, ["JC-2"]);
	bar(el, "JC-1").dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
	assert.deepEqual(edits, ["JC-2", "JC-1"], "keyboard users can edit dates without dragging");
});

test("tasks without dates are skipped rather than breaking the chart", () => {
	const { g, el } = setup();
	g.setData({ tasks: [{ name: "X", operation: "Op", status: "Open" }, ...sample().tasks], links: [] });
	assert.equal(el.querySelectorAll(".ppc-bar").length, 3);
});

test("the default tooltip escapes task data", () => {
	const { el, window } = setup();
	const g2 = setup({}, { tasks: [{ name: "JC-<img src=x>", operation: "<script>alert(1)</script>", status: "Open", workstation: "WS", revised_start: "2027-03-02 08:00:00", revised_end: "2027-03-02 10:00:00", movable: true }], links: [] });
	const b = g2.el.querySelector(".ppc-bar");
	b.dispatchEvent(new g2.window.MouseEvent("mouseenter", { bubbles: true, clientX: 10, clientY: 10 }));
	const tip = g2.window.document.querySelector(".ppc-gantt-tip");
	assert.equal(tip.style.display, "block");
	assert.ok(!tip.querySelector("script") && !tip.querySelector("img"));
	assert.match(tip.innerHTML, /&lt;script&gt;/);
	assert.ok(!g2.el.querySelector(".ppc-lrow.task img"), "grid cells are escaped too");
});

test("the 'today' line follows the server clock, not the browser clock", () => {
	const { g, el, lib } = setup({ now: "2027-03-02 12:00:00" });
	const left = parseFloat(el.querySelector(".ppc-today-line").style.left);
	const t0 = g._range.start;
	const expected = ((lib.math.parse("2027-03-02 12:00:00") - t0) / lib.math.DAY) * lib.SCALES.week.pxPerDay;
	assert.ok(Math.abs(left - expected) < 2, `${left} vs ${expected}`);
});

test("destroy removes listeners and DOM", () => {
	const { g, el, window } = setup();
	g.destroy();
	assert.equal(el.innerHTML, "");
	assert.equal(window.document.querySelectorAll(".ppc-gantt-tip").length, 0);
});

/* ------------------------------------------------------------------ baseline / revision / actual display */
function revised() {
	const d = sample();
	Object.assign(d.tasks[0], {
		revision: 2, has_baseline: true, baseline_label: "Initial schedule", sync_status: "Rescheduled",
		last_rescheduled_by: "planner@example.com", last_rescheduled_at: "2027-03-01 17:30:00",
		actual_start: "2027-03-02 08:30:00", actual_end: "2027-03-02 12:00:00",
	});
	Object.assign(d.tasks[1], { revision: 0, has_baseline: true, sync_status: "Failed", actual_start: "2027-03-02 13:30:00", actual_end: null });
	return d;
}

test("a rescheduled bar shows a ghost baseline bar, an actual line and a revision badge, none overlapping the planned bar", () => {
	const { el } = setup({}, revised());
	const b = bar(el, "JC-1");
	assert.match(b.querySelector(".ppc-rev").textContent, /^R2$/);
	assert.match(b.querySelector(".ppc-rev").title, /Rescheduled 2 times/);
	assert.equal(bar(el, "JC-2").querySelector(".ppc-rev"), null, "revision 0 shows no badge");

	const ghost = el.querySelector(".ppc-orig");
	assert.match(ghost.title, /Original plan \(Initial schedule\)/);
	const barTop = parseFloat(b.style.top);
	assert.ok(parseFloat(ghost.style.top) >= barTop + 20, "ghost is drawn below the 20px bar");
	assert.equal(parseFloat(ghost.style.left) < px(b), true, "the original plan started earlier");

	const actual = [...el.querySelectorAll(".ppc-actual")];
	assert.equal(actual.length, 2);
	const done = actual.find((a) => !a.classList.contains("running"));
	assert.match(done.title, /^Actual: .* → /);
	assert.ok(parseFloat(done.style.top) > parseFloat(ghost.style.top), "actual line sits under the ghost");
	assert.ok(Math.abs(wd(done) - 3.5 * (96 / 24)) < 0.5, "08:30 -> 12:00 is 3.5h at 4px/h");
	const running = actual.find((a) => a.classList.contains("running"));
	assert.match(running.title, /in progress/);
	assert.ok(wd(running) > 0);
});

test("a failed or drifting card is flagged on its bar", () => {
	const { el } = setup({}, revised());
	assert.ok(bar(el, "JC-2").querySelector(".ppc-sync-bad"));
	assert.match(bar(el, "JC-2").querySelector(".ppc-sync-bad").title, /synchronisation failed/);
	assert.equal(bar(el, "JC-1").querySelector(".ppc-sync-bad"), null);
});

test("schedule columns: original, current, actual, revision, last rescheduled and sync status", () => {
	const { g, el } = setup({}, revised());
	assert.deepEqual([...el.querySelectorAll(".ppc-gantt-left-head .ppc-lcol")].map((c) => c.textContent),
		["Operation / Job Card", "Finished good", "Workstation", "Status"]);
	assert.ok(!el.querySelector(".ppc-gantt-left.wide"));

	el.querySelector(".ppc-toggle-cols").click();
	assert.equal(g.gridMode, "schedule");
	assert.ok(el.querySelector(".ppc-gantt-left.wide"));
	assert.equal(el.querySelector(".ppc-toggle-cols").getAttribute("aria-pressed"), "true");
	assert.deepEqual([...el.querySelectorAll(".ppc-gantt-left-head .ppc-lcol")].map((c) => c.textContent),
		["Operation / Job Card", "Original start / end", "Current start / end", "Actual start / end", "Rev", "Last rescheduled", "Sync status", "Status"]);

	const row = el.querySelector('.ppc-lrow.task[data-name="JC-1"]');
	const cell = (cls) => row.querySelector("." + cls);
	assert.match(cell("c-orig").textContent, /01 Mar 08:00.*01 Mar 13:00/);
	assert.match(cell("c-cur").textContent, /02 Mar 08:00.*02 Mar 13:00/);
	assert.match(cell("c-act").textContent, /02 Mar 08:30.*02 Mar 12:00/);
	assert.equal(cell("c-rev").textContent, "2");
	assert.match(cell("c-last").textContent, /planner@example.com.*01 Mar 17:30/);
	assert.match(cell("c-sync").textContent, /Rescheduled/);
	assert.match(cell("c-sync").querySelector(".ppc-badge").className, /warn/);
	assert.match(el.querySelector('.ppc-lrow.task[data-name="JC-2"] .c-sync .ppc-badge').className, /bad/, "failed sync is red");

	el.querySelector(".ppc-toggle-cols").click();
	assert.equal(g.gridMode, "compact");
	assert.ok(!el.querySelector(".ppc-gantt-left.wide"));
});

test("a card without a baseline shows dashes, never an invented original", () => {
	const d = sample();
	Object.assign(d.tasks[1], { has_baseline: false, original_start: null, original_end: null, revision: 0, sync_status: "No baseline" });
	const { g, el } = setup({ gridMode: "schedule" }, d);
	assert.equal(g.gridMode, "schedule", "the mode can be restored from preferences");
	const row = el.querySelector('.ppc-lrow.task[data-name="JC-2"]');
	assert.equal(row.querySelector(".c-orig").textContent, "—");
	assert.equal(row.querySelector(".c-rev").textContent, "—");
	assert.equal(row.querySelector(".c-act").textContent, "—");
	assert.equal(el.querySelectorAll(".ppc-orig").length, 1, "still only JC-1 has a ghost");
});

test("selecting a bar or a row reports the task so the detail panel can load its history", () => {
	const picked = [];
	const { el } = setup({ onSelect: (t) => picked.push(t && t.name) }, revised());
	bar(el, "JC-1").click();
	el.querySelector('.ppc-lrow.task[data-name="JC-2"]').click();
	assert.deepEqual(picked, ["JC-1", "JC-2"]);
});

test("schedule columns escape user-controlled text", () => {
	const d = { tasks: [{ name: "JC-<b>1</b>", operation: "<i>Op</i>", status: "Open", workstation: "W", movable: true, revised_start: "2027-03-02 08:00:00", revised_end: "2027-03-02 10:00:00",
		last_rescheduled_by: "<img src=x onerror=alert(1)>", last_rescheduled_at: "2027-03-01 10:00:00", revision: 1, sync_status: "<u>x</u>", has_baseline: true }], links: [] };
	const { el } = setup({ gridMode: "schedule" }, d);
	assert.equal(el.querySelectorAll(".ppc-lrow.task img, .ppc-lrow.task i, .ppc-lrow.task u, .ppc-lrow.task b b").length, 0);
	assert.match(el.querySelector(".ppc-lrow.task .c-last").innerHTML, /&lt;img/);
});
