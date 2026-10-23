"""Test fixtures. Every record is created inside the test transaction, which the test base class
rolls back, so running the suite leaves the site's data untouched."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, flt, now_datetime, nowdate

from ppc_production_control.services.constants import PLAN_DT

PREFIX = "PPCT"  # every test master is named PPCT-...


def get_company() -> str:
	company = frappe.conf.get("ppc_test_company") or "_Test Company"
	if not frappe.db.exists("Company", company):
		raise unittest_skip(f"Company {company} not found; set ppc_test_company in site_config.json")
	return company


def unittest_skip(msg):
	import unittest

	return unittest.SkipTest(msg)


def make_warehouse(name: str, company: str) -> str:
	existing = frappe.db.get_value("Warehouse", {"warehouse_name": name, "company": company})
	if existing:
		return existing
	doc = frappe.get_doc({"doctype": "Warehouse", "warehouse_name": name, "company": company})
	doc.insert(ignore_permissions=True)
	return doc.name


def _existing_hsn_code() -> str | None:
	"""Sites running India Compliance validate the HSN link; borrow a code that already exists."""
	if not frappe.get_meta("Item").has_field("gst_hsn_code") or not frappe.db.table_exists("GST HSN Code"):
		return None
	return frappe.db.get_value("GST HSN Code", {}, "name")


def make_item(code: str, *, is_stock: int = 1, manufacture: bool = False) -> str:
	if frappe.db.exists("Item", code):
		return code
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"is_stock_item": is_stock,
			"valuation_rate": 10,
			"standard_rate": 100,
			"default_material_request_type": "Manufacture" if manufacture else "Purchase",
		}
	)
	hsn = _existing_hsn_code()
	if hsn:
		doc.gst_hsn_code = hsn
	_fill_mandatory_selects(doc)
	doc.insert(ignore_permissions=True)
	return doc.name


def _fill_mandatory_selects(doc):
	"""Customised sites sometimes mark extra Select fields mandatory; pick their first option so
	the fixtures stay valid on such sites. Generic - never names a customer-specific field."""
	for df in doc.meta.fields:
		if df.reqd and df.fieldtype == "Select" and not doc.get(df.fieldname) and not df.hidden:
			options = [o for o in (df.options or "").split("\n") if o]
			if options:
				doc.set(df.fieldname, options[0])


def make_workstation(name: str, capacity: int = 1) -> str:
	if frappe.db.exists("Workstation", name):
		frappe.db.set_value("Workstation", name, "production_capacity", capacity)
		return name
	frappe.get_doc(
		{"doctype": "Workstation", "workstation_name": name, "production_capacity": capacity, "hour_rate": 60}
	).insert(ignore_permissions=True)
	return name


def make_operation(name: str, workstation: str) -> str:
	if frappe.db.exists("Operation", name):
		return name
	frappe.get_doc({"doctype": "Operation", "name": name, "workstation": workstation}).insert(ignore_permissions=True)
	return name


def make_bom(item: str, company: str, components: list[tuple[str, float]], operations: list[dict] | None = None, submit: bool = True, source_warehouse: str | None = None):
	"""operations: [{"operation", "workstation", "time_in_mins", "sequence_id"}]"""
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": item,
			"company": company,
			"quantity": 1,
			"is_active": 1,
			"is_default": 1,
			"with_operations": 1 if operations else 0,
			"items": [
				{"item_code": c, "qty": q, "rate": 10, "source_warehouse": source_warehouse} for c, q in components
			],
			"operations": operations or [],
		}
	)
	if operations:
		bom.transfer_material_against = "Work Order"
	bom.insert(ignore_permissions=True)
	if submit:
		bom.submit()
	return bom


def make_customer(name: str = f"{PREFIX} Customer") -> str:
	existing = frappe.db.get_value("Customer", {"customer_name": name})
	if existing:
		return existing
	group = frappe.db.get_value("Customer Group", {"is_group": 0}) or "All Customer Groups"
	territory = frappe.db.get_value("Territory", {"is_group": 0}) or "All Territories"
	doc = frappe.get_doc(
		{"doctype": "Customer", "customer_name": name, "customer_group": group, "territory": territory, "customer_type": "Company"}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def selling_price_list() -> str:
	return frappe.db.get_single_value("Selling Settings", "selling_price_list") or "Standard Selling"


def ensure_item_price(item: str, rate: float = 100):
	"""Some sites (workflow on Item Price) refuse Sales Orders for unapproved prices. Create a price
	and, when the site has a workflow_state column, mark it Approved - the real rule is satisfied."""
	price_list = selling_price_list()
	name = frappe.db.get_value("Item Price", {"item_code": item, "price_list": price_list})
	if not name:
		doc = frappe.get_doc(
			{"doctype": "Item Price", "item_code": item, "price_list": price_list, "price_list_rate": rate, "selling": 1}
		)
		doc.insert(ignore_permissions=True)
		name = doc.name
	if frappe.get_meta("Item Price").has_field("workflow_state"):
		frappe.db.set_value("Item Price", name, "workflow_state", "Approved")


def ensure_stock(item: str, warehouse: str, qty: float = 100000):
	"""Bin rows only (no ledger): enough for sites whose Work Order submit checks Bin.actual_qty."""
	name = frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse})
	if name:
		frappe.db.set_value("Bin", name, "actual_qty", qty)
		return
	frappe.get_doc({"doctype": "Bin", "item_code": item, "warehouse": warehouse, "actual_qty": qty}).insert(
		ignore_permissions=True
	)


def make_sales_order(company: str, customer: str, lines: list[tuple], *, warehouse: str | None = None, delivery_in_days: int = 10, submit: bool = True):
	"""lines: [(item_code, qty)] or [(item_code, qty, rate)]"""
	items = []
	for line in lines:
		item, qty = line[0], line[1]
		rate = line[2] if len(line) > 2 else 100
		ensure_item_price(item, rate)
		items.append(
			{
				"item_code": item,
				"qty": qty,
				"rate": rate,
				"delivery_date": add_days(nowdate(), delivery_in_days),
				"warehouse": warehouse,
			}
		)
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": company,
			"customer": customer,
			"transaction_date": nowdate(),
			"delivery_date": add_days(nowdate(), delivery_in_days),
			"order_type": "Sales",
			"selling_price_list": selling_price_list(),
			"items": items,
		}
	)
	so.insert(ignore_permissions=True)
	if submit:
		so.submit()
	return so


def make_plan(company: str, sales_orders: list[str], *, combine: bool = False, auto_create: bool = False,
	auto_submit: bool = False, priority: str = "Medium", warehouse: str | None = None, submit: bool = False):
	"""A standard ERPNext Production Plan built exactly as the UI does: add Sales Orders, 'Get Items'."""
	plan = frappe.new_doc(PLAN_DT)
	plan.company = company
	plan.get_items_from = "Sales Order"
	plan.posting_date = nowdate()
	plan.combine_items = int(combine)
	plan.ppc_auto_create_work_orders = int(auto_create)
	plan.ppc_auto_submit_work_orders = int(auto_submit)
	plan.ppc_priority = priority
	for so in sales_orders:
		plan.append("sales_orders", {"sales_order": so})
	plan.get_items()
	start = add_to_date(now_datetime(), days=1)  # one value: rows of the same item can then be combined
	for row in plan.po_items:
		row.planned_start_date = start
		if warehouse:
			row.warehouse = warehouse
	plan.insert(ignore_permissions=True)
	if submit:
		plan.submit()
	return plan


def ensure_manufacturing_defaults(company: str):
	"""Work Orders need default warehouses; give the site ones for the duration of the test."""
	wip = make_warehouse(f"{PREFIX} WIP", company)
	fg = make_warehouse(f"{PREFIX} FG", company)
	frappe.db.set_single_value("Manufacturing Settings", "default_wip_warehouse", wip)
	frappe.db.set_single_value("Manufacturing Settings", "default_fg_warehouse", fg)
	return wip, fg


class PPCTestCase(FrappeTestCase):
	"""Each test runs in its own savepoint so tests cannot influence each other."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.company = get_company()
		cls.wip, cls.fg = ensure_manufacturing_defaults(cls.company)
		cls.customer = make_customer()
		cls.ws1 = make_workstation(f"{PREFIX} WS1", capacity=1)
		cls.ws2 = make_workstation(f"{PREFIX} WS2", capacity=1)
		cls.op_cut = make_operation(f"{PREFIX} Cut", cls.ws1)
		cls.op_asm = make_operation(f"{PREFIX} Assemble", cls.ws2)
		cls.rm1 = make_item(f"{PREFIX}-RM-1")
		cls.rm2 = make_item(f"{PREFIX}-RM-2")
		cls.rm_wh = make_warehouse(f"{PREFIX} RM", cls.company)
		ensure_stock(cls.rm1, cls.rm_wh)
		ensure_stock(cls.rm2, cls.rm_wh)
		# finished goods: with operations / without operations
		cls.fg_ops = make_item(f"{PREFIX}-FG-OPS", manufacture=True)
		cls.fg_plain = make_item(f"{PREFIX}-FG-PLAIN", manufacture=True)
		cls.bom_ops = make_bom(
			cls.fg_ops,
			cls.company,
			[(cls.rm1, 2), (cls.rm2, 1)],
			[
				{"operation": cls.op_cut, "workstation": cls.ws1, "time_in_mins": 30, "sequence_id": 1},
				{"operation": cls.op_asm, "workstation": cls.ws2, "time_in_mins": 60, "sequence_id": 2},
			],
			source_warehouse=cls.rm_wh,
		).name
		cls.bom_plain = make_bom(cls.fg_plain, cls.company, [(cls.rm1, 1)], source_warehouse=cls.rm_wh).name

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("ppc_case")
		frappe.flags.ppc_in_sync = False

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="ppc_case")

	# conveniences ------------------------------------------------------------------------
	def new_so(self, *lines, **kw):
		kw.setdefault("warehouse", self.fg)
		return make_sales_order(self.company, self.customer, list(lines), **kw)

	def new_plan(self, sales_orders, **kw):
		return make_plan(self.company, sales_orders if isinstance(sales_orders, list) else [sales_orders], **kw)

	def ops_plan(self, qty: int = 10, *, submit_work_orders: bool = True):
		"""A submitted standard plan whose single Work Order has two operations (=> two Job Cards)."""
		so = self.new_so((self.fg_ops, qty))
		plan = self.new_plan(so.name, auto_create=True, auto_submit=submit_work_orders)
		plan.submit()
		plan.reload()
		return plan, self.work_order_of(plan, self.fg_ops)

	def work_orders_of(self, plan, item_code: str | None = None) -> list:
		filters = {"production_plan": plan.name if hasattr(plan, "name") else plan, "docstatus": ["<", 2]}
		if item_code:
			filters["production_item"] = item_code
		return frappe.get_all("Work Order", filters=filters, fields=["name", "docstatus", "status", "qty", "production_item",
			"bom_no", "sales_order", "sales_order_item", "production_plan_item", "planned_start_date", "fg_warehouse",
			"wip_warehouse", "transfer_material_against"], order_by="creation asc")

	def work_order_of(self, plan, item_code: str) -> str:
		rows = self.work_orders_of(plan, item_code)
		return rows[0].name if rows else None

	def job_cards(self, wo: str, include_cancelled: bool = False) -> list:
		"""Job Cards of a Work Order, in sequence order."""
		filters = {"work_order": wo}
		if not include_cancelled:
			filters["docstatus"] = ["<", 2]
		return frappe.get_all(
			"Job Card",
			filters=filters,
			fields=["name", "operation", "sequence_id", "status", "docstatus", "expected_start_date",
				"expected_end_date", "workstation", "for_quantity", "modified"],
			order_by="sequence_id asc",
		)

	def complete_job_card(self, name: str):
		"""Record actual work so ERPNext's own submit rules (time logs, completed qty) are met."""
		doc = frappe.get_doc("Job Card", name)
		end = add_to_date(now_datetime(), hours=-1)
		doc.append("time_logs", {"from_time": add_to_date(end, hours=-1), "to_time": end, "completed_qty": doc.for_quantity})
		doc.save()
		return doc
