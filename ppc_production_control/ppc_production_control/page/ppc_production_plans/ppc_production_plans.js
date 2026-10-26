(function () {
	// Frappe hands over the query string / route options only while the route is being shown, and the bundle
	// loads asynchronously: take them now, hold them on the wrapper, and let the page apply them once, filtered.
	function takeRoute(wrapper) {
		const held = Object.assign({}, wrapper.ppc_route || {}, frappe.route_options || {});
		frappe.route_options = null;
		new URLSearchParams(window.location.search).forEach(function (v, k) { if (!(k in held)) held[k] = v; });
		wrapper.ppc_route = held;
	}

	frappe.pages["ppc-production-plans"].on_page_load = function (wrapper) {
		takeRoute(wrapper);
		// the bundle holds every Control Center page; it is loaded once and cached by the browser
		frappe.require(["ppc.bundle.css", "ppc_control.bundle.js"], function () {
			window.ppc.pages.plans.mount(wrapper);
			window.ppc.pages.plans.show(wrapper);
		});
	};

	frappe.pages["ppc-production-plans"].on_page_show = function (wrapper) {
		// coming back to the page (or arriving with filters from another page): apply them, refresh if the data is old
		takeRoute(wrapper);
		if (wrapper.ppc_instance && window.ppc) window.ppc.pages.plans.show(wrapper);
	};
})();
