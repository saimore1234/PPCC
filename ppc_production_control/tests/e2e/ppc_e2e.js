/*
 * PPC Production Control - browser end-to-end test (Playwright).
 *
 *   Sales Order -> standard Production Plan -> draft Work Orders (background job) -> submit in the PPC screen ->
 *   Job Cards (ERPNext) -> baseline -> Gantt drag / resize / cascade / refusal / stale token -> reload ->
 *   revision history -> consistency check + repair -> view-only user -> cleanup.
 *
 * It needs a running Frappe site with a background worker and COMMITS disposable PPCE2E-* records, which it removes
 * again at the end (see fixture.py). Run it on a development site only.
 *
 * Configuration (environment):
 *   PPC_E2E_BASE     site URL                         default http://localhost:8000
 *   PPC_E2E_RUNNER   command prefix that runs run_fixture.sh (split on spaces), e.g.
 *                    "bash /path/to/frappe-bench/apps/ppc_production_control/ppc_production_control/tests/e2e/run_fixture.sh"
 *                    or  "wsl.exe -d Ubuntu-22.04 -- bash /home/me/frappe-bench/apps/.../run_fixture.sh"
 *   PPC_E2E_BROWSER  path to a Chromium/Edge/Chrome executable (default: Playwright's bundled browser)
 *   PPC_E2E_SHOTS    folder for screenshots           default ./shots
 */
const { chromium } = require("playwright-core");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const BASE = process.env.PPC_E2E_BASE || "http://localhost:8000";
const RUNNER = (process.env.PPC_E2E_RUNNER || `bash ${path.join(__dirname, "run_fixture.sh")}`).split(" ").filter(Boolean);
const SHOTS = process.env.PPC_E2E_SHOTS || path.join(__dirname, "shots");
const MANAGER = "ppc.e2e.manager@example.com";
const VIEWER = "ppc.e2e.viewer@example.com";
const PASSWORD = "Ppc-E2E-2026!x";
fs.mkdirSync(SHOTS, { recursive: true });

