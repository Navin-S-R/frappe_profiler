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


def _install_fake_frappe_db(monkeypatch, indexed_columns_by_table, column_types_by_table):
    """Install a fake frappe.db.sql that answers SHOW INDEX and
    information_schema.columns lookups from the provided dicts.

    Args:
        indexed_columns_by_table: {"tabFoo": {"name", "parent", ...}}
        column_types_by_table:    {"tabFoo": {"name": "varchar", "json_col": "json"}}
    """
    import frappe

    def fake_sql(query, params=None, as_dict=False):
        # SHOW INDEX FROM `tabFoo`
        if "SHOW INDEX FROM" in query:
            # Parse the backtick-quoted table name.
            import re
            m = re.search(r"`([^`]+)`", query)
            if not m:
                return []
            table = m.group(1)
            cols = indexed_columns_by_table.get(table, set())
            return [
                {"Column_name": c, "Seq_in_index": 1}
                for c in cols
            ]

        # information_schema.columns lookup
        if "information_schema.columns" in query:
            table = None
            if isinstance(params, (tuple, list)) and params:
                table = params[0]
            elif isinstance(params, dict):
                table = params.get("table_name")
            cols = column_types_by_table.get(table, {})
            return [
                {"column_name": name, "data_type": dtype}
                for name, dtype in cols.items()
            ]

        return []

    class FakeDB:
        def sql(self, query, params=None, as_dict=False):
            return fake_sql(query, params, as_dict)

    monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
    monkeypatch.setattr(
        frappe, "log_error", lambda *a, **k: None, raising=False
    )


