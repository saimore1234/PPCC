/*
 * Regression test for the Gantt interaction that follows a refusal.
 *
 *   drag -> "someone else changed this card" refusal -> (reload) -> drag again, immediately
 *
 * Every wait is on observable state (see e2e_lib.js); every drag must produce exactly ONE reschedule request, and the
 * test FAILS at the timeout if the browser does not send it - there is no retry. The persisted dates are checked in the
 * database after every step. An event trace (pointer events + Gantt drag state) is kept and printed on failure.
 *
 *   PPC_E2E_CYCLES   how many repetitions of each timing variant (default 6)
 *
 * Commits disposable PPCE2E-* records and removes them (run on a development / staging site; see README).
 */
const L = require("./e2e_lib");
const { assert } = L;

// a crashed script must still clean up the data it committed: report stray rejections instead of dying
process.on("unhandledRejection", (e) => console.log("UNHANDLED REJECTION (continuing to cleanup):", e && e.message));

const CYCLES = Number(process.env.PPC_E2E_CYCLES || 6);
const DX = 24; // week scale: 24 px = 6 hours


(async () => {
	const run = L.makeRunner();
	const ONLY = process.env.PPC_E2E_ONLY || "";
	const problems = [];
	const rpc = [];
	const browser = await L.launch();
	let pageA, pageB, ctxA, ctxB, asm, cut, plan;
	const S = (name, fn, opts) => (ONLY && !/fixtures|open the Gantt/.test(name) && !name.includes(ONLY) ? Promise.resolve() : run.step(name, explain(fn), opts));
	const step = run.step;
	const outcomes = { settled: [], "in-flight": [] }; // what the second drag did, per timing variant
	const latencies = []; // server round trip of every drag, ms

	/** Failure context: what each page was doing and which dialogs were open. */
	const explain = (fn) => async () => {
		try { return await fn(); } catch (e) {
			for (const [label, pg] of [["A", pageA], ["B", pageB]]) {
				if (!pg) continue;
				const st = await L.pageState(pg).catch(() => null);
				const dlg = await pg.evaluate(() => [...document.querySelectorAll(".modal.show .modal-title")].map((t) => t.innerText)).catch(() => []);
				const vis = await pg.evaluate(() => ({ visibility: document.visibilityState, focus: document.hasFocus(), shown: window.__modalShown = (() => { const m = document.querySelector(".modal.show"); return m ? [getComputedStyle(m).opacity, getComputedStyle(m.querySelector(".modal-dialog")).transform] : null; })() })).catch(() => null);
				const html = await pg.evaluate(() => { const m = document.querySelector(".modal.show"); return m ? m.outerHTML.replace(/\s+/g, " ").slice(0, 700) : ""; }).catch(() => "");
				await pg.screenshot({ path: L.path.join(L.SHOTS, `r_fail_page_${label}.png`) }).catch(() => {});
				e.message += `
--- page ${label}: ${JSON.stringify(st)} ${JSON.stringify(vis)} dialogs=${JSON.stringify(dlg)} ${html ? "modal=" + html : ""}`;
			}
			throw e;
		}
	};

	const barLeftNow = (page) => L.barLeft(page, asm);
	const asmDb = () => L.snapshot().asm;
	const expectMsg = (r, kind) => {
		assert.equal(r.sent, 1, `exactly one reschedule request, saw ${r.sent}`);
		assert.equal(r.status, 200);
		if (kind === "stale") { assert.equal(r.msg.ok, false); assert.equal(r.msg.stale, true, JSON.stringify(r.msg)); }
		if (kind === "ok") assert.equal(r.msg.ok, true, JSON.stringify(r.msg));
	};

	/** One refusal followed by an immediate second drag. `variant`: "settled" waits for the reload; "in-flight" does not. */
	async function staleThenDrag(variant, dir) { // dir: +1 later / -1 earlier; alternating keeps the net movement at zero (a cumulative drift would eventually cross the Work Order's delivery date and the server would - rightly - ask for confirmation)
		const before = asmDb();
		const left0 = await barLeftNow(pageA);
		L.fx("disturb", { job_card: asm }); // another user saves the card: the page's concurrency token is now stale

		const r1 = await L.dragExpectingRequest(pageA, asm, dir * DX);
		expectMsg(r1, "stale");
		const m = await L.modalTitle(pageA);
		assert.match(m.title, /changed by someone else/i);
		const mid = asmDb();
		assert.equal(mid.expected_start_date, before.expected_start_date, "a refused change saves nothing");
		assert.equal(mid.ppc_schedule_revision, before.ppc_schedule_revision, "...and records no revision");
		assert.equal(mid.revisions.length, before.revisions.length);
		await L.dismissModal(pageA);

		// nothing is left half-updated in the page
		const shape = await pageA.evaluate((sel) => ({
			dragging: document.querySelectorAll(".ppc-bar.dragging").length, pending: document.querySelectorAll(".ppc-bar.pending").length,
			dragTip: (() => { const t = document.querySelector(".ppc-gantt-tip"); return getComputedStyle(t).display !== "none" && !t.querySelector("table"); })(), // the hover tooltip has a table, the drag label does not
		}), ".ppc-bar");
		assert.deepEqual(shape, { dragging: 0, pending: 0, dragTip: false }, "no leftover drag classes or drag-time tooltip");

		if (variant === "settled") {
			await L.untilIdle(pageA); // reload finished: the page now holds the fresh token
			assert.ok(Math.abs((await barLeftNow(pageA)) - left0) < 1, "the refused bar is back where it was");
		}
		const r2 = await L.dragExpectingRequest(pageA, asm, dir * DX); // the user drags again right away
		latencies.push(r1.ms, r2.ms);
		let outcome;
		if (variant === "settled") {
			expectMsg(r2, "ok");
			outcome = "ok";
		} else {
			assert.equal(r2.sent, 1, `exactly one request, saw ${r2.sent}`);
			assert.ok(r2.msg.ok === true || r2.msg.stale === true, "either saved or cleanly refused: " + JSON.stringify(r2.msg));
			outcome = r2.msg.ok ? "ok" : "stale";
		}
		if (outcome === "stale") await L.dismissModal(pageA);
		await L.untilIdle(pageA);
		const after = asmDb();
		const moved = outcome === "ok" ? dir * 6 : 0;
		assert.equal(L.secs(after.expected_start_date), L.plusHours(L.secs(before.expected_start_date), moved), `dates after '${outcome}'`);
		assert.equal(after.ppc_schedule_revision, before.ppc_schedule_revision + (outcome === "ok" ? 1 : 0));
		assert.equal(after.revisions.length, before.revisions.length + (outcome === "ok" ? 1 : 0));
		assert.equal(L.secs(after.ppc_revised_start), L.secs(after.expected_start_date), "accepted == stored");
		return outcome;
	}

	try {
		await step("fixtures: clean slate, users, plan, draft Work Order, submitted (Job Cards)", async () => {
			L.fx("cleanup");
			L.fx("users", { action: "create" });
			const made = L.fx("setup");
			assert.ok(made.plan, JSON.stringify(made));
			plan = made.plan;
			let n = 0;
			for (let i = 0; i < 45 && !n; i++) { await L.sleep(4000); n = L.fx("state").work_orders.length; } // polling the DATABASE for the background job
			assert.equal(n, 1);
			L.fx("submit_work_orders");
			const s = L.snapshot();
			assert.equal(s.wo.job_cards.length, 2);
			cut = s.cut.name; asm = s.asm.name;
			return { plan, cut, asm };
		}, { critical: true });

		ctxA = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
		pageA = await ctxA.newPage();
		run.openPages.push(pageA);
		L.watch(pageA, "A", problems, rpc);

		await S("open the Gantt as a PPC Manager", async () => {
			await L.login(pageA, L.MANAGER);
			await L.openControl(pageA, plan, "gantt");
			await pageA.waitForSelector(L.barSel(asm));
			await L.installTrace(pageA);
			const st = await L.pageState(pageA);
			assert.deepEqual(st, { drag: "idle", busy: 0, ajax: 0, modals: 0, backdrops: 0, freeze: false });
			return st;
		}, { critical: true });

		await S("a rejected drag leaves the Gantt exactly as it was and the next drag works at once", async () => {
			const before = asmDb();
			const left0 = await barLeftNow(pageA);
			await L.untilIdle(pageA);
			// an impossible move: starts before the predecessor ends (distance computed from the data, 4 px per hour at week scale);
			// the user cancels the confirmation
			const snapN = L.snapshot();
			const gapHours = (Date.parse(L.secs(snapN.asm.expected_start_date).replace(" ", "T") + "Z") - Date.parse(L.secs(snapN.cut.expected_end_date).replace(" ", "T") + "Z")) / 3600000;
			const px = -Math.ceil((gapHours + 12) * 4);
			assert.ok(px > -520, `premise: the predecessor must be reachable by dragging (gap ${gapHours.toFixed(1)} h)`);
			await L.dragBar(pageA, asm, px);
			const m = await L.modalTitle(pageA, 20000);
			await pageA.screenshot({ path: L.path.join(L.SHOTS, "r1_rejected.png") });
			const cancel = m.modal.getByRole("button", { name: "Cancel" });
			if (await cancel.count()) await cancel.first().click(); else await pageA.keyboard.press("Escape");
			await pageA.waitForFunction(() => !document.querySelector(".modal.show") && !document.querySelector(".modal-backdrop"));
			await L.untilIdle(pageA);
			const mid = asmDb();
			assert.equal(mid.expected_start_date, before.expected_start_date);
			assert.equal(mid.revisions.length, before.revisions.length);
			assert.ok(Math.abs((await barLeftNow(pageA)) - left0) < 1, "bar snapped back");
			const r = await L.dragExpectingRequest(pageA, asm, DX);
			expectMsg(r, "ok");
			await L.untilIdle(pageA);
			assert.equal(L.secs(asmDb().expected_start_date), L.plusHours(L.secs(before.expected_start_date), 6));
		});

		for (const variant of ["settled", "in-flight"]) {
			for (let i = 1; i <= CYCLES; i++) {
				await S(`refusal then immediate drag - ${variant} reload - cycle ${i}/${CYCLES}`, async () => {
					try {
						const o = await staleThenDrag(variant, i % 2 ? 1 : -1);
						outcomes[variant].push(o);
						return o;
					} catch (e) {
						e.message += "\n--- trace (last events) ---\n" + (await L.dumpTrace(pageA));
						throw e;
					}
				});
			}
		}

		await S("ROOT CAUSE of the earlier flake (characterisation): a refusal dialog that is still fading in ignores Escape, so the next drag lands on its overlay and sends nothing", async () => {
			await L.untilIdle(pageA);
			const before = asmDb();
			// slow the dialog's fade-in to 2 s so the window in which Bootstrap ignores hide() is wide and certain
			const slow = await pageA.addStyleTag({ content: ".modal.fade .modal-dialog{transition:transform 2s ease-out!important}.modal.fade{transition:opacity 2s linear!important}" });
			try {
				L.fx("disturb", { job_card: asm });
				const r1 = await L.dragExpectingRequest(pageA, asm, DX);
				expectMsg(r1, "stale");
				await pageA.waitForFunction(() => document.querySelector(".modal.show")); // the dialog has just appeared and is fading in
				await pageA.keyboard.press("Escape"); // what the earlier test did, instantly
				assert.equal((await L.pageState(pageA)).modals, 1, "Escape is ignored while the dialog is still animating in");
				await pageA.waitForFunction(L.modalFullyShown); // (the earlier test spent a few seconds reading the database here)
				// ...and then it dragged: the drag lands on the dialog's overlay
				let sent = 0;
				const onReq = (r) => { if (r.url().includes("reschedule_job_card")) sent++; };
				pageA.on("request", onReq);
				await L.dragBar(pageA, asm, DX);
				await pageA.evaluate(() => null);
				pageA.off("request", onReq);
				assert.equal(sent, 0, "the drag sent no request: it hit the dialog's overlay, not the bar");
				await pageA.waitForFunction(() => !document.querySelector(".modal.show") && !document.querySelector(".modal-backdrop"));
			} finally {
				await slow.evaluate((e) => e.remove());
			}
			await L.untilIdle(pageA);
			assert.equal(L.secs(asmDb().expected_start_date), L.secs(before.expected_start_date), "nothing was saved by the swallowed drag");
			const again = await L.dragExpectingRequest(pageA, asm, DX); // the click on the overlay dismissed the dialog: this drag works
			expectMsg(again, "ok");
			await L.untilIdle(pageA);
			assert.equal(L.secs(asmDb().expected_start_date), L.plusHours(L.secs(before.expected_start_date), 6));
		});

		await S("while a save is in flight the page is frozen: no second request can be started, and the one save is applied exactly once", async () => {
			await L.untilIdle(pageA);
			const before = asmDb();
			let release;
			const gate = new Promise((r) => { release = r; });
			const requests = [];
			const onReq = (r) => { if (r.url().includes("reschedule_job_card")) requests.push(r.postData()); };
			pageA.on("request", onReq);
			const hold = async (route) => { await gate; await route.continue().catch(() => {}); }; // the server "takes its time"
			await pageA.route("**/*reschedule_job_card*", hold);
			try {
				await L.dragBar(pageA, asm, DX);
				await pageA.waitForFunction(() => document.querySelector(".ppc-gantt").dataset.ppcDrag === "pending");
				await pageA.waitForFunction(() => { const f = document.getElementById("freeze"); return !!(f && f.classList.contains("in")); }); // Frappe adds the overlay's visible state a moment after the request starts
				const st = await L.pageState(pageA);
				assert.equal(st.drag, "pending");
				assert.equal(st.freeze, true, "Frappe's freeze overlay covers the page while the save is unanswered");
				assert.ok(await pageA.locator(L.barSel(asm)).evaluate((b) => b.classList.contains("pending")), "the bar looks 'saving'");
				await L.dragBar(pageA, asm, 48); // raw mouse input lands on the overlay, not on a bar
				assert.equal(requests.length, 1, "exactly one reschedule request while the first is unanswered");
				assert.equal(L.secs(asmDb().expected_start_date), L.secs(before.expected_start_date), "the server has not answered: nothing saved yet");
			} finally {
				release(); // the held request now goes through
			}
			await L.untilIdle(pageA);
			await pageA.unroute("**/*reschedule_job_card*", hold);
			pageA.off("request", onReq);
			assert.equal(requests.length, 1);
			const after = asmDb();
			assert.equal(L.secs(after.expected_start_date), L.plusHours(L.secs(before.expected_start_date), 6), "the one drag was saved, once");
			assert.equal(after.ppc_schedule_revision, before.ppc_schedule_revision + 1);
			assert.equal(await pageA.locator(".ppc-gantt").getAttribute("data-ppc-drag"), "idle");
		});

		await S("a reload that re-renders the chart in the middle of a drag does not lose the drag", async () => {
			await L.untilIdle(pageA);
			const before = asmDb();
			let release;
			const gate = new Promise((r) => { release = r; });
			const hold = async (route) => { await gate; await route.continue().catch(() => {}); };
			await pageA.route("**/*get_gantt_data*", hold); // the reload's data is held back
			let sent;
			try {
				await pageA.locator(".ppc-filter-actions").getByRole("button", { name: "Refresh" }).click(); // a reload starts...
				await pageA.waitForFunction(() => Number(document.querySelector(".ppc[data-ppc-busy]").getAttribute("data-ppc-busy")) > 0);
				const oldBar = await pageA.locator(L.barSel(asm)).elementHandle();
				// ...and the user starts a drag while it is in flight; the chart is re-rendered under the pointer, button still down
				sent = await L.dragExpectingRequest(pageA, asm, DX, undefined, 20000, async () => {
					release();
					await pageA.waitForFunction((el) => !el.isConnected, oldBar); // every bar element was replaced
				});
			} finally {
				release();
				await pageA.unroute("**/*get_gantt_data*", hold);
			}
			assert.equal(sent.sent, 1, "the drag still sent its request after the re-render");
			assert.ok(sent.msg.ok === true || sent.msg.stale === true, JSON.stringify(sent.msg));
			if (sent.msg.stale) await L.dismissModal(pageA);
			await L.untilIdle(pageA);
			const after = asmDb();
			assert.equal(L.secs(after.expected_start_date), L.plusHours(L.secs(before.expected_start_date), sent.msg.ok ? 6 : 0));
			assert.equal(await pageA.locator(".ppc-gantt").getAttribute("data-ppc-drag"), "idle");
			assert.deepEqual(await L.pageState(pageA), { drag: "idle", busy: 0, ajax: 0, modals: 0, backdrops: 0, freeze: false });
			return sent.msg.ok ? "saved" : "cleanly refused";
		});

		await S("two browsers drag the same card at the same moment: exactly one wins, the other is refused, nothing is lost", async () => {
			ctxB = await browser.newContext({ viewport: { width: 1600, height: 1100 } });
			pageB = await ctxB.newPage();
			run.openPages.push(pageB);
			L.watch(pageB, "B", problems, rpc);
			await L.login(pageB, L.MANAGER);
			await L.openControl(pageB, plan, "gantt");
			await pageB.waitForSelector(L.barSel(asm));
			await L.installTrace(pageB);
			// A reloads so both views hold the same token
			await L.openControl(pageA, plan, "gantt");
			await L.installTrace(pageA);
			const before = asmDb();
			const [ra, rb] = await Promise.all([L.dragExpectingRequest(pageA, asm, DX), L.dragExpectingRequest(pageB, asm, DX)]);
			const oks = [ra, rb].filter((r) => r.msg.ok === true).length;
			const stales = [ra, rb].filter((r) => r.msg.stale === true).length;
			assert.deepEqual([oks, stales], [1, 1], `one saved, one refused: ${JSON.stringify([ra.msg.ok, rb.msg.ok, ra.msg.stale, rb.msg.stale])}`);
			const loser = ra.msg.stale ? pageA : pageB;
			const visibility = { A: await pageA.evaluate(() => document.visibilityState), B: await pageB.evaluate(() => document.visibilityState) };
			await L.openControl(loser, plan, "gantt"); // the refused user reloads the screen
			await L.installTrace(loser);
			await loser.waitForSelector(L.barSel(asm));
			const after = asmDb();
			assert.equal(L.secs(after.expected_start_date), L.plusHours(L.secs(before.expected_start_date), 6), "exactly one 6 h move was applied, not two");
			assert.equal(after.ppc_schedule_revision, before.ppc_schedule_revision + 1);
			// the loser can now move it again immediately
			const again = await L.dragExpectingRequest(loser, asm, DX);
			expectMsg(again, "ok");
			await L.untilIdle(loser);
			assert.equal(L.secs(asmDb().expected_start_date), L.plusHours(L.secs(before.expected_start_date), 12));
			return { visibility };
		});

		await S("after a reload the Gantt shows exactly what the database holds", async () => {
			await L.openControl(pageA, plan, "gantt");
			const db = asmDb();
			const shown = await pageA.evaluate((n) => { const g = document.querySelector(".ppc-gantt").__ppcGantt; const t = g.taskByName(n); return { start: t.revised_start, revision: t.revision }; }, asm);
			assert.equal(L.secs(shown.start), L.secs(db.expected_start_date));
			assert.equal(shown.revision, db.ppc_schedule_revision);
		});

		await step("no unexpected browser errors during the whole run", async () => {
			assert.deepEqual(problems, [], problems.join("\n"));
		});
	} finally {
		let cleaned;
		try { cleaned = L.fx("cleanup"); } catch (e) { cleaned = { clean: false, error: String(e.message).slice(0, 200) }; }
		const ok = cleaned && cleaned.clean === true;
		run.results.push({ name: "cleanup: every PPCE2E record and throw-away user removed", status: ok ? "PASS" : "FAIL", error: ok ? undefined : JSON.stringify(cleaned) });
		console.log((ok ? "PASS  " : "FAIL  ") + "cleanup  " + JSON.stringify(cleaned));
		await browser.close();
	}

	const lat = latencies.slice().sort((a, b) => a - b);
	if (lat.length) console.log(`\nServer round trip of ${lat.length} drags: min ${lat[0]} ms, median ${lat[Math.floor(lat.length / 2)]} ms, max ${lat[lat.length - 1]} ms`);
	console.log(`Outcome of the second drag per variant: ${JSON.stringify(outcomes)}`);
	console.log(`DRAG REGRESSION: ${run.count("PASS")} passed, ${run.count("FAIL")} failed, ${run.count("SKIPPED")} skipped of ${run.results.length}`);
	L.fs.writeFileSync(L.path.join(L.SHOTS, "drag_regression_results.json"), JSON.stringify({ results: run.results, problems }, null, 2));
	process.exit(run.count("FAIL") ? 1 : 0);
})();
