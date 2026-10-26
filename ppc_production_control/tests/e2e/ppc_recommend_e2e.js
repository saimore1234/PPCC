/*
 * Best-machine recommendation on a Sales Order - browser end-to-end test (Playwright).
 *
 * A planner opens a draft Sales Order, presses "Find Best Machine" on the line, reads the ranking, picks another compatible machine,
 * saves, changes the quantity (the choice goes Stale), submits, builds a Production Plan and checks that the preference reached the
 * Work Order and its Job Cards - and what happens when the machine is in Maintenance by then.
 *
 * COMMITS disposable PPCE2E-* records and removes them again (fixture.py): run it on a development or staging site only.
 */
const L = require("./e2e_lib");
const { assert, path, SHOTS, BASE, PLANNER, fx, login } = L;

const TITLE = /Best Machine Recommendation/;

async function dialog(page, timeout = 60000) {
	await page.waitForFunction((src) => [...document.querySelectorAll(".modal.show .modal-title")].some((t) => new RegExp(src).test(t.innerText)), TITLE.source, { timeout });
	await page.waitForFunction(L.modalFullyShown);
	return page.locator(".modal.show", { has: page.locator(".modal-title", { hasText: TITLE }) }).last();
}

async function openFinder(page, so) {
	await page.goto(`${BASE}/app/sales-order/${so}`);
	await page.waitForSelector(".form-grid .grid-row[data-idx='1']", { timeout: 60000 });
	await L.untilIdle(page);
	await page.locator(".grid-row[data-idx='1'] .btn-open-row").first().click();
	const btn = page.locator(".grid-row-open [data-fieldname='ppc_find_best_machine'] button, .grid-row-open button[data-fieldname='ppc_find_best_machine']").first();
	await btn.waitFor({ state: "visible", timeout: 30000 });
	await btn.click();
	return dialog(page);
}

async function save(page) {
	await page.keyboard.press("Control+s");
	await page.waitForFunction(() => window.jQuery && window.jQuery.active === 0 && cur_frm && !cur_frm.is_dirty(), null, { timeout: 60000 });
}

