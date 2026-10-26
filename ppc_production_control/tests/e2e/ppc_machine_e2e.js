/*
 * Changing a Job Card's Workstation from the Gantt chart - browser end-to-end test (Playwright).
 *
 * Three plans of the same finished good give six Job Cards on two machines; extra machines (WS3-WS7) are added by the
 * fixture. Scenarios: drag to a free machine, conflict popup (details, cancel), alternative machine, next free slot via the
 * detail drawer, conflict override (refused for a planner, allowed with a reason for a manager), duplicate and
 * concurrent requests, persistence after a reload, and the schedule history that records it all.
 *
 * COMMITS disposable PPCE2E-* records and removes them again (fixture.py): run it on a development or staging site only.
 */
const L = require("./e2e_lib");
const { assert, path, SHOTS, BASE, MANAGER, PLANNER, VIEWER, fx, sleep, secs, plusHours, snapshot, login } = L;

const P = ".ppc-page:visible";
const API = "ppc_production_control.api.";
const ws = (n) => `PPCE2E ${n}`;

const cards = () => snapshot().raw.work_orders.map((w) => ({ wo: w, cut: w.job_cards[0], asm: w.job_cards[1] }));
const find = (name) => { for (const c of cards()) for (const j of c.wo.job_cards) if (j.name === name) return { card: j, wo: c.wo }; return null; };
const revs = (name) => find(name).card.revisions;
const lastRev = (name) => revs(name).at(-1);

/** The chart is as tall as the page so that every machine row is in view (no scrolling while dragging). */
async function openGantt(page) {
	await page.goto(`${BASE}/app/ppc-gantt?item_code=PPCE2E-FG`);
	await page.waitForSelector(`${P} .ppc-gantt .ppc-bar`, { timeout: 45000 });
	await page.waitForFunction(() => document.querySelectorAll(".ppc-page:not([hidden]) .ppc-bar").length === 6, null, { timeout: 30000 });
	await L.untilIdle(page);
	if (!(await page.locator(`${P} .ppc-all-machines`).isChecked())) await page.locator(`${P} .ppc-all-machines`).check();
	await page.evaluate(() => { document.querySelector(".ppc-page:not([hidden]) .ppc-gantt-main").style.height = "2000px"; });
	await page.waitForSelector(`${P} .ppc-lrow.group[data-group="${"PPCE2E WS7"}"]`);
}

async function dialog(page, re, timeout = 30000) {
	await page.waitForFunction((src) => [...document.querySelectorAll(".modal.show .modal-title")].some((t) => new RegExp(src).test(t.innerText)), re.source, { timeout });
	await page.waitForFunction(L.modalFullyShown); // a dialog that is still fading in ignores hide()
	return page.locator(".modal.show", { has: page.locator(".modal-title", { hasText: re }) }).last();
}

const button = (modal, name) => modal.getByRole("button", { name, exact: true });

/** Drag a bar onto a machine's row (and sideways by dx). */
async function dragToMachine(page, name, machine, dx = 0) {
	const bar = page.locator(L.barSel(name));
	await bar.scrollIntoViewIfNeeded();
	const box = await bar.boundingBox();
	const header = await page.locator(`${P} .ppc-lrow.group[data-group="${machine}"]`).boundingBox();
	const x = box.x + box.width / 2;
	const y = box.y + box.height / 2;
	await page.mouse.move(x, y);
	await page.mouse.down();
	await page.mouse.move(x + dx / 2, (y + header.y + 14) / 2, { steps: 6 });
	await page.mouse.move(x + dx, header.y + 14, { steps: 6 });
	await page.mouse.up();
}

const changeCall = (page) => page.waitForResponse((r) => r.url().includes(API + "change_job_card_workstation"), { timeout: 60000 });

