frappe.pages["ppc-production-control"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("PPC Production Control"),
		single_column: true,
	});

	frappe.require(["ppc.bundle.css", "ppc_control.bundle.js"], function () {
		wrapper.ppc_page = new PPCPage(page, wrapper);
	});
};

frappe.pages["ppc-production-control"].on_page_show = function (wrapper) {
	// arriving from a plan form: ?plan=... and tab
	if (wrapper.ppc_page) wrapper.ppc_page.applyRouteOptions();
};

class PPCPage {
	constructor(page, wrapper) {
		this.page = page;
		this.wrapper = wrapper;
		this.$body = $('<div class="ppc"></div>').appendTo(page.main);
		page.set_primary_action(__("New Production Plan"), () => frappe.new_doc("Production Plan"), "add");
		page.add_inner_button(__("PPC Settings"), () => frappe.set_route("Form", "PPC Settings"));
		page.add_inner_button(__("Audit Log"), () => frappe.set_route("List", "PPC Audit Log"));
		ppc.call("get_capabilities").then((c) => {
			this.caps = c;
			this.panel = new ppc.ControlPanel(this.$body, { caps: c.capabilities, batchSize: c.bulk_batch_size, maxTasks: c.max_gantt_tasks });
			this.applyRouteOptions(true);
		}).catch(() => {
			this.$body.html('<div class="ppc-empty">' + __("You do not have access to PPC Production Control.") + "</div>");
		});
	}

	applyRouteOptions(first) {
		if (!this.panel) return;
		const opts = frappe.route_options || {};
		frappe.route_options = null;
		if (opts.plan && this.panel.filterBar) this.panel.filterBar.set({ plan: opts.plan, company: this.panel.filterBar.controls.company.get_value() });
		if (opts.tab) this.panel.showTab(opts.tab, true);
		if (opts.plan || first) {
			// apply once the Link controls have taken their values
			setTimeout(() => this.panel.filterBar.apply(), 150);
		}
	}
}
