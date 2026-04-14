# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for v0.4.0 pin_baseline / unpin_baseline / set_comparison endpoints."""

import pytest

from frappe_profiler import api


class FakeCache:
	def __init__(self):
		self.store = {}

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value

	def get_value(self, key):
		return self.store.get(key)

	def delete_value(self, key):
		self.store.pop(key, None)


class FakeDB:
	def __init__(self, rows):
		self.rows = rows

	def get_value(self, doctype, filters, fields=None, as_dict=False):
		assert doctype == "Profiler Session"
		# Find row matching the filter
		row = None
		if isinstance(filters, dict):
			for r in self.rows.values():
				if all(r.get(k) == v for k, v in filters.items()):
					row = r
					break
		elif isinstance(filters, str):
			# filters is a docname
			row = self.rows.get(filters)
		if row is None:
			return None
		if fields is None:
			return row["name"]
		if isinstance(fields, str):
			return row.get(fields)
		if as_dict:
			return {f: row.get(f) for f in fields}
		return tuple(row.get(f) for f in fields)

	def set_value(self, doctype, name, field_or_dict, value=None):
		row = self.rows.get(name)
		if row is None:
			return
		if isinstance(field_or_dict, dict):
			row.update(field_or_dict)
		else:
			row[field_or_dict] = value

	def commit(self):
		pass

	def exists(self, *a, **kw):
		return True

	def count(self, doctype, filters=None):
		return 0


@pytest.fixture
def fake_env(monkeypatch):
	import frappe

	rows = {
		"PS-001": {
			"name": "PS-001",
			"session_uuid": "uuid-001",
			"label": "Sales Invoice flow",
			"title": "Sales Invoice flow",
			"user": "Administrator",
			"status": "Ready",
			"is_baseline": 0,
		},
		"PS-002": {
			"name": "PS-002",
			"session_uuid": "uuid-002",
			"label": "Sales Invoice flow",
			"title": "Sales Invoice flow",
			"user": "Administrator",
			"status": "Ready",
			"is_baseline": 0,
		},
	}

	cache = FakeCache()
	fake_db = FakeDB(rows)

	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "db", fake_db, raising=False)
	monkeypatch.setattr(
		frappe, "session",
		type("S", (), {"user": "Administrator"})(),
		raising=False,
	)
	monkeypatch.setattr(frappe, "get_roles", lambda u: ["System Manager"], raising=False)
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **kw: None, raising=False)

	return cache, rows


def test_pin_baseline_sets_cache_key_and_flag(fake_env):
	cache, rows = fake_env
	result = api.pin_baseline(session_uuid="uuid-001")
	assert result["pinned"] is True
	assert cache.store.get("profiler:baseline:Sales Invoice flow") == "PS-001"
	assert rows["PS-001"]["is_baseline"] == 1


def test_pin_overwrites_previous_baseline_for_same_label(fake_env):
	cache, rows = fake_env
	api.pin_baseline(session_uuid="uuid-001")
	# Now pin PS-002 — should clear PS-001's flag
	api.pin_baseline(session_uuid="uuid-002")
	assert cache.store.get("profiler:baseline:Sales Invoice flow") == "PS-002"
	assert rows["PS-001"]["is_baseline"] == 0
	assert rows["PS-002"]["is_baseline"] == 1


def test_unpin_baseline_clears_cache_key_and_flag(fake_env):
	cache, rows = fake_env
	api.pin_baseline(session_uuid="uuid-001")
	api.unpin_baseline(session_uuid="uuid-001")
	assert "profiler:baseline:Sales Invoice flow" not in cache.store
	assert rows["PS-001"]["is_baseline"] == 0


def test_set_comparison_one_off(fake_env, monkeypatch):
	cache, rows = fake_env
	rows["PS-001"]["compared_to_session"] = None

	result = api.set_comparison(session_uuid="uuid-001", compared_to="PS-002")
	assert result["set"] is True
	assert rows["PS-001"]["compared_to_session"] == "PS-002"


def test_api_start_inherits_baseline_from_cache(fake_env, monkeypatch):
	"""When a baseline is pinned for a label, api.start with that label
	pre-populates compared_to_session on the new session."""
	import datetime as _dt

	import frappe
	import frappe_profiler.api as api_module

	cache, rows = fake_env
	cache.store["profiler:baseline:smoke test"] = "PS-001"

	created = {}

	class FakeDoc:
		def __init__(self, fields):
			self.fields = dict(fields)

		def insert(self, ignore_permissions=False):
			created["doc"] = self.fields
			self.name = "PS-new-001"
			return self

	def fake_get_doc(arg):
		if isinstance(arg, dict) and arg.get("doctype") == "Profiler Session":
			return FakeDoc(arg)
		return None

	monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)
	monkeypatch.setattr(frappe, "generate_hash", lambda length: "fake-uuid", raising=False)
	# api.py imports now_datetime as a top-level name — patch it on the
	# api module so we don't hit Frappe's system-settings lookup.
	monkeypatch.setattr(
		api_module, "now_datetime",
		lambda: _dt.datetime(2026, 4, 14, 12, 0, 0),
		raising=False,
	)
	# api._require_profiler_user calls _require_user which reads frappe.session.user
	# (already stubbed in fake_env) — but it also calls frappe.get_roles and we
	# stubbed that too.
	monkeypatch.setattr(
		"frappe_profiler.session.set_session_meta",
		lambda *a, **kw: None,
		raising=False,
	)
	monkeypatch.setattr(
		"frappe_profiler.session.set_active_session",
		lambda *a, **kw: None,
		raising=False,
	)
	monkeypatch.setattr(
		"frappe_profiler.session.get_active_session_for",
		lambda u: None,
		raising=False,
	)
	monkeypatch.setattr(
		"frappe_profiler.capture._force_stop_inflight_capture",
		lambda local_proxy: None,
		raising=False,
	)

	api.start(label="smoke test")
	assert created["doc"].get("compared_to_session") == "PS-001"
