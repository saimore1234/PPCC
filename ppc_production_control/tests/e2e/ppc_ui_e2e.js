/*
 * PPC Control Center - browser test of the redesigned UI (Playwright).
 *
 * Covers what ppc_e2e.js (the schedule/sync flow) and ppc_drag_regression.js (the drag interaction) do not: the workspace,
 * navigation, dashboard KPIs and their drill-downs, filters, search, sorting, server-side pagination, bulk selection and
 * bulk submit, the Gantt controls and drawer, workload, history and exceptions pages, the slim Production Plan form,
 * the legacy route, permissions (viewer, user with no PPC role), light/dark contrast, responsive widths and keyboard use.
 *
 * Like the other browser tests it COMMITS disposable PPCE2E-* records and removes them at the end (see fixture.py), so run
 * it on a development or staging site only. Waiting is always on observable state or on the response of the request that
 * the action causes - never on a fixed delay.  Needs a site with more than 25 PPC Job Cards for the pagination step.
 */
const L = require("./e2e_lib");
const { assert, path, SHOTS, BASE, MANAGER, VIEWER, OUTSIDER, fx, sleep, login, openControl } = L;

const P = ".ppc-page:visible";
const API = "ppc_production_control.api.";
const parseNum = (t) => Number(String(t).replace(/[^\d.-]/g, ""));
const pagerTotal = (text) => { const m = /of\s+([\d,]+)/.exec(text || ""); return m ? Number(m[1].replace(/,/g, "")) : null; };

/** Resolves with the parsed JSON of the next response of an API method (start waiting BEFORE the action that causes it). */
const nextResponse = (page, method, timeout = 30000) =>
	page.waitForResponse((r) => r.url().includes(API + method), { timeout }).then(async (r) => ({ body: (await r.json()).message, post: decodeURIComponent(r.request().postData() || ""), status: r.status() }));

/** Make a Link / Data filter field take a value the way a user does: type, leave the field, wait for the list to answer. */
async function setFilter(page, fieldname, value, method) {
	const input = page.locator(`${P} [data-fieldname="${fieldname}"] input`).first();
	const answer = method ? nextResponse(page, method) : null;
	await input.fill(value);
	await input.press("Tab");
	return answer ? answer : null;
}

async function clickAndExpect(page, locator, method) {
	const answer = nextResponse(page, method);
	await locator.click();
	return answer;
}

/** A filter box shows its value once Frappe has finished filling it in (a link opened with ?plan= sets it asynchronously). */
const filterIs = (page, field, value) => page.waitForFunction(([f, v]) => { const i = [...document.querySelectorAll(`.ppc-page [data-fieldname="${f}"] input`)].find((e) => e.offsetParent !== null); return !!i && i.value === v; }, [field, value], { timeout: 20000 });

/** Rows of the visible table (excluding the empty-state row). */
const rowCount = (page) => page.locator(`${P} tbody tr[data-id]`).count();

/** Wait until the visible page's loads are finished. */
const idle = (page) => L.untilIdle(page);

/** WCAG relative luminance contrast of an element's text against its effective background. */
function contrastOf(page, selector) {
	return page.evaluate((sel) => {
		const el = [...document.querySelectorAll(sel)].find((e) => e.offsetParent !== null);
		if (!el) return null;
		const parse = (c) => { const m = /rgba?\(([^)]+)\)/.exec(c); if (!m) return null; const p = m[1].split(",").map((x) => parseFloat(x)); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
		const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); };
		let bg = { r: 255, g: 255, b: 255 };
		const layers = [];
		for (let n = el; n; n = n.parentElement) { const c = parse(getComputedStyle(n).backgroundColor); if (c && c.a > 0) { layers.push(c); if (c.a >= 1) break; } }
		for (const c of layers.reverse()) bg = { r: c.r * c.a + bg.r * (1 - c.a), g: c.g * c.a + bg.g * (1 - c.a), b: c.b * c.a + bg.b * (1 - c.a) };
		const fg = parse(getComputedStyle(el).color);
		const l1 = lum(fg), l2 = lum(bg);
		return Math.round(((Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)) * 100) / 100;
	}, selector);
}