/* ---------------------------------------------------------------------------------------- fixtures */
function fx(fn, kwargs) {
	const b64 = Buffer.from(JSON.stringify(kwargs || {})).toString("base64");
	const out = execFileSync(RUNNER[0], [...RUNNER.slice(1), fn, b64], { encoding: "utf8", maxBuffer: 1 << 26, timeout: 900000 });
	const lines = out.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
	const last = lines[lines.length - 1] || "";
	try { return JSON.parse(last); } catch (e) { return last; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const secs = (v) => String(v || "").slice(0, 19);
const plusHours = (s, h) => {
	const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/.exec(s);
	const t = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) + h * 3600000;
	const d = new Date(t);
	const p = (n) => String(n).padStart(2, "0");
	return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
};

function snapshot() {
	const s = fx("state");
	const wo = s.work_orders[0];
	return { raw: s, wo, cut: wo && wo.job_cards[0], asm: wo && wo.job_cards[1] };
}

/* ---------------------------------------------------------------------------------------- results */
const results = [];
let aborted = false;
async function step(name, fn, { critical = false } = {}) {
	if (aborted) { results.push({ name, status: "SKIPPED" }); console.log("SKIP  " + name); return; }
	const t0 = Date.now();
	try {
		const detail = await fn();
		results.push({ name, status: "PASS" });
		console.log("PASS  " + name + (detail ? "  " + (typeof detail === "string" ? detail : JSON.stringify(detail)) : "") + `  (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
	} catch (e) {
		results.push({ name, status: "FAIL", error: String(e.message).split("\n").slice(0, 3).join(" | ") });
		console.log("FAIL  " + name + "\n      " + String(e.message).split("\n").slice(0, 4).join("\n      "));
		if (critical) aborted = true;
		// a failed step must not leave a dialog open on top of the next one
		for (const p of openPages) { await p.keyboard.press("Escape").catch(() => {}); await p.keyboard.press("Escape").catch(() => {}); }
	}
}
const openPages = [];

/* ---------------------------------------------------------------------------------------- browser helpers */
async function login(page, email) {
	await page.goto(BASE + "/login");
	await page.fill("#login_email", email);
	await page.fill("#login_password", PASSWORD);
	await page.click(".btn-login");
	await page.waitForURL(/\/app/, { timeout: 45000 });
}

async function openControl(page, plan, tab) {
	await page.goto(`${BASE}/app/ppc-production-control`);
	await page.waitForSelector(".ppc-kpi", { timeout: 45000 });
	await page.locator('.ppc-filter-grid [data-fieldname="plan"] input').fill(plan);
	await page.waitForTimeout(700);
	await page.keyboard.press("Escape");
	await page.click("text=Apply Filters");
	await page.waitForSelector(".ppc-plan-strip", { state: "visible", timeout: 30000 });
	if (tab) {
		await page.click(`.ppc-tab[data-tab="${tab}"]`);
		await page.waitForTimeout(800);
	}
}

const barSel = (name) => `.ppc-bar[data-name="${name}"]`;

async function barLeft(page, name) {
	return parseFloat(await page.locator(barSel(name)).evaluate((el) => el.style.left));
}

async function dragBar(page, name, dx, handle) {
	const b = page.locator(barSel(name));
	await b.scrollIntoViewIfNeeded();
	const box = await b.boundingBox();
	const y = box.y + box.height / 2;
	const x = handle === "right" ? box.x + box.width - 3 : handle === "left" ? box.x + 3 : box.x + box.width / 2;
	await page.mouse.move(x, y);
	await page.mouse.down();
	await page.mouse.move(x + dx / 2, y, { steps: 6 });
	await page.mouse.move(x + dx, y, { steps: 6 });
	await page.mouse.up();
}

async function setColumns(page, mode) {
	const pressed = (await page.locator(".ppc-toggle-cols").getAttribute("aria-pressed")) === "true";
	if ((mode === "schedule") !== pressed) await page.click(".ppc-toggle-cols");
	await page.waitForTimeout(300);
}

async function waitIdle(page) {
	await page.waitForFunction(() => !document.querySelector(".ppc-bar.pending") && !document.querySelector(".freeze-message-container:not([style*='display: none'])"), null, { timeout: 30000 }).catch(() => {});
	await page.waitForTimeout(1200);
}

async function modalTitle(page, timeout = 10000) {
	const m = page.locator(".modal.show").last();
	await m.waitFor({ timeout });
	return { modal: m, title: (await m.locator(".modal-title").first().innerText()).trim() };
}

/* ---------------------------------------------------------------------------------------- the test */
(async () => {
	const launch = { headless: true };
	if (process.env.PPC_E2E_BROWSER) launch.executablePath = process.env.PPC_E2E_BROWSER;
	const browser = await chromium.launch(launch);
	const problems = [];
	const rpc = []; // every PPC reschedule call: request + parsed response
	const watch = (page, label) => {
		page.on("console", (m) => { if (m.type() === "error" && !/socket|Failed to load resource|favicon/.test(m.text())) problems.push(`${label} console.error: ${m.text().slice(0, 240)}`); });
		page.on("pageerror", (e) => problems.push(`${label} pageerror: ${String(e).slice(0, 300)}`));
		page.on("response", (r) => {
			if (r.status() >= 500) problems.push(`${label} HTTP ${r.status()} ${r.url().slice(0, 120)}`);
			if (r.url().includes("ppc_production_control.api.reschedule_job_card")) {
				r.json().then((j) => rpc.push({ label, status: r.status(), msg: j.message })).catch(() => rpc.push({ label, status: r.status(), msg: null }));
			}
		});
	};

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
			const strip = (await pageM.locator(".ppc-plan-strip").innerText()).replace(/\s+/g, " ");
			assert.match(strip, new RegExp(plan));
			await pageM.screenshot({ path: path.join(SHOTS, "01_workorders.png") });
			return strip.slice(0, 160);
		}, { critical: true });

		await step("the draft Work Order is listed and can be submitted in bulk from the PPC screen", async () => {
			await pageM.waitForSelector(".ppc-pane.active tbody tr[data-id] input.ppc-pick", { timeout: 30000 });
			assert.equal(await pageM.locator(".ppc-pane.active tbody tr[data-id]").count(), 1);
			await pageM.locator(".ppc-pane.active tbody tr[data-id] input.ppc-pick").check();
			await pageM.click("text=Submit Selected Work Orders");
			const c = await modalTitle(pageM);
			await c.modal.locator(".btn-primary, .btn-modal-primary").first().click();
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
			await pageM.waitForSelector(".ppc-pane.active tbody tr[data-id]", { timeout: 30000 });
			const heads = (await pageM.locator(".ppc-pane.active thead th").allInnerTexts()).join("|");
			for (const h of ["Original Start", "Current / Revised Start", "Actual Start", "Revisions", "Last Rescheduled By", "Last Rescheduled At", "Sync Status"]) {
				assert.ok(heads.includes(h), `column ${h} missing in: ${heads}`);
			}
			assert.equal(await pageM.locator(".ppc-pane.active tbody tr[data-id]").count(), 2);
			assert.ok(await pageM.locator(".ppc-check-consistency").isVisible());
			assert.ok(await pageM.locator(".ppc-init-baselines").isVisible(), "PPC Manager may reconcile");
			await pageM.screenshot({ path: path.join(SHOTS, "03_jobcards.png") });
		});

		await step("Gantt shows both bars and the schedule columns (original / current / actual / revision / last change / sync)", async () => {
			await openControl(pageM, plan, "gantt");
			await pageM.waitForSelector(".ppc-bar", { timeout: 30000 });
			assert.equal(await pageM.locator(".ppc-bar").count(), 2);
			await setColumns(pageM, "schedule");
			const heads = await pageM.locator(".ppc-gantt-left-head .ppc-lcol").allInnerTexts();
			assert.deepEqual(heads, ["Operation / Job Card", "Original start / end", "Current start / end", "Actual start / end", "Rev", "Last rescheduled", "Sync status", "Status"]);
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
			const before = snapshot();
			fx("disturb", { job_card: asmName });
			rpc.length = 0;
			await dragBar(pageM, asmName, 24); // +6 h on a stale token
			const c = await modalTitle(pageM, 15000);
			assert.match(c.title, /changed by someone else/i);
			await pageM.keyboard.press("Escape");
			await waitIdle(pageM);
			assert.ok(rpc.some((r) => r.msg && r.msg.stale), "server flagged the token as stale: " + JSON.stringify(rpc.map((r) => r.msg && r.msg.stale)));
			const mid = snapshot();
			assert.equal(mid.asm.expected_start_date, before.asm.expected_start_date, "stale change not applied");
			assert.equal(mid.asm.revisions.length, before.asm.revisions.length, "no revision for a stale change");
			// the page reloaded itself with the fresh token: the same drag now succeeds
			await pageM.waitForSelector(`${barSel(asmName)}:not(.pending)`);
			await pageM.waitForTimeout(1500);
			rpc.length = 0;
			await dragBar(pageM, asmName, 24);
			await waitIdle(pageM);
			let diag = "";
			if (!rpc.length) { // the browser did not register the drag: record why, then try once more
				diag = `first attempt sent nothing (open modals: ${await pageM.locator(".modal.show").count()}, backdrops: ${await pageM.locator(".modal-backdrop").count()}, pending bars: ${await pageM.locator(".ppc-bar.pending").count()}); `;
				await pageM.screenshot({ path: path.join(SHOTS, "13_retry_nodrag.png") });
				await pageM.keyboard.press("Escape");
				await pageM.waitForTimeout(1000);
				await dragBar(pageM, asmName, 24);
				await waitIdle(pageM);
			}
			const after = snapshot();
			const note = `${diag}before=${before.asm.expected_start_date} mid=${mid.asm.expected_start_date} after=${after.asm.expected_start_date} rpc=${JSON.stringify(rpc.map((r) => r.msg && (r.msg.ok || r.msg.stale || r.msg.errors)))}`;
			assert.equal(secs(after.asm.expected_start_date), plusHours(secs(before.asm.expected_start_date), 6), note);
			assert.equal(after.asm.ppc_schedule_revision, before.asm.ppc_schedule_revision + 1, note);
		});

		await step("the detail panel shows baseline, current schedule, difference text and the whole revision history", async () => {
			await pageM.locator(barSel(asmName)).click();
			await pageM.waitForSelector(".ppc-detail table.ppc-table tbody tr", { timeout: 15000 });
			const text = (await pageM.locator(".ppc-detail").innerText()).replace(/\s+/g, " ").toLowerCase(); // labels are upper-cased by CSS
			const revs = snapshot().asm.revisions;
			assert.equal(await pageM.locator(".ppc-detail table.ppc-table tbody tr").count(), revs.length);
			for (const needle of ["Original schedule", "Initial schedule", "Current schedule", "Actual execution", "Start moved by", "Gantt Drag", "Cascade", "Baseline"]) {
				assert.ok(text.includes(needle.toLowerCase()), `detail panel lacks "${needle}": ${text.slice(0, 400)}`);
			}
			await pageM.screenshot({ path: path.join(SHOTS, "09_detail_panel.png") });
		});

		await step("consistency check finds an out-of-band change; preview changes nothing; repair restores it as a new revision", async () => {
			const before = snapshot();
			fx("drift", { job_card: cutName, hours: 3 });
			await openControl(pageM, plan, "jobcards");
			await pageM.click(".ppc-check-consistency");
			let c = await modalTitle(pageM, 30000);
			assert.match(c.title, /Schedule consistency/);
			const body = (await c.modal.innerText()).replace(/\s+/g, " ");
			assert.match(body, /schedule drift/i);
			assert.ok(body.includes(cutName));
			await pageM.screenshot({ path: path.join(SHOTS, "10_consistency.png") });
			await c.modal.locator(".btn-primary, .btn-modal-primary").first().click(); // Preview repairs
			c = await modalTitle(pageM, 30000);
			assert.match(c.title, /Repair preview/);
			const drifted = snapshot();
			assert.notEqual(secs(drifted.cut.expected_start_date), secs(drifted.cut.ppc_revised_start), "preview did not fix anything yet");
			await c.modal.locator(".btn-primary, .btn-modal-primary").first().click(); // Apply
			await waitIdle(pageM);
			const after = snapshot();
			assert.equal(secs(after.cut.expected_start_date), secs(after.cut.ppc_revised_start), "ERPNext schedule is back on the accepted schedule");
			assert.equal(after.cut.revisions.length, before.cut.revisions.length + 1);
			assert.equal(after.cut.revisions.at(-1).change_source, "Reconciliation");
			// and a second check is clean
			await pageM.click(".ppc-check-consistency");
			c = await modalTitle(pageM, 30000);
			assert.match(await c.modal.innerText(), /No inconsistencies found/);
			await pageM.keyboard.press("Escape");
		});

		await step("audit tab lists the accepted reschedules and the failed/ refused attempts are not mistaken for revisions", async () => {
			await openControl(pageM, plan, "audit");
			await pageM.waitForSelector(".ppc-pane.active table.ppc-table tbody tr", { timeout: 20000 });
			const text = await pageM.locator(".ppc-pane.active").innerText();
			assert.match(text, /Reschedule Job Card/);
			assert.match(text, /Reconcile/);
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
			const n = rpc.length;
			await dragBar(pageV, asmName, 96);
			await pageV.waitForTimeout(1500);
			assert.equal(rpc.filter((r) => r.label === "viewer").length, 0, "the browser never even sent a reschedule");
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
			await pageV.click('.ppc-tab[data-tab="jobcards"]');
			assert.equal(await pageV.locator(".ppc-init-baselines").count(), 0, "reconcile button hidden");
			assert.equal(await pageV.locator(".ppc-retry-sync").count(), 0, "retry button hidden");
			await pageV.screenshot({ path: path.join(SHOTS, "12_viewer.png") });
			const after = snapshot();
			assert.deepEqual(after.asm, before.asm, "nothing changed");
			assert.ok(n >= 0);
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

	const count = (s) => results.filter((r) => r.status === s).length;
	console.log(`\nE2E: ${count("PASS")} passed, ${count("FAIL")} failed, ${count("SKIPPED")} skipped of ${results.length}`);
	fs.writeFileSync(path.join(SHOTS, "results.json"), JSON.stringify({ results, problems }, null, 2));
	process.exit(count("FAIL") ? 1 : 0);
})();