def test_already_indexed_column_is_suppressed(monkeypatch, empty_context):
    """v0.5.1 architect-review finding: Missing Index must check
    existing indexes before suggesting one. A suggestion for a
    column that's already the leftmost of an existing index is a
    false positive — the DB already has the index the user would
    'add.' Suppress it and surface a warning."""

    def fake_optimize(query):
        return _FakeDBIndex("tabCustomer", "customer_name")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabCustomer": {"name", "customer_name"}},
        column_types_by_table={
            "tabCustomer": {"name": "varchar", "customer_name": "varchar"}
        },
    )

    recording = {
        "uuid": "ix_indexed",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabCustomer WHERE customer_name = 'X'",
            "normalized_query": "SELECT * FROM tabCustomer WHERE customer_name = ?",
            "duration": 50.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    # customer_name is already indexed → no finding.
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert missing == [], (
        "Already-indexed column must NOT produce a Missing Index finding. "
        "The DBOptimizer heuristic doesn't check existing indexes — that's "
        "our job before emitting."
    )
    # And a warning must explain the suppression.
    assert any("already indexed" in w for w in result.warnings), (
        f"Expected a warning about the suppressed suggestion; got: {result.warnings}"
    )


def test_json_column_is_suppressed_as_unindexable(monkeypatch, empty_context):
    """JSON columns can't be btree-indexed directly. Suggesting
    ADD INDEX (json_col) would fail at DDL apply time. Drop it."""

    def fake_optimize(query):
        return _FakeDBIndex("tabThing", "meta")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabThing": {"name"}},
        column_types_by_table={"tabThing": {"name": "varchar", "meta": "json"}},
    )

    recording = {
        "uuid": "ix_json",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabThing WHERE JSON_EXTRACT(meta, '$.k') = 'v'",
            "normalized_query": "SELECT * FROM tabThing WHERE JSON_EXTRACT(meta, ?) = ?",
            "duration": 80.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert missing == []
    assert any("can't be btree-indexed" in w for w in result.warnings)


def test_text_column_gets_prefix_index_ddl(monkeypatch, empty_context):
    """TEXT/BLOB columns require a prefix length on the index.
    The plain DDL 'ADD INDEX idx_col (col)' fails with 'BLOB/TEXT
    column used in key specification without a key length.' The
    analyzer must emit a prefix-index DDL for these types."""

    def fake_optimize(query):
        return _FakeDBIndex("tabDoc", "body")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabDoc": {"name"}},
        column_types_by_table={"tabDoc": {"name": "varchar", "body": "text"}},
    )

    recording = {
        "uuid": "ix_text",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabDoc WHERE body LIKE 'keyword%'",
            "normalized_query": "SELECT * FROM tabDoc WHERE body LIKE ?",
            "duration": 60.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert len(missing) == 1
    detail = json.loads(missing[0]["technical_detail_json"])
    # DDL must include a prefix length — something like `body`(255).
    ddl = detail["suggested_ddl"]
    assert "`body`(" in ddl or "body(" in ddl, (
        f"TEXT column DDL must include prefix length; got: {ddl}"
    )
    # And the prefix must be a non-zero number of characters.
    assert "(255)" in ddl or "(767)" in ddl or "(100)" in ddl, (
        f"TEXT column DDL must specify a non-zero prefix length; got: {ddl}"
    )
    assert detail["verified_not_indexed"] is True


def test_nonexistent_column_is_suppressed(monkeypatch, empty_context):
    """If the SQL parser hallucinates or mis-parses and suggests a
    column that doesn't exist on the table, drop the suggestion."""

    def fake_optimize(query):
        return _FakeDBIndex("tabItem", "nonexistent_column")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabItem": {"name"}},
        column_types_by_table={"tabItem": {"name": "varchar", "item_name": "varchar"}},
    )

    recording = {
        "uuid": "ix_ghost",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabItem WHERE something",
            "normalized_query": "SELECT * FROM tabItem WHERE something",
            "duration": 40.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert missing == []


def test_actionable_findings_carry_verified_flag(monkeypatch, empty_context):
    """Positive case: a column that genuinely lacks an index gets an
    actionable finding with verified_not_indexed=True in the detail."""

    def fake_optimize(query):
        return _FakeDBIndex("tabProject", "project_manager")

    _install_fake_recorder_module(monkeypatch, fake_optimize)
    _install_fake_frappe_db(
        monkeypatch,
        indexed_columns_by_table={"tabProject": {"name", "status"}},  # project_manager NOT indexed
        column_types_by_table={
            "tabProject": {
                "name": "varchar",
                "status": "varchar",
                "project_manager": "varchar",
            }
        },
    )

    recording = {
        "uuid": "ix_good",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [{
            "query": "SELECT * FROM tabProject WHERE project_manager = 'alice@x.com'",
            "normalized_query": "SELECT * FROM tabProject WHERE project_manager = ?",
            "duration": 200.0,
            "stack": [],
        }],
    }

    result = index_suggestions.analyze([recording], empty_context)
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert len(missing) == 1
    detail = json.loads(missing[0]["technical_detail_json"])
    assert detail["verified_not_indexed"] is True
    assert detail["suggested_ddl"] == (
        "ALTER TABLE `tabProject` ADD INDEX `idx_project_manager` (`project_manager`);"
    )


def test_classifier_caches_per_table(monkeypatch, empty_context):
    """Multiple suggestions on the same table must trigger only ONE
    SHOW INDEX and one information_schema lookup per table, not one
    per suggestion."""

    def fake_optimize(query):
        # Two different columns on the same table.
        if "col_a" in query:
            return _FakeDBIndex("tabHot", "col_a")
        return _FakeDBIndex("tabHot", "col_b")

    _install_fake_recorder_module(monkeypatch, fake_optimize)

    import frappe
    show_index_calls = []
    info_schema_calls = []

    def counting_sql(query, params=None, as_dict=False):
        if "SHOW INDEX FROM" in query:
            show_index_calls.append(query)
            return [{"Column_name": "name", "Seq_in_index": 1}]
        if "information_schema.columns" in query:
            info_schema_calls.append(query)
            return [
                {"column_name": "name", "data_type": "varchar"},
                {"column_name": "col_a", "data_type": "varchar"},
                {"column_name": "col_b", "data_type": "varchar"},
            ]
        return []

    class FakeDB:
        def sql(self, query, params=None, as_dict=False):
            return counting_sql(query, params, as_dict)

    monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
    monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)

    recording = {
        "uuid": "ix_cache",
        "path": "/", "cmd": None, "method": "GET",
        "event_type": "HTTP Request", "duration": 100,
        "calls": [
            {
                "query": "SELECT * FROM tabHot WHERE col_a = 'X'",
                "normalized_query": "SELECT * FROM tabHot WHERE col_a = ?",
                "duration": 50.0, "stack": [],
            },
            {
                "query": "SELECT * FROM tabHot WHERE col_b = 'Y'",
                "normalized_query": "SELECT * FROM tabHot WHERE col_b = ?",
                "duration": 50.0, "stack": [],
            },
        ],
    }

    result = index_suggestions.analyze([recording], empty_context)
    # Two distinct findings (col_a, col_b on same table).
    missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
    assert len(missing) == 2
    # But only ONE SHOW INDEX call and ONE information_schema call —
    # the classifier cached per-table.
    assert len(show_index_calls) == 1, (
        f"Expected 1 SHOW INDEX call (cached per table), got {len(show_index_calls)}"
    )
    assert len(info_schema_calls) == 1, (
        f"Expected 1 information_schema call (cached per table), got {len(info_schema_calls)}"
    )


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
