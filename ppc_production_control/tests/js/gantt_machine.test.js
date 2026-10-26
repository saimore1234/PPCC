// Dragging a bar onto another Workstation row (jsdom, synthetic pointer events).
const test = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

function sample() {
	const base = { sales_order: "SO-1", item_code: "FG-1", work_order: "WO-1", status: "Open", movable: true, conflict: false, overdue: false };
	return {
		tasks: [
			{ ...base, name: "JC-1", operation: "Cut", workstation: "WS1", revised_start: "2027-03-02 08:00:00", revised_end: "2027-03-02 13:00:00" },
			{ ...base, name: "JC-2", operation: "Assemble", workstation: "WS2", revised_start: "2027-03-02 13:10:00", revised_end: "2027-03-02 23:10:00" },
			{ ...base, name: "JC-3", operation: "Pack", workstation: "WS2", status: "Completed", movable: false, revised_start: "2027-03-04 08:00:00", revised_end: "2027-03-04 10:00:00" },
		],
		links: [],
	};
}

function setup(options = {}, data) {
	const dom = new JSDOM('<!doctype html><body><div id="g"></div></body>', { pretendToBeVisual: true });
	global.window = dom.window;
	global.document = dom.window.document;
	delete require.cache[require.resolve("../../public/gantt/ppc_gantt.js")];
	const lib = require("../../public/gantt/ppc_gantt.js");
	const el = document.getElementById("g");
	const calls = [];
	const g = new lib.PPCGantt(el, Object.assign({
		scale: "week", groupBy: "workstation", now: "2027-03-02 08:00:00", canChangeWorkstation: true,
		onChange: (evt) => { calls.push(evt); return Promise.resolve(g._reply ? g._reply(evt) : { ok: true }); },
	}, options));
	g.setData(data || sample());
	return { g, el, lib, calls, window: dom.window };
}

const ev = (win, type, props) => new win.MouseEvent(type, Object.assign({ bubbles: true, cancelable: true, button: 0 }, props));
const bar = (el, name) => el.querySelector(`.ppc-bar[data-name="${name}"]`);
const stateOf = (el) => el.dataset.ppcDrag;
const groups = (el) => [...el.querySelectorAll(".ppc-lrow.group")].map((r) => r.dataset.group);

// Layout: WS1 header y 0-28, JC-1 row 28-68; WS2 header 68-96, JC-2 row 96-136, JC-3 row 136-176.
async function vdrag(window, target, { dx = 0, y0 = 48, y1 = 48, up = true } = {}) {
	target.dispatchEvent(ev(window, "pointerdown", { clientX: 200, clientY: y0 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 200 + dx / 2, clientY: (y0 + y1) / 2 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 200 + dx, clientY: y1 }));
	if (up) window.document.dispatchEvent(ev(window, "pointerup", { clientX: 200 + dx, clientY: y1 }));
	await new Promise((r) => setTimeout(r, 0));
}

test("dropping a bar on another Workstation row reports the new machine, with the dates unchanged", async () => {
	const { window, el, calls } = setup();
	await vdrag(window, bar(el, "JC-1"), { y1: 120 });
	assert.equal(calls.length, 1);
	assert.equal(calls[0].workstation, "WS2");
	assert.equal(calls[0].previousWorkstation, "WS1");
	assert.equal(calls[0].mode, "move");
	assert.equal(calls[0].start, calls[0].previous.start, "a vertical move alone keeps the dates");
	assert.equal(calls[0].end, calls[0].previous.end);
});

test("a diagonal drop changes the dates and the machine in one gesture", async () => {
	const { window, el, calls } = setup();
	await vdrag(window, bar(el, "JC-1"), { dx: 96, y1: 150 }); // week scale: 96 px = one day
	assert.equal(calls.length, 1);
	assert.equal(calls[0].workstation, "WS2");
	assert.equal(calls[0].start, "2027-03-03 08:00:00");
	assert.equal(calls[0].previous.start, "2027-03-02 08:00:00");
});

test("while dragging, the row it would land on is highlighted and the bar follows the pointer; both go away afterwards", async () => {
	const { window, el, g } = setup();
	const b = bar(el, "JC-1");
	b.dispatchEvent(ev(window, "pointerdown", { clientX: 200, clientY: 48 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 200, clientY: 120 }));
	assert.equal(el.querySelectorAll(".ppc-drop-band").length, 1);
	assert.match(el.querySelector(".ppc-drop-band").textContent, /WS2/);
	assert.ok(el.querySelector('.ppc-lrow.group[data-group="WS2"]').classList.contains("ppc-drop-target"));
	assert.match(b.style.transform, /translateY\(72px\)/);
	assert.match(g.$tip.innerHTML, /→ WS2/, "the drag label names the machine");
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 200, clientY: 50 })); // back over its own row
	assert.equal(el.querySelectorAll(".ppc-drop-band").length, 0);
	window.document.dispatchEvent(ev(window, "pointerup", { clientX: 200, clientY: 50 }));
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(el.querySelectorAll(".ppc-drop-band").length, 0);
	assert.equal(el.querySelectorAll(".ppc-drop-target").length, 0);
	assert.equal(b.style.transform, "");
});

