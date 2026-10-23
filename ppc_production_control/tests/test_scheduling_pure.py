"""Scheduling maths that needs no database."""

import unittest
from datetime import datetime, timedelta

from ppc_production_control.services import scheduling as s


def dt(day, hh, mm=0):
	return datetime(2027, 3, day, hh, mm)


def job(name, seq, start, end, status="Open", docstatus=0):
	return {
		"name": name,
		"sequence_id": seq,
		"expected_start_date": start,
		"expected_end_date": end,
		"status": status,
		"docstatus": docstatus,
	}


class TestPureScheduling(unittest.TestCase):
	def test_peak_concurrency(self):
		self.assertEqual(s.peak_concurrency([]), 0)
		self.assertEqual(s.peak_concurrency([(dt(1, 8), dt(1, 10))]), 1)
		# back to back is not an overlap
		self.assertEqual(s.peak_concurrency([(dt(1, 8), dt(1, 10)), (dt(1, 10), dt(1, 12))]), 1)
		self.assertEqual(s.peak_concurrency([(dt(1, 8), dt(1, 10)), (dt(1, 9), dt(1, 12))]), 2)
		# A overlaps B and C, but B and C never overlap each other
		self.assertEqual(
			s.peak_concurrency([(dt(1, 8), dt(1, 18)), (dt(1, 9), dt(1, 11)), (dt(1, 12), dt(1, 14))]), 2
		)
		self.assertEqual(
			s.peak_concurrency([(dt(1, 8), dt(1, 18)), (dt(1, 9), dt(1, 13)), (dt(1, 12), dt(1, 14))]), 3
		)
		# zero/negative length and missing values are ignored
		self.assertEqual(s.peak_concurrency([(dt(1, 8), dt(1, 8)), (None, dt(1, 9))]), 0)

	def test_groups_and_neighbours(self):
		jobs = [job("C", 3, dt(1, 15), dt(1, 16)), job("A", 1, dt(1, 8), dt(1, 10)),
			job("B1", 2, dt(1, 10), dt(1, 12)), job("B2", 2, dt(1, 10), dt(1, 13))]
		groups = s.group_by_sequence(jobs)
		self.assertEqual([[j["name"] for j in g] for g in groups], [["A"], ["B1", "B2"], ["C"]])
		pred, succ = s.dependency_neighbours(jobs, "B1")
		self.assertEqual([j["name"] for j in pred], ["A"])
		self.assertEqual([j["name"] for j in succ], ["C"])
		self.assertEqual(s.dependency_neighbours(jobs, "A"), ([], groups[1]))
		self.assertEqual(s.dependency_neighbours(jobs, "nope"), ([], []))

	def test_cascade_preserves_gap_and_duration(self):
		jobs = [
			job("A", 1, dt(1, 8), dt(1, 10)),
			job("B", 2, dt(1, 10, 10), dt(1, 12, 10)),  # ERPNext leaves a 10 minute gap
			job("C", 3, dt(1, 12, 20), dt(1, 13, 20)),
		]
		# A moves 5 hours later
		res = s.compute_cascade(jobs, "A", dt(1, 13), dt(1, 15))
		self.assertEqual(res["blocked"], [])
		shifts = {x["name"]: x for x in res["shifts"]}
		self.assertEqual(shifts["B"]["new_start"], dt(1, 15, 10))
		self.assertEqual(shifts["B"]["new_end"] - shifts["B"]["new_start"], timedelta(hours=2))
		self.assertEqual(shifts["C"]["new_start"], dt(1, 17, 20))
		self.assertEqual(shifts["C"]["new_end"] - shifts["C"]["new_start"], timedelta(hours=1))

	def test_cascade_leaves_unaffected_and_earlier_operations_alone(self):
		jobs = [
			job("A", 1, dt(1, 8), dt(1, 10)),
			job("B", 2, dt(1, 14), dt(1, 16)),  # plenty of slack
		]
		self.assertEqual(s.compute_cascade(jobs, "A", dt(1, 9), dt(1, 11))["shifts"], [])
		# moving the LAST operation never touches the earlier ones
		self.assertEqual(s.compute_cascade(jobs, "B", dt(1, 1), dt(1, 2))["shifts"], [])

	def test_cascade_blocked_by_finished_operation(self):
		jobs = [
			job("A", 1, dt(1, 8), dt(1, 10)),
			job("B", 2, dt(1, 10, 10), dt(1, 12), status="Completed", docstatus=1),
		]
		res = s.compute_cascade(jobs, "A", dt(1, 11), dt(1, 13))
		self.assertEqual(res["shifts"], [])
		self.assertEqual([b["name"] for b in res["blocked"]], ["B"])

	def test_cascade_parallel_operations_of_one_group_are_independent(self):
		jobs = [
			job("A", 1, dt(1, 8), dt(1, 10)),
			job("B1", 2, dt(1, 10), dt(1, 12)),
			job("B2", 2, dt(1, 10), dt(1, 11)),
			job("C", 3, dt(1, 12), dt(1, 13)),
		]
		res = s.compute_cascade(jobs, "A", dt(1, 9), dt(1, 11))
		names = {x["name"] for x in res["shifts"]}
		self.assertEqual(names, {"B1", "B2", "C"})
		shifts = {x["name"]: x for x in res["shifts"]}
		self.assertEqual(shifts["B1"]["new_start"], dt(1, 11))
		self.assertEqual(shifts["B2"]["new_start"], dt(1, 11))
		self.assertEqual(shifts["C"]["new_start"], dt(1, 13))  # waits for the longer of B1/B2

	def test_unscheduled_jobs_are_ignored_by_cascade(self):
		jobs = [job("A", 1, dt(1, 8), dt(1, 10)), job("B", 2, None, None)]
		self.assertEqual(s.compute_cascade(jobs, "A", dt(1, 9), dt(1, 11))["shifts"], [])

	def test_sequential_layout(self):
		jobs = [
			job("A", 1, None, None),
			job("B1", 2, None, None),
			job("B2", 2, None, None),
			job("C", 3, None, None),
		]
		minutes = {"A": 60, "B1": 120, "B2": 30, "C": 15}
		out = {x["name"]: x for x in s.plan_sequential_schedule(s.group_by_sequence(jobs), dt(1, 8), lambda j: minutes[j["name"]])}
		self.assertEqual((out["A"]["start"], out["A"]["end"]), (dt(1, 8), dt(1, 9)))
		self.assertEqual(out["B1"]["start"], dt(1, 9))
		self.assertEqual(out["B2"]["start"], dt(1, 9))  # parallel with B1
		self.assertEqual(out["C"]["start"], dt(1, 11))  # after the longest of the group

	def test_sequential_layout_respects_already_scheduled(self):
		jobs = [job("A", 1, dt(1, 8), dt(1, 20)), job("B", 2, None, None)]
		out = s.plan_sequential_schedule(s.group_by_sequence(jobs), dt(1, 8), lambda j: 60)
		self.assertEqual([x["name"] for x in out], ["B"])
		self.assertEqual(out[0]["start"], dt(1, 20))

	def test_duration_is_never_zero(self):
		out = s.plan_sequential_schedule([[job("A", 1, None, None)]], dt(1, 8), lambda j: 0)
		self.assertGreater(out[0]["end"], out[0]["start"])

	def test_date_helpers(self):
		self.assertIsNone(s.as_dt(None))
		self.assertIsNone(s.as_dt(""))
		self.assertIsNone(s.as_dt("not a date"))
		self.assertEqual(s.as_dt("2027-03-01 08:30:00"), datetime(2027, 3, 1, 8, 30))
		self.assertEqual(s.as_dt("2027-03-01"), datetime(2027, 3, 1, 0, 0))
		self.assertEqual(s.minutes_between(dt(1, 8), dt(1, 9, 30)), 90.0)

	def test_shift_rows_keeps_fragments(self):
		rows = [(dt(1, 8), dt(1, 12)), (dt(2, 8), dt(2, 10))]
		moved = s.shift_rows(rows, timedelta(days=3))
		self.assertEqual(moved, [(dt(4, 8), dt(4, 12)), (dt(5, 8), dt(5, 10))])
