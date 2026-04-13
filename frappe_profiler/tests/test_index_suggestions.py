# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for frappe_profiler.analyzers.index_suggestions.

This analyzer imports frappe.core.doctype.recorder.recorder._optimize_query
inside the function. To test in isolation we monkeypatch that import
using pytest's `monkeypatch` fixture, replacing _optimize_query with a
stub that returns a canned DBIndex or None.
"""

import json
import sys
import types

import pytest

from frappe_profiler.analyzers import index_suggestions


class _FakeDBIndex:
	def __init__(self, table, column):
		self.table = table
		self.column = column


def _install_fake_recorder_module(monkeypatch, optimize_query_impl):
	"""Register a fake frappe.core.doctype.recorder.recorder module.

	index_suggestions.analyze imports _optimize_query at call time via:
	    from frappe.core.doctype.recorder.recorder import _optimize_query
	We install a minimal fake module hierarchy in sys.modules so the
	import succeeds without a real Frappe site.
	"""
	# Build the module chain only if not already present
	for mod_name in (
		"frappe",
		"frappe.core",
		"frappe.core.doctype",
		"frappe.core.doctype.recorder",
		"frappe.core.doctype.recorder.recorder",
	):
		if mod_name not in sys.modules:
			monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))

	# Attach the fake _optimize_query to the recorder submodule
	recorder_mod = sys.modules["frappe.core.doctype.recorder.recorder"]
	monkeypatch.setattr(recorder_mod, "_optimize_query", optimize_query_impl, raising=False)


def test_single_suggestion_per_table_column(monkeypatch, empty_context):
	"""Multiple unique queries suggesting the same index → one finding."""

	def fake_optimize(query: str):
		return _FakeDBIndex("tabLead", "status")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 200,
		"calls": [
			{
				"query": f"SELECT * FROM tabLead WHERE status = '{s}'",
				"normalized_query": f"SELECT * FROM tabLead WHERE STATUS = '{s}'",
				"duration": 30.0,
				"stack": [],
			}
			for s in ("Open", "Closed", "Qualified", "Lost")
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	# 4 unique normalized queries → 4 optimizer calls → 1 deduped suggestion
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1
	assert "tabLead" in missing[0]["title"]
	assert "status" in missing[0]["title"]
	detail = json.loads(missing[0]["technical_detail_json"])
	assert detail["table"] == "tabLead"
	assert detail["column"] == "status"
	assert detail["affected_query_count"] == 4
	assert "ALTER TABLE" in detail["suggested_ddl"]


def test_optimizer_exception_produces_warning(monkeypatch, empty_context):
	"""Fix #9: failures must surface as warnings, not be silently dropped."""

	def fake_optimize(query: str):
		raise ValueError("synthetic parse error")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix2",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 50,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT ?",
				"duration": 5.0,
				"stack": [],
			},
			{
				"query": "SELECT 2",
				"normalized_query": "SELECT ??",
				"duration": 5.0,
				"stack": [],
			},
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert result.findings == []
	assert len(result.warnings) == 1
	assert "Could not analyze" in result.warnings[0]
	assert "ValueError" in result.warnings[0]


def test_no_suggestion_when_optimizer_returns_none(monkeypatch, empty_context):
	def fake_optimize(query: str):
		return None

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix3",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 50,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT ?",
				"duration": 5.0,
				"stack": [],
			},
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert result.findings == []
	assert result.warnings == []


def test_severity_scales_with_savings(monkeypatch, empty_context):
	"""Large cumulative savings → High severity."""

	def fake_optimize(query: str):
		return _FakeDBIndex("tabBig", "account")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix4",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 5000,
		"calls": [
			{
				"query": f"SELECT * FROM tabBig WHERE account = 'A{i}'",
				"normalized_query": f"SELECT * FROM tabBig WHERE ACCOUNT = 'A{i}'",
				"duration": 200.0,
				"stack": [],
			}
			for i in range(5)  # 5 × 200ms = 1000ms
		],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert len(result.findings) == 1
	# 1000ms > HIGH_IMPACT_MS (500)
	assert result.findings[0]["severity"] == "High"