(async () => {
	const run = L.makeRunner();
	const { step, results, openPages } = run;
	const browser = await L.launch();
	const problems = [];
	let plan, so, snap, jcNames = [];
	let pageM, pageV;
	try {
		await step("fixtures: clean slate, throw-away users (manager, viewer, no-role), plan, draft Work Order submitted -> 2 Job Cards", async () => {
			fx("cleanup");
			fx("users", { action: "create" });
			const made = fx("setup");
			assert.ok(made.plan, JSON.stringify(made));
			plan = made.plan; so = made.sales_order;
			let wos = [];
			for (let i = 0; i < 45 && !wos.length; i++) { await sleep(4000); wos = fx("state").work_orders; }
			assert.equal(wos.length, 1, "the background job made the draft Work Order");
			fx("submit_work_orders");
			snap = L.snapshot();
			assert.equal(snap.wo.job_cards.length, 2);
			jcNames = snap.wo.job_cards.map((c) => c.name);
			return { plan, so, jcNames };
		}, { critical: true });

		const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
		pageM = await ctx.newPage();
		openPages.push(pageM);
		L.watch(pageM, "manager", problems);
		await step("PPC Manager logs in", async () => { await login(pageM, MANAGER); }, { critical: true });

		/* ------------------------------------------------------------------------------------ workspace and navigation */
		await step("the PPC Control Center workspace links to every page", async () => {
			await pageM.goto(`${BASE}/app/ppc-control-center`);
			await pageM.waitForSelector(".shortcut-widget-box", { timeout: 45000 });
			await pageM.waitForFunction(() => document.querySelectorAll(".shortcut-widget-box").length >= 10, null, { timeout: 30000 });
			const text = (await pageM.locator(".shortcut-widget-box").allInnerTexts()).join(" | ").replace(/\s+/g, " ");
			for (const label of ["Dashboard", "Production Plans", "Work Orders", "Job Cards", "Production Gantt", "Workload & Capacity", "Schedule History", "Exceptions & Failed Synchronizations", "PPC Settings", "PPC Audit Log"]) {
				assert.ok(text.includes(label), `workspace lacks "${label}"`);
			}
			await pageM.screenshot({ path: path.join(SHOTS, "ui01_workspace.png") });
			await pageM.locator(".shortcut-widget-box", { hasText: "Production Gantt" }).first().click();
			await pageM.waitForURL(/\/app\/ppc-gantt/);
			await pageM.waitForSelector(".ppc-page:visible .ppc-gantt", { timeout: 30000 });
		});

		await step("the navigation strip opens every page, marks the current one, and PPC Settings opens the form", async () => {
			await openControl(pageM, null, "dashboard");
			const pages = [["Production Plans", "ppc-production-plans", "plans"], ["Work Orders", "ppc-work-orders", "workorders"], ["Job Cards", "ppc-job-cards", "jobcards"],
				["Production Gantt", "ppc-gantt", "gantt"], ["Workload & Capacity", "ppc-workload", "workload"], ["Schedule History", "ppc-schedule-history", "history"],
				["Exceptions", "ppc-exceptions", "exceptions"], ["Dashboard", "ppc-dashboard", "dashboard"]];
			for (const [label, route, key] of pages) {
				await pageM.locator(`${P} .ppc-nav a`, { hasText: new RegExp("^" + label.replace(/[&]/g, "\\&") + "$") }).click();
				await pageM.waitForURL(new RegExp("/app/" + route + "$"));
				await pageM.waitForSelector(`.ppc-page[data-ppc-page="${key}"]:visible`); // Frappe switches pages: wait for the target to be the one showing
				const current = (await pageM.locator(`.ppc-page[data-ppc-page="${key}"]:visible .ppc-nav a[aria-current="page"]`).innerText()).trim();
				assert.equal(current, label, "the strip marks the page that is showing");
				assert.equal(await pageM.locator(`${P} .ppc-nav a`).count(), 9);
				await idle(pageM);
			}
			await pageM.locator(`${P} .ppc-nav a`, { hasText: "PPC Settings" }).click();
			await pageM.waitForURL(/\/app\/ppc-settings/);
		});

		/* ------------------------------------------------------------------------------------ dashboard */
		await step("dashboard: ten KPIs; each opens the matching list with the filter applied, and the number equals the list total", async () => {
			const checks = {
				total_plans: ["get_production_plans", "ppc-production-plans"], draft_work_orders: ["get_work_orders", "ppc-work-orders"], open_work_orders: ["get_work_orders", "ppc-work-orders"],
				total_job_cards: ["get_job_cards", "ppc-job-cards"], work_in_progress: ["get_job_cards", "ppc-job-cards"], completed_job_cards: ["get_job_cards", "ppc-job-cards"],
				overdue_operations: ["get_job_cards", "ppc-job-cards"], scheduling_conflicts: ["get_job_cards", "ppc-job-cards"], failed_syncs: ["get_job_cards", "ppc-job-cards"],
				pending_qty: [null, "ppc-production-plans"],
			};
			await openControl(pageM, null, "dashboard");
			await pageM.waitForSelector(`${P} .ppc-kpi2`);
			assert.equal(await pageM.locator(`${P} .ppc-kpi2`).count(), 10);
			await pageM.screenshot({ path: path.join(SHOTS, "ui02_dashboard.png") });
			const seen = {};
			for (const [key, [method, route]] of Object.entries(checks)) {
				await openControl(pageM, null, "dashboard");
				await pageM.waitForSelector(`${P} .ppc-kpi2[data-kpi="${key}"]`);
				const tile = parseNum(await pageM.locator(`${P} .ppc-kpi2[data-kpi="${key}"] .v`).innerText());
				const answer = method ? nextResponse(pageM, method) : null;
				await pageM.locator(`${P} .ppc-kpi2[data-kpi="${key}"]`).click();
				await pageM.waitForURL(new RegExp("/app/" + route + "$"));
				if (answer) {
					const r = await answer;
					assert.equal(r.body.total, tile, `${key}: tile ${tile} vs list total ${r.body.total} (${r.post})`);
					await idle(pageM);
					const shown = pagerTotal(await pageM.locator(`${P} .ppc-pager`).innerText());
					assert.equal(shown, tile, `${key}: the pager shows the same number`);
					seen[key] = tile;
				}
			}
			return seen;
		});

		await step("dashboard filters: period fills the dates, planner narrows the numbers, Refresh makes one request, Create Production Plan opens a new form", async () => {
			await openControl(pageM, null, "dashboard");
			const dates = await pageM.evaluate(() => window.PPCUiLib.periodRange("next7", frappe.datetime.get_today()));
			let answer = nextResponse(pageM, "get_dashboard_overview");
			await pageM.locator(`${P} [data-fieldname="period"] select`).selectOption("Next 7 days");
			let r = await answer;
			assert.ok(r.post.includes(dates.date_from) && r.post.includes(dates.date_to), "the request carries the period's dates: " + r.post);
			assert.equal(await pageM.locator(`${P} [data-fieldname="date_from"] input`).inputValue().then((v) => v.length > 0), true);
			await pageM.locator(`${P} .ppc-fclear`).click();
			await idle(pageM);
			answer = setFilter(pageM, "planner", MANAGER, "get_dashboard_overview");
			r = await answer;
			assert.equal(r.body.kpis.total_plans, 0, "the manager created no plan");
			assert.equal(parseNum(await pageM.locator(`${P} .ppc-kpi2[data-kpi="total_plans"] .v`).innerText()), 0);
			await pageM.locator(`${P} .ppc-fclear`).click();
			await idle(pageM);
			const refresh = nextResponse(pageM, "get_dashboard_overview");
			await pageM.locator(".page-head:visible").getByRole("button", { name: "Refresh" }).click();
			await refresh;
			await pageM.locator(`${P} .ppc-pagebar-meta`).getByText(/Updated/).waitFor();
			await pageM.locator(".page-head:visible").getByRole("button", { name: "Create Production Plan" }).click();
			await pageM.waitForURL(/\/app\/production-plan\/new/);
		});

		/* ------------------------------------------------------------------------------------ plans */
		await step("Production Plans: columns, search by plan and Sales Order (one debounced request), sort, status filter, links to Work Orders / Job Cards / the plan", async () => {
			await openControl(pageM, null, "plans");
			const heads = (await pageM.locator(`${P} thead th`).allInnerTexts()).join("|");
			for (const h of ["Production Plan", "Company", "Posting Date", "Status", "Planner", "Sales Orders", "Finished Goods", "Planned Qty", "Work Orders", "Job Cards", "Planning Status", "Last Updated"]) {
				assert.ok(heads.includes(h), `column ${h} missing in ${heads}`);
			}
			// typing a plan id sends exactly one request (debounced), and finds exactly that plan
			const sent = [];
			const onReq = (q) => { if (q.url().includes(API + "get_production_plans")) sent.push(q.postData()); };
			pageM.on("request", onReq);
			const answer = nextResponse(pageM, "get_production_plans");
			await pageM.locator(`${P} [data-fieldname="search"] input`).pressSequentially(plan, { delay: 25 });
			const r = await answer;
			await idle(pageM);
			pageM.off("request", onReq);
			assert.equal(sent.length, 1, "debounced: one request for the whole word, got " + sent.length);
			assert.equal(r.body.total, 1);
			assert.equal(await rowCount(pageM), 1);
			// by Sales Order
			await pageM.locator(`${P} [data-fieldname="search"] input`).fill("");
			await idle(pageM);
			const a2 = nextResponse(pageM, "get_production_plans");
			await pageM.locator(`${P} [data-fieldname="search"] input`).fill(so);
			assert.ok((await a2).body.rows.some((x) => x.name === plan), "found by Sales Order");
			await idle(pageM);
			// links from the row
			await pageM.locator(`${P} .ppc-golink[data-page="workorders"]`).first().click();
			await pageM.waitForURL(/ppc-work-orders$/);
			await pageM.waitForSelector(`${P} [data-fieldname="plan"] input`);
			await filterIs(pageM, "plan", plan);
			await openControl(pageM, null, "plans");
			await setFilter(pageM, "search", plan, "get_production_plans");
			await idle(pageM);
			await pageM.locator(`${P} .ppc-golink[data-page="jobcards"]`).first().click();
			await pageM.waitForURL(/ppc-job-cards$/);
			await filterIs(pageM, "plan", plan);
			await idle(pageM);
			assert.equal(await rowCount(pageM), 2);
			// sort + status filter
			await openControl(pageM, null, "plans");
			const s1 = await clickAndExpect(pageM, pageM.locator(`${P} thead th`, { hasText: "Posting Date" }), "get_production_plans");
			assert.match(s1.post, /sort_order=asc/);
			const s2 = await clickAndExpect(pageM, pageM.locator(`${P} thead th`, { hasText: "Posting Date" }), "get_production_plans");
			assert.match(s2.post, /sort_order=desc/);
			await idle(pageM);
			const st = nextResponse(pageM, "get_production_plans");
			await pageM.locator(`${P} [data-fieldname="plan_status"] select`).selectOption("In Process");
			const sr = await st;
			assert.ok(sr.body.rows.every((x) => x.status === "In Process"), "only In Process plans");
			await idle(pageM);
			await pageM.locator(`${P} tbody tr[data-id] a`, { hasText: /^MFG-PP-/ }).first().click();
			await pageM.waitForURL(/\/app\/production-plan\/MFG-PP-/);
		});

		/* ------------------------------------------------------------------------------------ work orders */
		await step("Work Orders: status filter, clear, more filters; the pre-check reports a submitted Work Order as blocked and changes nothing", async () => {
			await openControl(pageM, plan, "workorders");
			assert.equal(await rowCount(pageM), 1);
			let a = nextResponse(pageM, "get_work_orders");
			await pageM.locator(`${P} [data-fieldname="wo_status"] select`).selectOption("Draft");
			assert.equal((await a).body.total, 0, "no draft Work Order is left for the plan");
			await idle(pageM);
			assert.match(await pageM.locator(`${P} .ppc-empty`).first().innerText(), /No Work Orders match/);
			a = nextResponse(pageM, "get_work_orders");
			await pageM.locator(`${P} [data-fieldname="wo_status"] select`).selectOption("Open");
			assert.equal((await a).body.total, 1);
			await idle(pageM);
			await pageM.locator(`${P} .ppc-fmore-toggle`).click();
			assert.ok(await pageM.locator(`${P} [data-fieldname="warehouse"] input`).isVisible(), "more filters are shown");
			await pageM.locator(`${P} .ppc-fclear`).click();
			await idle(pageM);
			assert.equal(await pageM.locator(`${P} [data-fieldname="plan"] input`).inputValue(), "", "clear removes the plan filter");
			const woName = snap.wo.name;
			const pre = await pageM.evaluate((n) => frappe.call({ method: "ppc_production_control.api.precheck_work_orders", args: { work_orders: [n] } }).then((r) => r.message), woName);
			assert.equal(pre.results.length, 1);
			assert.equal(pre.results[0].ok, false, "a submitted Work Order cannot be submitted again");
			assert.ok((pre.results[0].blockers || []).length > 0);
			assert.equal(L.snapshot().wo.docstatus, 1, "the pre-check changed nothing");
			return pre.results[0].blockers;
		});

		/* ------------------------------------------------------------------------------------ job cards */
		await step("Job Cards: server-side pagination, page size, sorting; nothing but one page is loaded at a time", async () => {
			await openControl(pageM, null, "jobcards");
			const total = pagerTotal(await pageM.locator(`${P} .ppc-pager`).innerText());
			assert.ok(total > 25, `needs a site with more than 25 PPC Job Cards for this step, found ${total}`);
			const size = nextResponse(pageM, "get_job_cards");
			await pageM.locator(`${P} .ppc-pager select`).selectOption("25");
			const sr = await size;
			assert.match(sr.post, /page_length=25/);
			assert.match(sr.post, /page=1/);
			assert.equal(sr.body.rows.length, 25, "the server returned one page, not everything");
			await idle(pageM);
			assert.equal(await rowCount(pageM), 25, "only one page of rows is in the page");
			const first = await pageM.locator(`${P} tbody tr[data-id]`).first().getAttribute("data-id");
			const next = await clickAndExpect(pageM, pageM.locator(`${P} .ppc-pager button`, { hasText: "Next" }), "get_job_cards");
			assert.match(next.post, /page=2/);
			await idle(pageM);
			assert.match(await pageM.locator(`${P} .ppc-pager`).innerText(), /26–\d+ of/);
			assert.notEqual(await pageM.locator(`${P} tbody tr[data-id]`).first().getAttribute("data-id"), first, "page 2 shows other rows");
			const bigger = nextResponse(pageM, "get_job_cards");
			await pageM.locator(`${P} .ppc-pager select`).selectOption("100");
			const br = await bigger;
			assert.match(br.post, /page_length=100/);
			assert.match(br.post, /page=1/, "changing the page size goes back to page 1");
			await idle(pageM);
			const sort = await clickAndExpect(pageM, pageM.locator(`${P} thead th`, { hasText: "Job Card" }).first(), "get_job_cards");
			assert.match(sort.post, /sort_by=name/);
			await idle(pageM);
			await pageM.screenshot({ path: path.join(SHOTS, "ui03_jobcards.png") });
		});

		await step("Job Cards: select all matching the filters, row selection, clear; viewer-style protections are server-side too", async () => {
			await openControl(pageM, plan, "jobcards");
			assert.equal(await rowCount(pageM), 2);
			await pageM.locator(`${P} .ppc-bar-sel button, ${P} .ppc-actionbar ~ * button`, { hasText: /Select all draft Job Cards matching the filters/ }).first().click();
			await pageM.locator(`${P}`).getByText("2 selected").waitFor();
			await pageM.locator(`${P}`).getByRole("button", { name: "Clear selection" }).click();
			await pageM.locator(`${P}`).getByText("0 selected").waitFor();
			await pageM.locator(`${P} tbody tr[data-id] input.ppc-pick`).first().check();
			await pageM.locator(`${P}`).getByText("1 selected").waitFor();
			await pageM.locator(`${P}`).getByRole("button", { name: "Clear selection" }).click();
		});

		/* ------------------------------------------------------------------------------------ gantt page */
		await step("Gantt page: full-width timeline, scales, zoom, Today, grouping, search, collapse, task list, date window, drawer", async () => {
			await openControl(pageM, plan, "gantt");
			await pageM.waitForSelector(`${P} .ppc-bar`);
			assert.equal(await pageM.locator(`${P} .ppc-bar`).count(), 2);
			const widthOfPage = await pageM.evaluate(() => document.querySelector(".ppc-page:not([hidden])").getBoundingClientRect().width);
			const widthOfGantt = await pageM.locator(`${P} .ppc-gantt`).evaluate((e) => e.getBoundingClientRect().width);
			assert.ok(widthOfGantt > widthOfPage * 0.9, `the Gantt uses the page width (${widthOfGantt} of ${widthOfPage})`);
			for (const scale of ["day", "week", "month", "quarter"]) {
				await pageM.locator(`${P} .ppc-scale[data-scale="${scale}"]`).click();
				await pageM.waitForFunction((s) => document.querySelector(`.ppc-page:not([hidden]) .ppc-scale[data-scale="${s}"]`).getAttribute("aria-pressed") === "true", scale);
			}
			await pageM.locator(`${P} .ppc-scale[data-scale="week"]`).click();
			const w0 = await pageM.locator(`${P} .ppc-gantt-inner`).evaluate((e) => e.getBoundingClientRect().width);
			await pageM.locator(`${P} .ppc-zoom-in`).click();
			const w1 = await pageM.locator(`${P} .ppc-gantt-inner`).evaluate((e) => e.getBoundingClientRect().width);
			assert.notEqual(w0, w1, "zoom changes the timeline scale");
			await pageM.locator(`${P} .ppc-scale[data-scale="week"]`).click();
			await pageM.locator(`${P} .ppc-today`).click();
			// grouping and collapsing
			await pageM.locator(`${P} .ppc-group`).selectOption({ index: 1 });
			await pageM.waitForSelector(`${P} .ppc-lrow.group`);
			const groups = await pageM.locator(`${P} .ppc-lrow.group`).count();
			assert.ok(groups >= 1);
			await pageM.locator(`${P} .ppc-group`).selectOption("workstation");
			assert.equal(await pageM.locator(`${P} .ppc-lrow.group`).count(), 2, "grouped by workstation: one group per workstation");
			const head = pageM.locator(`${P} .ppc-lrow.group`).first();
			await head.click();
			await pageM.waitForFunction(() => document.querySelector(".ppc-page:not([hidden]) .ppc-lrow.group").getAttribute("aria-expanded") === "false");
			assert.equal(await pageM.locator(`${P} .ppc-bar`).count(), 1, "a collapsed group hides its bars");
			await pageM.locator(`${P} .ppc-lrow.group`).first().click();
			assert.equal(await pageM.locator(`${P} .ppc-bar`).count(), 2);
			// search
			await pageM.locator(`${P} .ppc-gsearch`).fill("Assemble");
			await pageM.waitForFunction(() => document.querySelectorAll(".ppc-page:not([hidden]) .ppc-bar").length === 1);
			await pageM.locator(`${P} .ppc-gsearch`).fill("");
			await pageM.waitForFunction(() => document.querySelectorAll(".ppc-page:not([hidden]) .ppc-bar").length === 2);
			// task list
			await pageM.locator(`${P} .ppc-toggle-grid`).click();
			await pageM.waitForFunction(() => !document.querySelector(".ppc-page:not([hidden]) .ppc-gantt-left") || getComputedStyle(document.querySelector(".ppc-page:not([hidden]) .ppc-gantt-left")).display === "none");
			await pageM.locator(`${P} .ppc-toggle-grid`).click();
			// date window: a window with nothing in it, and Show all dates
			let a = nextResponse(pageM, "get_gantt_data");
			await pageM.locator(`${P} [data-fieldname="window_from"] input`).fill("2031-01-01");
			await pageM.locator(`${P} [data-fieldname="window_from"] input`).press("Tab");
			const empty = await a;
			assert.equal(empty.body.tasks.length, 0, "nothing is scheduled in 2031");
			await idle(pageM);
			assert.ok(await pageM.locator(`${P} .ppc-ganttpage.ppc-gantt-empty`).count() > 0, "an empty window says so");
			a = nextResponse(pageM, "get_gantt_data");
			await pageM.locator(`${P} .ppc-fit-all`).click();
			assert.equal((await a).body.tasks.length, 2);
			await idle(pageM);
			// the drawer
			await pageM.locator(L.barSel(jcNames[1])).click();
			await pageM.waitForSelector(`${P} .ppc-drawer[data-open]`, { timeout: 20000 }).catch(async (e) => {
				const st = await pageM.evaluate(() => ({ drawers: [...document.querySelectorAll(".ppc-drawer")].map((d) => ({ hidden: d.hidden, open: d.hasAttribute("data-open"), display: getComputedStyle(d).display, visible: d.offsetParent !== null || getComputedStyle(d).position === "fixed" })), bars: document.querySelectorAll(".ppc-bar").length }));
				throw new Error(e.message.split("\n")[0] + " | " + JSON.stringify(st));
			});
			await idle(pageM);
			assert.match(await pageM.locator(`${P} .ppc-drawer[data-open]`).innerText(), /Original schedule/i);
			await pageM.screenshot({ path: path.join(SHOTS, "ui04_gantt_drawer.png") });
			await pageM.keyboard.press("Escape");
			await pageM.waitForFunction(() => !document.querySelector(".ppc-page:not([hidden]) .ppc-drawer[data-open]"));
		});

		/* ------------------------------------------------------------------------------------ workload */
		await step("Workload & Capacity: scheduled hours per workstation; unavailable data is shown as unavailable, never invented", async () => {
			await openControl(pageM, null, "workload");
			await pageM.waitForSelector(`${P} table.ppc-workload tbody tr`);
			const rows = await pageM.locator(`${P} table.ppc-workload tbody tr`).evaluateAll((trs) => trs.map((tr) => [...tr.children].map((td) => td.innerText.trim())));
			const heads = await pageM.locator(`${P} table.ppc-workload thead th`).allInnerTexts();
			for (const h of ["Workstation", "Available", "Scheduled", "Remaining", "Utilization", "Overloaded periods"]) assert.ok(heads.some((x) => x.includes(h)), `column ${h}`);
			const col = (name) => heads.findIndex((x) => x.includes(name));
			const ws = (n) => rows.find((r) => r[0] === n);
			assert.ok(ws("PPCE2E WS1") && ws("PPCE2E WS2"), "the fixture workstations are listed");
			assert.equal(ws("PPCE2E WS1")[col("Scheduled")], "20 h");
			assert.equal(ws("PPCE2E WS2")[col("Scheduled")], "30 h");
			for (const r of rows) {
				const avail = r[col("Available")], util = r[col("Utilization")];
				if (/unavailable/i.test(avail)) assert.ok(/unavailable/i.test(util) && !/%/.test(util), `${r[0]}: no availability means no utilization`);
				else {
					const hours = parseFloat(avail), sched = parseFloat(r[col("Scheduled")]), pct = parseFloat(util);
					assert.ok(hours > 0 && Math.abs((100 * sched) / hours - pct) < 1.1, `${r[0]}: utilization ${util} = scheduled ${sched} / available ${hours}`);
				}
			}
			await pageM.screenshot({ path: path.join(SHOTS, "ui05_workload.png") });
			const a = nextResponse(pageM, "get_workload");
			await setFilter(pageM, "workstation", "PPCE2E WS1");
			assert.deepEqual((await a).body.rows.map((w) => w.workstation), ["PPCE2E WS1"]);
		});

		/* ------------------------------------------------------------------------------------ history and exceptions */
		await step("Schedule History: baselines only on request, open the drawer from a row, Escape closes it and focus returns", async () => {
			await openControl(pageM, plan, "history");
			assert.equal(await rowCount(pageM), 0, "no revision yet: only baselines exist");
			assert.match(await pageM.locator(`${P} .ppc-empty`).first().innerText(), /No schedule changes/);
			const a = nextResponse(pageM, "get_revision_log");
			await pageM.locator(`${P} [data-fieldname="revision_type"] select`).selectOption("All");
			assert.equal((await a).body.total, 2, "two baselines");
			await idle(pageM);
			assert.equal(await rowCount(pageM), 2);
			const btn = pageM.locator(`${P} tbody tr[data-id] .ppc-history`).first();
			await btn.focus();
			await pageM.keyboard.press("Enter");
			await pageM.waitForSelector(`${P} .ppc-drawer[data-open]`);
			await idle(pageM);
			await pageM.keyboard.press("Escape");
			await pageM.waitForFunction(() => !document.querySelector(".ppc-page:not([hidden]) .ppc-drawer[data-open]"));
			assert.equal(await pageM.evaluate(() => document.activeElement && document.activeElement.className), await btn.evaluate((e) => e.className), "focus went back to the button that opened the drawer");
		});

		await step("Exceptions: type chips filter the list; Preview repairs is a preview", async () => {
			await openControl(pageM, null, "exceptions");
			await pageM.waitForSelector(`${P} .ppc-chip`);
			const chips = await pageM.locator(`${P} .ppc-chip`).evaluateAll((els) => els.map((e) => ({ type: e.dataset.type, n: Number(e.querySelector(".n").innerText), title: e.innerText.replace(/^\d+\s*/, "").trim() })));
			assert.ok(chips.length >= 8, "all exception types are offered");
			const withRows = chips.find((c) => c.n > 0);
			if (withRows) {
				const a = nextResponse(pageM, "get_exceptions");
				await pageM.locator(`${P} .ppc-chip[data-type="${withRows.type}"]`).click();
				const r = await a;
				assert.ok(r.body.rows.length > 0 && r.body.rows.every((x) => x.type === withRows.type), "only the chosen type is listed");
				assert.equal(await pageM.locator(`${P} .ppc-chip[data-type="${withRows.type}"]`).getAttribute("aria-pressed"), "true");
				await idle(pageM);
				const b = nextResponse(pageM, "get_exceptions");
				await pageM.locator(`${P} .ppc-chip[data-type="${withRows.type}"]`).click();
				await b;
			}
			const before = L.snapshot();
			await pageM.locator(`${P} .ppc-preview-repair`).click();
			const c = await L.modalTitle(pageM, 30000);
			assert.match(c.title, /nothing has been changed yet/i);
			await L.dismissModal(pageM);
			assert.deepEqual(L.snapshot().raw.work_orders, before.raw.work_orders, "previewing repairs changed nothing");
		});

		/* ------------------------------------------------------------------------------------ plan form and legacy route */
		await step("the Production Plan form keeps a short summary with links and no embedded Gantt; the old route forwards", async () => {
			await pageM.goto(`${BASE}/app/production-plan/${plan}`);
			await pageM.waitForSelector(".ppc-plan-summary", { timeout: 45000 });
			assert.equal(await pageM.locator(".ppc-gantt:visible").count(), 0, "no Gantt on the standard form");
			assert.equal(await pageM.locator(".ppc-kpi2:visible, .ppc-tab:visible").count(), 0, "no embedded dashboard");
			await pageM.screenshot({ path: path.join(SHOTS, "ui06_plan_form.png") });
			await pageM.locator(".ppc-plan-summary a", { hasText: /Gantt/ }).first().click();
			await pageM.waitForURL(/\/app\/ppc-gantt/);
			await pageM.waitForSelector(`${P} .ppc-bar`);
			await filterIs(pageM, "plan", plan);
			await pageM.goto(`${BASE}/app/ppc-production-control?tab=jobcards&plan=${plan}`);
			await pageM.waitForURL(/\/app\/ppc-job-cards/, { timeout: 30000 });
			await pageM.waitForSelector(`${P} tbody tr[data-id]`);
			await filterIs(pageM, "plan", plan); // the old link keeps its plan
		});

		/* ------------------------------------------------------------------------------------ permissions */
		await step("a viewer sees the pages but gets no bulk or repair actions; a user without a PPC role is refused by the pages and by the server", async () => {
			const ctxV = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
			pageV = await ctxV.newPage();
			openPages.push(pageV);
			L.watch(pageV, "viewer", problems);
			await login(pageV, VIEWER);
			await openControl(pageV, plan, "workorders");
			assert.equal(await pageV.locator(`${P} .ppc-submit-wo`).count(), 0);
			assert.equal(await pageV.locator(`${P} input.ppc-pick`).count(), 0, "no checkboxes without the submit capability");
			await openControl(pageV, null, "dashboard");
			assert.equal(await pageV.locator(`${P} .ppc-kpi2`).count(), 10, "viewer can read the dashboard");
			const viewer = await pageV.evaluate(async (wo) => {
				const post = (m, body) => fetch("/api/method/ppc_production_control.api." + m, { method: "POST", headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token }, body: JSON.stringify(body) }).then((r) => r.status);
				return { precheck: await post("precheck_work_orders", { work_orders: [wo[0]] }), submit: await post("submit_selected_work_orders", { plan: wo[1], work_orders: [wo[0]] }) };
			}, [snap.wo.name, plan]);
			assert.equal(viewer.precheck, 403);
			assert.equal(viewer.submit, 403);

			const ctxO = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
			const pageO = await ctxO.newPage();
			// no console watch here: Frappe itself logs the server's "not permitted" traceback when a page is refused, which is the expected outcome
			await login(pageO, OUTSIDER);
			const codes = {};
			for (const [name, route] of [["gantt", "ppc-gantt"], ["dashboard", "ppc-dashboard"], ["jobcards", "ppc-job-cards"]]) {
				await pageO.goto(`${BASE}/app/${route}`);
				await pageO.waitForFunction(() => /not permitted|permission|not found/i.test(document.body.innerText) || document.querySelector(".ppc-page:not([hidden])"), null, { timeout: 30000 });
				codes[name] = await pageO.locator(".ppc-page:visible .ppc-kpi2, .ppc-page:visible .ppc-bar, .ppc-page:visible tbody tr[data-id]").count();
				assert.equal(codes[name], 0, `${name}: no data is shown to a user without a PPC role`);
			}
			const api = await pageO.evaluate(async () => {
				const get = (m) => fetch("/api/method/ppc_production_control.api." + m).then((r) => r.status);
				return { overview: await get("get_dashboard_overview"), jobs: await get("get_job_cards"), gantt: await get("get_gantt_data"), workload: await get("get_workload"), exceptions: await get("get_exceptions") };
			});
			for (const [k, v] of Object.entries(api)) assert.equal(v, 403, `${k} should be refused with 403, got ${v}`);
			await ctxO.close();
			return api;
		});

		/* ------------------------------------------------------------------------------------ look and feel */
		await step("light and dark theme: text keeps at least 4.5:1 contrast on the dashboard, a list and the Gantt", async () => {
			const measured = {};
			for (const theme of ["light", "dark"]) {
				await pageM.goto(`${BASE}/app/ppc-dashboard`);
				await pageM.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
				await pageM.waitForSelector(`${P} .ppc-kpi2`);
				await idle(pageM);
				for (const [what, sel] of [["kpi value", ".ppc-kpi2 .v"], ["kpi label", ".ppc-kpi2 .l"], ["panel title", ".ppc-panel-head h3"], ["nav item", ".ppc-nav-item:not(.active)"], ["active nav", ".ppc-nav-item.active"],
					["description", ".ppc-pagebar-desc"], ["table text", ".ppc-panel .ppc-table td"], ["table head", ".ppc-panel .ppc-table th"]]) {
					const c = await contrastOf(pageM, `${P.replace(":visible", "")} ${sel}`);
					if (c === null) continue;
					measured[`${theme} ${what}`] = c;
					assert.ok(c >= 4.5, `${theme}: ${what} contrast ${c} < 4.5`);
				}
				await pageM.screenshot({ path: path.join(SHOTS, `ui07_dashboard_${theme}.png`) });
				await openControl(pageM, null, "jobcards");
				await pageM.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
				for (const [what, sel] of [["list cell", ".ppc-table tbody td"], ["list head", ".ppc-table thead th"], ["pager", ".ppc-pager"], ["filter label", ".ppc-filters2 label"]]) {
					const c = await contrastOf(pageM, sel);
					if (c === null) continue;
					measured[`${theme} ${what}`] = c;
					assert.ok(c >= 4.5, `${theme}: ${what} contrast ${c} < 4.5`);
				}
				await openControl(pageM, plan, "gantt");
				await pageM.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
				await pageM.screenshot({ path: path.join(SHOTS, `ui08_gantt_${theme}.png`) });
			}
			await pageM.evaluate(() => document.documentElement.setAttribute("data-theme", "light"));
			return measured;
		});

		await step("responsive: no sideways page scroll on any page at desktop, tablet and phone widths", async () => {
			const routes = ["dashboard", "plans", "workorders", "jobcards", "gantt", "workload", "history", "exceptions"];
			const bad = [];
			for (const [w, h] of [[1440, 900], [1024, 768], [768, 1024], [390, 800]]) {
				await pageM.setViewportSize({ width: w, height: h });
				for (const r of routes) {
					await openControl(pageM, r === "gantt" ? plan : null, r);
					await pageM.waitForSelector(`${P} .ppc-nav`);
					const over = await pageM.evaluate(() => ({ doc: document.documentElement.scrollWidth - window.innerWidth, body: document.body.scrollWidth - window.innerWidth }));
					if (over.doc > 1 || over.body > 1) bad.push(`${r}@${w}: +${Math.max(over.doc, over.body)}px`);
					if (w === 390 && ["dashboard", "workorders", "gantt"].includes(r)) await pageM.screenshot({ path: path.join(SHOTS, `ui09_${r}_${w}.png`) });
				}
			}
			await pageM.setViewportSize({ width: 1600, height: 1000 });
			assert.deepEqual(bad, [], "pages wider than the screen: " + bad.join(", "));
		});

		await step("keyboard: Tab reaches the KPI tiles with a visible focus ring, Enter opens the list", async () => {
			await openControl(pageM, null, "dashboard");
			await pageM.waitForSelector(`${P} .ppc-kpi2`);
			await pageM.locator(`${P} .ppc-nav-item.active`).focus();
			let hit = false;
			for (let i = 0; i < 60 && !hit; i++) {
				await pageM.keyboard.press("Tab");
				hit = await pageM.evaluate(() => document.activeElement && document.activeElement.classList.contains("ppc-kpi2"));
			}
			assert.ok(hit, "a KPI tile is reachable with Tab");
			const ring = await pageM.evaluate(() => { const s = getComputedStyle(document.activeElement); return { outline: s.outlineStyle + " " + s.outlineWidth, shadow: s.boxShadow }; });
			assert.ok(!/^none/.test(ring.outline) || ring.shadow !== "none", "focus is visible: " + JSON.stringify(ring));
			await pageM.keyboard.press("Enter");
			await pageM.waitForURL(/\/app\/ppc-(production-plans|work-orders|job-cards)$/);
		});

		await step("Job Cards bulk submit: confirmation first; each record gets its own result; nothing is bypassed", async () => {
			await openControl(pageM, plan, "jobcards");
			await pageM.locator(`${P}`).getByRole("button", { name: /Select all draft Job Cards matching the filters/ }).click();
			await pageM.locator(`${P}`).getByText("2 selected").waitFor();
			await pageM.locator(`${P} .ppc-submit-jc`).click();
			const c = await L.modalTitle(pageM, 15000);
			assert.match(await c.modal.innerText(), /Submit 2 Job Card/);
			await L.dismissModal(pageM); // cancel: nothing happens
			const states0 = await pageM.evaluate((names) => Promise.all(names.map((n) => frappe.db.get_value("Job Card", n, "docstatus").then((r) => r.message.docstatus))), jcNames);
			assert.deepEqual(states0, [0, 0], "cancelling the confirmation submitted nothing");
			await pageM.locator(`${P} .ppc-submit-jc`).click();
			const c2 = await L.modalTitle(pageM, 15000);
			await c2.modal.locator(".btn-primary, .btn-modal-primary").first().click();
			await pageM.waitForSelector(".modal.show .ppc-results table tbody tr", { timeout: 90000 });
			const dialog = await pageM.locator(".modal.show .ppc-results").last().innerText();
			for (const n of jcNames) assert.ok(dialog.includes(n), `result for ${n}`);
			const rows = await pageM.locator(".modal.show .ppc-results tbody tr").evaluateAll((trs) => trs.map((tr) => [tr.children[0].innerText.trim(), /Done/.test(tr.children[1].innerText)]));
			await pageM.screenshot({ path: path.join(SHOTS, "ui10_bulk_result.png") });
			await L.dismissModal(pageM);
			const states = await pageM.evaluate((names) => Promise.all(names.map((n) => frappe.db.get_value("Job Card", n, "docstatus").then((r) => r.message.docstatus))), jcNames);
			for (const [name, ok] of rows) assert.equal(states[jcNames.indexOf(name)], ok ? 1 : 0, `${name}: result "${ok ? "done" : "failed"}" matches the record`);
			return rows;
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
	console.log(`\nUI E2E: ${run.count("PASS")} passed, ${run.count("FAIL")} failed, ${run.count("SKIPPED")} skipped of ${results.length}`);
	L.fs.writeFileSync(path.join(SHOTS, "results_ui.json"), JSON.stringify({ results, problems }, null, 2));
	process.exit(run.count("FAIL") ? 1 : 0);
})();
