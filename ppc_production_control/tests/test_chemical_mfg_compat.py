"""PPC must coexist with the chemical_mfg app on the same site (same Job Card / Work Order hooks and overrides).

Skipped, not failed, when chemical_mfg is not installed on the site under test."""

import unittest
from datetime import timedelta

import frappe

from ppc_production_control.services import scheduling
from ppc_production_control.tests.utils import PPCTestCase

INSTALLED = "chemical_mfg" in frappe.get_installed_apps()


@unittest.skipUnless(INSTALLED, "chemical_mfg is not installed on this site")
class TestChemicalMfgCompatibility(PPCTestCase):
	def setUp(self):
		super().setUp()
		self.plan, self.wo = self.ops_plan(10)
		self.cut, self.asm = self.job_cards(self.wo)

	def card(self, name):
		return frappe.db.get_value("Job Card", name, ["expected_start_date", "expected_end_date", "ppc_schedule_revision", "quality_inspection_template"], as_dict=True)

	def test_both_apps_hook_the_same_events_without_replacing_each_other(self):
		hooks = frappe.get_hooks("doc_events")
		validators = hooks["Job Card"]["validate"]
		validators = [validators] if isinstance(validators, str) else validators
		self.assertTrue(any(v.startswith("chemical_mfg.") for v in validators), validators)
		self.assertTrue(any(v.startswith("ppc_production_control.") for v in validators), validators)
		self.assertTrue(any(h.startswith("chemical_mfg.") for h in frappe.get_hooks("override_doctype_class").get("Job Card", [])))
		self.assertIn("Job Card", frappe.get_hooks("override_doctype_class"))

	def test_job_cards_created_by_the_work_order_follow_the_bom_once(self):
		cards = self.job_cards(self.wo)
		self.assertEqual([c.operation for c in cards], [self.op_cut, self.op_asm])
		self.assertEqual(frappe.db.count("Job Card", {"work_order": self.wo}), 2, "no duplicates from two apps reacting to the same submit")
		for c in cards:
			self.assertEqual(frappe.db.get_value("Job Card", c.name, "ppc_schedule_revision"), 0)
			self.assertEqual(frappe.db.count("PPC Schedule Revision", {"job_card": c.name}), 1)

	def test_quality_template_survives_a_reschedule(self):
		if not frappe.get_meta("Work Order Operation").has_field("custom_quality_template"):
			self.skipTest("custom_quality_template field missing")
		tpl = "PPC-TEST-QIT"
		if not frappe.db.exists("Quality Inspection Parameter", "PPC-TEST-QIP"):
			frappe.get_doc({"doctype": "Quality Inspection Parameter", "parameter": "PPC-TEST-QIP"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Quality Inspection Template", tpl):
			frappe.get_doc({"doctype": "Quality Inspection Template", "quality_inspection_template_name": tpl,
				"item_quality_inspection_parameter": [{"specification": "PPC-TEST-QIP"}]}).insert(ignore_permissions=True)
		frappe.db.set_value("Work Order Operation", {"parent": self.wo, "operation": self.op_asm}, "custom_quality_template", tpl)
		# chemical_mfg copies it onto the Job Card on save
		doc = frappe.get_doc("Job Card", self.asm.name)
		doc.save()
		self.assertEqual(self.card(self.asm.name).quality_inspection_template, tpl)

		c = self.card(self.asm.name)
		res = scheduling.reschedule_job_card(self.asm.name, c.expected_start_date + timedelta(days=1), c.expected_end_date + timedelta(days=1), confirmed=True)
		self.assertTrue(res["ok"], res)
		self.assertEqual(self.card(self.asm.name).quality_inspection_template, tpl, "chemical_mfg's field is not disturbed by PPC's save")

	def test_reschedule_is_not_recursive_and_writes_exactly_one_audit_and_revision(self):
		before_cards = frappe.db.count("Job Card", {"work_order": self.wo})
		audits = frappe.db.count("PPC Audit Log", {"action": "Reschedule Job Card", "job_card": self.asm.name})
		c = self.card(self.asm.name)
		res = scheduling.reschedule_job_card(self.asm.name, c.expected_start_date + timedelta(days=1), c.expected_end_date + timedelta(days=1), confirmed=True)
		self.assertTrue(res["ok"], res)
		self.assertEqual(frappe.db.count("Job Card", {"work_order": self.wo}), before_cards)
		self.assertEqual(frappe.db.count("PPC Audit Log", {"action": "Reschedule Job Card", "job_card": self.asm.name}), audits + 1)
		self.assertEqual(frappe.db.count("PPC Schedule Revision", {"job_card": self.asm.name}), 2)
		self.assertEqual(self.card(self.asm.name).ppc_schedule_revision, 1)

	def test_work_order_sync_goes_through_chemical_mfgs_work_order_class_cleanly(self):
		c = self.card(self.asm.name)
		res = scheduling.reschedule_job_card(self.asm.name, c.expected_start_date + timedelta(days=2), c.expected_end_date + timedelta(days=2), confirmed=True)
		self.assertTrue(res["ok"], res)
		self.assertEqual(frappe.db.get_value("Work Order", self.wo, "docstatus"), 1)
		self.assertEqual(scheduling.as_dt(frappe.db.get_value("Work Order", self.wo, "planned_end_date")).replace(microsecond=0),
			self.card(self.asm.name).expected_end_date.replace(microsecond=0))

	def test_chemical_mfg_sequence_rule_still_applies_and_leaves_no_revision(self):
		self.complete_job_card(self.asm.name)  # second operation finished before the first
		from ppc_production_control.services import job_cards as jc_service

		res = jc_service.submit_selected_job_cards(self.plan.name, [self.asm.name])
		self.assertEqual(res["failed"], 1)
		self.assertEqual(frappe.db.count("PPC Schedule Revision", {"job_card": self.asm.name}), 1)
		self.assertEqual(frappe.db.get_value("Job Card", self.asm.name, "docstatus"), 0)
