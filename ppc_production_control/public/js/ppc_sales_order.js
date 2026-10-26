/* Sales Order - "Find Best Machine" for each item line.
 *
 * The browser only asks and displays. The ranking comes from ppc_production_control.api.recommend_machines (BOM, operations, compatible
 * Workstations, capacity and what is already booked); the machine the planner picks is stored on the line and checked again by the server when
 * the order is saved. It is a planning recommendation, not a reservation: the PPC scheduling engine validates the real schedule when Work
 * Orders and Job Cards are created.
 */
(function () {
	"use strict";

	const METHOD = "ppc_production_control.api.recommend_machines";
	const esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));
	const stamp = (v) => (v ? frappe.datetime.str_to_user(v) : "—");
	const RISK = {
		meets: { label: __("Meets delivery date"), css: "ok" },
		tight: { label: __("Tight"), css: "warn" },
		misses: { label: __("Misses delivery date"), css: "bad" },
		unknown: { label: __("Unknown"), css: "muted" },
	};

	function hours(minutes) {
		if (minutes == null) return "—";
		if (minutes < 90) return Math.round(minutes) + " " + __("min");
		return (Math.round((minutes / 60) * 10) / 10) + " " + __("h");
	}

	function risk(state, slack) {
		const r = RISK[state] || RISK.unknown;
		let extra = "";
		if (slack != null && state !== "unknown") extra = " (" + (slack >= 0 ? "+" : "") + slack + " " + __("d") + ")";
		return '<span class="ppc-rec-pill ' + r.css + '">' + esc(r.label + extra) + "</span>";
	}

	function candidateRow(op, c, selected) {
		const id = "ppc-rec-" + op.idx + "-" + c.workstation.replace(/[^A-Za-z0-9_-]/g, "_");
		const badges = [];
		if (c.workstation === op.recommended) badges.push('<span class="ppc-rec-pill ok">' + __("Recommended") + "</span>");
		const warn = (c.warnings || []).map((w) => '<div class="ppc-rec-note">' + esc(w) + "</div>").join("");
		const why = (c.explanation || []).map((w) => "<li>" + esc(w) + "</li>").join("");
		return (
			'<tr class="' + (c.workstation === selected ? "sel" : "") + '">' +
			'<td><input type="radio" name="ppc-rec-op-' + op.idx + '" id="' + id + '" data-op="' + esc(op.operation) + '" data-ws="' + esc(c.workstation) + '"' + (c.workstation === selected ? " checked" : "") + "></td>" +
			"<td><b>#" + (c.rank || "—") + "</b></td>" +
			'<td><label for="' + id + '"><b>' + esc(c.workstation) + "</b></label> " + badges.join(" ") + warn + "</td>" +
			'<td><span class="ppc-rec-pill ok">' + __("Compatible") + "</span></td>" +
			"<td>" + esc(c.capacity) + (c.utilization != null ? " · " + c.utilization + "% " + __("booked") : "") + "</td>" +
			"<td>" + hours(c.elapsed_minutes != null ? c.elapsed_minutes : op.minutes) + "</td>" +
			"<td>" + stamp(c.start) + "</td>" +
			"<td>" + stamp(c.end) + "</td>" +
			"<td>" + (c.delivery_risk ? risk(c.delivery_risk, c.delivery_slack_days) : '<span class="ppc-rec-pill muted">—</span>') + "</td>" +
			"<td><b>" + (c.score != null ? c.score : "—") + "</b></td>" +
			'<td><details><summary>' + __("Why") + "</summary><ul>" + why + "</ul></details></td></tr>"
		);
	}

	function operationHtml(op, selected) {
		let html = '<div class="ppc-rec-op"><div class="ppc-rec-op-head"><b>' + esc(op.idx + ". " + op.operation) + "</b> <span class=\"ppc-rec-sub\">" +
			(op.minutes != null ? hours(op.minutes) : __("duration not in the BOM")) +
			(op.sequence_id ? " · " + __("sequence") + " " + op.sequence_id : "") + "</span></div>";
		(op.warnings || []).forEach((w) => { html += '<div class="ppc-rec-warn">' + esc(w) + "</div>"; });
		if (op.candidates.length) {
			html += '<div class="ppc-rec-scroll"><table class="ppc-rec-table"><thead><tr><th></th><th>' + __("Rank") + "</th><th>" + __("Workstation") + "</th><th>" + __("Compatibility") +
				"</th><th>" + __("Capacity") + "</th><th>" + __("Processing time") + "</th><th>" + __("Earliest start") + "</th><th>" + __("Est. completion") + "</th><th>" + __("Delivery") +
				"</th><th>" + __("Score") + "</th><th></th></tr></thead><tbody>" +
				op.candidates.map((c) => candidateRow(op, c, selected)).join("") + "</tbody></table></div>";
		}
		if ((op.excluded || []).length) {
			html += '<div class="ppc-rec-note">' + __("Not offered") + ": " + op.excluded.map((x) => esc(x.workstation + " (" + x.reason + ")")).join(", ") + "</div>";
		}
		return html + "</div>";
	}

	function bodyHtml(r, args) {
		const o = r.overall || {};
		let html = '<div class="ppc-rec">' +
			'<div class="ppc-rec-summary">' +
			"<div><span>" + __("Item") + "</span><b>" + esc(r.item_code) + (r.item_name && r.item_name !== r.item_code ? " · " + esc(r.item_name) : "") + "</b></div>" +
			"<div><span>" + __("Quantity") + "</span><b>" + esc(r.qty) + (r.uom ? " " + esc(r.uom) : "") + "</b></div>" +
			"<div><span>" + __("BOM") + "</span><b>" + (r.bom ? esc(r.bom.name) : "—") + "</b></div>" +
			"<div><span>" + __("Estimated start") + "</span><b>" + stamp(o.start) + "</b></div>" +
			"<div><span>" + __("Estimated completion") + "</span><b>" + stamp(o.completion) + "</b></div>" +
			"<div><span>" + __("Delivery date") + "</span><b>" + (o.delivery_date ? frappe.datetime.str_to_user(o.delivery_date) : "—") + "</b> " + risk(o.risk, o.slack_days) + "</div>" +
			"</div>";
		(r.warnings || []).forEach((w) => { html += '<div class="ppc-rec-warn">' + esc(w) + "</div>"; });
		html += (r.operations || []).map((op) => operationHtml(op, op.selected)).join("");
		html += '<div class="ppc-rec-foot">' + esc(r.disclaimer || "") + "</div></div>";
		return html;
	}

	function fetch(args, forced) {
		return frappe.call({
			method: METHOD,
			args: Object.assign({}, args, { forced: forced && Object.keys(forced).length ? JSON.stringify(forced) : null }),
			freeze: true,
			freeze_message: __("Calculating…"),
		}).then((r) => r.message);
	}

	function lineArgs(frm, row) {
		return {
			item_code: row.item_code,
			qty: row.qty,
			uom: row.uom || null,
			bom_no: row.bom_no || null,
			delivery_date: row.delivery_date || frm.doc.delivery_date || null,
			sales_order: frm.is_new() ? null : frm.doc.name,
		};
	}

	function apply(frm, cdt, cdn, result, forced) {
		const picks = (result.operations || []).map((op) => ({ operation: op.operation, sequence_id: op.sequence_id, workstation: forced[op.operation] || op.selected })).filter((p) => p.workstation);
		const manual = (result.operations || []).some((op) => op.selected && op.recommended && op.selected !== op.recommended);
		const set = (k, v) => frappe.model.set_value(cdt, cdn, k, v);
		set("ppc_preferred_workstation", picks.length ? picks[0].workstation : null);
		set("ppc_machine_plan", JSON.stringify({ operations: picks, basis: result.basis, bom: result.bom && result.bom.name, calculated_at: result.calculated_at }));
		set("ppc_machine_preference", picks.length ? (manual ? "Manual" : "Recommended") : "No Preference");
		set("ppc_est_start", result.overall.start);
		set("ppc_est_completion", result.overall.completion);
		set("ppc_recommendation_status", manual ? "Selected" : result.status);
		frm.dirty();
		frappe.show_alert({ message: __("Machine selection set on row {0}. Save the order to keep it.", [frappe.get_doc(cdt, cdn).idx]), indicator: "green" });
	}

	function show(frm, cdt, cdn, args, result, forced) {
		if (!result.operations || !result.operations.length) {
			frappe.msgprint({ title: __("Find Best Machine"), indicator: "orange", message: (result.warnings || []).map(esc).join("<br>") || __("No recommendation is possible for this item.") });
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("Best Machine Recommendation"),
			size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "body" }],
			primary_action_label: __("Use Selection"),
			primary_action() {
				if (result.operations.some((op) => !op.selected)) {
					frappe.show_alert({ message: __("Some operations have no machine to choose."), indicator: "orange" });
					return;
				}
				apply(frm, cdt, cdn, result, forced);
				d.hide();
			},
			secondary_action_label: __("Clear Preference"),
			secondary_action() {
				["ppc_preferred_workstation", "ppc_machine_plan", "ppc_est_start", "ppc_est_completion", "ppc_recommendation_status"].forEach((k) => frappe.model.set_value(cdt, cdn, k, null));
				frappe.model.set_value(cdt, cdn, "ppc_machine_preference", "No Preference");
				frm.dirty();
				d.hide();
			},
		});
		d.fields_dict.body.$wrapper.html(bodyHtml(result, args));
		d.fields_dict.body.$wrapper.on("change", "input[type=radio]", function () {
			const pick = Object.assign({}, forced);
			pick[this.dataset.op] = this.dataset.ws;
			d.hide();
			fetch(args, pick).then((r) => show(frm, cdt, cdn, args, r, pick));
		});
		d.show();
	}

	frappe.ui.form.on("Sales Order Item", {
		ppc_find_best_machine(frm, cdt, cdn) {
			const row = locals[cdt][cdn];
			if (!row.item_code) return frappe.msgprint(__("Select an item first."));
			if (!(flt(row.qty) > 0)) return frappe.msgprint(__("Enter the quantity first."));
			const args = lineArgs(frm, row);
			const previous = {};
			try {
				((JSON.parse(row.ppc_machine_plan || "{}").operations) || []).forEach((p) => { if (row.ppc_machine_preference === "Manual") previous[p.operation] = p.workstation; });
			} catch (e) { /* an unreadable stored plan is ignored: the server decides again */ }
			fetch(args, previous).then((r) => show(frm, cdt, cdn, args, r, previous));
		},
		// what the recommendation was calculated for changed: say so at once (the server marks it again on save)
		qty: markStale, item_code: markStale, uom: markStale, bom_no: markStale, delivery_date: markStale,
	});

	function markStale(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.ppc_preferred_workstation && ["Recommended", "Selected"].includes(row.ppc_recommendation_status)) {
			frappe.model.set_value(cdt, cdn, "ppc_recommendation_status", "Stale");
		}
	}

	frappe.ui.form.on("Sales Order", {
		refresh(frm) {
			const attention = (frm.doc.items || []).filter((r) => ["Stale", "Unavailable", "Incomplete Data"].includes(r.ppc_recommendation_status));
			if (attention.length) {
				frm.dashboard.add_comment(
					__("Machine recommendation needs attention on row(s) {0}: use “Find Best Machine” to recalculate.", [attention.map((r) => r.idx).join(", ")]),
					"orange", true
				);
			}
		},
	});
})();
