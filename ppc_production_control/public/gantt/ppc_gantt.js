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

	/** Two header rows for a scale. Cells are {label, start, end, weekend?}. */
	function headerTiers(scaleKey, startMs, endMs) {
		const top = [];
		const bottom = [];
		let t;
		if (scaleKey === "day") {
			for (t = startOfDay(startMs); t < endMs; t += DAY) {
				const d = new Date(t);
				top.push({ label: WEEKDAYS[d.getUTCDay()] + ", " + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear(), start: t, end: t + DAY });
				for (let h = 0; h < 24; h += 2) {
					bottom.push({ label: pad(h), start: t + h * HOUR, end: t + (h + 2) * HOUR });
				}
			}
		} else if (scaleKey === "week") {
			for (t = mondayOf(startMs); t < endMs; t += 7 * DAY) {
				const w = isoWeek(t);
				const d = new Date(t);
				top.push({ label: "W" + w.week + " · " + d.getUTCDate() + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear(), start: t, end: t + 7 * DAY });
			}
			for (t = startOfDay(startMs); t < endMs; t += DAY) {
				const d = new Date(t);
				bottom.push({ label: WEEKDAYS[d.getUTCDay()].charAt(0) + " " + d.getUTCDate(), start: t, end: t + DAY, weekend: d.getUTCDay() === 0 || d.getUTCDay() === 6 });
			}
		} else if (scaleKey === "month") {
			for (t = monthStart(startMs); t < endMs; t = nextMonthStart(t)) {
				const d = new Date(t);
				top.push({ label: MONTHS_LONG[d.getUTCMonth()] + " " + d.getUTCFullYear(), start: t, end: nextMonthStart(t) });
			}
			for (t = startOfDay(startMs); t < endMs; t += DAY) {
				const d = new Date(t);
				bottom.push({ label: "" + d.getUTCDate(), start: t, end: t + DAY, weekend: d.getUTCDay() === 0 || d.getUTCDay() === 6 });
			}
		} else {
			for (t = quarterStart(startMs); t < endMs; t = addMonths(t, 3)) {
				const d = new Date(t);
				top.push({ label: "Q" + (Math.floor(d.getUTCMonth() / 3) + 1) + " " + d.getUTCFullYear(), start: t, end: addMonths(t, 3) });
			}
			for (t = monthStart(startMs); t < endMs; t = nextMonthStart(t)) {
				const d = new Date(t);
				bottom.push({ label: MONTHS[d.getUTCMonth()], start: t, end: nextMonthStart(t) });
			}
		}
		return { top: top, bottom: bottom };
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
	function layoutRows(tasks, groupBy, collapsed, rowH, groupH) {
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

	/* ------------------------------------------------------------------------------ component */
	const ROW_H = 34;
	const GROUP_H = 28;
	const HEAD_TOP_H = 22;
	const HEAD_BOTTOM_H = 22;
	const HEAD_H = HEAD_TOP_H + HEAD_BOTTOM_H;
	const GRID_W = 430;

	function PPCGantt(container, options) {
		this.el = typeof container === "string" ? document.querySelector(container) : container;
		this.opts = Object.assign(
			{
				height: 560,
				scale: "week",
				groupBy: "workstation",
				now: null, // server "now" (naive string); falls back to browser clock
				canEdit: true,
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
		this._loadedAt = Date.now();
		this._serverNow = parse(this.opts.now);
		this._drag = null;
		this._build();
	}

	PPCGantt.prototype._build = function () {
		const self = this;
		this.el.classList.add("ppc-gantt");
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
		this._onKey = function (e) { if (e.key === "Escape" && self._drag) self._dragCancel(); };
		document.addEventListener("pointermove", this._onMove);
		document.addEventListener("pointerup", this._onUp);
		document.addEventListener("keydown", this._onKey);

		this._renderToolbar();
		this._renderLegend();
	};

	PPCGantt.prototype.destroy = function () {
		document.removeEventListener("pointermove", this._onMove);
		document.removeEventListener("pointerup", this._onUp);
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
	};

	PPCGantt.prototype._renderLegend = function () {
		const items = [
			["st-open", "○", "Open (draft)"], ["st-material", "◐", "Material transferred"], ["st-wip", "▶", "Work in progress"],
			["st-hold", "Ⅱ", "On hold"], ["st-done", "✔", "Completed / submitted"],
		];
		this.$legend.innerHTML =
			items.map(function (i) { return '<span class="ppc-legend-item"><span class="ppc-swatch ' + i[0] + '">' + i[1] + "</span> " + i[2] + "</span>"; }).join("") +
			'<span class="ppc-legend-item"><span class="ppc-swatch ppc-sw-orig"></span> Original plan</span>' +
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

		const range = computeRange(tasks, this.scale === "quarter" ? 14 : 2) || {
			start: startOfDay(this.now()) - 2 * DAY,
			end: startOfDay(this.now()) + 14 * DAY,
		};
		const nowMs = this.now();
		range.start = Math.min(range.start, startOfDay(nowMs) - DAY);
		range.end = Math.max(range.end, startOfDay(nowMs) + 2 * DAY);
		this._range = range;
		const width = Math.ceil(xOf(range.end, range.start, sc.pxPerDay));

		const layout = layoutRows(tasks, this.groupBy, this.collapsed, ROW_H, GROUP_H);
		this._layout = layout;
		this.$inner.style.width = width + "px";
		this.$rows.style.height = layout.height + "px";
		this.$rows.style.width = width + "px";

		this._renderHead(range, sc, width);
		this._renderGridColumns(range, sc);
		this._renderLeft(layout);
		this._renderBars(layout, range, sc, width);
	};

	PPCGantt.prototype._renderHead = function (range, sc, width) {
		const tiers = headerTiers(sc.key, range.start, range.end);
		const cell = function (c, cls, h) {
			const left = Math.max(0, xOf(c.start, range.start, sc.pxPerDay));
			const right = Math.min(width, xOf(c.end, range.start, sc.pxPerDay));
			return '<div class="ppc-hcell ' + cls + (c.weekend ? " weekend" : "") + '" style="left:' + left + "px;width:" + Math.max(0, right - left) + "px;height:" + h + 'px">' + esc(c.label) + "</div>";
		};
		this.$head.style.width = width + "px";
		this.$head.innerHTML =
			'<div class="ppc-htop" style="height:' + HEAD_TOP_H + 'px">' + tiers.top.map(function (c) { return cell(c, "top", HEAD_TOP_H); }).join("") + "</div>" +
			'<div class="ppc-hbottom" style="height:' + HEAD_BOTTOM_H + 'px">' + tiers.bottom.map(function (c) { return cell(c, "bottom", HEAD_BOTTOM_H); }).join("") + "</div>";
		this.$leftHead.style.height = HEAD_H + "px";
		this.$leftHead.innerHTML =
			'<div class="ppc-lcol c-op">Operation / Job Card</div><div class="ppc-lcol c-item">Finished good</div><div class="ppc-lcol c-ws">Workstation</div><div class="ppc-lcol c-st">Status</div>';
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
			const meta = STATUS_META[t.status] || STATUS_META.Open;
			return '<div class="ppc-lrow task' + (self.selected === t.name ? " selected" : "") + '" style="height:' + r.h + 'px" data-name="' + esc(t.name) + '">' +
				'<div class="ppc-lcol c-op" title="' + esc(t.name) + '"><b>' + esc(t.operation || "") + "</b> <span class=\"text-muted\">" + esc(t.name) + "</span></div>" +
				'<div class="ppc-lcol c-item" title="' + esc(t.item_name || t.item_code) + '">' + esc(t.item_code || "") + "</div>" +
				'<div class="ppc-lcol c-ws">' + esc(t.workstation || "—") + "</div>" +
				'<div class="ppc-lcol c-st"><span class="ppc-badge ' + meta.cls + '">' + meta.glyph + " " + esc(t.status) + "</span></div></div>";
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
			const y = r.y + 6;
			pos[t.name] = { x: x, w: w, y: y, mid: y + 11 };
			const meta = STATUS_META[t.status] || STATUS_META.Open;
			const editable = self.opts.canEdit && t.movable;
			const cls = ["ppc-bar", meta.cls, editable ? "editable" : "locked", t.is_estimated ? "estimated" : "", t.conflict ? "conflict" : "", t.overdue ? "overdue" : "", self.selected === t.name ? "selected" : ""].filter(Boolean).join(" ");
			const os = parse(t.original_start);
			const oe = parse(t.original_end);
			if (os != null && oe != null && (os !== s || oe !== e)) {
				const ox = xOf(os, range.start, sc.pxPerDay);
				const ow = Math.max(6, xOf(oe, range.start, sc.pxPerDay) - ox);
				html.push('<div class="ppc-orig" style="left:' + ox + "px;width:" + ow + "px;top:" + (y + 24) + 'px" title="Original plan: ' + esc(formatShort(os)) + " → " + esc(formatShort(oe)) + '"></div>');
			}
			html.push(
				'<div class="' + cls + '" data-name="' + esc(t.name) + '" style="left:' + x + "px;width:" + w + "px;top:" + y + 'px" tabindex="0" role="button" aria-label="' +
				esc((t.operation || "") + ", " + t.name + ", " + t.status + ", " + formatShort(s) + " to " + formatShort(e) + (t.conflict ? ", conflict" : "")) + '">' +
				(editable ? '<span class="ppc-handle left" data-handle="resize-left"></span>' : "") +
				'<span class="ppc-bar-label">' + meta.glyph + " " + esc(t.operation || t.name) +
				(w > 150 ? " · " + esc(t.item_code || "") : "") + (w > 260 ? " · " + esc(t.workstation || "") : "") + "</span>" +
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
		if (this.opts.tooltip) return this.opts.tooltip(t, { esc: esc, formatShort: formatShort, formatDuration: formatDuration });
		return "<b>" + esc(t.name) + "</b><br>" + esc(t.operation) + " · " + esc(t.status);
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
		const handle = e.target && e.target.dataset ? e.target.dataset.handle : null;
		const mode = handle || "move";
		const sc = SCALES[this.scale];
		this._hideTip();
		this._drag = {
			name: name, bar: bar, mode: mode, x0: e.clientX, scroll0: this.$right.scrollLeft,
			start0: parse(t.revised_start), end0: parse(t.revised_end), sc: sc, moved: false, pending: false,
		};
		this._drag.cur = { start: this._drag.start0, end: this._drag.end0 };
		bar.classList.add("dragging");
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
		this._dragLabel(e, prop);
		// auto scroll near the edges
		const r = this.$right.getBoundingClientRect();
		if (r.width) {
			if (e.clientX > r.right - 40) this.$right.scrollLeft += 18;
			else if (e.clientX < r.left + 40) this.$right.scrollLeft -= 18;
		}
	};

	PPCGantt.prototype._dragLabel = function (e, prop) {
		this.$tip.style.display = "block";
		this.$tip.innerHTML = "<b>" + esc(formatShort(prop.start)) + " → " + esc(formatShort(prop.end)) + "</b><br><span class=\"text-muted\">" + esc(formatDuration(prop.end - prop.start)) + "</span>";
		this._moveTip(e);
	};

	PPCGantt.prototype._dragCancel = function () {
		const d = this._drag;
		if (!d) return;
		this._drag = null;
		this._hideTip();
		this.render();
	};

	PPCGantt.prototype._dragEnd = function () {
		const d = this._drag;
		if (!d || d.pending) return;
		const self = this;
		this._hideTip();
		if (!d.moved || !this.opts.onChange) {
			this._drag = null;
			this.render();
			return;
		}
		d.pending = true;
		d.bar.classList.add("pending");
		const evt = {
			task: this.taskByName(d.name),
			mode: d.mode === "move" ? "move" : "resize",
			start: format(d.cur.start),
			end: format(d.cur.end),
			previous: { start: format(d.start0), end: format(d.end0) },
		};
		Promise.resolve(this.opts.onChange(evt)).then(
			function (res) {
				self._drag = null;
				if (res && res.ok) {
					const patch = { revised_start: res.start || evt.start, revised_end: res.end || evt.end };
					if (res.modified) patch.modified = res.modified;
					Object.assign(self.taskByName(d.name), patch);
				}
				self.render(); // success: new position; refusal/failure: snaps back to the stored dates
			},
			function () {
				self._drag = null;
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
		STATUS_META: STATUS_META,
		math: {
			parse: parse, format: format, formatShort: formatShort, formatDuration: formatDuration, snap: snap,
			startOfDay: startOfDay, isoWeek: isoWeek, headerTiers: headerTiers, xOf: xOf, timeOf: timeOf,
			applyDrag: applyDrag, computeRange: computeRange, layoutRows: layoutRows, linkPath: linkPath, esc: esc,
			DAY: DAY, HOUR: HOUR, MIN: MIN,
		},
	};
	if (typeof module !== "undefined" && module.exports) module.exports = api;
	if (root) root.PPCGanttLib = api;
})(typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : this);
