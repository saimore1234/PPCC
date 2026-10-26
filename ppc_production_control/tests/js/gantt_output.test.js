// The finished item and its output quantity next to the machine, on every bar and in the task list.
const test = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

function mount(tasks, options = {}) {
	const dom = new JSDOM('<!doctype html><body><div id="g"></div></body>', { pretendToBeVisual: true });
	global.window = dom.window;
	global.document = dom.window.document;
	delete require.cache[require.resolve("../../public/gantt/ppc_gantt.js")];
	const lib = require("../../public/gantt/ppc_gantt.js");
	const el = document.getElementById("g");
	const g = new lib.PPCGantt(el, Object.assign({ scale: "week", groupBy: "workstation", now: "2026-10-30 08:00:00" }, options));
	const base = { sales_order: "SO-1", work_order: "WO-1", status: "Open", movable: true, conflict: false, overdue: false };
	g.setData({
		tasks: tasks.map((t, i) => ({ ...base, name: "JC-" + (i + 1), revised_start: "2026-10-30 08:00:00", revised_end: "2026-10-30 18:00:00", ...t })),
		links: [],
	});
	return { g, el, lib };
}

const label = (el, name) => el.querySelector(`.ppc-bar[data-name="${name}"] .ppc-bar-label`).textContent;

test("quantity text: planned only, or done / planned; no trailing zeros; blank when unknown", () => {
	const { lib } = mount([]);
	const q = lib.math.qtyText;
	assert.equal(q({ planned_qty: 10 }), "10");
	assert.equal(q({ planned_qty: 10.0, completed_qty: 0 }), "10");
	assert.equal(q({ planned_qty: 10, completed_qty: 4 }), "4 / 10");
	assert.equal(q({ planned_qty: 2.5, completed_qty: 0.125 }), "0.125 / 2.5");
	assert.equal(q({ planned_qty: 0.1 + 0.2 }), "0.3");
	assert.equal(q({ planned_qty: 1234567.5 }), "1234567.5");
	assert.equal(q({}), "");
	assert.equal(q({ planned_qty: null }), "");
	assert.equal(q({ planned_qty: "abc" }), "");
});

test("every bar shows operation, machine, finished item and output quantity - also the narrow ones", () => {
	const { el } = mount([
		{ operation: "Cut", workstation: "WS-1", item_code: "FG-1", planned_qty: 10, completed_qty: 0 },
		{ operation: "Pack", workstation: "WS-2", item_code: "FG-2", planned_qty: 250, completed_qty: 100, revised_end: "2026-10-30 09:00:00" },
		{ operation: "Check", workstation: "WS-2", item_code: "FG-3" },
	]);
	assert.equal(label(el, "JC-1"), "○ Cut · WS-1 · FG-1 × 10");
	assert.match(label(el, "JC-2"), /Pack · WS-2 · FG-2 × 100 \/ 250/);
	assert.match(label(el, "JC-3"), /Check · WS-2 · FG-3$/, "no quantity known: no 'x'");
	assert.match(el.querySelector('.ppc-bar[data-name="JC-1"]').getAttribute("aria-label"), /Cut, JC-1, WS-1, FG-1 x 10, Open/);
});

test("the task list has an Output qty column next to the finished good (compact and schedule views)", () => {
	const { g, el } = mount([{ operation: "Cut", workstation: "WS-1", item_code: "FG-1", planned_qty: 10, completed_qty: 4 }]);
	const heads = () => [...el.querySelectorAll(".ppc-gantt-left-head .ppc-lcol")].map((c) => c.textContent);
	assert.deepEqual(heads(), ["Operation / Job Card", "Finished good", "Output qty", "Workstation", "Status"]);
	const row = el.querySelector(".ppc-lrow.task");
	assert.equal(row.querySelector(".c-qty").textContent, "4 / 10");
	assert.match(row.querySelector(".c-qty").getAttribute("title"), /done \/ planned\): 4 \/ 10/);
	g.setGridMode("schedule");
	assert.ok(heads().includes("Output qty"));
	assert.equal(el.querySelector(".ppc-lrow.task .c-qty").textContent, "4 / 10");
	assert.equal(el.querySelector(".ppc-gantt-left").style.width, "876px");
});

test("a card with no quantity shows a dash, and text is escaped", () => {
	const { el } = mount([{ operation: "Cut", workstation: "WS-1", item_code: "<b>FG</b>" }]);
	assert.equal(el.querySelector(".ppc-lrow.task .c-qty").textContent, "—");
	assert.ok(!el.querySelector(".ppc-bar-label b"), "item codes are not interpreted as HTML");
});

test("the default tooltip names the item and its output quantity", () => {
	const { g } = mount([{ operation: "Cut", workstation: "WS-1", item_code: "FG-1", planned_qty: 10, completed_qty: 4 }]);
	assert.match(g._tipHtml(g.taskByName("JC-1")), /FG-1 × 4 \/ 10/);
});

test("a bar too short for its text shows the label beside it; the label flips to the left at the end of the chart; long bars keep it inside", () => {
	const { el } = mount([
		{ operation: "Early", workstation: "WS-1", item_code: "FG-1", planned_qty: 1, revised_start: "2026-10-20 08:00:00", revised_end: "2026-10-20 10:00:00" },
		{ operation: "Short", workstation: "WS-1", item_code: "FG-2", planned_qty: 5, revised_start: "2026-10-30 08:00:00", revised_end: "2026-10-30 13:00:00" },
		{ operation: "Long", workstation: "WS-1", item_code: "FG-3", planned_qty: 9, revised_start: "2026-10-22 08:00:00", revised_end: "2026-11-04 08:00:00" },
	]);
	const bar = (n) => el.querySelector(`.ppc-bar[data-name="${n}"]`);
	assert.ok(bar("JC-2").classList.contains("narrow"), "5 hours is about 20px at this scale");
	assert.ok(!bar("JC-3").classList.contains("narrow"), "13 days is wide enough for its text");
	assert.ok(bar("JC-1").classList.contains("narrow"));
	// the last bar of the chart has no room on its right
	const latest = mount([
		{ operation: "First", workstation: "WS-1", item_code: "FG-1", revised_start: "2026-10-01 08:00:00", revised_end: "2026-10-01 10:00:00" },
		{ operation: "Last", workstation: "WS-1", item_code: "FG-2", revised_start: "2026-11-20 08:00:00", revised_end: "2026-11-20 10:00:00" },
	]).el;
	assert.ok(latest.querySelector('.ppc-bar[data-name="JC-2"]').classList.contains("flip"));
	assert.ok(!latest.querySelector('.ppc-bar[data-name="JC-1"]').classList.contains("flip"), "the first bar has room on its right");
	assert.match(latest.querySelector('.ppc-bar[data-name="JC-2"] .ppc-bar-label').textContent, /Last · WS-1 · FG-2/, "the text is still there, just not clipped");
});
