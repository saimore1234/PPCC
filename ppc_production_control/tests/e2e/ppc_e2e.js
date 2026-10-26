/*
 * PPC Production Control - browser end-to-end test (Playwright).
 *
 *   Sales Order -> standard Production Plan -> draft Work Orders (background job) -> submit in the PPC screen ->
 *   Job Cards (ERPNext) -> baseline -> Gantt drag / resize / cascade / refusal / stale token -> reload ->
 *   revision history -> consistency check + repair -> view-only user -> cleanup.
 *
 * It needs a running Frappe site with a background worker and COMMITS disposable PPCE2E-* records, which it removes
 * again at the end (see fixture.py). Run it on a development or staging site only. Configuration and the waiting
 * rules (state, never fixed delays) are described in e2e_lib.js; the drag interaction has its own regression test,
 * ppc_drag_regression.js.
 */
const L = require("./e2e_lib");
const { assert, path, SHOTS, MANAGER, VIEWER, fx, sleep, secs, plusHours, snapshot, login, openControl, barSel, barLeft, dragBar, setColumns, modalTitle } = L;
const waitIdle = L.untilIdle;

/* ---------------------------------------------------------------------------------------- the test */
(async () => {
	const run = L.makeRunner();
	const { step, results, openPages } = run;
	const count = run.count;
	const browser = await L.launch();
	const problems = [];
	const rpc = []; // every PPC reschedule call: request + parsed response
	const watch = (page, label) => L.watch(page, label, problems, rpc);

	let snap, plan, cutName, asmName;
	let pageM, pageV;
	try {
		await step("fixtures: clean slate, throw-away users, Sales Order, submitted standard Production Plan", async () => {
			fx("cleanup");
			fx("users", { action: "create" });
			const made = fx("setup");
			assert.ok(made.plan && made.sales_order, JSON.stringify(made));
			plan = made.plan;
			return made;
		}, { critical: true });

		await step("the background job creates the draft Work Order automatically (no duplicate)", async () => {
			let wos = [];
			for (let i = 0; i < 45 && !wos.length; i++) { await sleep(4000); wos = fx("state").work_orders; }
			await sleep(5000); // a duplicate would show up here
			snap = snapshot();
			assert.equal(snap.raw.work_orders.length, 1, "exactly one Work Order for the single plan row");
			assert.equal(snap.wo.docstatus, 0);
			assert.equal(snap.wo.status, "Draft");
			assert.equal(snap.wo.job_cards.length, 0, "Job Cards only exist after the Work Order is submitted");
		}, { critical: true });

		const ctx = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
		pageM = await ctx.newPage();
		openPages.push(pageM);
		watch(pageM, "manager");

		await step("PPC Manager (no System Manager role) logs in and filters the control page by the plan", async () => {
			await login(pageM, MANAGER);
			await openControl(pageM, plan, "workorders");
			const filter = await pageM.locator('.ppc-page:visible [data-fieldname="plan"] input').inputValue();
			assert.equal(filter, plan, "the plan from the link is applied as a filter");
			await pageM.screenshot({ path: path.join(SHOTS, "01_workorders.png") });
			return filter;
		}, { critical: true });

		await step("the draft Work Order is listed and can be submitted in bulk from the PPC screen", async () => {
			await pageM.waitForSelector(".ppc-page:visible tbody tr[data-id] input.ppc-pick", { timeout: 30000 });
			assert.equal(await pageM.locator(".ppc-page:visible tbody tr[data-id]").count(), 1);
			await pageM.locator(".ppc-page:visible tbody tr[data-id] input.ppc-pick").check();
			await pageM.click(".ppc-page:visible .ppc-submit-wo");
			const pre = await modalTitle(pageM, 30000); // every record is checked first; problems are shown BEFORE anything is submitted
			assert.match(pre.title, /Check before submitting/);
			assert.match(await pre.modal.innerText(), /1 ready/);
			await pageM.screenshot({ path: path.join(SHOTS, "02a_precheck.png") });
			assert.equal(snapshot().wo.docstatus, 0, "the pre-check submitted nothing");
			await pre.modal.locator(".btn-primary, .btn-modal-primary").first().click();
			await pageM.waitForSelector("text=1 succeeded", { timeout: 90000 });
			await pageM.screenshot({ path: path.join(SHOTS, "02_wo_submitted.png") });
			await pageM.keyboard.press("Escape");
			snap = snapshot();
			assert.equal(snap.wo.docstatus, 1);
		}, { critical: true });

		await step("ERPNext created one Job Card per BOM operation; each got an immutable baseline (revision 0)", async () => {
			snap = snapshot();
			const cards = snap.wo.job_cards;
			assert.equal(cards.length, 2, "one Job Card per operation, no duplicates");
			assert.deepEqual(cards.map((c) => c.operation), ["PPCE2E Cut", "PPCE2E Assemble"]);
			for (const c of cards) {
				assert.equal(c.ppc_schedule_revision, 0);
				assert.equal(c.ppc_baseline_source, "Created");
				assert.equal(c.ppc_original_start, c.expected_start_date);
				assert.equal(c.revisions.length, 1);
				assert.equal(c.revisions[0].revision_type, "Baseline");
				assert.ok(c.time_rows >= 1, "real scheduled time rows exist");
			}
			assert.ok(secs(snap.cut.expected_end_date) < secs(snap.asm.expected_start_date), "operations are in sequence");
			cutName = snap.cut.name; asmName = snap.asm.name;
			return { cutName, asmName };
		}, { critical: true });

		await step("Job Cards tab shows the new revision columns", async () => {
			await openControl(pageM, plan, "jobcards");
			await pageM.waitForSelector(".ppc-page:visible tbody tr[data-id]", { timeout: 30000 });
			const heads = (await pageM.locator(".ppc-page:visible thead th").allInnerTexts()).join("|");
			for (const h of ["Original Start / End", "Current Start / End", "Actual Start / End", "Revisions", "Last Rescheduled By", "Sync Status"]) {
				assert.ok(heads.includes(h), `column ${h} missing in: ${heads}`);
			}
			assert.equal(await pageM.locator(".ppc-page:visible tbody tr[data-id]").count(), 2);
			assert.ok(await pageM.locator(".ppc-page:visible .ppc-submit-jc").isVisible(), "PPC Manager may submit Job Cards");
			await pageM.screenshot({ path: path.join(SHOTS, "03_jobcards.png") });
		});

		await step("Gantt shows both bars and the schedule columns (original / current / actual / revision / last change / sync)", async () => {
			await openControl(pageM, plan, "gantt");
			await L.installTrace(pageM);
			await pageM.waitForSelector(".ppc-bar", { timeout: 30000 });
						assert.equal(await pageM.locator(".ppc-bar").count(), 2);
			await setColumns(pageM, "schedule");
			const heads = await pageM.locator(".ppc-gantt-left-head .ppc-lcol").allInnerTexts();
			assert.deepEqual(heads, ["Operation / Job Card", "Output qty", "Original start / end", "Current start / end", "Actual start / end", "Rev", "Last rescheduled", "Sync status", "Status"]);
			assert.equal(await pageM.locator(".ppc-orig").count(), 0, "not rescheduled yet: no ghost bar");
			assert.equal(await pageM.locator(`.ppc-lrow[data-name="${asmName}"] .c-rev`).innerText(), "0");
			await pageM.screenshot({ path: path.join(SHOTS, "04_gantt_columns.png") });
			await setColumns(pageM, "compact");
		});

		let asmLeft;
		await step("dragging the last operation 1 day later persists the real Job Card schedule, syncs the Work Order and records revision 1", async () => {
			const before = snapshot();
			const left0 = await barLeft(pageM, asmName);
			rpc.length = 0;
			await dragBar(pageM, asmName, 96); // week scale: 96 px = 1 day, snap 1 h
			await waitIdle(pageM);
			assert.ok(rpc.some((r) => r.msg && r.msg.ok), "server accepted: " + JSON.stringify(rpc));
			const after = snapshot();
			const a = after.asm;
			assert.equal(secs(a.expected_start_date), plusHours(secs(before.asm.expected_start_date), 24));
			assert.equal(secs(a.expected_end_date), plusHours(secs(before.asm.expected_end_date), 24));
			assert.equal(a.ppc_schedule_revision, 1);
			assert.equal(a.ppc_original_start, before.asm.ppc_original_start, "the baseline is never overwritten");
			assert.equal(secs(a.ppc_revised_start), secs(a.expected_start_date), "accepted == stored");
			assert.deepEqual(a.revisions.map((r) => [r.revision_no, r.change_source]), [[0, "Baseline"], [1, "Gantt Drag"]]);
			assert.equal(a.revisions[1].changed_by, MANAGER);
			assert.equal(a.time_rows, before.asm.time_rows, "ERPNext time rows moved, not duplicated");
			assert.equal(secs(after.wo.planned_end_date), secs(a.expected_end_date), "Work Order end = latest Job Card end");
			assert.equal(secs(after.wo.planned_start_date), secs(after.cut.expected_start_date), "Work Order start = earliest Job Card start");
			assert.equal(after.cut.ppc_schedule_revision, 0, "the other card was not touched");
			asmLeft = await barLeft(pageM, asmName);
			assert.ok(asmLeft > left0 + 90, `bar moved right (${left0} -> ${asmLeft})`);
			assert.equal(await pageM.locator(`${barSel(asmName)} .ppc-rev`).innerText(), "R1");
			assert.equal(await pageM.locator(".ppc-orig").count(), 1, "ghost bar of the original plan");
		});

		await step("after a full page reload the Gantt shows the persisted dates, ghost bar and revision badge", async () => {
			await openControl(pageM, plan, "gantt");
			await L.installTrace(pageM);
			await pageM.waitForSelector(".ppc-bar");
			assert.ok(Math.abs((await barLeft(pageM, asmName)) - asmLeft) < 1, "same position after reload");
			assert.equal(await pageM.locator(`${barSel(asmName)} .ppc-rev`).innerText(), "R1");
			assert.equal(await pageM.locator(".ppc-orig").count(), 1);
			await setColumns(pageM, "schedule");
			const row = pageM.locator(`.ppc-lrow[data-name="${asmName}"]`);
			assert.equal(await row.locator(".c-rev").innerText(), "1");
			assert.match(await row.locator(".c-sync").innerText(), /Rescheduled/);
			assert.ok((await row.locator(".c-last").innerText()).includes(MANAGER));
			await pageM.screenshot({ path: path.join(SHOTS, "05_after_drag_reload.png") });
			await setColumns(pageM, "compact");
		});

		await step("resizing the first operation's end records revision 1 as 'Gantt Resize' and keeps one time row", async () => {
			const before = snapshot();
			rpc.length = 0;
			await dragBar(pageM, cutName, 16, "right"); // +4 h
			await waitIdle(pageM);
			const after = snapshot();
			assert.equal(secs(after.cut.expected_end_date), plusHours(secs(before.cut.expected_end_date), 4));
			assert.equal(secs(after.cut.expected_start_date), secs(before.cut.expected_start_date));
			assert.equal(after.cut.ppc_schedule_revision, 1);
			assert.equal(after.cut.revisions[1].change_source, "Gantt Resize");
			assert.equal(after.cut.time_rows, 1);
			assert.equal(secs(after.wo.planned_end_date), secs(after.asm.expected_end_date), "Work Order still ends with the last operation");
		});

		await step("moving the first operation into its successor asks for confirmation, then cascades with one correlation id", async () => {
			const before = snapshot();
			rpc.length = 0;
			await dragBar(pageM, cutName, 96); // +1 day -> now overlaps the successor
			const c = await modalTitle(pageM);
			assert.match(c.title, /Confirm schedule change/);
			assert.ok(await c.modal.locator('[data-fieldname="shift_dependents"] input[type="checkbox"]').first().isChecked(), "shift following operations is pre-selected");
			await pageM.screenshot({ path: path.join(SHOTS, "06_confirm_cascade.png") });
			await c.modal.locator('[data-fieldname="reason"] textarea').fill("Material arrives a day late");
			await c.modal.locator(".btn-primary, .btn-modal-primary").first().click();
			await waitIdle(pageM);
			const after = snapshot();
			assert.equal(after.cut.ppc_schedule_revision, 2);
			assert.equal(after.asm.ppc_schedule_revision, 2);
			const rc = after.cut.revisions[2], ra = after.asm.revisions[2];
			assert.equal(rc.change_source, "Gantt Drag");
			assert.equal(ra.change_source, "Cascade");
			assert.equal(rc.change_reason, "Material arrives a day late");
			assert.ok(rc.correlation_id && rc.correlation_id === ra.correlation_id, "one user action = one correlation id");
			assert.ok(secs(after.asm.expected_start_date) >= secs(after.cut.expected_end_date), "sequence restored");
			assert.equal(secs(after.wo.planned_start_date), secs(after.cut.expected_start_date));
			assert.equal(secs(after.wo.planned_end_date), secs(after.asm.expected_end_date));
			assert.ok(secs(after.cut.expected_start_date) > secs(before.cut.expected_start_date));
			await pageM.screenshot({ path: path.join(SHOTS, "07_after_cascade.png") });
		});

		await step("an impossible move (before the predecessor) is refused or cancelled and changes nothing", async () => {
			const before = snapshot();
			const left0 = await barLeft(pageM, asmName);
			await dragBar(pageM, asmName, -300);
			const c = await modalTitle(pageM, 15000);
			await pageM.screenshot({ path: path.join(SHOTS, "08_refused.png") });
			const cancel = c.modal.getByRole("button", { name: "Cancel" });
			if (await cancel.count()) await cancel.first().click(); else await pageM.keyboard.press("Escape");
			await waitIdle(pageM);
			const after = snapshot();
			assert.equal(after.asm.expected_start_date, before.asm.expected_start_date);
			assert.equal(after.asm.ppc_schedule_revision, before.asm.ppc_schedule_revision, "no revision for a refused change");
			assert.equal(after.asm.revisions.length, before.asm.revisions.length);
			assert.equal(after.cut.revisions.length, before.cut.revisions.length);
			assert.equal(after.wo.planned_end_date, before.wo.planned_end_date);
			assert.ok(Math.abs((await barLeft(pageM, asmName)) - left0) < 1, "the bar snapped back");
			return c.title;
		});

		await step("a stale browser view is rejected (someone else changed the card) and nothing is saved; then a fresh attempt works", async () => {
			try {
			const before = snapshot();
			fx("disturb", { job_card: asmName });
			const r1 = await L.dragExpectingRequest(pageM, asmName, 24); // +6 h on a stale token: exactly one request
			assert.equal(r1.sent, 1);
			assert.equal(r1.msg.stale, true, "server flagged the token as stale: " + JSON.stringify(r1.msg));
			const c = await modalTitle(pageM, 15000);
			assert.match(c.title, /changed by someone else/i);
			await L.dismissModal(pageM);
			await waitIdle(pageM); // the page reloaded itself with the fresh token
			const mid = snapshot();
			assert.equal(mid.asm.expected_start_date, before.asm.expected_start_date, "stale change not applied");
			assert.equal(mid.asm.revisions.length, before.asm.revisions.length, "no revision for a stale change");
			const r2 = await L.dragExpectingRequest(pageM, asmName, 24); // the same drag now succeeds - no retry, no delay
			assert.equal(r2.sent, 1);
			assert.equal(r2.msg.ok, true, JSON.stringify(r2.msg));
			await waitIdle(pageM);
			const after = snapshot();
			assert.equal(secs(after.asm.expected_start_date), plusHours(secs(before.asm.expected_start_date), 6));
			assert.equal(after.asm.ppc_schedule_revision, before.asm.ppc_schedule_revision + 1);
		} catch (e) { e.message += "\n--- trace (last events) ---\n" + (await L.dumpTrace(pageM)); throw e; }
		});

		await step("the detail panel shows baseline, current schedule, difference text and the whole revision history", async () => {
			await pageM.locator(barSel(asmName)).click();
			await pageM.waitForSelector(".ppc-drawer[data-open] table.ppc-table tbody tr", { timeout: 15000 });
			await waitIdle(pageM);
			const text = (await pageM.locator(".ppc-drawer[data-open]").innerText()).replace(/\s+/g, " ").toLowerCase(); // labels are upper-cased by CSS
			const revs = snapshot().asm.revisions;
			assert.equal(await pageM.locator(".ppc-drawer[data-open] table.ppc-table tbody tr").count(), revs.length);
			for (const needle of ["Original schedule", "Initial schedule", "Current schedule", "Actual execution", "Start moved by", "Gantt Drag", "Cascade", "Baseline"]) {
				assert.ok(text.includes(needle.toLowerCase()), `detail panel lacks "${needle}": ${text.slice(0, 400)}`);
			}
			await pageM.screenshot({ path: path.join(SHOTS, "09_detail_panel.png") });
		});

		await step("consistency check finds an out-of-band change; preview changes nothing; repair restores it as a new revision", async () => {
			const before = snapshot();
			fx("drift", { job_card: cutName, hours: 3 });
			await openControl(pageM, plan, "exceptions");
			const rowText = (await pageM.locator(".ppc-page:visible tbody").innerText()).replace(/\s+/g, " ");
			assert.ok(rowText.includes(cutName), "the drifted Job Card is listed as an exception");
			assert.match((await pageM.locator('.ppc-page:visible .ppc-chip[data-type="schedule_drift"]').innerText()).replace(/\s+/g, " "), /^1 /, "one schedule drift is counted");
			await pageM.screenshot({ path: path.join(SHOTS, "10_consistency.png") });
			await pageM.click(".ppc-page:visible .ppc-preview-repair");
			let c = await modalTitle(pageM, 30000);
			assert.match(c.title, /Repair preview/);
			assert.ok((await c.modal.innerText()).includes(cutName));
			const drifted = snapshot();
			assert.notEqual(secs(drifted.cut.expected_start_date), secs(drifted.cut.ppc_revised_start), "preview did not fix anything yet");
			await c.modal.locator(".btn-primary, .btn-modal-primary").first().click(); // Apply
			await waitIdle(pageM);
			const after = snapshot();
			assert.equal(secs(after.cut.expected_start_date), secs(after.cut.ppc_revised_start), "ERPNext schedule is back on the accepted schedule");
			assert.equal(after.cut.revisions.length, before.cut.revisions.length + 1);
			assert.equal(after.cut.revisions.at(-1).change_source, "Reconciliation");
			// and a second check is clean
			await openControl(pageM, plan, "exceptions");
			assert.equal((await pageM.locator('.ppc-page:visible .ppc-chip[data-type="schedule_drift"] .n').innerText()).trim(), "0");
		});

		await step("audit tab lists the accepted reschedules and the failed/ refused attempts are not mistaken for revisions", async () => {
			await openControl(pageM, plan, "history", { revision_type: "All" });
			await pageM.waitForSelector(".ppc-page:visible table.ppc-table tbody tr", { timeout: 20000 });
			const text = await pageM.locator(".ppc-page:visible").innerText();
			for (const src of ["Gantt Drag", "Gantt Resize", "Cascade", "Reconciliation"]) assert.ok(text.includes(src), `history page lacks ${src}`);
			const n = await pageM.locator(".ppc-page:visible tbody tr[data-id]").count();
			const expected = snapshot().raw.work_orders[0].job_cards.reduce((t, c) => t + c.revisions.length, 0);
			assert.equal(n, expected, "every revision (and baseline) is listed exactly once");
			await pageM.screenshot({ path: path.join(SHOTS, "11_audit.png") });
		});

		await step("a view-only user sees the plan but cannot drag, reschedule, reconcile or call the API", async () => {
			const before = snapshot();
			const ctxV = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
			pageV = await ctxV.newPage();
			watch(pageV, "viewer");
			await login(pageV, VIEWER);
			await openControl(pageV, plan, "gantt");
			await pageV.waitForSelector(".ppc-bar", { timeout: 30000 });
			assert.equal(await pageV.locator(".ppc-bar").count(), 2);
			assert.equal(await pageV.locator(".ppc-handle").count(), 0, "no resize handles");
			assert.ok((await pageV.locator(".ppc-bar.locked").count()) === 2, "bars are read-only");
			let viewerRequests = 0;
			pageV.on("request", (r) => { if (r.url().includes("reschedule_job_card")) viewerRequests++; });
			await dragBar(pageV, asmName, 96);
			await pageV.evaluate(() => null); // round trip: every event of the drag has been handled
			await waitIdle(pageV);
			assert.equal(viewerRequests, 0, "the browser never even sent a reschedule");
			assert.equal(await pageV.locator(".ppc-gantt").getAttribute("data-ppc-drag"), "idle");
			// the server refuses direct calls too
			const status = await pageV.evaluate(async ([jc, s, e]) => {
				const post = (m, body) => fetch("/api/method/ppc_production_control.api." + m, { method: "POST", headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token }, body: JSON.stringify(body) }).then((r) => r.status);
				return {
					reschedule: await post("reschedule_job_card", { job_card: jc, start: s, end: e, confirmed: 1 }),
					repair: await post("repair_schedule_inconsistencies", { dry_run: 0 }),
					init: await post("initialize_missing_baselines", { dry_run: 0 }),
					retry: await post("retry_failed_synchronizations", {}),
					correct: await post("correct_schedule_revision", { revision: "x", reason: "not allowed at all" }),
				};
			}, [asmName, "2030-01-01 08:00:00", "2030-01-01 12:00:00"]);
			for (const [k, v] of Object.entries(status)) assert.ok(v === 403, `${k} should be 403 for a viewer, got ${v}`);
			await openControl(pageV, plan, "jobcards");
			assert.equal(await pageV.locator(".ppc-page:visible .ppc-submit-jc").count(), 0, "submit button hidden");
			assert.equal(await pageV.locator(".ppc-page:visible .ppc-edit").count(), 0, "no date editing for a viewer");
			await openControl(pageV, plan, "exceptions");
			assert.equal(await pageV.locator(".ppc-page:visible .ppc-init-baselines").count(), 0, "reconcile button hidden");
			assert.equal(await pageV.locator(".ppc-page:visible .ppc-retry-sync").count(), 0, "retry button hidden");
			assert.equal(await pageV.locator(".ppc-page:visible .ppc-preview-repair").count(), 0, "repair preview hidden");
			await pageV.screenshot({ path: path.join(SHOTS, "12_viewer.png") });
			const after = snapshot();
			assert.deepEqual(after.asm, before.asm, "nothing changed");
			return status;
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

	console.log(`\nE2E: ${count("PASS")} passed, ${count("FAIL")} failed, ${count("SKIPPED")} skipped of ${results.length}`);
	L.fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify({ results, problems }, null, 2));
	process.exit(count("FAIL") ? 1 : 0);
})();