(async () => {
	const run = L.makeRunner();
	const { step, results, openPages } = run;
	const browser = await L.launch();
	const problems = [];
	let pageM, pageP, pageV, P1, P2, P3, baseline = {};
	try {
		await step("fixtures: three plans, six Job Cards on two machines, five extra machines, manager / planner / viewer", async () => {
			fx("cleanup");
			fx("users", { action: "create" });
			fx("machines");
			fx("setup");
			fx("second_plan");
			fx("second_plan");
			let n = 0;
			for (let i = 0; i < 45 && n < 3; i++) { await sleep(4000); n = fx("state").work_orders.length; }
			await sleep(6000);
			assert.equal(fx("state").work_orders.length, 3, "one draft Work Order per plan, no duplicates");
			fx("submit_work_orders");
			const [a, b, c] = cards();
			[P1, P2, P3] = [a, b, c];
			for (const x of [P1, P2, P3]) assert.equal(x.wo.job_cards.length, 2);
			assert.equal(P1.asm.workstation, ws("WS2"));
			baseline = { asm1: { ...P1.asm }, wo1: { ...P1.wo } };
			return { P1: P1.wo.name, P2: P2.wo.name, P3: P3.wo.name };
		}, { critical: true });

		const ctx = await browser.newContext({ viewport: { width: 1700, height: 2300 } });
		pageM = await ctx.newPage();
		openPages.push(pageM);
		L.watch(pageM, "manager", problems);
		await step("the PPC Manager opens the Production Gantt (all machines shown)", async () => {
			await login(pageM, MANAGER);
			await openGantt(pageM);
			assert.equal(await pageM.locator(`${P} .ppc-bar`).count(), 6);
			const label = await pageM.locator(`${P} ${L.barSel(P1.asm.name)} .ppc-bar-label`).innerText();
			assert.ok(label.includes(ws("WS2")), `every bar shows its machine: "${label}"`);
			await pageM.screenshot({ path: path.join(SHOTS, "m01_gantt.png") });
		}, { critical: true });

		/* ------------------------------------------------------------------------------------------ 1. free machine */
		await step("dragging a Job Card down onto a free machine asks for confirmation, then moves it (dates, baseline and Work Order unchanged)", async () => {
			const before = find(P1.asm.name);
			const revCount = before.card.revisions.length;
			const call = changeCall(pageM);
			await dragToMachine(pageM, P1.asm.name, ws("WS5"));
			const d = await dialog(pageM, /^Change Workstation$/);
			const text = (await d.innerText()).replace(/\s+/g, " ");
			assert.ok(text.includes(ws("WS2")) && text.includes(ws("WS5")), "current and proposed machine are shown: " + text.slice(0, 200));
			assert.ok(/unchanged/i.test(text), "the dates stay as they are");
			assert.equal(find(P1.asm.name).card.workstation, ws("WS2"), "nothing is saved before the confirmation");
			await pageM.screenshot({ path: path.join(SHOTS, "m02_confirm.png") });
			await button(d, "Change Workstation").click();
			const res = await (await call).json();
			assert.equal(res.message.ok, true, JSON.stringify(res.message));
			await L.untilIdle(pageM);

			const after = find(P1.asm.name);
			assert.equal(after.card.workstation, ws("WS5"), "the real Job Card is on the new machine");
			assert.equal(secs(after.card.expected_start_date), secs(before.card.expected_start_date));
			assert.equal(secs(after.card.expected_end_date), secs(before.card.expected_end_date));
			assert.equal(secs(after.card.ppc_original_start), secs(baseline.asm1.ppc_original_start), "the original baseline is untouched");
			assert.equal(secs(after.wo.planned_end_date), secs(before.wo.planned_end_date), "Work Order dates follow the policy, which has not moved");
			assert.equal(after.card.revisions.length, revCount + 1, "exactly one history entry");
			const r = lastRev(P1.asm.name);
			assert.deepEqual([r.change_source, r.previous_workstation, r.new_workstation, r.override_used], ["Workstation Change", ws("WS2"), ws("WS5"), 0]);
			assert.equal(r.changed_by, MANAGER);
			// the bar now sits under its new machine
			const rowsAfter = await pageM.locator(`${P} .ppc-gantt-left-rows .ppc-lrow`).evaluateAll((rows) => rows.map((x) => x.dataset.group || x.dataset.name));
			const g = rowsAfter.indexOf(ws("WS5"));
			assert.equal(rowsAfter[g + 1], P1.asm.name);
		});

		await step("after a full reload the Job Card is still on the new machine (persisted), and the history says so", async () => {
			await openGantt(pageM);
			const rows = await pageM.locator(`${P} .ppc-gantt-left-rows .ppc-lrow`).evaluateAll((r) => r.map((x) => x.dataset.group || x.dataset.name));
			assert.equal(rows[rows.indexOf(ws("WS5")) + 1], P1.asm.name);
			assert.ok((await pageM.locator(`${P} ${L.barSel(P1.asm.name)} .ppc-bar-label`).innerText()).includes(ws("WS5")));
			await pageM.locator(L.barSel(P1.asm.name)).click();
			await pageM.waitForSelector(`${P} .ppc-drawer[data-open] table.ppc-table tbody tr`);
			await L.untilIdle(pageM);
			const drawer = (await pageM.locator(`${P} .ppc-drawer[data-open]`).innerText()).replace(/\s+/g, " ");
			assert.ok(drawer.includes("Workstation Change") && drawer.includes(`${ws("WS2")} → ${ws("WS5")}`), "the drawer's history shows the machine change: " + drawer.slice(-300));
			await pageM.keyboard.press("Escape");
		});

		/* -------------------------------------------------------------------------------- 2-4. conflict, cancel */
		await step("an occupied machine shows the conflict popup with the blocking Job Card, its interval and the five choices; Cancel restores everything", async () => {
			const a = find(P2.asm.name).card;
			const blocker = P3.cut;
			fx("place", { job_card: blocker.name, workstation: ws("WS3"), start: plusHours(secs(a.expected_start_date), 1), end: plusHours(secs(a.expected_end_date), 1) });
			await openGantt(pageM);
			const before = { card: find(P2.asm.name).card, revs: revs(P2.asm.name).length };
			let sent = 0;
			const onReq = (r) => { if (r.url().includes(API + "change_job_card_workstation")) sent++; };
			pageM.on("request", onReq);
			await dragToMachine(pageM, P2.asm.name, ws("WS3"));
			const d = await dialog(pageM, /Workstation Conflict Detected/);
			const text = (await d.innerText()).replace(/\s+/g, " ");
			for (const needle of ["The selected machine is already scheduled during the requested time", P2.asm.name, ws("WS3"), blocker.name, "PPCE2E Cut", find(blocker.name).wo.name, "Scheduled From", "Scheduled To"]) {
				assert.ok(text.includes(needle), `conflict popup lacks "${needle}": ${text.slice(0, 500)}`);
			}
			for (const label of ["Cancel Change", "Choose Another Machine", "Find Next Available Slot", "Reschedule Conflicting Operation", "Override Conflict"]) {
				assert.ok(await button(d, label).count(), `button "${label}" is offered to a PPC Manager`);
			}
			await pageM.screenshot({ path: path.join(SHOTS, "m03_conflict.png") });
			await button(d, "Cancel Change").click();
			await pageM.waitForFunction(() => !document.querySelector(".modal.show"));
			await L.untilIdle(pageM);
			pageM.off("request", onReq);
			assert.equal(sent, 0, "nothing was sent to save");
			const after = find(P2.asm.name);
			assert.equal(after.card.workstation, ws("WS2"));
			assert.equal(after.card.revisions.length, before.revs, "no history entry for a change that was cancelled");
			assert.equal(secs(after.card.expected_start_date), secs(before.card.expected_start_date));
			const rows = await pageM.locator(`${P} .ppc-gantt-left-rows .ppc-lrow`).evaluateAll((r) => r.map((x) => x.dataset.group || x.dataset.name));
			const g = rows.indexOf(ws("WS2"));
			assert.ok(rows.slice(g + 1, g + 6).includes(P2.asm.name), "the bar is back on its own machine");
			assert.equal(await pageM.locator(`${P} .ppc-drop-band`).count(), 0);
		});

		/* --------------------------------------------------------------------------------------- 5. another machine */
		await step("'Choose Another Machine' lists machines with availability and conflicts; picking a free one saves the change", async () => {
			await dragToMachine(pageM, P2.asm.name, ws("WS3"));
			let d = await dialog(pageM, /Workstation Conflict Detected/);
			await button(d, "Choose Another Machine").click();
			d = await dialog(pageM, /Choose Another Machine/);
			await d.locator(".ppc-m-list tbody tr").first().waitFor({ timeout: 30000 });
			const rows = await d.locator(".ppc-m-list tbody tr").evaluateAll((trs) => trs.map((tr) => ({ name: tr.children[0].innerText.split("\n")[0].trim(), availability: tr.children[2].innerText.trim(), inWay: tr.children[3].innerText, compat: tr.children[6].innerText.trim(), text: tr.innerText })));
			const by = Object.fromEntries(rows.map((r) => [r.name, r]));
			assert.equal(by[ws("WS3")].availability, "busy");
			assert.ok(by[ws("WS3")].inWay.includes(P3.cut.name), "the busy machine says what is in the way");
			assert.equal(by[ws("WS4")].availability, "available");
			assert.equal(by[ws("WS2")].availability, "current");
			assert.match(by[ws("WS4")].compat, /not configured/i, "compatibility is never invented from names");
			assert.match(await d.innerText(), /No Workstation Type is configured/);
			await d.locator(".ppc-m-search").fill("WS4");
			await pageM.waitForFunction(() => document.querySelectorAll(".modal.show .ppc-m-list tbody tr").length === 1);
			await pageM.screenshot({ path: path.join(SHOTS, "m04_picker.png") });
			const call = changeCall(pageM);
			await d.locator(".ppc-m-pick").click();
			const c = await dialog(pageM, /^Change Workstation$/);
			assert.ok((await c.innerText()).includes(ws("WS4")));
			await button(c, "Change Workstation").click();
			assert.equal((await (await call).json()).message.ok, true);
			await L.untilIdle(pageM);
			assert.equal(find(P2.asm.name).card.workstation, ws("WS4"));
			assert.equal(lastRev(P2.asm.name).new_workstation, ws("WS4"));
		});

		/* ------------------------------------------------------------------- 6, 10-12. next slot from the drawer */
		await step("'Find Next Available Slot' (from the detail drawer) previews the slot; accepting it moves the dates, the Work Order and the history", async () => {
			const asm3 = find(P3.asm.name).card;
			const blocker = P2.cut;
			fx("place", { job_card: blocker.name, workstation: ws("WS3"), start: secs(asm3.expected_start_date), end: plusHours(secs(asm3.expected_end_date), 0) });
			await openGantt(pageM);
			const before = find(P3.asm.name);
			const originalStart = secs(before.card.ppc_original_start);
			await pageM.locator(L.barSel(P3.asm.name)).click();
			await pageM.waitForSelector(`${P} .ppc-drawer[data-open] .ppc-change-ws`);
			await L.untilIdle(pageM);
			await pageM.locator(`${P} .ppc-drawer[data-open] .ppc-change-ws`).click();
			let d = await dialog(pageM, /Choose Another Machine/);
			await d.locator(".ppc-m-list tbody tr").first().waitFor();
			await d.locator(".ppc-m-search").fill("WS3");
			await pageM.waitForFunction(() => document.querySelectorAll(".modal.show .ppc-m-list tbody tr").length === 1);
			const nextCol = await d.locator(".ppc-m-list tbody tr").first().evaluate((tr) => tr.children[5].innerText);
			assert.ok(nextCol.trim() && !/none in/.test(nextCol), "the busy machine shows when it is next free: " + nextCol);
			await d.locator(".ppc-m-slot").click(); // "Use next slot"
			const c = await dialog(pageM, /^Change Workstation$/);
			const text = (await c.innerText()).replace(/\s+/g, " ");
			assert.ok(text.includes(ws("WS3")) && !/unchanged/.test(text), "the proposal shows new dates: " + text.slice(0, 300));
			const call = changeCall(pageM);
			await button(c, "Change Workstation").click();
			assert.equal((await (await call).json()).message.ok, true);
			await L.untilIdle(pageM);

			const after = find(P3.asm.name);
			assert.equal(after.card.workstation, ws("WS3"));
			assert.ok(secs(after.card.expected_start_date) >= secs(find(blocker.name).card.expected_end_date), "the new slot starts when the blocking operation ends");
			assert.equal(secs(after.card.ppc_original_start), originalStart, "baseline preserved");
			const starts = after.wo.job_cards.map((j) => secs(j.expected_start_date)).sort();
			const ends = after.wo.job_cards.map((j) => secs(j.expected_end_date)).sort();
			assert.equal(secs(after.wo.planned_start_date), starts[0], "Work Order start = earliest operation start");
			assert.equal(secs(after.wo.planned_end_date), ends.at(-1), "Work Order end = latest operation end");
			const r = lastRev(P3.asm.name);
			assert.equal(r.change_source, "Workstation Change");
			assert.ok(secs(r.accepted_start) === secs(after.card.expected_start_date));
			assert.notEqual(secs(before.card.expected_start_date), secs(after.card.expected_start_date));
		});

		/* ---------------------------------------------------------------------------------- 7-8. override */
		const ctxP = await browser.newContext({ viewport: { width: 1700, height: 2300 } });
		pageP = await ctxP.newPage();
		openPages.push(pageP);
		L.watch(pageP, "planner", problems);

		await step("a planner is not offered the override and the server refuses it; a manager needs a reason and then it is audited", async () => {
			const a = find(P1.asm.name).card; // on WS5
			fx("place", { job_card: P2.cut.name, workstation: ws("WS6"), start: secs(a.expected_start_date), end: secs(a.expected_end_date) });
			// planner: UI
			await login(pageP, PLANNER);
			await openGantt(pageP);
			await dragToMachine(pageP, P1.asm.name, ws("WS6"));
			let d = await dialog(pageP, /Workstation Conflict Detected/);
			assert.equal(await button(d, "Override Conflict").count(), 0, "no override button for a planner");
			assert.ok(await button(d, "Find Next Available Slot").count());
			assert.match(await d.innerText(), /needs the Override Conflicts permission/);
			await button(d, "Cancel Change").click();
			await pageP.waitForFunction(() => !document.querySelector(".modal.show"));
			// planner: the server
			const denied = await pageP.evaluate(async ([jc, target, reason]) => {
				const r = await fetch("/api/method/ppc_production_control.api.change_job_card_workstation", {
					method: "POST", headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token },
					body: JSON.stringify({ job_card: jc, workstation: target, confirmed: 1, override_conflicts: 1, override_reason: reason }),
				});
				return r.status;
			}, [P1.asm.name, ws("WS6"), "I really need this machine right now"]);
			assert.equal(denied, 403);
			assert.equal(find(P1.asm.name).card.workstation, ws("WS5"), "nothing changed");
			const warn = await pageM.evaluate((jc) => frappe.db.get_list("PPC Audit Log", { filters: { job_card: jc, action: "Change Workstation", result: "Warning" }, fields: ["user"] }), P1.asm.name);
			assert.equal(warn.length, 1, "the refused override is recorded as an audit event");

			// manager: UI
			await openGantt(pageM);
			const revCount = revs(P1.asm.name).length;
			await dragToMachine(pageM, P1.asm.name, ws("WS6"));
			d = await dialog(pageM, /Workstation Conflict Detected/);
			await button(d, "Override Conflict").click();
			const o = await dialog(pageM, /^Override Conflict$/);
			await o.locator('[data-fieldname="reason"] textarea').fill("short");
			await button(o, "Override and Change").click();
			await sleep(600);
			assert.equal(find(P1.asm.name).card.workstation, ws("WS5"), "a reason that short is not accepted");
			assert.ok(await o.isVisible(), "the dialog stays open");
			await o.locator('[data-fieldname="reason"] textarea').fill("Customer escalation approved by the plant head");
			const call = changeCall(pageM);
			await button(o, "Override and Change").click();
			assert.equal((await (await call).json()).message.ok, true);
			await L.untilIdle(pageM);
			assert.equal(find(P1.asm.name).card.workstation, ws("WS6"));
			assert.equal(revs(P1.asm.name).length, revCount + 1);
			const r = lastRev(P1.asm.name);
			assert.equal(r.override_used, 1);
			assert.equal(r.override_reason, "Customer escalation approved by the plant head");
			assert.ok(JSON.parse(r.conflict_details).some((c) => c.job_card === P2.cut.name), "the conflict that was overridden is on the record");
			assert.equal(r.changed_by, MANAGER);
		});

		/* ---------------------------------------------------------------------------- 9. duplicates, 17. concurrency */
		await step("a double click saves once; the same request twice changes nothing the second time; two planners racing for one slot: exactly one wins", async () => {
			// double click on the confirmation
			let sent = 0;
			const onReq = (r) => { if (r.url().includes(API + "change_job_card_workstation")) sent++; };
			pageM.on("request", onReq);
			await openGantt(pageM);
			const call = changeCall(pageM);
			await dragToMachine(pageM, P1.cut.name, ws("WS7"));
			const d = await dialog(pageM, /^Change Workstation$/);
			await button(d, "Change Workstation").dblclick();
			await call;
			await L.untilIdle(pageM);
			pageM.off("request", onReq);
			assert.equal(sent, 1, "one request for a double click");
			assert.equal(revs(P1.cut.name).filter((r) => r.change_source === "Workstation Change").length, 1);

			// the same request id twice (a retry after a lost answer)
			const post = (page, body) => page.evaluate(async (b) => {
				const r = await fetch("/api/method/ppc_production_control.api.change_job_card_workstation", { method: "POST", headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token }, body: JSON.stringify(b) });
				return { status: r.status, body: (await r.json()).message };
			}, body);
			const again = await post(pageM, { job_card: P3.cut.name, workstation: ws("WS4"), confirmed: 1, correlation_id: "dupe-0001" });
			const n = revs(P3.cut.name).length;
			const twice = await post(pageM, { job_card: P3.cut.name, workstation: ws("WS4"), confirmed: 1, correlation_id: "dupe-0001" });
			assert.equal(again.body.ok, true, JSON.stringify(again));
			assert.equal(twice.body.duplicate, true, JSON.stringify(twice));
			assert.equal(revs(P3.cut.name).length, n, "no second history entry");

			// two sessions, one free slot
			const t = (j) => secs(find(j).card.expected_end_date);
			const slot = plusHours([P1.cut, P2.cut, P3.cut, P1.asm, P2.asm, P3.asm].map((j) => t(j.name)).sort().at(-1), 48);
			fx("place", { job_card: P2.asm.name, workstation: ws("WS4"), start: slot, end: plusHours(slot, 2) });
			fx("place", { job_card: P3.asm.name, workstation: ws("WS3"), start: slot, end: plusHours(slot, 2) });
			const body = (jc) => ({ job_card: jc, workstation: ws("WS5"), confirmed: 1 });
			const count = (j) => revs(j.name).filter((r) => r.change_source === "Workstation Change").length;
			const before = { [P2.asm.name]: count(P2.asm), [P3.asm.name]: count(P3.asm) };
			const results = await Promise.all([post(pageM, body(P2.asm.name)), post(pageP, body(P3.asm.name))]);
			const won = results.filter((r) => r.body && r.body.ok);
			const lost = results.filter((r) => r.body && !r.body.ok);
			assert.equal(won.length, 1, "exactly one request wins the slot: " + JSON.stringify(results.map((r) => r.body && (r.body.ok || r.body.errors || r.body.conflicts && "conflict"))));
			assert.equal(lost.length, 1);
			assert.ok(lost[0].body.conflict || lost[0].body.needs_override, "the other one gets a conflict: " + JSON.stringify(lost[0].body).slice(0, 300));
			const onWs5 = [P2.asm, P3.asm].filter((j) => find(j.name).card.workstation === ws("WS5"));
			assert.equal(onWs5.length, 1, "only one of the two ended up on the machine");
			const winner = onWs5[0];
			const loser = winner === P2.asm ? P3.asm : P2.asm;
			const machineChanges = (j) => revs(j.name).filter((r) => r.change_source === "Workstation Change").length;
			assert.equal(machineChanges(winner), before[winner.name] + 1, "the winner has one new machine-change entry");
			assert.equal(machineChanges(loser), before[loser.name], "the loser has none: a refused change leaves no history");
		});

		/* ------------------------------------------------------------------------------ the record, and a viewer */
		await step("Schedule History lists the machine changes; a viewer sees no way to change a machine and the server refuses", async () => {
			await pageM.goto(`${BASE}/app/ppc-schedule-history?work_order=${P1.wo.name}&revision_type=All`);
			await pageM.waitForSelector(`${P} tbody tr[data-id]`);
			await L.untilIdle(pageM);
			const text = (await pageM.locator(`${P} tbody`).innerText()).replace(/\s+/g, " ");
			assert.ok(text.includes("Workstation Change") && text.includes(`${ws("WS2")} → ${ws("WS5")}`), "the log shows the machine change");
			await pageM.screenshot({ path: path.join(SHOTS, "m05_history.png") });
			const ctxV = await browser.newContext({ viewport: { width: 1700, height: 1400 } });
			pageV = await ctxV.newPage();
			openPages.push(pageV);
			L.watch(pageV, "viewer", problems);
			await login(pageV, VIEWER);
			await pageV.goto(`${BASE}/app/ppc-gantt?item_code=PPCE2E-FG`);
			await pageV.waitForSelector(`${P} .ppc-bar`);
			await L.untilIdle(pageV);
			await pageV.locator(L.barSel(P1.asm.name)).click();
			await pageV.waitForSelector(`${P} .ppc-drawer[data-open] table.ppc-table`);
			assert.equal(await pageV.locator(`${P} .ppc-change-ws`).count(), 0, "no Change Workstation button for a viewer");
			const status = await pageV.evaluate(async (jc) => {
				const r = await fetch("/api/method/ppc_production_control.api.change_job_card_workstation", { method: "POST", headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token }, body: JSON.stringify({ job_card: jc, workstation: "PPCE2E WS4", confirmed: 1 }) });
				return r.status;
			}, P1.asm.name);
			assert.equal(status, 403);
		});

		await step("no unexpected browser errors during the whole run", async () => {
			assert.deepEqual(problems, [], problems.join("\n"));
		});
	} finally {
		try {
			const cleaned = fx("cleanup");
			const ok = cleaned && cleaned.clean === true;
			results.push({ name: "cleanup: every PPCE2E record and throw-away user removed", status: ok ? "PASS" : "FAIL", error: ok ? undefined : JSON.stringify(cleaned) });
			console.log((ok ? "PASS  " : "FAIL  ") + "cleanup  " + JSON.stringify(cleaned));
		} catch (e) {
			results.push({ name: "cleanup", status: "FAIL", error: String(e.message).slice(0, 300) });
			console.log("FAIL  cleanup  " + e.message);
		}
		await browser.close();
	}
	console.log(`\nMACHINE E2E: ${run.count("PASS")} passed, ${run.count("FAIL")} failed, ${run.count("SKIPPED")} skipped of ${results.length}`);
	L.fs.writeFileSync(path.join(SHOTS, "results_machine.json"), JSON.stringify({ results, problems }, null, 2));
	process.exit(run.count("FAIL") ? 1 : 0);
})();
