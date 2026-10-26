/*
 * PPC Gantt - dependency-free production Gantt chart.
 *
 * Why not frappe-gantt? Frappe bundles frappe-gantt 0.6, which has no row grouping, no frozen
 * task grid, no quarter scale and snaps to whole days only - none of which a production
 * schedule can live without. This file has no third-party code, so there is no version / licence
 * to track (it is MIT like the rest of the app).
 *
 * Time handling: every instant is a "naive" timestamp (ms since epoch computed as if the wall-clock
 * time were UTC). Frappe stores datetimes naive in the *site* time zone, so reading/writing them
 * through UTC arithmetic means the browser's time zone and DST can never move a scheduled time.
 */
(function (root) {
	"use strict";

	const MIN = 60 * 1000;
	const HOUR = 60 * MIN;
	const DAY = 24 * HOUR;
	const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
	const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
	const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
	const WEEKDAYS_LONG = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

	const SCALES = {
		day: { key: "day", label: "Day", pxPerDay: 288, snapMin: 15 },
		week: { key: "week", label: "Week", pxPerDay: 96, snapMin: 60 },
		month: { key: "month", label: "Month", pxPerDay: 32, snapMin: 360 },
		quarter: { key: "quarter", label: "Quarter", pxPerDay: 9, snapMin: 1440 },
	};
	const SCALE_ORDER = ["quarter", "month", "week", "day"]; // zoomed out -> zoomed in

	/* ------------------------------------------------------------------------------ time maths */
	function pad(n) {
		return n < 10 ? "0" + n : "" + n;
	}

	function parse(str) {
		if (str == null || str === "") return null;
		if (typeof str === "number") return str;
		const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(String(str));
		if (!m) return null;
		return Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
	}

	function format(ms) {
		const d = new Date(ms);
		return (
			d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " +
			pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds())
		);
	}

	function formatShort(ms) {
		const d = new Date(ms);
		return pad(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()] + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
	}

	/** "Wed 21 Oct 2026 08:00": the full date for tooltips and screen readers. */
	function formatFull(ms) {
		const d = new Date(ms);
		return WEEKDAYS[d.getUTCDay()] + " " + pad(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear() + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
	}

	/** "Wednesday, 21 October 2026". */
	function fullDate(ms) {
		const d = new Date(ms);
		return WEEKDAYS_LONG[d.getUTCDay()] + ", " + d.getUTCDate() + " " + MONTHS_LONG[d.getUTCMonth()] + " " + d.getUTCFullYear();
	}

	/** "19 – 25 Oct 2026", "26 Oct – 01 Nov 2026", "28 Dec 2026 – 03 Jan 2027": the month and year appear exactly where they change. */
	function dateRange(firstMs, lastMs) {
		const a = new Date(firstMs);
		const b = new Date(lastMs);
		const day = function (d) { return pad(d.getUTCDate()); };
		if (a.getUTCFullYear() !== b.getUTCFullYear()) {
			return day(a) + " " + MONTHS[a.getUTCMonth()] + " " + a.getUTCFullYear() + " – " + day(b) + " " + MONTHS[b.getUTCMonth()] + " " + b.getUTCFullYear();
		}
		if (a.getUTCMonth() !== b.getUTCMonth()) {
			return day(a) + " " + MONTHS[a.getUTCMonth()] + " – " + day(b) + " " + MONTHS[b.getUTCMonth()] + " " + b.getUTCFullYear();
		}
		return day(a) + " – " + day(b) + " " + MONTHS[b.getUTCMonth()] + " " + b.getUTCFullYear();
	}

	/**
	 * Label of a one-day column that adapts to the column's width - it never falls back to a single-letter weekday:
	 *   >= 84px  "Wed 21 Oct"      >= 54px  "21 Oct"      >= 26px  "21" over "Oct"      narrower  "21"
	 * (the tier above carries the month and year, and every cell's tooltip carries the full date).
	 */
	function dayCell(ms, widthPx) {
		const d = new Date(ms);
		const dd = pad(d.getUTCDate());
		const mon = MONTHS[d.getUTCMonth()];
		if (widthPx >= 84) return { label: WEEKDAYS[d.getUTCDay()] + " " + dd + " " + mon };
		if (widthPx >= 54) return { label: dd + " " + mon };
		if (widthPx >= 26) return { label: dd, sub: mon };
		return { label: dd };
	}

	function formatDuration(ms) {
		const mins = Math.round(ms / MIN);
		const d = Math.floor(mins / 1440);
		const h = Math.floor((mins % 1440) / 60);
		const m = mins % 60;
		return [d ? d + "d" : "", h ? h + "h" : "", m || (!d && !h) ? m + "m" : ""].filter(Boolean).join(" ");
	}

	function startOfDay(ms) {
		return Math.floor(ms / DAY) * DAY;
	}

	function snap(ms, snapMin) {
		const unit = snapMin * MIN;
		return Math.round(ms / unit) * unit;
	}

	function isoWeek(ms) {
		const d = new Date(startOfDay(ms));
		const day = (d.getUTCDay() + 6) % 7; // Monday = 0
		d.setUTCDate(d.getUTCDate() - day + 3); // Thursday of this week
		const firstThursday = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
		const week = 1 + Math.round(((d - firstThursday) / DAY - 3 + ((firstThursday.getUTCDay() + 6) % 7)) / 7);
		return { week: week, year: d.getUTCFullYear() };
	}

	function mondayOf(ms) {
		const day = (new Date(ms).getUTCDay() + 6) % 7;
		return startOfDay(ms) - day * DAY;
	}

	function nextMonthStart(ms) {
		const d = new Date(ms);
		return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
	}

	function monthStart(ms) {
		const d = new Date(ms);
		return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
	}

	function quarterStart(ms) {
		const d = new Date(ms);
		return Date.UTC(d.getUTCFullYear(), Math.floor(d.getUTCMonth() / 3) * 3, 1);
	}

	function addMonths(ms, n) {
		const d = new Date(ms);
		return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + n, 1);
	}

	/**
	 * Header rows for a scale: {top, bottom} - and {top, mid, bottom} for the quarter view. Cells are
	 * {label, sub?, title, start, end, weekend?, today?}. `title` is the full date including the year (plus the time zone
	 * when `opts.tz` is given). All maths is on the naive site-time values the server sends, never on the browser's clock
	 * or time zone, so the labels are the same wherever the page is opened.
	 */
	function headerTiers(scaleKey, startMs, endMs, opts) {
		opts = opts || {};
		const tz = opts.tz ? " · " + opts.tz : "";
		const nowMs = opts.nowMs;
		const px = SCALES[scaleKey].pxPerDay;
		const top = [];
		const bottom = [];
		let mid = null;
		const mark = function (cell) {
			if (nowMs != null && nowMs >= cell.start && nowMs < cell.end) cell.today = true;
			return cell;
		};
		let t;
		if (scaleKey === "day") {
			for (t = startOfDay(startMs); t < endMs; t += DAY) {
				const d = new Date(t);
				top.push(mark({ label: WEEKDAYS[d.getUTCDay()] + " " + pad(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear(), title: fullDate(t) + tz, start: t, end: t + DAY, weekend: d.getUTCDay() === 0 || d.getUTCDay() === 6 }));
				for (let h = 0; h < 24; h += 2) {
					bottom.push(mark({ label: pad(h) + ":00", title: fullDate(t) + ", " + pad(h) + ":00 – " + pad(h + 2) + ":00" + tz, start: t + h * HOUR, end: t + (h + 2) * HOUR }));
				}
			}
		} else if (scaleKey === "week") {
			for (t = mondayOf(startMs); t < endMs; t += 7 * DAY) {
				const w = isoWeek(t);
				top.push(mark({ label: "W" + w.week + " · " + dateRange(t, t + 6 * DAY), title: "Week " + w.week + ", " + dateRange(t, t + 6 * DAY) + tz, start: t, end: t + 7 * DAY }));
			}
			for (t = startOfDay(startMs); t < endMs; t += DAY) {
				const d = new Date(t);
				bottom.push(mark(Object.assign(dayCell(t, px), { title: fullDate(t) + tz, start: t, end: t + DAY, weekend: d.getUTCDay() === 0 || d.getUTCDay() === 6 })));
			}
		} else if (scaleKey === "month") {
			for (t = monthStart(startMs); t < endMs; t = nextMonthStart(t)) {
				const d = new Date(t);
				top.push(mark({ label: MONTHS_LONG[d.getUTCMonth()] + " " + d.getUTCFullYear(), title: MONTHS_LONG[d.getUTCMonth()] + " " + d.getUTCFullYear() + tz, start: t, end: nextMonthStart(t) }));
			}
			for (t = startOfDay(startMs); t < endMs; t += DAY) {
				const d = new Date(t);
				bottom.push(mark(Object.assign(dayCell(t, px), { title: fullDate(t) + tz, start: t, end: t + DAY, weekend: d.getUTCDay() === 0 || d.getUTCDay() === 6 })));
			}
		} else {
			mid = [];
			for (t = quarterStart(startMs); t < endMs; t = addMonths(t, 3)) {
				const d = new Date(t);
				top.push(mark({ label: "Q" + (Math.floor(d.getUTCMonth() / 3) + 1) + " " + d.getUTCFullYear(), title: "Quarter " + (Math.floor(d.getUTCMonth() / 3) + 1) + " " + d.getUTCFullYear() + tz, start: t, end: addMonths(t, 3) }));
			}
			for (t = monthStart(startMs); t < endMs; t = nextMonthStart(t)) {
				const d = new Date(t);
				mid.push(mark({ label: MONTHS_LONG[d.getUTCMonth()] + " " + d.getUTCFullYear(), title: MONTHS_LONG[d.getUTCMonth()] + " " + d.getUTCFullYear() + tz, start: t, end: nextMonthStart(t) }));
			}
			for (t = mondayOf(startMs); t < endMs; t += 7 * DAY) {
				const w = isoWeek(t);
				bottom.push(mark(Object.assign(dayCell(t, px * 7), { sub: "W" + w.week, title: "Week " + w.week + ", " + dateRange(t, t + 6 * DAY) + tz, start: t, end: t + 7 * DAY })));
			}
		}
		const out = { top: top, bottom: bottom };
		if (mid) out.mid = mid;
		return out;
	}

	function xOf(ms, t0, pxPerDay) {
		return ((ms - t0) / DAY) * pxPerDay;
	}

	function timeOf(x, t0, pxPerDay) {
		return t0 + (x / pxPerDay) * DAY;
	}

	/** Apply a drag of `deltaMs` to [start,end] for the given handle. Returns the proposed range. */
	function applyDrag(mode, start, end, deltaMs, snapMin) {
		const unit = snapMin * MIN;
		const d = snap(deltaMs, snapMin);
		if (mode === "resize-left") {
			return { start: Math.min(start + d, end - unit), end: end };
		}
		if (mode === "resize-right") {
			return { start: start, end: Math.max(end + d, start + unit) };
		}
		return { start: start + d, end: end + d };
	}

	/** Visible time window for a task list (padded). */
	function computeRange(tasks, padDays) {
		let lo = Infinity;
		let hi = -Infinity;
		tasks.forEach(function (t) {
			const s = parse(t.revised_start);
			const e = parse(t.revised_end);
			const os = parse(t.original_start);
			const oe = parse(t.original_end);
			[s, os].forEach(function (v) { if (v != null && v < lo) lo = v; });
			[e, oe].forEach(function (v) { if (v != null && v > hi) hi = v; });
		});
		if (!isFinite(lo)) return null;
		return { start: startOfDay(lo) - padDays * DAY, end: startOfDay(hi) + (padDays + 1) * DAY };
	}

	const GROUPERS = {
		none: function () { return ""; },
		workstation: function (t) { return t.workstation || "(No workstation)"; },
		work_order: function (t) { return t.work_order || "(No work order)"; },
		sales_order: function (t) { return t.sales_order || "(No sales order)"; },
		item_code: function (t) { return t.item_code || "(No item)"; },
		operation: function (t) { return t.operation || "(No operation)"; },
	};

	/** Flat list of rows (group headers + tasks) respecting collapsed groups. */
	const NO_MACHINE = "(No workstation)";

	/** [{key, y0, y1}] - the vertical extent of every group, header row included. */
	function groupSpans(layout) {
		const spans = [];
		layout.rows.forEach(function (r) {
			if (r.type === "group") spans.push({ key: r.key, y0: r.y, y1: layout.height });
		});
		for (let i = 0; i + 1 < spans.length; i++) spans[i].y1 = spans[i + 1].y0;
		return spans;
	}

	function layoutRows(tasks, groupBy, collapsed, rowH, groupH, extraGroups) {
		const keyOf = GROUPERS[groupBy] || GROUPERS.none;
		const rows = [];
		let y = 0;
		if (groupBy === "none" || !GROUPERS[groupBy]) {
			tasks.forEach(function (t) {
				rows.push({ type: "task", task: t, y: y, h: rowH });
				y += rowH;
			});
			return { rows: rows, height: y };
		}
		const groups = [];
		const byKey = {};
		tasks.forEach(function (t) {
			const k = keyOf(t);
			if (!byKey[k]) {
				byKey[k] = { key: k, tasks: [] };
				groups.push(byKey[k]);
			}
			byKey[k].tasks.push(t);
		});
		if (groupBy === "workstation") {
			(extraGroups || []).forEach(function (k) {
				if (!byKey[k]) {
					byKey[k] = { key: k, tasks: [] };
					groups.push(byKey[k]);
				}
			});
		}
		groups.sort(function (a, b) { return a.key < b.key ? -1 : a.key > b.key ? 1 : 0; });
		groups.forEach(function (g) {
			const isCollapsed = !!(collapsed && collapsed[g.key]);
			rows.push({ type: "group", key: g.key, count: g.tasks.length, collapsed: isCollapsed, y: y, h: groupH });
			y += groupH;
			if (!isCollapsed) {
				g.tasks.forEach(function (t) {
					rows.push({ type: "task", task: t, y: y, h: rowH, group: g.key });
					y += rowH;
				});
			}
		});
		return { rows: rows, height: y };
	}

	/** Elbow connector from the end of one bar to the start of the next. */
	function linkPath(x1, y1, x2, y2) {
		const gap = 10;
		if (x2 >= x1 + gap * 2) {
			const mx = x1 + gap;
			return "M" + x1 + "," + y1 + " H" + mx + " V" + y2 + " H" + x2;
		}
		// successor starts before predecessor ends: route around so the violation is visible
		const midY = y1 + (y2 > y1 ? 1 : -1) * 14;
		return "M" + x1 + "," + y1 + " H" + (x1 + gap) + " V" + midY + " H" + (x2 - gap) + " V" + y2 + " H" + x2;
	}

	const STATUS_META = {
		"Open": { cls: "st-open", glyph: "○", label: "Open (draft)" },
		"Material Transferred": { cls: "st-material", glyph: "◐", label: "Material transferred" },
		"Work In Progress": { cls: "st-wip", glyph: "▶", label: "Work in progress" },
		"On Hold": { cls: "st-hold", glyph: "Ⅱ", label: "On hold" },
		"Submitted": { cls: "st-done", glyph: "✔", label: "Submitted" },
		"Completed": { cls: "st-done", glyph: "✔", label: "Completed" },
		"Cancelled": { cls: "st-cancel", glyph: "✕", label: "Cancelled" },
	};

	function esc(value) {
		return String(value == null ? "" : value)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;")
			.replace(/'/g, "&#39;");
	}

	/** Two-line cell: start above end. */
	function twoLine(s, e) {
		if (parse(s) == null) return '<span class="text-muted">—</span>';
		return '<span class="ppc-2l">' + esc(formatShort(parse(s))) + "</span>" + '<span class="ppc-2l">' + (parse(e) != null ? esc(formatShort(parse(e))) : "…") + "</span>";
	}

	function lastWho(t) {
		if (!t.last_rescheduled_at) return '<span class="text-muted">—</span>';
		return '<span class="ppc-2l">' + esc(t.last_rescheduled_by || "") + '</span><span class="ppc-2l">' + esc(formatShort(parse(t.last_rescheduled_at))) + "</span>";
	}

	function syncCell(t) {
		const s = t.sync_status || "";
		const bad = s === "Failed" || s === "Drift";
		const warn = s === "Rescheduled" || s === "Estimated" || s === "No baseline";
		return '<span class="ppc-badge ' + (bad ? "bad" : warn ? "warn" : "ok") + '">' + esc(s || "—") + "</span>";
	}

	function rowStatusBadge(t) {
		const meta = STATUS_META[t.status] || STATUS_META.Open;
		return '<span class="ppc-badge ' + meta.cls + '">' + meta.glyph + " " + esc(t.status) + "</span>";
	}

	/** Task-grid column sets. `w` is the pixel width of fixed columns; the last column flexes. */
	/** 5, 2.5, 0.125 - quantities without trailing zeros. */
	function fmtQty(v) {
		if (v == null || v === "") return "";
		const n = Number(v);
		return isNaN(n) ? "" : String(Math.round(n * 1000) / 1000);
	}

	/** Output quantity of a Job Card: "10" (planned) or "4 / 10" once something is done. */
	function qtyText(t) {
		const planned = fmtQty(t.planned_qty);
		if (planned === "") return "";
		const done = Number(t.completed_qty) || 0;
		return done > 0 ? fmtQty(done) + " / " + planned : planned;
	}

	const COLUMN_SETS = {
		compact: [
			{ cls: "c-op", label: "Operation / Job Card", w: 170, html: function (t) { return "<b>" + esc(t.operation || "") + '</b> <span class="text-muted">' + esc(t.name) + "</span>"; }, title: function (t) { return t.name; } },
			{ cls: "c-item", label: "Finished good", w: 80, html: function (t) { return esc(t.item_code || ""); }, title: function (t) { return t.item_name || t.item_code || ""; } },
			{ cls: "c-qty", label: "Output qty", w: 76, html: function (t) { return esc(qtyText(t)) || "—"; }, title: function (t) { return qtyText(t) ? "Output quantity (done / planned): " + qtyText(t) : ""; } },
			{ cls: "c-ws", label: "Workstation", w: 80, html: function (t) { return esc(t.workstation || "—"); } },
			{ cls: "c-st", label: "Status", w: 0, html: rowStatusBadge },
		],
		schedule: [
			{ cls: "c-op", label: "Operation / Job Card", w: 160, html: function (t) { return "<b>" + esc(t.operation || "") + '</b><span class="ppc-2l text-muted">' + esc(t.name) + "</span>"; }, title: function (t) { return t.name + " · " + (t.workstation || ""); } },
			{ cls: "c-qty", label: "Output qty", w: 76, html: function (t) { return esc(qtyText(t)) || "—"; }, title: function (t) { return t.item_code ? t.item_code + (qtyText(t) ? " × " + qtyText(t) : "") : ""; } },
			{ cls: "c-orig", label: "Original start / end", w: 112, html: function (t) { return twoLine(t.original_start, t.original_end); }, title: function (t) { return t.baseline_label || ""; } },
			{ cls: "c-cur", label: "Current start / end", w: 112, html: function (t) { return twoLine(t.revised_start, t.revised_end); } },
			{ cls: "c-act", label: "Actual start / end", w: 112, html: function (t) { return twoLine(t.actual_start, t.actual_end); } },
			{ cls: "c-rev", label: "Rev", w: 40, html: function (t) { return t.has_baseline === false ? '<span class="text-muted">—</span>' : esc(t.revision || 0); } },
			{ cls: "c-last", label: "Last rescheduled", w: 124, html: lastWho },
			{ cls: "c-sync", label: "Sync status", w: 96, html: syncCell },
			{ cls: "c-st", label: "Status", w: 0, html: rowStatusBadge },
		],
	};

	const COMPACT_W = 570;
	const LABEL_W = 270; // room the bar's text needs; a shorter bar shows it beside itself
	const SCHEDULE_W = 876;

	/* ------------------------------------------------------------------------------ component */
	const ROW_H = 40;
	const BAR_TOP = 5; // bar is 20px high
	const BAR_H = 20;
	const GHOST_TOP = 28; // original-plan ghost bar sits under the bar
	const ACTUAL_TOP = 32; // actual-execution line under the ghost
	const GROUP_H = 28;
	const HEAD_TOP_H = 22;
	const HEAD_MID_H = 22;
	const HEAD_BOTTOM_H = 22; // one-line cells ("Wed 21 Oct")
	const HEAD_BOTTOM_2_H = 36; // two-line cells ("21" over "Oct")
	
	function PPCGantt(container, options) {
		this.el = typeof container === "string" ? document.querySelector(container) : container;
		this.opts = Object.assign(
			{
				height: 560,
				scale: "week",
				groupBy: "workstation",
				now: null, // server "now" (naive string); falls back to browser clock
				canEdit: true,
				canChangeWorkstation: false, // dragging a bar to another Workstation row (needs groupBy "workstation")
				timezoneLabel: "", // shown in the header tooltips, e.g. the site time zone
				gridMode: "compact", // "compact" | "schedule" (original / current / actual / revision columns)
				tooltip: null,
				onChange: null,
				onEdit: null,
				onSelect: null,
			},
			options || {}
		);
		this.scale = SCALES[this.opts.scale] ? this.opts.scale : "week";
		this.groupBy = this.opts.groupBy;
		this.tasks = [];
		this.links = [];
		this.search = "";
		this.collapsed = {};
		this.selected = null;
		this.showGrid = true;
		this.gridMode = this.opts.gridMode === "schedule" ? "schedule" : "compact";
		this._loadedAt = Date.now();
		this._serverNow = parse(this.opts.now);
		this._drag = null;
		this._build();
		this._syncDragState();
	}

	PPCGantt.prototype._build = function () {
		const self = this;
		this.el.classList.add("ppc-gantt");
		this.el.__ppcGantt = this; // lets browser tests and diagnostics reach the instance
		this.el.innerHTML =
			'<div class="ppc-gantt-toolbar" role="toolbar" aria-label="Gantt controls"></div>' +
			'<div class="ppc-gantt-main" style="height:' + this.opts.height + 'px">' +
			'<div class="ppc-gantt-left"><div class="ppc-gantt-left-head"></div><div class="ppc-gantt-left-clip"><div class="ppc-gantt-left-rows"></div></div></div>' +
			'<div class="ppc-gantt-right" tabindex="0"><div class="ppc-gantt-inner"><div class="ppc-gantt-head"></div><div class="ppc-gantt-rows"></div></div></div>' +
			"</div>" +
			'<div class="ppc-gantt-legend"></div>';
		this.$toolbar = this.el.querySelector(".ppc-gantt-toolbar");
		this.$main = this.el.querySelector(".ppc-gantt-main");
		this.$left = this.el.querySelector(".ppc-gantt-left");
		this.$leftHead = this.el.querySelector(".ppc-gantt-left-head");
		this.$leftRows = this.el.querySelector(".ppc-gantt-left-rows");
		this.$right = this.el.querySelector(".ppc-gantt-right");
		this.$inner = this.el.querySelector(".ppc-gantt-inner");
		this.$head = this.el.querySelector(".ppc-gantt-head");
		this.$rows = this.el.querySelector(".ppc-gantt-rows");
		this.$legend = this.el.querySelector(".ppc-gantt-legend");
		this.$tip = document.createElement("div");
		this.$tip.className = "ppc-gantt-tip";
		this.$tip.setAttribute("role", "tooltip");
		this.$tip.style.display = "none";
		document.body.appendChild(this.$tip);

		this.$right.addEventListener("scroll", function () {
			self.$leftRows.style.transform = "translateY(" + -self.$right.scrollTop + "px)";
		});
		this.$left.addEventListener("wheel", function (e) {
			self.$right.scrollTop += e.deltaY;
			e.preventDefault();
		}, { passive: false });
		this.$right.addEventListener("wheel", function (e) {
			if (e.ctrlKey) {
				e.preventDefault();
				self.zoom(e.deltaY < 0 ? 1 : -1);
			}
		}, { passive: false });
		this._onMove = function (e) { self._dragMove(e); };
		this._onUp = function (e) { self._dragEnd(e); };
		// Escape (or the browser taking the pointer away) abandons a drag in progress - never a change that is already
		// on its way to the server: that one is answered, and only its own answer ends it.
		this._onKey = function (e) { if (e.key === "Escape" && self._drag && !self._drag.pending) self._dragCancel(); };
		this._onPointerCancel = function () { if (self._drag && !self._drag.pending) self._dragCancel(); };
		document.addEventListener("pointermove", this._onMove);
		document.addEventListener("pointerup", this._onUp);
		document.addEventListener("pointercancel", this._onPointerCancel);
		document.addEventListener("keydown", this._onKey);

		this._renderToolbar();
		this._renderLegend();
	};

	/** Observable interaction state: `data-ppc-drag` on the root is idle | dragging | pending (a change is being saved). */
	PPCGantt.prototype._syncDragState = function () {
		const d = this._drag;
		if (this.el && this.el.dataset) this.el.dataset.ppcDrag = d ? (d.pending ? "pending" : "dragging") : "idle";
	};

	PPCGantt.prototype.destroy = function () {
		document.removeEventListener("pointermove", this._onMove);
		document.removeEventListener("pointerup", this._onUp);
		document.removeEventListener("pointercancel", this._onPointerCancel);
		document.removeEventListener("keydown", this._onKey);
		if (this.$tip && this.$tip.parentNode) this.$tip.parentNode.removeChild(this.$tip);
		this.el.innerHTML = "";
		this.el.classList.remove("ppc-gantt");
	};

	PPCGantt.prototype.now = function () {
		if (this._serverNow != null) return this._serverNow + (Date.now() - this._loadedAt);
		const d = new Date();
		return Date.UTC(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds());
	};

	/* --------------------------------------------------------------------------------- data */
	PPCGantt.prototype.setData = function (data) {
		this.tasks = (data && data.tasks) || [];
		this.links = (data && data.links) || [];
		this.truncated = !!(data && data.truncated);
		this.machines = ((data && data.workstations) || []).map(function (w) { return typeof w === "string" ? w : w.name; });
		this.render();
	};

	/** Show every Workstation as a row, also those with no operation in view, so a bar can be dropped on them. */
	PPCGantt.prototype.setShowAllMachines = function (on) {
		this.showAllMachines = !!on;
		this.render();
	};

	PPCGantt.prototype.visibleTasks = function () {
		const q = this.search.trim().toLowerCase();
		return this.tasks.filter(function (t) {
			if (parse(t.revised_start) == null || parse(t.revised_end) == null) return false;
			if (!q) return true;
			return [t.name, t.operation, t.workstation, t.work_order, t.sales_order, t.item_code, t.item_name, t.customer_name, t.status]
				.some(function (v) { return v && String(v).toLowerCase().indexOf(q) !== -1; });
		});
	};

	PPCGantt.prototype.updateTask = function (name, patch) {
		const t = this.tasks.find(function (x) { return x.name === name; });
		if (t) Object.assign(t, patch);
		this.render();
	};

	/* ----------------------------------------------------------------------------- toolbar */
	PPCGantt.prototype._renderToolbar = function () {
		const self = this;
		const scaleBtns = SCALE_ORDER.slice().reverse().map(function (k) {
			return '<button type="button" class="btn btn-xs btn-default ppc-scale" data-scale="' + k + '" aria-pressed="false">' + SCALES[k].label + "</button>";
		}).join("");
		const groupOpts = [["workstation", "Workstation"], ["work_order", "Work Order"], ["sales_order", "Sales Order"], ["item_code", "Finished Good"], ["operation", "Operation"], ["none", "No grouping"]]
			.map(function (g) { return '<option value="' + g[0] + '">' + g[1] + "</option>"; }).join("");
		this.$toolbar.innerHTML =
			'<div class="btn-group" role="group" aria-label="Time scale">' + scaleBtns + "</div>" +
			'<div class="btn-group" role="group" aria-label="Zoom">' +
			'<button type="button" class="btn btn-xs btn-default ppc-zoom-out" title="Zoom out" aria-label="Zoom out">−</button>' +
			'<button type="button" class="btn btn-xs btn-default ppc-zoom-in" title="Zoom in" aria-label="Zoom in">+</button></div>' +
			'<button type="button" class="btn btn-xs btn-default ppc-today">Today</button>' +
			'<label class="ppc-toolbar-field">Group by <select class="form-control input-xs ppc-group">' + groupOpts + "</select></label>" +
			'<input type="search" class="form-control input-xs ppc-gsearch" placeholder="Search bars…" aria-label="Search bars">' +
			'<button type="button" class="btn btn-xs btn-default ppc-toggle-grid" title="Show or hide the task list">Task list</button>' +
			'<button type="button" class="btn btn-xs btn-default ppc-toggle-cols" aria-pressed="false" title="Show original, current and actual dates, revision count, last change and sync status">Schedule columns</button>' +
			'<span class="ppc-gantt-count text-muted"></span>';
		this.$toolbar.querySelectorAll(".ppc-scale").forEach(function (b) {
			b.addEventListener("click", function () { self.setScale(b.dataset.scale); });
		});
		this.$toolbar.querySelector(".ppc-zoom-in").addEventListener("click", function () { self.zoom(1); });
		this.$toolbar.querySelector(".ppc-zoom-out").addEventListener("click", function () { self.zoom(-1); });
		this.$toolbar.querySelector(".ppc-today").addEventListener("click", function () { self.goToToday(); });
		const group = this.$toolbar.querySelector(".ppc-group");
		group.value = this.groupBy;
		group.addEventListener("change", function () { self.setGroupBy(group.value); });
		let timer = null;
		this.$toolbar.querySelector(".ppc-gsearch").addEventListener("input", function (e) {
			clearTimeout(timer);
			timer = setTimeout(function () { self.search = e.target.value; self.render(); }, 150);
		});
		this.$toolbar.querySelector(".ppc-toggle-grid").addEventListener("click", function () {
			self.showGrid = !self.showGrid;
			self.render();
		});
		this.$toolbar.querySelector(".ppc-toggle-cols").addEventListener("click", function () {
			self.setGridMode(self.gridMode === "schedule" ? "compact" : "schedule");
		});
	};

	PPCGantt.prototype._renderLegend = function () {
		const items = [
			["st-open", "○", "Open (draft)"], ["st-material", "◐", "Material transferred"], ["st-wip", "▶", "Work in progress"],
			["st-hold", "Ⅱ", "On hold"], ["st-done", "✔", "Completed / submitted"],
		];
		this.$legend.innerHTML =
			items.map(function (i) { return '<span class="ppc-legend-item"><span class="ppc-swatch ' + i[0] + '">' + i[1] + "</span> " + i[2] + "</span>"; }).join("") +
			'<span class="ppc-legend-item"><span class="ppc-swatch ppc-sw-orig"></span> Original plan (baseline)</span>' +
				'<span class="ppc-legend-item"><span class="ppc-swatch ppc-sw-actual"></span> Actual execution</span>' +
				'<span class="ppc-legend-item"><span class="ppc-rev">R2</span> Rescheduled (revision count)</span>' +
				'<span class="ppc-legend-item"><span class="ppc-sync-bad">⚠</span> Sync failed / out of sync</span>' +
			'<span class="ppc-legend-item"><span class="ppc-swatch ppc-sw-est"></span> Estimated dates</span>' +
			'<span class="ppc-legend-item"><span class="ppc-swatch ppc-sw-conflict"></span> Conflict</span>' +
			'<span class="ppc-legend-item">🔒 Locked (cannot be moved)</span>';
	};

	PPCGantt.prototype.setScale = function (key) {
		if (!SCALES[key]) return;
		const centre = this._visibleCentreTime();
		this.scale = key;
		this.render();
		if (centre != null) this._scrollToTime(centre);
	};

	PPCGantt.prototype.zoom = function (dir) {
		const i = SCALE_ORDER.indexOf(this.scale) + (dir > 0 ? 1 : -1);
		if (i >= 0 && i < SCALE_ORDER.length) this.setScale(SCALE_ORDER[i]);
	};

	PPCGantt.prototype.setGridMode = function (mode) {
		this.gridMode = mode === "schedule" ? "schedule" : "compact";
		if (this.gridMode === "schedule") this.showGrid = true;
		this.render();
	};

	PPCGantt.prototype.setGroupBy = function (key) {
		this.groupBy = GROUPERS[key] ? key : "none";
		this.render();
	};

	PPCGantt.prototype.goToToday = function () {
		this._scrollToTime(this.now());
	};

	PPCGantt.prototype._visibleCentreTime = function () {
		if (!this._range || !this.$right) return null;
		const px = SCALES[this._renderedScale || this.scale].pxPerDay;
		return timeOf(this.$right.scrollLeft + this.$right.clientWidth / 2, this._range.start, px);
	};

	PPCGantt.prototype._scrollToTime = function (ms) {
		if (!this._range) return;
		const px = SCALES[this.scale].pxPerDay;
		this.$right.scrollLeft = Math.max(0, xOf(ms, this._range.start, px) - this.$right.clientWidth / 2);
	};

	/* ----------------------------------------------------------------------------- render */
	PPCGantt.prototype.render = function () {
		const sc = SCALES[this.scale];
		this._renderedScale = this.scale;
		const tasks = this.visibleTasks();
		this.$toolbar.querySelectorAll(".ppc-scale").forEach(function (b) {
			const on = b.dataset.scale === sc.key;
			b.classList.toggle("active", on);
			b.setAttribute("aria-pressed", on ? "true" : "false");
		});
		this.$toolbar.querySelector(".ppc-group").value = this.groupBy;
		const count = this.$toolbar.querySelector(".ppc-gantt-count");
		count.textContent = tasks.length + " bar" + (tasks.length === 1 ? "" : "s") + (this.truncated ? " (list truncated - narrow the filters)" : "");
		this.$left.style.display = this.showGrid ? "" : "none";
		const gw = this.gridMode === "schedule" ? SCHEDULE_W : COMPACT_W;
		this.$left.style.flex = "0 0 " + gw + "px";
		this.$left.style.width = gw + "px";
		this.$left.classList.toggle("wide", this.gridMode === "schedule");
		const colsBtn = this.$toolbar.querySelector(".ppc-toggle-cols");
		colsBtn.classList.toggle("active", this.gridMode === "schedule");
		colsBtn.setAttribute("aria-pressed", this.gridMode === "schedule" ? "true" : "false");

		const range = computeRange(tasks, this.scale === "quarter" ? 14 : 2) || {
			start: startOfDay(this.now()) - 2 * DAY,
			end: startOfDay(this.now()) + 14 * DAY,
		};
		const nowMs = this.now();
		range.start = Math.min(range.start, startOfDay(nowMs) - DAY);
		range.end = Math.max(range.end, startOfDay(nowMs) + 2 * DAY);
		this._range = range;
		const width = Math.ceil(xOf(range.end, range.start, sc.pxPerDay));

		const extra = this.showAllMachines && this.groupBy === "workstation" ? this.machines || [] : null;
		const layout = layoutRows(tasks, this.groupBy, this.collapsed, ROW_H, GROUP_H, extra);
		this._layout = layout;
		this._spans = groupSpans(layout);
		this.$inner.style.width = width + "px";
		this.$rows.style.height = layout.height + "px";
		this.$rows.style.width = width + "px";

		this._renderHead(range, sc, width);
		this._renderGridColumns(range, sc);
		this._renderLeft(layout);
		this._renderBars(layout, range, sc, width);
	};

	PPCGantt.prototype._renderHead = function (range, sc, width) {
		const tiers = headerTiers(sc.key, range.start, range.end, { nowMs: this.now(), tz: this.opts.timezoneLabel });
		const cell = function (c, cls, h) {
			const left = Math.max(0, xOf(c.start, range.start, sc.pxPerDay));
			const right = Math.min(width, xOf(c.end, range.start, sc.pxPerDay));
			return '<div class="ppc-hcell ' + cls + (c.weekend ? " weekend" : "") + (c.today ? " today" : "") + (c.sub ? " two" : "") + '" title="' + esc(c.title || "") +
				'" style="left:' + left + "px;width:" + Math.max(0, right - left) + "px;height:" + h + 'px">' + esc(c.label) + (c.sub ? '<span class="sub">' + esc(c.sub) + "</span>" : "") + "</div>";
		};
		const bottomH = tiers.bottom.some(function (c) { return c.sub; }) ? HEAD_BOTTOM_2_H : HEAD_BOTTOM_H;
		const rows = [["htop", "top", tiers.top, HEAD_TOP_H]];
		if (tiers.mid) rows.push(["hmid", "mid", tiers.mid, HEAD_MID_H]);
		rows.push(["hbottom", "bottom", tiers.bottom, bottomH]);
		const headH = rows.reduce(function (n, r) { return n + r[3]; }, 0);
		this._headH = headH;
		this.$head.style.width = width + "px";
		this.$head.style.height = headH + "px";
		this.$head.innerHTML = rows.map(function (r) {
			return '<div class="ppc-' + r[0] + '" style="height:' + r[3] + 'px">' + r[2].map(function (c) { return cell(c, r[1], r[3]); }).join("") + "</div>";
		}).join("");
		this.$leftHead.style.height = headH + "px";
		this.$leftHead.innerHTML = COLUMN_SETS[this.gridMode].map(function (c) {
			return '<div class="ppc-lcol ' + c.cls + '">' + esc(c.label) + "</div>";
		}).join("");
		this._tiers = tiers;
	};

	PPCGantt.prototype._renderGridColumns = function (range, sc) {
		// weekend shading + vertical grid lines come from the bottom header tier
		const bg = this._tiers.bottom.map(function (c) {
			const left = xOf(c.start, range.start, sc.pxPerDay);
			const w = xOf(c.end, range.start, sc.pxPerDay) - left;
			return '<div class="ppc-vline' + (c.weekend ? " weekend" : "") + '" style="left:' + left + "px;width:" + w + 'px"></div>';
		}).join("");
		this._gridHtml = bg;
	};

	PPCGantt.prototype._renderLeft = function (layout) {
		const self = this;
		const html = layout.rows.map(function (r) {
			if (r.type === "group") {
				return '<div class="ppc-lrow group" style="height:' + r.h + 'px" data-group="' + esc(r.key) + '" role="button" tabindex="0" aria-expanded="' + !r.collapsed + '">' +
					'<span class="ppc-caret">' + (r.collapsed ? "▸" : "▾") + "</span> " + esc(r.key) + ' <span class="text-muted">(' + r.count + ")</span></div>";
			}
				const t = r.task;
				const cols = COLUMN_SETS[self.gridMode].map(function (c) {
					return '<div class="ppc-lcol ' + c.cls + '"' + (c.title ? ' title="' + esc(c.title(t)) + '"' : "") + ">" + c.html(t) + "</div>";
				}).join("");
				return '<div class="ppc-lrow task' + (self.selected === t.name ? " selected" : "") + '" style="height:' + r.h + 'px" data-name="' + esc(t.name) + '">' + cols + "</div>";
		}).join("");
		this.$leftRows.style.height = layout.height + "px";
		this.$leftRows.innerHTML = html;
		this.$leftRows.querySelectorAll(".ppc-lrow.group").forEach(function (g) {
			const toggle = function () {
				self.collapsed[g.dataset.group] = !self.collapsed[g.dataset.group];
				self.render();
			};
			g.addEventListener("click", toggle);
			g.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
		});
		this.$leftRows.querySelectorAll(".ppc-lrow.task").forEach(function (row) {
			row.addEventListener("click", function () { self._select(row.dataset.name, true); });
			row.addEventListener("dblclick", function () { self._edit(row.dataset.name); });
		});
	};

	PPCGantt.prototype._renderBars = function (layout, range, sc, width) {
		const self = this;
		const pos = {};
		const html = [this._gridHtml];
		layout.rows.forEach(function (r) {
			html.push('<div class="ppc-hrow' + (r.type === "group" ? " group" : "") + '" style="top:' + r.y + "px;height:" + r.h + 'px"></div>');
			if (r.type !== "task") return;
			const t = r.task;
			const s = parse(t.revised_start);
			const e = parse(t.revised_end);
			const x = xOf(s, range.start, sc.pxPerDay);
			const w = Math.max(6, xOf(e, range.start, sc.pxPerDay) - x);
			const y = r.y + BAR_TOP;
			pos[t.name] = { x: x, w: w, y: y, mid: y + BAR_H / 2 };
			const meta = STATUS_META[t.status] || STATUS_META.Open;
			const editable = self.opts.canEdit && t.movable;
			const cls = ["ppc-bar", meta.cls, editable ? "editable" : "locked", t.is_estimated ? "estimated" : "", t.conflict ? "conflict" : "", t.overdue ? "overdue" : "", w < LABEL_W ? "narrow" : "", w < LABEL_W && x + w + LABEL_W > width && x > LABEL_W ? "flip" : "", self.selected === t.name ? "selected" : ""].filter(Boolean).join(" ");
			const os = parse(t.original_start);
			const oe = parse(t.original_end);
			if (os != null && oe != null && (os !== s || oe !== e)) {
				const ox = xOf(os, range.start, sc.pxPerDay);
				const ow = Math.max(6, xOf(oe, range.start, sc.pxPerDay) - ox);
				html.push('<div class="ppc-orig" style="left:' + ox + "px;width:" + ow + "px;top:" + (r.y + GHOST_TOP) + 'px" title="Original plan' + (t.baseline_label ? " (" + esc(t.baseline_label) + ")" : "") + ": " + esc(formatFull(os)) + " → " + esc(formatFull(oe)) + '"></div>');
			}
			const as = parse(t.actual_start);
			if (as != null) {
				const running = parse(t.actual_end) == null;
				const ae = running ? Math.max(as, self.now()) : parse(t.actual_end);
				const ax = xOf(as, range.start, sc.pxPerDay);
				const aw = Math.max(4, xOf(ae, range.start, sc.pxPerDay) - ax);
				html.push('<div class="ppc-actual' + (running ? " running" : "") + '" style="left:' + ax + "px;width:" + aw + "px;top:" + (r.y + ACTUAL_TOP) + 'px" title="Actual: ' +
					esc(formatFull(as)) + " → " + (running ? "in progress" : esc(formatFull(ae))) + '"></div>');
			}
			const flagged = t.sync_status === "Failed" || t.sync_status === "Drift";
			html.push(
				'<div class="' + cls + '" data-name="' + esc(t.name) + '" style="left:' + x + "px;width:" + w + "px;top:" + y + 'px" tabindex="0" role="button" aria-label="' +
				esc((t.operation || "") + ", " + t.name + ", " + (t.workstation || "no workstation") + (t.item_code ? ", " + t.item_code + (qtyText(t) ? " x " + qtyText(t) : "") : "") + ", " + t.status + ", " + formatFull(s) + " to " + formatFull(e) + (t.conflict ? ", conflict" : "")) + '">' +
				(editable ? '<span class="ppc-handle left" data-handle="resize-left"></span>' : "") +
				'<span class="ppc-bar-label">' + meta.glyph + " " + esc(t.operation || t.name) + " · " + esc(t.workstation || "no machine") +
				(t.item_code ? " · " + esc(t.item_code) + (qtyText(t) ? " × " + esc(qtyText(t)) : "") : "") + "</span>" +
				(t.revision > 0 ? '<span class="ppc-rev" title="Rescheduled ' + esc(t.revision) + (t.revision === 1 ? " time" : " times") + '">R' + esc(t.revision) + "</span>" : "") +
				(flagged ? '<span class="ppc-sync-bad" title="' + (t.sync_status === "Failed" ? "Last synchronisation failed" : "Job Card and accepted schedule differ") + '">⚠</span>' : "") +
				(!editable ? '<span class="ppc-lock" aria-hidden="true">🔒</span>' : "") +
				(editable ? '<span class="ppc-handle right" data-handle="resize-right"></span>' : "") +
				"</div>"
			);
		});
		// today marker
		const nowX = xOf(this.now(), range.start, sc.pxPerDay);
		html.push('<div class="ppc-today-line" style="left:' + nowX + 'px" title="Now"><span>Today</span></div>');
		// dependencies
		const paths = [];
		this.links.forEach(function (l) {
			const a = pos[l.from];
			const b = pos[l.to];
			if (!a || !b) return;
			const x1 = a.x + a.w;
			const violated = b.x < x1;
			paths.push('<path d="' + linkPath(x1, a.mid, b.x, b.mid) + '" class="ppc-link' + (violated ? " violated" : "") + '" marker-end="url(#ppc-arrow' + (violated ? "-bad" : "") + ')"/>');
		});
		html.push(
			'<svg class="ppc-links" width="' + width + '" height="' + layout.height + '" aria-hidden="true">' +
			'<defs><marker id="ppc-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L8,4 L0,8 z" class="ppc-arrowhead"/></marker>' +
			'<marker id="ppc-arrow-bad" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L8,4 L0,8 z" class="ppc-arrowhead bad"/></marker></defs>' +
			paths.join("") + "</svg>"
		);
		this.$rows.innerHTML = html.join("");
		this._pos = pos;
		this.$rows.querySelectorAll(".ppc-bar").forEach(function (bar) {
			const name = bar.dataset.name;
			bar.addEventListener("pointerdown", function (e) { self._dragStart(e, bar, name); });
			bar.addEventListener("click", function () { self._select(name, false); });
			bar.addEventListener("dblclick", function () { self._edit(name); });
			bar.addEventListener("keydown", function (e) {
				if (e.key === "Enter" || e.key === " ") { e.preventDefault(); self._edit(name); }
			});
			bar.addEventListener("mouseenter", function (e) { self._showTip(e, name); });
			bar.addEventListener("mousemove", function (e) { self._moveTip(e); });
			bar.addEventListener("mouseleave", function () { self._hideTip(); });
			bar.addEventListener("focus", function () { self._showTip(null, name, bar); });
			bar.addEventListener("blur", function () { self._hideTip(); });
		});
		if (this._drag) this._rebindDrag();
	};

	/** A reload re-renders every bar. If a drag or a save is in flight, the *new* bar element must carry on as the one being
	 *  dragged / saved - otherwise the screen shows an idle chart while a change is on its way to the server (and a
	 *  half-finished drag would keep moving a bar nobody can see). */
	PPCGantt.prototype._rebindDrag = function () {
		const d = this._drag;
		const bar = Array.prototype.find.call(this.$rows.querySelectorAll(".ppc-bar"), function (b) { return b.dataset.name === d.name; });
		if (!bar) return; // the card is no longer in the (filtered) list: nothing to show
		d.bar = bar;
		bar.classList.add(d.pending ? "pending" : "dragging");
		const x = xOf(d.cur.start, this._range.start, d.sc.pxPerDay);
		bar.style.left = x + "px";
		bar.style.width = Math.max(6, xOf(d.cur.end, this._range.start, d.sc.pxPerDay) - x) + "px";
		if (d.vertical && d.targetWs) { // the freshly built chart carries on showing where the bar is being dropped
			bar.style.transform = "translateY(" + d.dy + "px)";
			const span = (this._spans || []).find(function (sp) { return sp.key === d.targetWs; });
			if (span) this._showDropBand(span);
		}
	};

	PPCGantt.prototype.taskByName = function (name) {
		return this.tasks.find(function (t) { return t.name === name; });
	};

	PPCGantt.prototype._select = function (name, scroll) {
		this.selected = name;
		this.$rows.querySelectorAll(".ppc-bar.selected").forEach(function (b) { b.classList.remove("selected"); });
		this.$leftRows.querySelectorAll(".ppc-lrow.selected").forEach(function (b) { b.classList.remove("selected"); });
		const bar = this.$rows.querySelector('.ppc-bar[data-name="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
		if (bar) bar.classList.add("selected");
		const row = this.$leftRows.querySelector('.ppc-lrow[data-name="' + (window.CSS && CSS.escape ? CSS.escape(name) : name) + '"]');
		if (row) row.classList.add("selected");
		if (scroll && this._pos && this._pos[name]) {
			this.$right.scrollLeft = Math.max(0, this._pos[name].x - this.$right.clientWidth / 3);
		}
		if (this.opts.onSelect) this.opts.onSelect(this.taskByName(name));
	};

	PPCGantt.prototype._edit = function (name) {
		const t = this.taskByName(name);
		if (t && this.opts.onEdit) this.opts.onEdit(t);
	};

	/* --------------------------------------------------------------------------- tooltip */
	PPCGantt.prototype._tipHtml = function (t) {
		if (this.opts.tooltip) return this.opts.tooltip(t, { esc: esc, formatShort: formatShort, formatFull: formatFull, formatDuration: formatDuration });
		return "<b>" + esc(t.name) + "</b><br>" + esc(t.operation) + " · " + esc(t.workstation || "") + " · " + esc(t.status) + "<br>" +
			(t.item_code ? esc(t.item_code) + (qtyText(t) ? " × " + esc(qtyText(t)) : "") + "<br>" : "") +
			esc(formatFull(parse(t.revised_start))) + " → " + esc(formatFull(parse(t.revised_end)));
	};

	PPCGantt.prototype._showTip = function (e, name, anchor) {
		if (this._drag) return;
		const t = this.taskByName(name);
		if (!t) return;
		this.$tip.innerHTML = this._tipHtml(t);
		this.$tip.style.display = "block";
		if (e) this._moveTip(e);
		else if (anchor) {
			const r = anchor.getBoundingClientRect();
			this.$tip.style.left = r.left + "px";
			this.$tip.style.top = r.bottom + 6 + "px";
		}
	};

	PPCGantt.prototype._moveTip = function (e) {
		const pad = 14;
		let left = e.clientX + pad;
		let top = e.clientY + pad;
		const w = this.$tip.offsetWidth || 280;
		const h = this.$tip.offsetHeight || 160;
		if (left + w > window.innerWidth - 8) left = e.clientX - w - pad;
		if (top + h > window.innerHeight - 8) top = e.clientY - h - pad;
		this.$tip.style.left = Math.max(4, left) + "px";
		this.$tip.style.top = Math.max(4, top) + "px";
	};

	PPCGantt.prototype._hideTip = function () {
		this.$tip.style.display = "none";
	};

	/* ------------------------------------------------------------------------------ drag */
	PPCGantt.prototype._dragStart = function (e, bar, name) {
		const t = this.taskByName(name);
		if (!t || !this.opts.canEdit || !t.movable || (e.button != null && e.button !== 0)) return;
		if (this._drag && this._drag.pending) return; // a change is still being saved: one at a time, and its answer must not hit a new drag
		if (this._drag) this._drag.bar.classList.remove("dragging"); // a leftover drag whose pointerup was lost: replace it
		const handle = e.target && e.target.dataset ? e.target.dataset.handle : null;
		const mode = handle || "move";
		const sc = SCALES[this.scale];
		this._hideTip();
		this._drag = {
			name: name, bar: bar, mode: mode, x0: e.clientX, scroll0: this.$right.scrollLeft,
			start0: parse(t.revised_start), end0: parse(t.revised_end), sc: sc, moved: false, pending: false,
			// a move (not a resize) may also change the machine, by dropping the bar on another Workstation row
			y0: e.clientY, dy: 0, sourceWs: t.workstation || null, targetWs: null,
			vertical: mode === "move" && this.groupBy === "workstation" && !!this.opts.canChangeWorkstation,
		};
		this._drag.cur = { start: this._drag.start0, end: this._drag.end0 };
		bar.classList.add("dragging");
		this._syncDragState();
		if (bar.setPointerCapture && e.pointerId != null) {
			try { bar.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
		}
		e.preventDefault();
	};

	PPCGantt.prototype._dragMove = function (e) {
		const d = this._drag;
		if (!d || d.pending) return;
		const dx = e.clientX - d.x0 + (this.$right.scrollLeft - d.scroll0);
		const deltaMs = (dx / d.sc.pxPerDay) * DAY;
		const prop = applyDrag(d.mode, d.start0, d.end0, deltaMs, d.sc.snapMin);
		d.cur = prop;
		d.moved = prop.start !== d.start0 || prop.end !== d.end0;
		const x = xOf(prop.start, this._range.start, d.sc.pxPerDay);
		const w = Math.max(6, xOf(prop.end, this._range.start, d.sc.pxPerDay) - x);
		d.bar.style.left = x + "px";
		d.bar.style.width = w + "px";
		if (d.vertical) this._dragTarget(e, d);
		this._dragLabel(e, prop);
		// auto scroll near the edges
		const r = this.$right.getBoundingClientRect();
		if (r.width) {
			if (e.clientX > r.right - 40) this.$right.scrollLeft += 18;
			else if (e.clientX < r.left + 40) this.$right.scrollLeft -= 18;
		}
	};

	/** Which Workstation row is the pointer over? Follows the pointer with the bar and lights up the row it would land on. */
	PPCGantt.prototype._dragTarget = function (e, d) {
		const rect = this.$rows.getBoundingClientRect();
		const y = e.clientY - rect.top;
		const span = (this._spans || []).find(function (sp) { return y >= sp.y0 && y < sp.y1; });
		d.dy = e.clientY - d.y0;
		d.bar.style.transform = Math.abs(d.dy) > 3 ? "translateY(" + d.dy + "px)" : "";
		const home = d.sourceWs || NO_MACHINE;
		const target = span && span.key !== NO_MACHINE && span.key !== home ? span.key : null;
		if (target !== d.targetWs) {
			d.targetWs = target;
			this._showDropBand(target ? span : null);
		}
	};

	PPCGantt.prototype._showDropBand = function (span) {
		this._clearDropBand();
		if (!span) return;
		const band = document.createElement("div");
		band.className = "ppc-drop-band";
		band.style.top = span.y0 + "px";
		band.style.height = span.y1 - span.y0 + "px";
		band.innerHTML = '<span class="ppc-drop-label">→ ' + esc(span.key) + "</span>";
		this.$rows.appendChild(band);
		const row = this.$leftRows.querySelector('.ppc-lrow.group[data-group="' + (window.CSS && CSS.escape ? CSS.escape(span.key) : span.key) + '"]');
		if (row) row.classList.add("ppc-drop-target");
	};

	PPCGantt.prototype._clearDropBand = function () {
		this.el.querySelectorAll(".ppc-drop-band").forEach(function (b) { b.parentNode.removeChild(b); });
		this.el.querySelectorAll(".ppc-drop-target").forEach(function (r) { r.classList.remove("ppc-drop-target"); });
	};

	PPCGantt.prototype._dragLabel = function (e, prop) {
		this.$tip.style.display = "block";
		this.$tip.innerHTML = "<b>" + esc(formatFull(prop.start)) + " → " + esc(formatFull(prop.end)) + "</b><br><span class=\"text-muted\">" + esc(formatDuration(prop.end - prop.start)) + (this._drag && this._drag.targetWs ? "<br>→ " + esc(this._drag.targetWs) : "") + "</span>";
		this._moveTip(e);
	};

	PPCGantt.prototype._dragCancel = function () {
		const d = this._drag;
		if (!d) return;
		this._drag = null;
		this._syncDragState();
		this._hideTip();
		this.render();
	};

	PPCGantt.prototype._dragEnd = function () {
		const d = this._drag;
		if (!d || d.pending) return;
		const self = this;
		this._hideTip();
		const machineChanged = !!d.targetWs;
		if ((!d.moved && !machineChanged) || !this.opts.onChange) {
			this._drag = null;
			this._syncDragState();
			d.bar.classList.remove("dragging");
			d.bar.style.transform = "";
			this._clearDropBand();
			// A press that did not move anything is a click: do not rebuild the chart here, or the bar the browser is
			// about to deliver the click to no longer exists and the click (selection, detail drawer) is lost.
			return;
		}
		d.pending = true;
		this._syncDragState();
		d.bar.classList.add("pending");
		const evt = {
			task: this.taskByName(d.name),
			mode: d.mode === "move" ? "move" : "resize",
			start: format(d.cur.start),
			end: format(d.cur.end),
			previous: { start: format(d.start0), end: format(d.end0) },
		};
		if (machineChanged) {
			evt.workstation = d.targetWs; // the Workstation row the bar was dropped on
			evt.previousWorkstation = d.sourceWs;
		}
		Promise.resolve(this.opts.onChange(evt)).then(
			function (res) {
				if (self._drag === d) self._drag = null; // only ever end the drag this answer belongs to
				self._syncDragState();
				if (res && res.ok) {
					const patch = { revised_start: res.start || evt.start, revised_end: res.end || evt.end };
					if (res.modified) patch.modified = res.modified;
					if (res.workstation) patch.workstation = res.workstation;
					Object.assign(self.taskByName(d.name), patch);
				}
				self.render(); // success: new position; refusal/failure: snaps back to the stored dates
			},
			function () {
				if (self._drag === d) self._drag = null;
				self._syncDragState();
				self.render();
			}
		);
	};

	/* ------------------------------------------------------------------------------ export */
	const api = {
		PPCGantt: PPCGantt,
		SCALES: SCALES,
		SCALE_ORDER: SCALE_ORDER,
		GROUPERS: GROUPERS,
		NO_MACHINE: NO_MACHINE,
		STATUS_META: STATUS_META,
		COLUMN_SETS: COLUMN_SETS,
		math: {
			qtyText: qtyText, fmtQty: fmtQty,
			parse: parse, format: format, formatShort: formatShort, formatFull: formatFull, fullDate: fullDate, dateRange: dateRange, dayCell: dayCell,
			formatDuration: formatDuration, snap: snap,
			startOfDay: startOfDay, isoWeek: isoWeek, headerTiers: headerTiers, xOf: xOf, timeOf: timeOf,
			applyDrag: applyDrag, computeRange: computeRange, layoutRows: layoutRows, linkPath: linkPath, esc: esc,
			DAY: DAY, HOUR: HOUR, MIN: MIN,
		},
	};
	if (typeof module !== "undefined" && module.exports) module.exports = api;
	if (root) root.PPCGanttLib = api;
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);
