/* The single tabbed "PPC Production Control" page was replaced by the PPC Control Center (separate pages).
 * The route stays so that bookmarks and links keep working: it forwards to the matching new page. */
frappe.pages["ppc-production-control"].on_page_load = function (wrapper) {
	frappe.ui.make_app_page({ parent: wrapper, title: __("PPC Control Center"), single_column: true });
};

frappe.pages["ppc-production-control"].on_page_show = function () {
	const opts = frappe.route_options || {};
	frappe.route_options = null;
	frappe.require(["ppc.bundle.css", "ppc_control.bundle.js"], function () {
		const target = window.PPCUiLib.legacyTarget(opts.tab);
		window.ppc.go(target.key, opts.plan ? { plan: opts.plan } : {});
	});
};