(async () => {
	const run = L.makeRunner();
	const { step, results, openPages } = run;
	const browser = await L.launch();
	const problems = [];
	let page, setup, picked, recommended;
	try {
		await step("fixtures: lathes, a press, an item whose BOM turns on the lathe type, a draft Sales Order, a planner", async () => {
			fx("cleanup");
			fx("users", { action: "create" });
			setup = fx("rec_setup");
			assert.ok(setup.sales_order && setup.workstations.R1);
			const ctx = await browser.newContext({ viewport: { width: 1500, height: 1100 } });
			page = await ctx.newPage();
			openPages.push(page);
			L.watch(page, "planner", problems);
			await login(page, PLANNER);
		}, { critical: true });

		await step("Find Best Machine lists the compatible machines, ranked, with the explanation and the planning-only notice", async () => {
			const d = await openFinder(page, setup.sales_order);
			const text = (await d.innerText()).replace(/\s+/g, " ");
			for (const w of ["R1", "R2", "R3"]) assert.ok(text.includes(`PPCE2E ${w}`), `${w} is offered`);
			assert.ok(!text.includes("PPCE2E RP"), "the press is not compatible and is not offered");
			assert.match(text, /Recommended/);
			assert.match(text, /Compatible/);
			assert.match(text, /not a reservation/);
			assert.match(text, /estimated completion/i);
			assert.equal(await d.locator("input[type=radio]").count(), 3);
			assert.equal(await d.locator("input[type=radio]:checked").count(), 1, "the recommended machine is pre-selected");
			recommended = await d.locator("input[type=radio]:checked").getAttribute("data-ws");
			await d.locator("details summary").first().click();
			assert.match((await d.innerText()), /Compatible:/);
			await page.screenshot({ path: path.join(SHOTS, "r01_dialog.png") });
		});

		await step("the planner picks another compatible machine; the dialog recalculates with that choice", async () => {
			const d = await dialog(page);
			const other = await d.locator(`input[type=radio]:not([data-ws="${recommended}"])`).first().getAttribute("data-ws");
			picked = other;
			await d.locator(`input[data-ws="${other}"]`).check();
			const d2 = await dialog(page);
			assert.equal(await d2.locator("input[type=radio]:checked").getAttribute("data-ws"), other);
			await page.screenshot({ path: path.join(SHOTS, "r02_picked.png") });
			await d2.getByRole("button", { name: "Use Selection" }).click();
			await page.waitForFunction(() => !document.querySelector(".modal.show"));
		});

		await step("saving keeps it on the correct line, estimated by the server, status Selected", async () => {
			await save(page);
			const st = fx("rec_state", { sales_order: setup.sales_order });
			const row = st.rows[0];
			assert.equal(row.ppc_preferred_workstation, picked);
			assert.equal(row.ppc_machine_preference, "Manual");
			assert.equal(row.ppc_recommendation_status, "Selected");
			assert.ok(row.ppc_est_start && row.ppc_est_completion && row.ppc_recommendation_updated_at);
			assert.ok(row.ppc_est_completion > row.ppc_est_start, "completion after start");
		});

		await step("changing the quantity marks the recommendation Stale and keeps the choice", async () => {
			await page.goto(`${BASE}/app/sales-order/${setup.sales_order}`);
			await page.waitForSelector(".form-grid .grid-row[data-idx='1']");
			await L.untilIdle(page);
			await page.locator(".grid-row[data-idx='1'] .btn-open-row").first().click();
			const qty = page.locator(".grid-row-open [data-fieldname='qty'] input").first();
			await qty.fill("25");
			await qty.blur();
			await save(page);
			const row = fx("rec_state", { sales_order: setup.sales_order }).rows[0];
			assert.equal(row.ppc_recommendation_status, "Stale");
			assert.equal(row.ppc_preferred_workstation, picked);
			await page.reload();
			await page.waitForSelector(".grid-row[data-idx='1']"); await L.untilIdle(page);
			await page.screenshot({ path: path.join(SHOTS, "r03_stale.png") });
		});

		await step("recalculating after the change makes it current again", async () => {
			const d = await openFinder(page, setup.sales_order);
			assert.equal(await d.locator("input[type=radio]:checked").getAttribute("data-ws"), picked, "the earlier manual choice is kept as the starting point");
			await d.getByRole("button", { name: "Use Selection" }).click();
			await page.waitForFunction(() => !document.querySelector(".modal.show"));
			await save(page);
			const row = fx("rec_state", { sales_order: setup.sales_order }).rows[0];
			assert.equal(row.ppc_recommendation_status, "Selected");
		});

		await step("the preference reaches the Production Plan, the Work Order and its Job Cards", async () => {
			fx("rec_submit", { sales_order: setup.sales_order });
			const out = fx("rec_plan", { sales_order: setup.sales_order });
			const so = fx("rec_state", { sales_order: setup.sales_order }).rows[0];
			assert.equal(out.plan_items[0].sales_order_item, so.name, "the source line is referenced");
			assert.equal(out.plan_items[0].ppc_preferred_workstation, picked);
			assert.equal(out.work_orders.length, 1);
			const wo = out.work_orders[0];
			assert.equal(wo.operations[0].workstation, picked);
			assert.match(wo.note || "", /applied/);
			assert.ok(wo.job_cards.length >= 1 && wo.job_cards.every((j) => j.workstation === picked), "the Job Cards are on the preferred machine: " + JSON.stringify(wo.job_cards));
		});

		await step("a machine in Maintenance is listed as not offered, with the reason, and cannot be chosen", async () => {
			const s2 = fx("rec_setup");
			const so = s2.sales_order;
			fx("rec_submit", { sales_order: so });
			// set the preference and send it to a plan after the machine has gone down: done through the same server path as the form
			fx("rec_workstation_status", { workstation: s2.workstations.R3, status: "Maintenance" });
			const d = await (async () => {
				await page.goto(`${BASE}/app/sales-order/${so}`);
				await page.waitForSelector(".form-grid .grid-row[data-idx='1']");
				await L.untilIdle(page);
				await page.locator(".grid-row[data-idx='1'] .btn-open-row").first().click();
				await page.locator(".grid-row-open [data-fieldname='ppc_find_best_machine'] button").first().click();
				return dialog(page);
			})();
			const text = (await d.innerText()).replace(/\s+/g, " ");
			assert.match(text, /PPCE2E R3 \(Workstation is Maintenance\)/, "the machine in maintenance is listed as not offered, with the reason");
			assert.equal(await d.locator("input[type=radio]").count(), 2);
			await page.keyboard.press("Escape");
			await page.waitForFunction(() => !document.querySelector(".modal.show"));
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
	console.log(`\nRECOMMEND E2E: ${run.count("PASS")} passed, ${run.count("FAIL")} failed, ${run.count("SKIPPED")} skipped of ${results.length}`);
	L.fs.writeFileSync(path.join(SHOTS, "results_recommend.json"), JSON.stringify({ results, problems }, null, 2));
	process.exit(run.count("FAIL") ? 1 : 0);
})();
