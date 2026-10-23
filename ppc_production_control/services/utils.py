"""Small helpers shared by the PPC services."""

import hashlib
import json
import uuid
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import get_datetime

from ppc_production_control.services.constants import DATETIME_FORMAT, SETTINGS_DT


def parse_json(value, default=None):
	"""Accept a JSON string, an already-parsed value, or None."""
	if value is None or value == "":
		return default
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			frappe.throw(_("Invalid JSON payload."), frappe.ValidationError)
	return value


def as_list(value) -> list:
	value = parse_json(value, default=[])
	if isinstance(value, (list, tuple, set)):
		return list(value)
	return [value]


def dt_str(value) -> str | None:
	"""Naive 'YYYY-MM-DD HH:MM:SS' string (Frappe stores datetimes in system time zone, naive)."""
	if not value:
		return None
	return get_datetime(value).strftime(DATETIME_FORMAT)


def new_row_key() -> str:
	return uuid.uuid4().hex[:12]


def make_key(*parts) -> str:
	"""Deterministic idempotency key for a (plan, row) pair."""
	raw = "|".join(str(p) for p in parts)
	return hashlib.sha256(raw.encode()).hexdigest()[:32]


def get_settings():
	return frappe.get_single(SETTINGS_DT)


def in_test() -> bool:
	return bool(frappe.flags.in_test)


def commit_if_allowed():
	"""Persist progress between independent units of work.

	Skipped under the test runner, where every test case runs inside one rolled-back transaction.
	"""
	if not in_test():
		frappe.db.commit()


@contextmanager
def savepoint():
	"""Run a block in a DB savepoint: roll back the block and re-raise on error."""
	name = "ppc_" + uuid.uuid4().hex[:12]
	frappe.local.message_log = []  # each unit of work reports only its own messages
	frappe.db.savepoint(name)
	try:
		yield name
	except Exception:
		frappe.db.rollback(save_point=name)
		raise
	else:
		frappe.db.release_savepoint(name)


def lock_plan(plan_name: str):
	"""Serialise orchestration of one plan across workers (row-level `SELECT ... FOR UPDATE`)."""
	frappe.db.sql("select name from `tabProduction Plan` where name = %s for update", (plan_name,))


def error_text(exc: BaseException) -> str:
	"""A message that is safe to show to end users (no stack trace).

	Uses the exception's own message - never the whole message log, which in a bulk run also holds
	the alerts of every earlier (successful) record.
	"""
	text = frappe.utils.strip_html_tags(str(exc)).strip()
	if not text:
		text = _last_message() or exc.__class__.__name__
	frappe.local.message_log = []
	return " ".join(text.split())[:1000]


def _last_message() -> str:
	for entry in reversed(list(frappe.local.message_log or [])):
		try:
			data = json.loads(entry) if isinstance(entry, str) else entry
			message = frappe.utils.strip_html_tags((data or {}).get("message", "")).strip()
			if message:
				return message
		except Exception:
			continue
	return ""


def enqueue_once(method: str, job_id: str, queue: str = "default", timeout: int = 1800, **kwargs):
	"""Queue a background job unless the same job is already waiting or running.

	Under the test runner the job executes inline so tests stay deterministic.
	"""
	if in_test():
		return frappe.call(method, **kwargs)
	return frappe.enqueue(
		method,
		queue=queue,
		timeout=timeout,
		job_id=job_id,
		deduplicate=True,
		enqueue_after_commit=True,
		**kwargs,
	)


def chunks(items: list, size: int):
	size = max(1, int(size or 1))
	for i in range(0, len(items), size):
		yield items[i : i + size]


def short_exception(exc: BaseException) -> str:
	return (str(exc) or exc.__class__.__name__)[:500]
