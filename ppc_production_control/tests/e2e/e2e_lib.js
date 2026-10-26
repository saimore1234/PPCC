/*
 * Shared helpers for the PPC browser tests (ppc_e2e.js, ppc_drag_regression.js).
 *
 * Configuration (environment):
 *   PPC_E2E_BASE     site URL                         default http://localhost:8000
 *   PPC_E2E_RUNNER   command prefix that runs run_fixture.sh (split on spaces), e.g.
 *                    "bash /path/to/frappe-bench/apps/ppc_production_control/ppc_production_control/tests/e2e/run_fixture.sh"
 *                    or  "wsl.exe -d Ubuntu-22.04 -- bash /home/me/frappe-bench/apps/.../run_fixture.sh"
 *   PPC_E2E_BROWSER  path to a Chromium/Edge/Chrome executable (default: Playwright's bundled browser)
 *   PPC_E2E_SHOTS    folder for screenshots           default ./shots
 *   PPC_E2E_SITE     (used by run_fixture.sh) site the fixtures run against
 *
 * Waiting: tests wait for *observable state*, never for a fixed time. The app publishes it:
 *   .ppc-gantt[data-ppc-drag]   idle | dragging | pending   (a change is being saved)
 *   .ppc-page[data-ppc-busy]    number of loads in flight on the page that is showing (Frappe keeps visited pages in the DOM, hidden)
 * plus jQuery.active (Frappe's own count of in-flight requests), open modals/backdrops and the #freeze overlay.
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
const OUTSIDER = "ppc.e2e.outsider@example.com";
const PLANNER = "ppc.e2e.planner@example.com";
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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms)); // only for polling the *database* between fixture calls
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

/* ---------------------------------------------------------------------------------------- step runner */
function makeRunner() {
	const results = [];
	const openPages = [];
	let aborted = false;
	async function step(name, fn, { critical = false } = {}) {
		if (aborted) { results.push({ name, status: "SKIPPED" }); console.log("SKIP  " + name); return; }
		const t0 = Date.now();
		try {
			const detail = await fn();
			results.push({ name, status: "PASS" });
			console.log("PASS  " + name + (detail ? "  " + (typeof detail === "string" ? detail : JSON.stringify(detail)) : "") + `  (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
		} catch (e) {
			results.push({ name, status: "FAIL", error: String(e.message).split("\n").slice(0, 8).join(" | ") });
			console.log("FAIL  " + name + "\n      " + String(e.message).split("\n").slice(0, 9).join("\n      "));
			if (critical) aborted = true;
			for (const p of openPages) { await p.keyboard.press("Escape").catch(() => {}); await p.keyboard.press("Escape").catch(() => {}); } // no dialog may stay on top of the next step
		}
	}
	return { step, results, openPages, count: (s) => results.filter((r) => r.status === s).length };
}

async function launch() {
	const opts = { headless: true };
	if (process.env.PPC_E2E_BROWSER) opts.executablePath = process.env.PPC_E2E_BROWSER;
	const browser = await chromium.launch(opts);
	// A development machine running a web server, a worker, two databases and a browser can be slow: allow generous
	// time for navigation and element lookups. (This never affects how drags are synchronised - that waits on state.)
	const newContext = browser.newContext.bind(browser);
	browser.newContext = async (o) => {
		const ctx = await newContext(o);
		ctx.setDefaultTimeout(Number(process.env.PPC_E2E_TIMEOUT || 60000));
		ctx.setDefaultNavigationTimeout(Number(process.env.PPC_E2E_NAV_TIMEOUT || 120000));
		return ctx;
	};
	return browser;
}

/** Console errors / 5xx / page errors, and a log of every reschedule request with its parsed response. */
function watch(page, label, problems, rpc) {
	page.on("console", (m) => { if (m.type() === "error" && !/socket|Failed to load resource|favicon/.test(m.text())) problems.push(`${label} console.error: ${m.text().slice(0, 240)}`); });
	page.on("pageerror", (e) => problems.push(`${label} pageerror: ${String(e).slice(0, 300)}`));
	page.on("response", (r) => {
		if (r.status() >= 500) problems.push(`${label} HTTP ${r.status()} ${r.url().slice(0, 120)}`);
		if (rpc && r.url().includes("ppc_production_control.api.reschedule_job_card")) {
			r.json().then((j) => rpc.push({ label, status: r.status(), msg: j.message })).catch(() => rpc.push({ label, status: r.status(), msg: null }));
		}
	});
}

/* ---------------------------------------------------------------------------------------- browser helpers */
async function login(page, email) {
	await page.goto(BASE + "/login");
	await page.fill("#login_email", email);
	await page.fill("#login_password", PASSWORD);
	await page.click(".btn-login");
	await page.waitForURL(/\/app/, { timeout: 45000 });
}

const ROUTES = {
	dashboard: "ppc-dashboard", plans: "ppc-production-plans", workorders: "ppc-work-orders", jobcards: "ppc-job-cards", gantt: "ppc-gantt",
	workload: "ppc-workload", history: "ppc-schedule-history", audit: "ppc-schedule-history", exceptions: "ppc-exceptions",
};

/** The page element of the PPC page that is showing now (visited pages stay in the DOM, hidden). */
const visiblePage = (page) => page.locator(".ppc-page:visible").first();

/** Open a PPC Control Center page the way a link from another page does: route options in the URL, e.g. ?plan=MFG-PP-1. */
async function openControl(page, plan, tab, extra) {
	const route = ROUTES[tab || "dashboard"];
	assert.ok(route, "unknown PPC page " + tab);
	const qs = new URLSearchParams(Object.assign(plan ? { plan } : {}, extra || {})).toString();
	await page.goto(`${BASE}/app/${route}${qs ? "?" + qs : ""}`);
	await page.waitForSelector(`.ppc-page[data-ppc-page]:visible`, { timeout: 45000 });
	await untilIdle(page);
}

const barSel = (name) => `.ppc-bar[data-name="${name}"]`;

async function barLeft(page, name) {
	return parseFloat(await page.locator(barSel(name)).evaluate((el) => el.style.left));
}

async function dragBar(page, name, dx, handle, between) {
	const b = page.locator(barSel(name));
	await b.scrollIntoViewIfNeeded();
	// The chart auto-scrolls while the pointer is within 40px of its edge (by design). A bar that happens to sit there - it
	// depends on the time of day the fixture was made - would pick up extra scroll and move further than dragged, so
	// bring the bar to the middle of the timeline first.
	await b.evaluate((el) => {
		const sc = el.closest(".ppc-gantt-right");
		if (!sc) return;
		const r = el.getBoundingClientRect(), s = sc.getBoundingClientRect();
		if (r.left < s.left + 160 || r.right > s.right - 160) sc.scrollLeft += r.left + r.width / 2 - (s.left + s.width / 2);
	});
	const box = await b.boundingBox();
	const y = box.y + box.height / 2;
	const x = handle === "right" ? box.x + box.width - 3 : handle === "left" ? box.x + 3 : box.x + box.width / 2;
	await page.mouse.move(x, y);
	await page.mouse.down();
	await page.mouse.move(x + dx / 2, y, { steps: 6 });
	if (between) await between(); // something happens while the button is still held down
	await page.mouse.move(x + dx, y, { steps: 6 });
	await page.mouse.up();
}

async function setColumns(page, mode) {
	const pressed = (await page.locator(".ppc-toggle-cols").getAttribute("aria-pressed")) === "true";
	if ((mode === "schedule") !== pressed) {
		await page.click(".ppc-toggle-cols");
		await page.waitForFunction((m) => document.querySelector(".ppc-toggle-cols").getAttribute("aria-pressed") === String(m === "schedule"), mode);
	}
}

/** What the page is doing right now - everything a user would have to wait for before the next action is safe. */
function pageState(page) {
	return page.evaluate(() => {
		const shown = (sel) => [...document.querySelectorAll(sel)].find((e) => e.offsetParent !== null) || null;
		const g = shown(".ppc-gantt");
		const root = shown(".ppc-page[data-ppc-busy]");
		const freeze = document.getElementById("freeze");
		return {
			drag: g ? g.dataset.ppcDrag : null,
			busy: root ? Number(root.getAttribute("data-ppc-busy")) : 0,
			ajax: window.jQuery ? window.jQuery.active : 0,
			modals: document.querySelectorAll(".modal.show").length,
			backdrops: document.querySelectorAll(".modal-backdrop").length,
			freeze: !!(freeze && freeze.classList.contains("in")),
		};
	});
}

/** Resolves when nothing is loading, saving, dragging or covering the page. Deterministic: waits on state, not time. */
async function untilIdle(page, timeout = 60000) {
	await page.waitForFunction(() => {
		const shown = (sel) => [...document.querySelectorAll(sel)].find((e) => e.offsetParent !== null) || null;
		const g = shown(".ppc-gantt");
		const root = shown(".ppc-page[data-ppc-busy]");
		const freeze = document.getElementById("freeze");
		return (!g || g.dataset.ppcDrag === "idle") && (!root || Number(root.getAttribute("data-ppc-busy")) === 0) &&
			(!window.jQuery || window.jQuery.active === 0) && !document.querySelector(".modal.show") && !document.querySelector(".modal-backdrop") &&
			!(freeze && freeze.classList.contains("in"));
	}, null, { timeout });
}

async function modalTitle(page, timeout = 10000) {
	const m = page.locator(".modal.show").last();
	await m.waitFor({ timeout });
	return { modal: m, title: (await m.locator(".modal-title").first().innerText()).trim() };
}

/** Close the top dialog the way a user does (Escape) and wait until it is really gone. */
/** True once the top dialog has finished animating in. Bootstrap ignores every attempt to hide a dialog before that. */
function modalFullyShown() {
	const m = document.querySelector(".modal.show");
	if (!m) return true;
	const d = m.querySelector(".modal-dialog");
	const t = getComputedStyle(d).transform;
	return getComputedStyle(m).opacity === "1" && (t === "none" || t === "matrix(1, 0, 0, 1, 0, 0)");
}

async function dismissModal(page) {
	await page.bringToFront();
	await page.waitForFunction(modalFullyShown); // a dialog that is still fading in ignores Escape and the close button
	const close = page.locator(".modal.show .btn-modal-close");
	if (await close.count()) await close.last().click(); // what a user does: press the dialog's close button
	else await page.keyboard.press("Escape");
	await page.waitForFunction(() => !document.querySelector(".modal.show") && !document.querySelector(".modal-backdrop"));
}

/**
 * Drag a bar and require that exactly one reschedule request is sent. If the browser never sends one this *fails
 * at the timeout* - it is never papered over with a retry.
 */
async function dragExpectingRequest(page, name, dx, handle, timeout = 20000, between) {
	const sent = [];
	const onReq = (r) => { if (r.url().includes("ppc_production_control.api.reschedule_job_card")) sent.push(r.postData()); };
	page.on("request", onReq);
	const t0 = Date.now();
	try {
		const respPromise = page.waitForResponse((r) => r.url().includes("ppc_production_control.api.reschedule_job_card"), { timeout });
		await dragBar(page, name, dx, handle, between);
		const resp = await respPromise;
		const body = await resp.json();
		return { status: resp.status(), msg: body.message, sent: sent.length, post: resp.request().postData(), ms: Date.now() - t0 };
	} finally {
		page.off("request", onReq);
	}
}

/* a trace of what the page saw: pointer events and the Gantt's own drag/render calls */
async function installTrace(page) {
	await page.evaluate(() => {
		if (window.__ppcTraceInstalled) return;
		window.__ppcTraceInstalled = true;
		window.__trace = [];
		const t0 = performance.now();
		const log = (what, extra) => window.__trace.push(Object.assign({ t: Math.round(performance.now() - t0), what }, extra || {}));
		for (const type of ["pointerdown", "pointerup", "click"]) {
			document.addEventListener(type, (e) => log(type, { target: (e.target.className && String(e.target.className).slice(0, 40)) || e.target.tagName, x: Math.round(e.clientX), prevented: e.defaultPrevented }), true);
		}
		const P = window.PPCGanttLib.PPCGantt.prototype;
		for (const fn of ["_dragStart", "_dragEnd", "_dragCancel", "render"]) {
			const orig = P[fn];
			P[fn] = function () {
				const before = this._drag ? (this._drag.pending ? "pending" : "dragging") : "idle";
				const r = orig.apply(this, arguments);
				log("gantt." + fn, { drag: before + ">" + (this._drag ? (this._drag.pending ? "pending" : "dragging") : "idle"), moved: this._drag && this._drag.moved });
				return r;
			};
		}
		const origCall = frappe.call;
		frappe.call = function (opts) {
			const m = (opts && (opts.method || (opts.args && opts.args.cmd))) || "";
			if (/reschedule_job_card|get_gantt_data/.test(m)) log("call " + m.split(".").pop(), { freeze: !!opts.freeze });
			return origCall.apply(this, arguments);
		};
	});
}

const dumpTrace = (page) => page.evaluate(() => (window.__trace || []).slice(-60).map((e) => JSON.stringify(e)).join("\n")).catch(() => "(no trace)");

module.exports = {
	assert, path, fs, chromium, BASE, SHOTS, MANAGER, VIEWER, OUTSIDER, PLANNER, PASSWORD, fx, sleep, secs, plusHours, snapshot, makeRunner, launch, watch,
	installTrace, dumpTrace, login, openControl, visiblePage, ROUTES, visiblePage, ROUTES, modalFullyShown, barSel, barLeft, dragBar, setColumns, pageState, untilIdle, modalTitle, dismissModal, dragExpectingRequest,
};
