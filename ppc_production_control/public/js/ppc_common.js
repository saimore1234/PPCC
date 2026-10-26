/* PPC Production Control - shared front-end helpers (window.ppc). Loaded through ppc_control.bundle.js */
(function () {
	"use strict";
	const ppc = (window.ppc = window.ppc || {});
	const API = "ppc_production_control.api.";

	/* ------------------------------------------------------------------------- server calls */
	/**
	 * Promise wrapper around frappe.call for this app's API. Frappe shows server errors itself
	 * (message dialog); callers only need `.catch(() => {})` to stop their own flow.
	 */
	ppc.call = function (method, args, opts) {
		opts = opts || {};
		return new Promise(function (resolve, reject) {
			frappe.call({
				method: API + method,
				type: "POST",
				args: args || {},
				freeze: !!opts.freeze,
				freeze_message: opts.freeze_message,
				callback: function (r) { resolve(r.message); },
				error: function (r) { reject(r); },
			});
		});
	};

	/* ------------------------------------------------------------------------- formatting */
	ppc.esc = function (v) { return frappe.utils.escape_html(v == null ? "" : String(v)); };

	/** The time zone the schedule is kept in: Frappe's system time zone. The Gantt draws and saves in it, so a bar's dates are
	 *  the very values ERPNext holds, whatever the browser's or the user's own zone is. */
	ppc.siteTimeZone = function () {
		return (frappe.boot.time_zone && frappe.boot.time_zone.system) || (frappe.sys_defaults && frappe.sys_defaults.time_zone) || "";
	};

	/** A short note when the user's profile time zone differs from the site's (other desk screens may show converted times). */
	ppc.timeZoneNote = function () {
		const site = ppc.siteTimeZone();
		const user = frappe.boot.time_zone && frappe.boot.time_zone.user;
		if (!site) return "";
		return user && user !== site ? __("Times are in the site time zone {0}; your profile time zone is {1}.", [site, user]) : __("Times are in the site time zone {0}.", [site]);
	};

	ppc.dt = function (v) {
		if (!v) return "";
		try { return frappe.datetime.str_to_user(v); } catch (e) { return ppc.esc(v); }
	};

	ppc.num = function (v, digits) {
		if (v == null || v === "") return "";
		return format_number(v, null, digits == null ? 3 : digits).replace(/\.0+$/, "");
	};

	ppc.link = function (doctype, name) {
		if (!name) return "";
		return '<a href="/app/' + frappe.router.slug(doctype) + "/" + encodeURIComponent(name) + '">' + ppc.esc(name) + "</a>";
	};

	const STATUS_CLASS = {
		"Open": "st-open", "Material Transferred": "st-material", "Work In Progress": "st-wip", "On Hold": "st-hold",
		"Submitted": "st-done", "Completed": "st-done", "Cancelled": "st-cancel",
		"Draft": "st-open", "Not Started": "st-open", "In Process": "st-wip", "Stopped": "st-hold", "Closed": "st-done",
	};
	ppc.badge = function (status, cls) {
		if (!status) return "";
		return '<span class="ppc-badge ' + (cls || STATUS_CLASS[status] || "st-open") + '">' + ppc.esc(__(status)) + "</span>";
	};

	const SYNC_CLASS = {
		"Synced": "ok", "Submitted": "ok", "Created": "ok", "Scheduled": "ok", "Rescheduled": "warn", "Estimated": "warn",
		"Out of Sync": "warn", "Submit Failed": "bad", "Error": "bad", "Missing": "bad", "Cancelled": "bad", "Not linked": "warn",
		"Failed": "bad", "Drift": "bad", "No baseline": "warn",
	};
	ppc.syncBadge = function (s) { return s ? ppc.badge(s, SYNC_CLASS[s] || "") : ""; };

	/* ------------------------------------------------------------------------- local prefs */
	ppc.pref = {
		get: function (key, fallback) {
			try {
				const v = window.localStorage.getItem("ppc:" + key);
				return v == null ? fallback : JSON.parse(v);
			} catch (e) { return fallback; }
		},
		set: function (key, value) {
			try { window.localStorage.setItem("ppc:" + key, JSON.stringify(value)); } catch (e) { /* private mode */ }
		},
	};

	/* ------------------------------------------------------------------------- DataTable */
	/**
	 * columns: [{key, label, sort?: serverSortKey, align?: "num", html?: row => html, text?: row => string, hidden?: bool, wrap?: bool}]
	 * options:
	 *   selectable        show a checkbox column
	 *   isEligible(row)   rows that may be ticked (others show a disabled box); default: all
	 *   rowClass(row)
	 *   onSort(key, dir)  server-side sort
	 *   onPage(page)      server-side paging
	 *   onSelectAllMatching() -> Promise<string[]>   fetch every eligible id in the filtered result
	 *   storageKey        remember column visibility
	 */
	ppc.DataTable = class DataTable {
		constructor(parent, columns, options) {
			this.$parent = $(parent);
			this.columns = columns;
			this.opts = Object.assign({ idKey: "name", selectable: false, emptyText: __("Nothing to show."), pageLength: 50 }, options || {});
			this.rows = [];
			this.selected = new Set();
			this.total = 0;
			this.page = 1;
			this.pages = 1;
			this.sort = { key: this.opts.sortKey || null, dir: this.opts.sortDir || "asc" };
			const saved = this.opts.storageKey ? ppc.pref.get("cols:" + this.opts.storageKey, null) : null;
			if (saved) this.columns.forEach((c) => { if (c.key in saved) c.hidden = !saved[c.key]; });
			this._build();
		}

		_build() {
			this.$el = $('<div class="ppc-dt"></div>').appendTo(this.$parent.empty());
			this.$bar = $('<div class="ppc-toolbar"></div>').appendTo(this.$el);
			this.$wrap = $('<div class="ppc-table-wrap"></div>').appendTo(this.$el);
			this.$pager = $('<div class="ppc-pager"></div>').appendTo(this.$el);
		}

		ids() { return this.rows.map((r) => r[this.opts.idKey]); }
		eligible(row) { return this.opts.isEligible ? this.opts.isEligible(row) : true; }
		selection() { return Array.from(this.selected); }

		setData(res) {
			this.rows = res.rows || [];
			this.total = res.total != null ? res.total : this.rows.length;
			this.page = res.page || 1;
			this.pages = res.pages || 1;
			// drop selections that are no longer eligible anywhere on screen (keeps cross-page picks)
			this.render();
		}

		clearSelection() { this.selected.clear(); this._refreshSelection(); }

		render() {
			const cols = this.columns.filter((c) => !c.hidden);
			const self = this;
			let html = '<table class="ppc-table"><thead><tr>';
			if (this.opts.selectable) html += '<th style="width:28px"><input type="checkbox" class="ppc-all" title="' + __("Select all on this page") + '" aria-label="' + __("Select all on this page") + '"></th>';
			cols.forEach(function (c) {
				const sorted = self.sort.key === (c.sort || c.key);
				const arrow = c.sort && sorted ? (self.sort.dir === "asc" ? " ▲" : " ▼") : "";
				html += '<th class="' + (c.sort ? "sortable" : "") + '" data-key="' + c.key + '"' + (c.sort ? ' role="button" tabindex="0"' : "") + ">" + ppc.esc(c.label) + '<span class="arrow">' + arrow + "</span></th>";
			});
			html += "</tr></thead><tbody>";
			if (!this.rows.length) {
				html += '<tr><td colspan="' + (cols.length + (this.opts.selectable ? 1 : 0)) + '" class="ppc-empty">' + this.opts.emptyText + "</td></tr>";
			}
			this.rows.forEach(function (row) {
				const id = row[self.opts.idKey];
				const ok = self.eligible(row);
				html += '<tr data-id="' + ppc.esc(id) + '" class="' + (self.opts.rowClass ? self.opts.rowClass(row) : "") + '">';
				if (self.opts.selectable) {
					html += '<td><input type="checkbox" class="ppc-pick" ' + (ok ? "" : "disabled") + (self.selected.has(id) ? " checked" : "") + ' aria-label="' + __("Select") + " " + ppc.esc(id) + '"></td>';
				}
				cols.forEach(function (c) {
					const cell = c.html ? c.html(row) : ppc.esc(c.text ? c.text(row) : row[c.key]);
					html += '<td class="' + (c.align || "") + (c.wrap ? " wrap" : "") + '">' + (cell == null ? "" : cell) + "</td>";
				});
				html += "</tr>";
			});
			html += "</tbody></table>";
			this.$wrap.html(html);

			this.$wrap.find("th.sortable").on("click keydown", function (e) {
				if (e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return;
				const col = self.columns.find((c) => c.key === $(this).data("key"));
				const key = col.sort || col.key;
				self.sort.dir = self.sort.key === key && self.sort.dir === "asc" ? "desc" : "asc";
				self.sort.key = key;
				if (self.opts.onSort) self.opts.onSort(self.sort.key, self.sort.dir);
			});
			this.$wrap.find(".ppc-all").on("change", function () {
				const on = this.checked;
				self.rows.forEach(function (row) {
					const id = row[self.opts.idKey];
					if (!self.eligible(row)) return;
					on ? self.selected.add(id) : self.selected.delete(id);
				});
				self._refreshSelection();
			});
			this.$wrap.find(".ppc-pick").on("change", function () {
				const id = $(this).closest("tr").data("id") + "";
				this.checked ? self.selected.add(id) : self.selected.delete(id);
				self._refreshSelection(true);
			});
			this._renderBar();
			this._renderPager();
			this._refreshSelection(true);
		}

		_renderBar() {
			const self = this;
			this.$bar.empty();
			if (this.opts.selectable) {
				this.$count = $('<span class="ppc-sel-count"></span>').appendTo(this.$bar);
				if (this.opts.onSelectAllMatching) {
					$('<button type="button" class="btn btn-xs btn-default">' + (this.opts.selectAllLabel || __("Select all matching the filters")) + "</button>")
						.appendTo(this.$bar).on("click", function () {
							const $b = $(this).prop("disabled", true);
							Promise.resolve(self.opts.onSelectAllMatching())
								.then(function (ids) { (ids || []).forEach((id) => self.selected.add(id)); self.render(); })
								.finally(function () { $b.prop("disabled", false); });
						});
				}
				$('<button type="button" class="btn btn-xs btn-default">' + __("Clear selection") + "</button>").appendTo(this.$bar).on("click", function () { self.clearSelection(); });
			}
			$('<span class="spacer"></span>').appendTo(this.$bar);
			if (this.opts.extraTools) this.opts.extraTools(this.$bar);
			// column chooser
			const $menuWrap = $('<span class="ppc-colmenu"><button type="button" class="btn btn-xs btn-default">' + __("Columns") + "</button></span>").appendTo(this.$bar);
			$menuWrap.find("button").on("click", function (e) {
				e.stopPropagation();
				const $menu = $menuWrap.find(".menu");
				if ($menu.length) return $menu.remove();
				const $m = $('<div class="menu"></div>').appendTo($menuWrap);
				self.columns.forEach(function (c) {
					const $l = $('<label><input type="checkbox"> ' + ppc.esc(c.label) + "</label>").appendTo($m);
					$l.find("input").prop("checked", !c.hidden).on("change", function () {
						c.hidden = !this.checked;
						if (self.opts.storageKey) {
							const map = {};
							self.columns.forEach((x) => (map[x.key] = !x.hidden));
							ppc.pref.set("cols:" + self.opts.storageKey, map);
						}
						self.render();
					});
				});
				$(document).one("click", function () { $m.remove(); });
				$m.on("click", (ev) => ev.stopPropagation());
			});
		}

		_renderPager() {
			const self = this;
			this.$pager.empty();
			if (!this.opts.onPage) {
				this.$pager.append('<span class="text-muted">' + __("{0} record(s)", [this.total]) + "</span>");
				return;
			}
			const first = this.total ? (this.page - 1) * this.opts.pageLength + 1 : 0;
			const last = Math.min(this.total, this.page * this.opts.pageLength);
			$('<button type="button" class="btn btn-xs btn-default" ' + (this.page <= 1 ? "disabled" : "") + ">‹ " + __("Prev") + "</button>").appendTo(this.$pager).on("click", () => this.opts.onPage(this.page - 1));
			this.$pager.append('<span>' + __("{0}–{1} of {2}", [first, last, this.total]) + " · " + __("page {0} / {1}", [this.page, this.pages]) + "</span>");
			$('<button type="button" class="btn btn-xs btn-default" ' + (this.page >= this.pages ? "disabled" : "") + ">" + __("Next") + " ›</button>").appendTo(this.$pager).on("click", () => this.opts.onPage(this.page + 1));
			const $size = $('<select class="form-control input-xs" style="width:auto;height:24px;padding:0 4px" aria-label="' + __("Rows per page") + '"></select>').appendTo(this.$pager);
			[25, 50, 100, 200].forEach((n) => $size.append('<option value="' + n + '"' + (n === this.opts.pageLength ? " selected" : "") + ">" + n + "</option>"));
			$size.on("change", function () { if (self.opts.onPageSize) self.opts.onPageSize(parseInt(this.value, 10)); });
		}

		_refreshSelection(skipRender) {
			if (this.$count) this.$count.text(__("{0} selected", [this.selected.size]));
			if (!skipRender) {
				this.$wrap.find(".ppc-pick").each((i, el) => {
					el.checked = this.selected.has($(el).closest("tr").data("id") + "");
				});
			}
			const eligible = this.rows.filter((r) => this.eligible(r));
			const all = eligible.length && eligible.every((r) => this.selected.has(r[this.opts.idKey]));
			this.$wrap.find(".ppc-all").prop("checked", !!all);
			if (this.opts.onSelectionChange) this.opts.onSelectionChange(this.selection());
		}
	};

	/* ------------------------------------------------------------------------- bulk actions */
	/**
	 * Submit `ids` in server-sized batches with a progress bar, then show a per-record outcome.
	 * `method` must return {results: [{name, ok, skipped, message}]}.
	 */
	ppc.bulk = function (opts) {
		const ids = opts.ids || [];
		if (!ids.length) {
			frappe.show_alert({ message: __("Select at least one record."), indicator: "orange" });
			return Promise.resolve(null);
		}
		return new Promise(function (resolve) {
			frappe.confirm(
				__("{0} {1} record(s)?", [opts.verb || __("Submit"), ids.length]) +
					"<br><span class='text-muted'>" + (opts.hint || __("Each record is validated by ERPNext; failures are listed afterwards and nothing is bypassed.")) + "</span>",
				function () {
					ppc.runBatches(opts).then(resolve);
				},
				function () { resolve(null); }
			);
		});
	};

	ppc.runBatches = async function (opts) {
		const ids = opts.ids;
		const size = Math.max(1, opts.batchSize || 25);
		const all = [];
		let done = 0;
		frappe.show_progress(opts.title || __("Working"), 0, ids.length, __("Starting…"), true);
		try {
			for (let i = 0; i < ids.length; i += size) {
				const chunk = ids.slice(i, i + size);
				const res = await ppc.call(opts.method, Object.assign({}, opts.args, { [opts.argName]: chunk }));
				all.push(...(res.results || []));
				done += chunk.length;
				frappe.show_progress(opts.title || __("Working"), done, ids.length, __("{0} of {1} processed", [done, ids.length]), true);
				await new Promise((r) => setTimeout(r, 0)); // yield to the browser: the UI stays responsive
			}
		} catch (e) {
			all.push({ name: __("(stopped)"), ok: false, message: __("The request failed; processing stopped. Records already processed are listed.") });
		} finally {
			frappe.hide_progress();
		}
		ppc.showResults(opts.title || __("Result"), all, opts.doctype);
		if (opts.onDone) opts.onDone(all);
		return all;
	};

	ppc.showResults = function (title, results, doctype) {
		const ok = results.filter((r) => r.ok).length;
		const skipped = results.filter((r) => !r.ok && r.skipped).length;
		const failed = results.length - ok - skipped;
		let html = '<p><span class="ppc-result-ok">' + __("{0} succeeded", [ok]) + '</span> · <span class="ppc-result-fail">' + __("{0} failed", [failed]) + '</span> · <span class="ppc-result-skip">' + __("{0} skipped", [skipped]) + "</span></p>";
		html += '<div class="ppc-results"><table class="ppc-table"><thead><tr><th>' + __("Record") + "</th><th>" + __("Result") + "</th><th>" + __("Message") + "</th></tr></thead><tbody>";
		results.forEach(function (r) {
			const cls = r.ok ? "ppc-result-ok" : r.skipped ? "ppc-result-skip" : "ppc-result-fail";
			const label = r.ok ? "✔ " + __("Done") : r.skipped ? "– " + __("Skipped") : "✖ " + __("Failed");
			html += "<tr><td>" + (doctype ? ppc.link(doctype, r.name) : ppc.esc(r.name)) + '</td><td class="' + cls + '">' + label + '</td><td class="wrap">' + ppc.esc(r.message || "") + "</td></tr>";
		});
		html += "</tbody></table></div>";
		const d = new frappe.ui.Dialog({ title: title, size: "large", fields: [{ fieldtype: "HTML", fieldname: "body" }] });
		d.fields_dict.body.$wrapper.html(html);
		d.show();
	};

	ppc.listHtml = function (items, cls) {
		return '<ul class="ppc-warnlist ' + (cls || "") + '">' + items.map((i) => "<li>" + ppc.esc(i) + "</li>").join("") + "</ul>";
	};
})();
