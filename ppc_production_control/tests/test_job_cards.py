"""Job Card tracking / generation / duplicate prevention / bulk submission for a standard Production Plan."""

import frappe

from ppc_production_control.services import job_cards as jc_service
from ppc_production_control.services import sync
from ppc_production_control.tests.utils import PPCTestCase


class TestJobCards(PPCTestCase):
	# 11/13. BOM with operations -> Job Cards tagged with the plan ------------------------------------
	def test_cards_follow_bom_operations_and_belong_to_the_plan(self):
		plan, wo = self.ops_plan(10)
		cards = self.job_cards(wo)
		self.assertEqual([c.operation for c in cards], [self.op_cut, self.op_asm])
		self.assertEqual([c.sequence_id for c in cards], [1, 2])
		self.assertEqual([c.workstation for c in cards], [self.ws1, self.ws2])
		self.assertEqual({c.for_quantity for c in cards}, {10})
		for c in cards:
			doc = frappe.get_doc("Job Card", c.name)
			self.assertEqual(doc.ppc_production_plan, plan.name, "tagged when ERPNext created it")
			self.assertEqual(doc.work_order, wo)
			self.assertTrue(doc.ppc_original_start and doc.ppc_original_end, "first schedule remembered")
			self.assertEqual(doc.ppc_original_start, doc.expected_start_date)

	# 12. BOM without operations -> no artificial cards -------------------------------------------------
	def test_no_cards_for_bom_without_operations(self):
		so = self.new_so((self.fg_plain, 5))
		plan = self.new_plan(so.name, auto_create=True, auto_submit=True)
		plan.submit()
		wo = self.work_order_of(plan, self.fg_plain)
		self.assertEqual(frappe.db.count("Job Card", {"work_order": wo}), 0)
		res = jc_service.generate_job_cards(plan.name)
		self.assertEqual(frappe.db.count("Job Card", {"work_order": wo}), 0)
		self.assertIn("no operations", res["work_orders"][0]["message"])

	def test_draft_work_order_has_no_cards_to_generate(self):
		plan, wo = self.ops_plan(10, submit_work_orders=False)
		res = jc_service.generate_job_cards(plan.name)
		self.assertEqual(res["work_orders"][0]["created"], 0)
		self.assertIn("Draft", res["work_orders"][0]["message"])
		self.assertEqual(frappe.db.count("Job Card", {"work_order": wo}), 0)

	# 14. duplicate prevention --------------------------------------------------------------------------
	def test_generate_and_refresh_never_duplicate(self):
		plan, wo = self.ops_plan(10)
		before = [c.name for c in self.job_cards(wo)]
		for _ in range(3):
			res = jc_service.generate_job_cards(plan.name)
			self.assertEqual(res["work_orders"][0]["created"], 0)
			sync.refresh_plan(plan.name)
			sync.sync_job_cards(plan.name)
		self.assertEqual([c.name for c in self.job_cards(wo)], before)

	def test_only_the_missing_operation_is_regenerated(self):
		plan, wo = self.ops_plan(10)
		cards = self.job_cards(wo)
		frappe.delete_doc("Job Card", cards[1].name)  # lose the 'Assemble' card

		res = jc_service.generate_job_cards(plan.name)
		self.assertEqual(res["work_orders"][0]["created"], 1)
		again = self.job_cards(wo)
		self.assertEqual([c.operation for c in again], [self.op_cut, self.op_asm])
		self.assertEqual(again[0].name, cards[0].name, "the surviving card is untouched")
		self.assertEqual(frappe.db.get_value("Job Card", again[1].name, "ppc_production_plan"), plan.name)

		self.assertEqual(jc_service.generate_job_cards(plan.name)["work_orders"][0]["created"], 0)
		self.assertEqual(len(self.job_cards(wo)), 2)

	def test_refresh_retags_cards_and_reports_missing_baselines(self):
		plan, wo = self.ops_plan(10)
		for c in self.job_cards(wo):
			frappe.db.set_value("Job Card", c.name, {"ppc_production_plan": None, "ppc_original_start": None, "ppc_original_end": None})
		out = sync.sync_job_cards(plan.name)
		self.assertEqual((out["tagged"], out["missing_baseline"], out["cards"]), (2, 2, 2))
		self.assertEqual(frappe.db.count("Job Card", {"ppc_production_plan": plan.name}), 2)
		self.assertEqual(frappe.db.count("Job Card", {"ppc_production_plan": plan.name, "ppc_original_start": ["is", "set"]}), 0,
			"a refresh must not invent a baseline: it only reports the missing ones")
		self.assertEqual(sync.sync_job_cards(plan.name)["tagged"], 0, "second run changes nothing")

	def test_cancelled_card_is_left_alone(self):
		plan, wo = self.ops_plan(10)
		cards = self.job_cards(wo)
		self.complete_job_card(cards[0].name)
		jc_service.submit_selected_job_cards(plan.name, [cards[0].name])
		frappe.get_doc("Job Card", cards[0].name).cancel()
		sync.refresh_plan(plan.name)
		self.assertEqual(frappe.db.get_value("Job Card", cards[0].name, "docstatus"), 2)

	# 16. bulk Job Card submission ---------------------------------------------------------------------------
	def test_bulk_submit_runs_predecessors_first_and_reports_each(self):
		plan, wo = self.ops_plan(10)
		cards = self.job_cards(wo)
		for c in cards:
			self.complete_job_card(c.name)
		other_plan, other_wo = self.ops_plan(5)
		foreign = self.job_cards(other_wo)[0].name

		sel = [cards[1].name, cards[0].name, foreign, "JC-NOPE"]  # ticked "backwards" + foreign + unknown
		res = jc_service.submit_selected_job_cards(plan.name, sel)
		by = {r["name"]: r for r in res["results"]}
		self.assertTrue(by[cards[0].name]["ok"], by[cards[0].name])
		self.assertTrue(by[cards[1].name]["ok"], by[cards[1].name])
		self.assertTrue(by[foreign]["skipped"])
		self.assertEqual(frappe.db.get_value("Job Card", foreign, "docstatus"), 0)
		self.assertTrue(by["JC-NOPE"]["skipped"])
		self.assertEqual((res["submitted"], res["skipped"], res["failed"]), (2, 2, 0))
		for c in cards:
			self.assertEqual(frappe.db.get_value("Job Card", c.name, "docstatus"), 1)

		res = jc_service.submit_selected_job_cards(plan.name, [cards[0].name])  # never twice
		self.assertTrue(res["results"][0]["skipped"])
		self.assertIn("Already submitted", res["results"][0]["message"])

	def test_out_of_sequence_submit_is_reported_not_bypassed(self):
		plan, wo = self.ops_plan(10)
		cards = self.job_cards(wo)
		self.complete_job_card(cards[1].name)  # second operation done before the first
		res = jc_service.submit_selected_job_cards(plan.name, [cards[1].name])
		self.assertEqual(res["failed"], 1)
		message = res["results"][0]["message"]
		self.assertIn("must be completed before", message)
		self.assertNotIn("created", message, "the message must not carry earlier alerts of the same request")
		self.assertEqual(frappe.db.get_value("Job Card", cards[1].name, "docstatus"), 0)
		failed = frappe.get_all("PPC Audit Log", filters={"ppc_plan": plan.name, "job_card": cards[1].name, "result": "Failed"})
		self.assertEqual(len(failed), 1)

	def test_failure_of_one_card_does_not_stop_the_others(self):
		plan, wo = self.ops_plan(10)
		other_plan, other_wo = self.ops_plan(5)
		a, b = self.job_cards(wo), self.job_cards(other_wo)
		self.complete_job_card(a[1].name)  # out of sequence -> fails
		self.complete_job_card(b[0].name)  # first operation of the other plan -> succeeds
		res = jc_service.submit_selected_job_cards(plan.name, [a[1].name])
		self.assertEqual((res["submitted"], res["failed"]), (0, 1))
		res = jc_service.submit_selected_job_cards(other_plan.name, [b[0].name])
		self.assertEqual((res["submitted"], res["failed"]), (1, 0))

	def test_cancelled_job_card_is_not_submitted(self):
		plan, wo = self.ops_plan(10)
		cards = self.job_cards(wo)
		self.complete_job_card(cards[0].name)
		jc_service.submit_selected_job_cards(plan.name, [cards[0].name])
		frappe.get_doc("Job Card", cards[0].name).cancel()
		res = jc_service.submit_selected_job_cards(plan.name, [cards[0].name])
		self.assertTrue(res["results"][0]["skipped"])
		self.assertIn("Cancelled", res["results"][0]["message"])

	def test_batch_limit_is_enforced(self):
		plan, _wo = self.ops_plan(10)
		with self.assertRaises(frappe.ValidationError):
			jc_service.submit_selected_job_cards(plan.name, [f"JC-{i}" for i in range(jc_service.MAX_PER_CALL + 1)])
