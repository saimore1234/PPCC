/* PPC Control Center - change a Job Card's Workstation: confirmation, conflict popup, alternative machines, next free slot,
 * rescheduling the conflicting operation, and the audited override. Everything the planner sees here is decided by the
 * server (check_workstation_change / change_job_card_workstation); nothing is saved from the browser's own conflict check. */
(function () {
	"use strict";
	const ppc = window.ppc;

	/* ------------------------------------------------------------------------------------------ a dialog with named buttons */
	/**
	 * opts: {title, html, indicator, size, fields[], actions: [{key, label, primary?, css?}], validate(key, dialog) -> message|null,
	 *        onShow(dialog, finish)}.  Resolves {key, values}; closing the dialog any other way resolves {key: "cancel"}.
	 */
	ppc.choose = function (opts) {
		return new Promise(function (resolve) {
			let done = false;
			let d;
			const finish = function (key, extra) {
				if (done) return;
				if (opts.validate && key !== "cancel") {
					const problem = opts.validate(key, d);
					if (problem) { frappe.show_alert({ message: problem, indicator: "orange" }); return; }
				}
				done = true;
				const values = {};
				(opts.fields || []).forEach(function (f) { if (f.fieldname) values[f.fieldname] = d.get_value(f.fieldname); });
				d.hide();
				resolve(Object.assign({ key: key, values: values }, extra || {}));
			};
			const primary = (opts.actions || []).find(function (a) { return a.primary; });
			d = new frappe.ui.Dialog({
				title: opts.title,
				size: opts.size || "large",
				indicator: opts.indicator,
				fields: [{ fieldtype: "HTML", fieldname: "ppc_body" }].concat(opts.fields || []),
				primary_action_label: primary ? primary.label : null,
				primary_action: primary ? function () { finish(primary.key); } : null,
			});
			(opts.actions || []).filter(function (a) { return !a.primary; }).forEach(function (a) {
				d.add_custom_action(a.label, function () { finish(a.key); }, a.css || "btn-default");
			});
			d.fields_dict.ppc_body.$wrapper.html(opts.html || "");
			d.$wrapper.on("hidden.bs.modal", function () { finish("cancel"); }); // closing = cancelling
			if (opts.onShow) opts.onShow(d, finish);
			d.show();
		});
	};

	/* ------------------------------------------------------------------------------------------------------- small renderers */
	const e = ppc.esc;
	const kv = function (k, v) { return '<div class="ppc-kvrow"><span class="k">' + k + '</span><span class="v">' + v + "</span></div>"; };
	const range = function (a, b) { return ppc.dt(a) + " → " + ppc.dt(b); };

	function conflictHtml(check) {
		const rows = check.conflicts || [];
		let html = '<p class="ppc-conflict-lead">' + __("The selected machine is already scheduled during the requested time.") + "</p>" +
			'<div class="ppc-kv">' + kv(__("Job Card"), ppc.link("Job Card", check.job_card)) + kv(__("Target Workstation"), "<b>" + e(check.target_workstation) + "</b>") +
			kv(__("Requested time"), e(range(check.start, check.end))) + "</div>";
		if (rows.length === 1) {
			const c = rows[0];
			html += '<h5 class="ppc-sub">' + __("In the way") + '</h5><div class="ppc-kv">' +
				kv(__("Conflicting Job Card"), ppc.link("Job Card", c.job_card)) + kv(__("Conflicting Operation"), e(c.operation || "")) +
				kv(__("Scheduled From"), e(ppc.dt(c.from))) + kv(__("Scheduled To"), e(ppc.dt(c.to))) + kv(__("Conflicting Work Order"), ppc.link("Work Order", c.work_order)) +
				kv(__("Status"), ppc.badge(c.status)) + "</div>";
		} else if (rows.length > 1) {
			html += '<h5 class="ppc-sub">' + __("In the way") + "</h5>" + ppc.miniTable([
				{ label: __("Job Card"), html: function (r) { return ppc.link("Job Card", r.job_card); } },
				{ label: __("Operation"), html: function (r) { return e(r.operation || ""); } },
				{ label: __("Work Order"), html: function (r) { return ppc.link("Work Order", r.work_order); } },
				{ label: __("Scheduled From"), html: function (r) { return e(ppc.dt(r.from)); } },
				{ label: __("Scheduled To"), html: function (r) { return e(ppc.dt(r.to)); } },
			], rows);
		}
		if (rows.length && check.capacity > 1) html += '<p class="text-muted">' + __("This machine can run {0} operations at the same time; {1} are already running in this period.", [check.capacity, check.peak_others]) + "</p>";
		const others = (check.warnings || []).filter(function (w) { return w.code !== "workstation"; });
		if (others.length) html += "<ul class='ppc-warnlist'>" + others.map(function (w) { return "<li class='" + (w.needs_override ? "must" : "") + "'>" + e(w.message) + "</li>"; }).join("") + "</ul>";
		return html;
	}

	function pushPreviewHtml(check) {
		const plan = ((check.resolution || {}).reschedule_conflicts) || {};
		if (!(check.conflicts || []).length) return "";
		if (!plan.allowed) return plan.reason ? '<p class="text-muted ppc-note">' + __("Moving the conflicting operation is not possible: {0}", [e(plan.reason)]) + "</p>" : "";
		return '<h5 class="ppc-sub">' + __("If you reschedule the conflicting operation") + "</h5>" + ppc.miniTable([
			{ label: __("Job Card"), html: function (r) { return ppc.link("Job Card", r.job_card); } },
			{ label: __("Now"), html: function (r) { return e(range(r.old_start, r.old_end)); } },
			{ label: __("Would move to"), html: function (r) { return "<b>" + e(range(r.new_start, r.new_end)) + "</b>"; } },
			{ label: __("Following operations"), html: function (r) { return r.cascade.length ? e(r.cascade.map(function (c) { return c.job_card; }).join(", ")) : "–"; } },
		], plan.shifts);
	}

	function cascadeHtml(check) {
		const shifts = (check.cascade || {}).shifts || [];
		if (!shifts.length) return "";
		return '<h5 class="ppc-sub">' + __("Following operations that will move") + "</h5>" + ppc.miniTable([
			{ label: __("Job Card"), html: function (r) { return ppc.link("Job Card", r.job_card); } },
			{ label: __("Now"), html: function (r) { return e(range(r.old_start, r.old_end)); } },
			{ label: __("New"), html: function (r) { return "<b>" + e(range(r.new_start, r.new_end)) + "</b>"; } },
		], shifts);
	}

	/* ---------------------------------------------------------------------------------------------------- the dialogs */
	function confirmDialog(check, state) {
		const dates = check.dates_changed
			? e(range(state.original.start, state.original.end)) + " → <b>" + e(range(check.start, check.end)) + "</b>"
			: e(range(check.start, check.end)) + ' <span class="text-muted">(' + __("unchanged") + ")</span>";
		let html = '<div class="ppc-kv">' + kv(__("Job Card"), ppc.link("Job Card", check.job_card) + " · " + e(check.operation || "")) +
			kv(__("Current Workstation"), e(check.current_workstation || "–")) + kv(__("Proposed Workstation"), "<b>" + e(check.target_workstation) + "</b>") +
			kv(__("Scheduled"), dates) + "</div>";
		const comp = check.compatibility || {};
		html += comp.state === "compatible" ? '<p class="ppc-flag ok">' + e(comp.message) + "</p>" : comp.state === "not_configured" ? '<p class="text-muted ppc-note">' + e(comp.message) + "</p>" : "";
		if (check.utilization != null) html += '<p class="text-muted ppc-note">' + __("Load on {0} in this period: {1}%", [e(check.target_workstation), check.utilization]) + "</p>";
		const soft = (check.warnings || []);
		if (soft.length) html += "<ul class='ppc-warnlist'>" + soft.map(function (w) { return "<li>" + e(w.message) + "</li>"; }).join("") + "</ul>";
		html += cascadeHtml(check);
		return ppc.choose({
			title: __("Change Workstation"), indicator: "blue", html: html,
			fields: [{ fieldtype: "Small Text", fieldname: "reason", label: __("Reason (optional)"), default: state.reason || "" }],
			actions: [{ key: "confirm", label: __("Change Workstation"), primary: true }, { key: "cancel", label: __("Cancel Change") }],
		});
	}

	function conflictDialog(check, state) {
		const plan = ((check.resolution || {}).reschedule_conflicts) || {};
		const hasSuccessor = (check.warnings || []).some(function (w) { return w.code === "successor"; }) && !state.shift;
		const actions = [{ key: "cancel", label: __("Cancel Change") }, { key: "other", label: __("Choose Another Machine") }, { key: "slot", label: __("Find Next Available Slot") }];
		if (hasSuccessor) actions.push({ key: "shift", label: __("Shift Following Operations") });
		if (plan.allowed) actions.push({ key: "push", label: __("Reschedule Conflicting Operation") });
		if (check.can_override) actions.push({ key: "override", label: __("Override Conflict"), css: "btn-danger" });
		return ppc.choose({
			title: __("Workstation Conflict Detected"), indicator: "red",
			html: conflictHtml(check) + pushPreviewHtml(check) + (check.can_override ? "" : '<p class="text-muted ppc-note">' + __("Overriding a conflict needs the Override Conflicts permission (PPC Manager).") + "</p>"),
			actions: actions,
		});
	}

	function overrideDialog(check) {
		const musts = (check.warnings || []).filter(function (w) { return w.needs_override; }).map(function (w) { return w.message; });
		return ppc.choose({
			title: __("Override Conflict"), indicator: "red", size: "small",
			html: "<p>" + __("You are about to change the Workstation although:") + "</p>" + ppc.listHtml(musts) + '<p class="text-muted">' + __("The override and your reason are recorded in the schedule history and the audit log.") + "</p>",
			fields: [{ fieldtype: "Small Text", fieldname: "reason", label: __("Reason for the override"), reqd: 1 }],
			validate: function (key, d) { return key === "override" && (d.get_value("reason") || "").trim().length < 10 ? __("Give a reason of at least 10 characters.") : null; },
			actions: [{ key: "override", label: __("Override and Change"), primary: true, css: "btn-danger" }, { key: "back", label: __("Back") }],
		});
	}

	async function slotDialog(task, state) {
		let slot;
		try { slot = await ppc.call("find_next_workstation_slot", { job_card: task.name, workstation: state.workstation, start: state.start, end: state.end }, { freeze: true, freeze_message: __("Looking for a free slot…") }); } catch (x) { return null; }
		if (!slot.found) {
			await ppc.choose({ title: __("Next Available Slot"), indicator: "orange", size: "small", html: "<p>" + e(slot.message) + "</p>", actions: [{ key: "back", label: __("Back"), primary: true }] });
			return null;
		}
		const html = '<div class="ppc-kv">' + kv(__("Workstation"), "<b>" + e(slot.workstation) + "</b>") + kv(__("Requested"), e(range(state.start, state.end))) +
			kv(__("Next free slot"), "<b>" + e(range(slot.start, slot.end)) + "</b>") + "</div>" +
			(slot.constraint ? '<p class="text-muted ppc-note">' + __("Placed {0}.", [e(slot.constraint)]) + "</p>" : "") +
			'<p class="text-muted ppc-note">' + __("Nothing is saved until you confirm the change on the next screen.") + "</p>";
		const r = await ppc.choose({ title: __("Next Available Slot"), indicator: "green", size: "small", html: html, actions: [{ key: "use", label: __("Use This Slot"), primary: true }, { key: "back", label: __("Back") }] });
		return r.key === "use" ? { start: slot.start, end: slot.end } : null;
	}

	/** Searchable list of Workstations with availability for the requested interval. Resolves {workstation, slot?} or null. */
	async function pickerDialog(task, state) {
		const badge = function (c) {
			if (c.is_current) return '<span class="ppc-badge st-done">' + __("current") + "</span>";
			return c.available ? '<span class="ppc-badge st-open">' + __("available") + "</span>" : '<span class="ppc-badge st-hold">' + __("busy") + "</span>";
		};
		const compat = function (c) {
			return { compatible: '<span class="ppc-flag ok" title="' + e(c.compatibility.message) + '">' + __("compatible") + "</span>", incompatible: '<span class="ppc-flag bad" title="' + e(c.compatibility.message) + '">' + __("wrong type") + "</span>" }[c.compatibility.state] ||
				'<span class="text-muted" title="' + e(c.compatibility.message) + '">' + __("not configured") + "</span>";
		};
		const result = await ppc.choose({
			title: __("Choose Another Machine"), size: "extra-large",
			html: '<div class="ppc-picker"><input type="search" class="form-control ppc-m-search" placeholder="' + e(__("Search Workstation or type")) + '" aria-label="' + e(__("Search Workstation")) + '">' +
				'<p class="text-muted ppc-note ppc-m-info"></p><div class="ppc-m-list" aria-live="polite"></div></div>',
			actions: [{ key: "back", label: __("Back") }],
			onShow: function (d, finish) {
				const $w = d.fields_dict.ppc_body.$wrapper;
				const $list = $w.find(".ppc-m-list");
				let req = 0;
				const load = function () {
					const id = ++req;
					$list.html('<div class="ppc-skeleton">' + __("Loading…") + "</div>");
					ppc.call("get_workstation_candidates", { job_card: task.name, start: state.start, end: state.end, search: $w.find(".ppc-m-search").val() || "" }).then(function (res) {
						if (id !== req) return;
						$w.find(".ppc-m-info").text(res.compatibility_configured ? __("Required Workstation Type: {0}", [res.required_type]) : __("No Workstation Type is configured for this operation: compatibility cannot be checked."));
						if (!res.workstations.length) return $list.html('<div class="ppc-empty ppc-empty-sm">' + __("No Workstation matches.") + "</div>");
						$list.html('<div class="ppc-table-wrap"><table class="ppc-table"><thead><tr>' + [__("Workstation"), __("Type"), __("Availability"), __("In the way"), __("Load"), __("Next free slot"), __("Compatibility"), ""].map(function (h) { return "<th>" + h + "</th>"; }).join("") +
							"</tr></thead><tbody>" + res.workstations.map(function (c, i) {
								const inWay = c.conflicts.map(function (b) { return e(b.job_card) + " (" + e(ppc.dt(b.from)) + " → " + e(ppc.dt(b.to)) + ")"; }).join("<br>") + (c.conflict_count > c.conflicts.length ? "<br>+" + (c.conflict_count - c.conflicts.length) : "");
								return "<tr data-i='" + i + "'><td><b>" + e(c.name) + "</b>" + (c.status && c.status !== "Production" ? ' <span class="text-muted">(' + e(c.status) + ")</span>" : "") + "</td><td>" + e(c.workstation_type || "–") + "</td><td>" + badge(c) + "</td><td class='wrap'>" + (inWay || "–") +
									"</td><td>" + (c.utilization == null ? "–" : c.utilization + "%") + "</td><td>" + (c.next_slot ? e(ppc.dt(c.next_slot.start)) : '<span class="text-muted">' + __("none in 120 days") + "</span>") + "</td><td>" + compat(c) + "</td><td>" +
									(c.is_current ? "" : '<button type="button" class="btn btn-xs btn-primary ppc-m-pick" data-i="' + i + '">' + __("Select") + "</button>" +
										(!c.available && c.next_slot ? ' <button type="button" class="btn btn-xs btn-default ppc-m-slot" data-i="' + i + '">' + __("Use next slot") + "</button>" : "")) + "</td></tr>";
							}).join("") + "</tbody></table></div>");
						$list.off("click").on("click", ".ppc-m-pick, .ppc-m-slot", function () {
							const c = res.workstations[$(this).data("i")];
							finish("pick", { workstation: c.name, slot: $(this).hasClass("ppc-m-slot") ? c.next_slot : null });
						});
					}).catch(function () { if (id === req) $list.html('<div class="ppc-error" role="alert">' + __("The list could not be loaded.") + "</div>"); });
				};
				$w.find(".ppc-m-search").on("input", window.PPCUiLib.debounce(load, 300));
				load();
				setTimeout(function () { $w.find(".ppc-m-search").trigger("focus"); }, 250);
			},
		});
		return result.key === "pick" ? { workstation: result.workstation, slot: result.slot } : null;
	}

	/* ------------------------------------------------------------------------------------------------------------ the flow */
	/**
	 * Move `task` (a Gantt task or Job Card row) to another Workstation. `target` may be null: the planner then picks the machine
	 * first. opts: {start, end}. Resolves {ok, workstation, start, end, modified, ...}; {ok: false, cancelled|stale} otherwise.
	 */
	ppc.changeWorkstation = async function (task, target, opts) {
		opts = opts || {};
		const state = {
			workstation: target, start: opts.start || task.revised_start, end: opts.end || task.revised_end, shift: false, reason: "",
			original: { start: task.revised_start, end: task.revised_end }, token: task.modified,
			correlation: "ws" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8), // the same request is never applied twice
		};
		const cancelled = { ok: false, cancelled: true };
		for (let round = 0; round < 25; round++) {
			if (!state.workstation) {
				const pick = await pickerDialog(task, state);
				if (!pick) return cancelled;
				state.workstation = pick.workstation;
				if (pick.slot) { state.start = pick.slot.start; state.end = pick.slot.end; }
			}
			let check;
			try {
				check = await ppc.call("check_workstation_change", { job_card: task.name, workstation: state.workstation, start: state.start, end: state.end, shift_dependents: state.shift ? 1 : 0 }, { freeze: true, freeze_message: __("Checking the machine…") });
			} catch (x) { return cancelled; }
			if (check.errors && check.errors.length) {
				await ppc.choose({ title: __("Cannot change the Workstation"), indicator: "red", size: "small", html: ppc.listHtml(check.errors), actions: [{ key: "close", label: __("Close"), primary: true }] });
				return cancelled;
			}
			check.dates_changed = !(ppc_sameSecond(state.start, state.original.start) && ppc_sameSecond(state.end, state.original.end));
			let action;
			if (check.needs_override || (check.conflicts && check.conflicts.length)) {
				action = await conflictDialog(check, state);
			} else {
				action = await confirmDialog(check, state);
				if (action.key === "confirm") state.reason = (action.values && action.values.reason) || "";
			}
			let extra = {};
			if (action.key === "cancel") return cancelled;
			if (action.key === "other") { state.workstation = null; continue; }
			if (action.key === "shift") { state.shift = true; continue; }
			if (action.key === "slot") {
				const slot = await slotDialog(task, state);
				if (slot) { state.start = slot.start; state.end = slot.end; }
				continue;
			}
			if (action.key === "push") extra = { reschedule_conflicts: 1 };
			if (action.key === "override") {
				const o = await overrideDialog(check);
				if (o.key !== "override") continue;
				extra = { override_conflicts: 1, override_reason: o.values.reason };
			}
			// action: confirm | push | override  -> save, the server checks again under the machine's lock
			let res;
			try {
				res = await ppc.call("change_job_card_workstation", Object.assign({
					job_card: task.name, workstation: state.workstation, start: state.start, end: state.end, reason: state.reason, confirmed: 1,
					shift_dependents: state.shift ? 1 : 0, expected_modified: state.token, correlation_id: state.correlation,
				}, extra), { freeze: true, freeze_message: __("Changing the Workstation…") });
			} catch (x) { return cancelled; }
			if (res.ok) {
				frappe.show_alert({ message: __("{0} moved from {1} to {2}", [task.name, res.previous_workstation || "–", res.workstation]) + (res.override_used ? " (" + __("conflict overridden") + ")" : ""), indicator: "green" });
				const mine = (res.changed || []).find(function (c) { return c.job_card === task.name; }) || {};
				return { ok: true, workstation: res.workstation, start: mine.new_start || state.start, end: mine.new_end || state.end, modified: res.modified, changed: res.changed, duplicate: !!res.duplicate };
			}
			if (res.stale) {
				frappe.msgprint({ title: __("Schedule changed by someone else"), indicator: "red", message: ppc.listHtml(res.errors) });
				return { ok: false, stale: true };
			}
			if (res.errors && res.errors.length && !res.conflict) {
				frappe.msgprint({ title: __("The Workstation was not changed"), indicator: "red", message: ppc.listHtml(res.errors) });
				if (!res.needs_confirmation) return { ok: false };
			}
			if (res.conflict || res.needs_override) { // the machine was taken since the check: show the fresh situation
				frappe.show_alert({ message: __("The machine's schedule changed; please review the conflict."), indicator: "orange" });
				continue;
			}
			if (!res.needs_confirmation) return { ok: false };
		}
		return cancelled;
	};

	function ppc_sameSecond(a, b) { return String(a || "").slice(0, 19) === String(b || "").slice(0, 19); }
})();