test("without the capability, on its own row, or on 'no workstation', a vertical drag does nothing", async () => {
	let s = setup({ canChangeWorkstation: false });
	await vdrag(s.window, bar(s.el, "JC-1"), { y1: 120 });
	assert.equal(s.calls.length, 0, "not allowed to change machine");

	s = setup();
	await vdrag(s.window, bar(s.el, "JC-1"), { y1: 60 }); // still on WS1's row
	assert.equal(s.calls.length, 0);

	const data = sample();
	data.tasks.push({ ...data.tasks[0], name: "JC-9", operation: "Pick", workstation: null, revised_start: "2027-03-05 08:00:00", revised_end: "2027-03-05 10:00:00" });
	s = setup({}, data);
	const none = s.g._spans.find((sp) => sp.key === "(No workstation)");
	assert.ok(none, "an operation without a machine gets its own group");
	await vdrag(s.window, bar(s.el, "JC-1"), { y0: 48 + 40, y1: none.y0 + 10 });
	assert.equal(s.calls.length, 0, "a Job Card cannot be dropped onto 'no workstation'");
});

test("resizing never changes the machine, and neither does a locked bar", async () => {
	const { window, el, calls } = setup();
	const handle = bar(el, "JC-1").querySelector(".ppc-handle.right");
	await vdrag(window, handle, { dx: 48, y1: 120 });
	assert.equal(calls.length, 1);
	assert.equal(calls[0].mode, "resize");
	assert.equal(calls[0].workstation, undefined);
	await vdrag(window, bar(el, "JC-3"), { y0: 150, y1: 50 }); // completed card
	assert.equal(calls.length, 1);
});

test("a refused machine change puts the bar back on its own row", async () => {
	const { g, window, el, calls } = setup();
	g._reply = () => ({ ok: false });
	await vdrag(window, bar(el, "JC-1"), { y1: 120 });
	assert.equal(calls.length, 1);
	assert.equal(g.taskByName("JC-1").workstation, "WS1");
	const rows = [...el.querySelectorAll(".ppc-lrow")];
	const header = rows.findIndex((r) => r.dataset.group === "WS1");
	assert.equal(rows.findIndex((r) => r.dataset.name === "JC-1"), header + 1, "still directly under the WS1 header");
	assert.equal(stateOf(el), "idle");
	assert.equal(bar(el, "JC-1").style.transform, "");
});

test("an accepted machine change moves the bar under its new group", async () => {
	const { g, window, el } = setup();
	g._reply = (evt) => ({ ok: true, workstation: evt.workstation, start: evt.start, end: evt.end });
	await vdrag(window, bar(el, "JC-1"), { y1: 120 });
	assert.equal(g.taskByName("JC-1").workstation, "WS2");
	assert.deepEqual(groups(el), ["WS2"], "WS1 has no operation left in view");
	assert.match(bar(el, "JC-1").querySelector(".ppc-bar-label").textContent, /WS2/);
});

test("Escape cancels a vertical drag completely", async () => {
	const { window, el, calls, g } = setup();
	bar(el, "JC-1").dispatchEvent(ev(window, "pointerdown", { clientX: 200, clientY: 48 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 200, clientY: 120 }));
	assert.equal(el.querySelectorAll(".ppc-drop-band").length, 1);
	window.document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
	window.document.dispatchEvent(ev(window, "pointerup", { clientX: 200, clientY: 120 }));
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(calls.length, 0);
	assert.equal(el.querySelectorAll(".ppc-drop-band").length, 0);
	assert.equal(stateOf(el), "idle");
	assert.equal(g.taskByName("JC-1").workstation, "WS1");
});

test("machines without any operation in view can be shown as drop targets", async () => {
	const data = { ...sample(), workstations: [{ name: "WS1" }, { name: "WS2" }, { name: "WS3" }, { name: "WS4" }] };
	const { g, window, el, calls } = setup({}, data);
	assert.deepEqual(groups(el), ["WS1", "WS2"]);
	g.setShowAllMachines(true);
	assert.deepEqual(groups(el), ["WS1", "WS2", "WS3", "WS4"]);
	assert.match(el.querySelector('.ppc-lrow.group[data-group="WS3"]').textContent, /\(0\)/);
	const ws3 = g._spans.find((sp) => sp.key === "WS3");
	await vdrag(window, bar(el, "JC-1"), { y1: ws3.y0 + 10 });
	assert.equal(calls.length, 1);
	assert.equal(calls[0].workstation, "WS3");
	g.setShowAllMachines(false);
	assert.deepEqual(groups(el), ["WS1", "WS2"]);
});

test("grouping by something other than Workstation turns machine dragging off", async () => {
	const { g, window, el, calls } = setup();
	g.setGroupBy("work_order");
	await vdrag(window, bar(el, "JC-1"), { y1: 120 });
	assert.equal(calls.length, 0);
	assert.equal(el.querySelectorAll(".ppc-drop-band").length, 0);
});

test("a reload while the bar is being dropped on another machine keeps showing where it is going", async () => {
	const { g, window, el } = setup();
	bar(el, "JC-1").dispatchEvent(ev(window, "pointerdown", { clientX: 200, clientY: 48 }));
	window.document.dispatchEvent(ev(window, "pointermove", { clientX: 200, clientY: 120 }));
	g.setData(sample()); // data arrives mid-drag: every bar element is replaced
	assert.equal(el.querySelectorAll(".ppc-drop-band").length, 1, "the drop target is still lit");
	assert.match(bar(el, "JC-1").style.transform, /translateY/);
	window.document.dispatchEvent(ev(window, "pointerup", { clientX: 200, clientY: 120 }));
	await new Promise((r) => setTimeout(r, 0));
	assert.equal(stateOf(el), "idle");
});
