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


def test_parser_limitation_valueerror_gets_soft_warning(monkeypatch, empty_context):
	"""v0.5.1: ValueError from sql_metadata is a parser limitation, not
	a bug we should scream about. It must produce the soft "Skipped N
	queries whose shape exceeds the parser" warning — NOT the loud
	"Could not analyze" warning that tells users to check Error Log.
	And it must NOT write to frappe.log_error."""

	def fake_optimize(query: str):
		raise ValueError("synthetic parse error")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	# Track whether log_error was called — parser limitations must NOT
	# end up in Frappe's Error Log.
	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe,
		"log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

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
	# Soft warning, not the loud one.
	assert len(result.warnings) == 1
	assert "exceeds the DBOptimizer heuristic" in result.warnings[0], (
		f"Expected soft parser-limit warning; got: {result.warnings}"
	)
	assert "Could not analyze" not in result.warnings[0], (
		"ValueError is a parser limitation — must not use the "
		"loud 'Could not analyze / see Error Log' phrasing"
	)
	# And crucially: no Error Log entry for parser limitations.
	assert log_error_calls == [], (
		f"Parser limitations must not be written to frappe.log_error. "
		f"Calls recorded: {log_error_calls}"
	)


def test_real_error_still_produces_loud_warning_and_logs(monkeypatch, empty_context):
	"""Unexpected exception types (not ValueError/TypeError) may indicate
	a profiler bug — those still get the loud warning + Error Log entries
	so the team can investigate."""

	def fake_optimize(query: str):
		raise RuntimeError("unexpected internal optimizer error")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe,
		"log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

	recording = {
		"uuid": "ix_real_err",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 10,
		"calls": [{
			"query": "SELECT 1",
			"normalized_query": "SELECT ?",
			"duration": 5.0,
			"stack": [],
		}],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert result.findings == []
	# Loud warning for real errors.
	loud = [w for w in result.warnings if "Could not analyze" in w]
	assert len(loud) == 1
	assert "RuntimeError" in loud[0]
	# And DID log to Error Log.
	assert len(log_error_calls) >= 1, (
		"Real errors must be written to frappe.log_error so they're "
		"discoverable for investigation."
	)


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


# ---------------------------------------------------------------------------
# v0.5.1 regression guards: filter non-SELECT statements BEFORE the optimizer.
# A real production session showed 47% "parse failures" (334 of 705) because
# transaction markers (BEGIN/COMMIT/SAVEPOINT/SET) were being fed to sql_metadata.
# None of those benefit from an index suggestion, and most raise ValueError.
# The fix: filter by leading keyword before ever calling _optimize_query.
# ---------------------------------------------------------------------------


def test_non_select_statements_are_skipped_without_failures(monkeypatch, empty_context):
	"""BEGIN/COMMIT/SAVEPOINT/SET/etc. must never reach _optimize_query.

	Pre-v0.5.1, these were counted as 'parse failures' and polluted the
	report with a warning saying 'Could not analyze 47% of your queries',
	which was misleading — the queries weren't optimization targets to
	begin with.
	"""
	optimize_call_count = {"n": 0}

	def fake_optimize(query: str):
		optimize_call_count["n"] += 1
		# If this is called at all on a non-SELECT, the filter is broken.
		return None

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix_non_select",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [
			# All statement types that caused ValueError in production:
			{"query": "BEGIN", "normalized_query": "BEGIN",
			 "duration": 0.5, "stack": []},
			{"query": "COMMIT", "normalized_query": "COMMIT",
			 "duration": 0.3, "stack": []},
			{"query": "ROLLBACK", "normalized_query": "ROLLBACK",
			 "duration": 0.2, "stack": []},
			{"query": "SAVEPOINT sp1", "normalized_query": "SAVEPOINT sp1",
			 "duration": 0.1, "stack": []},
			{"query": "RELEASE SAVEPOINT sp1",
			 "normalized_query": "RELEASE SAVEPOINT sp1",
			 "duration": 0.1, "stack": []},
			{"query": "SET autocommit = 0",
			 "normalized_query": "SET autocommit = 0",
			 "duration": 0.1, "stack": []},
			{"query": "SHOW TABLES", "normalized_query": "SHOW TABLES",
			 "duration": 1.0, "stack": []},
			{"query": "CALL some_proc()", "normalized_query": "CALL some_proc()",
			 "duration": 5.0, "stack": []},
			{"query": "ALTER TABLE tabFoo ADD COLUMN x INT",
			 "normalized_query": "ALTER TABLE tabFoo ADD COLUMN x INT",
			 "duration": 10.0, "stack": []},
		],
	}

	result = index_suggestions.analyze([recording], empty_context)

	# The optimizer must not have been called at all — everything was
	# filtered by query type first.
	assert optimize_call_count["n"] == 0, (
		f"Non-SELECT statements must be skipped before reaching "
		f"_optimize_query; got {optimize_call_count['n']} optimizer calls"
	)

	# No findings (nothing was analyzed).
	assert result.findings == []

	# The skipped count must surface as an informational warning,
	# separately from 'parse failures'.
	assert any("Skipped" in w and "non-SELECT" in w for w in result.warnings), (
		f"Expected a 'Skipped N non-SELECT' warning; got: {result.warnings}"
	)

	# And crucially — there must NOT be a "Could not analyze" parse-failure
	# warning, because nothing actually failed.
	assert not any("Could not analyze" in w for w in result.warnings), (
		f"Non-SELECT skips must not be reported as parse failures; "
		f"got: {result.warnings}"
	)


def test_select_statements_still_analyzed_alongside_skips(monkeypatch, empty_context):
	"""Positive case: mix SELECTs with non-SELECTs. The SELECTs get
	analyzed; the non-SELECTs get skipped; the counts are reported
	separately."""

	def fake_optimize(query: str):
		# Only reachable for the SELECT statements.
		assert query.strip().upper().startswith("SELECT"), (
			f"Only SELECTs should reach the optimizer; got: {query!r}"
		)
		return _FakeDBIndex("tabLead", "status")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix_mixed",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [
			{"query": "BEGIN", "normalized_query": "BEGIN",
			 "duration": 0.5, "stack": []},
			{"query": "SELECT * FROM tabLead WHERE status = 'Open'",
			 "normalized_query": "SELECT * FROM tabLead WHERE status = ?",
			 "duration": 50.0, "stack": []},
			{"query": "COMMIT", "normalized_query": "COMMIT",
			 "duration": 0.3, "stack": []},
		],
	}

	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1, (
		f"SELECT should still produce a finding; got {missing}"
	)
	# 2 non-SELECTs skipped.
	assert any("Skipped 2" in w for w in result.warnings), (
		f"Expected 'Skipped 2 non-SELECT' warning; got: {result.warnings}"
	)


def test_select_with_leading_comment_is_still_recognised(monkeypatch, empty_context):
	"""Frappe prepends `/* comment */` to some queries for tracing.
	The query-type filter must see through comments and recognise the
	underlying SELECT, otherwise we'd skip legitimate optimization
	targets."""

	def fake_optimize(query: str):
		return _FakeDBIndex("tabCommented", "key_col")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	recording = {
		"uuid": "ix_comment",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [{
			"query": "/* app: frappe */ SELECT * FROM tabCommented WHERE key_col = ?",
			"normalized_query": "/* app: frappe */ SELECT * FROM tabCommented WHERE key_col = ?",
			"duration": 50.0, "stack": [],
		}],
	}
	result = index_suggestions.analyze([recording], empty_context)
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1, (
		f"SELECT prefixed with a /* comment */ must still be analyzed; "
		f"got findings={missing}, warnings={result.warnings}"
	)


# ---------------------------------------------------------------------------
# v0.5.1 regression guard: exact production payload from ERPNext item search
# ---------------------------------------------------------------------------
# ERPNext's item search dialog issues a query sql_metadata can't parse:
# correlated subquery in WHERE IN, ORDER BY with if(locate(...), locate(...))
# expressions. DBOptimizer's tuple unpacking then raises:
#
#   ValueError: too many values to unpack (expected 2, got 4)
#
# Pre-fix: this showed up as an Error Log entry ("frappe_profiler optimizer
# failure") AND a loud "Could not analyze 1 of N queries — see Error Log"
# warning. Neither is actionable — the user cannot rewrite the ERPNext
# core query and cannot add an index to fix a parse failure. v0.5.1
# classifies these as parser limitations (soft informational warning, no
# Error Log noise).


_ERPNEXT_ITEM_SEARCH_QUERY = """
SELECT tabItem.name,
       item_name,
       item_group,
       customer_code,
       if(length(tabItem.description) > ?, concat(substr(tabItem.description, ?, ?), ?), description) AS description
FROM tabItem
WHERE tabItem.docstatus < ?
  AND tabItem.disabled=?
  AND tabItem.has_variants=?
  AND (tabItem.end_of_life > ?
       OR coalesce(tabItem.end_of_life, ?)=?)
  AND (item_name like ?
       OR description like ?
       OR item_group like ?
       OR customer_code like ?
       OR name like ?
       OR item_code like ?
       OR tabItem.item_code IN
         (SELECT parent
          FROM `tabItem Barcode`
          WHERE barcode LIKE ?)
       OR tabItem.description LIKE ?)
  AND `tabItem`.`is_sales_item`=?
  AND `tabItem`.`has_variants`=?
ORDER BY if(locate(?, name), locate(?, name), ?),
         if(locate(?, item_name), locate(?, item_name), ?),
         idx DESC,
         name,
         item_name
LIMIT ?,
      ?
"""


def test_erpnext_item_search_valueerror_is_soft_skip(monkeypatch, empty_context):
	"""Exact production payload: the ERPNext item-search query triggers
	``ValueError: too many values to unpack (expected 2, got 4)`` inside
	sql_metadata. The analyzer must:

	  1. Not crash (continue processing other queries)
	  2. Not write to frappe.log_error — it's not a profiler bug
	  3. Emit the soft 'Skipped N queries whose shape exceeds…' warning
	  4. Still produce index suggestions for OTHER queries in the same
	     session that DO parse cleanly
	"""

	def fake_optimize(query: str):
		# The complex query raises exactly the production error.
		if "tabItem.item_code IN" in query:
			raise ValueError("too many values to unpack (expected 2, got 4)")
		# A simpler query succeeds so we can assert mixed-behavior works.
		return _FakeDBIndex("tabLead", "status")

	_install_fake_recorder_module(monkeypatch, fake_optimize)
	_install_fake_frappe_db(
		monkeypatch,
		indexed_columns_by_table={"tabLead": {"name"}},
		column_types_by_table={
			"tabLead": {"name": "varchar", "status": "varchar"},
		},
	)

	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe,
		"log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

	recording = {
		"uuid": "ix_erpnext_item_search",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": _ERPNEXT_ITEM_SEARCH_QUERY,
				"normalized_query": _ERPNEXT_ITEM_SEARCH_QUERY,
				"duration": 180.0,
				"stack": [],
			},
			# A clean query that DOES produce a suggestion, alongside
			# the problematic one — verifies mixed behavior.
			{
				"query": "SELECT * FROM tabLead WHERE status = 'Open'",
				"normalized_query": "SELECT * FROM tabLead WHERE status = ?",
				"duration": 50.0,
				"stack": [],
			},
		],
	}

	result = index_suggestions.analyze([recording], empty_context)

	# 1. No crash, the clean query still produced its suggestion.
	missing = [f for f in result.findings if f["finding_type"] == "Missing Index"]
	assert len(missing) == 1
	assert "tabLead" in missing[0]["title"]

	# 2. No Error Log entry for the ValueError.
	assert log_error_calls == [], (
		f"Parser-limit ValueError must not log to frappe.log_error. "
		f"Calls recorded: {log_error_calls}"
	)

	# 3. Soft warning present, loud warning absent.
	soft = [w for w in result.warnings if "exceeds the DBOptimizer heuristic" in w]
	loud = [w for w in result.warnings if "Could not analyze" in w]
	assert len(soft) == 1, (
		f"Expected soft parser-limit warning; got warnings={result.warnings}"
	)
	assert loud == [], (
		f"Loud 'Could not analyze' warning must not fire for parser "
		f"limitations; got: {loud}"
	)
	# And the soft warning must count exactly one skipped query.
	assert "Skipped 1 " in soft[0]


def test_typeerror_from_optimizer_is_also_a_soft_skip(monkeypatch, empty_context):
	"""TypeError is another sql_metadata failure mode (unexpected None
	in its token stream). Must be classified as a parser limitation
	alongside ValueError."""

	def fake_optimize(query: str):
		raise TypeError("'NoneType' object is not subscriptable")

	_install_fake_recorder_module(monkeypatch, fake_optimize)

	log_error_calls = []
	import frappe
	monkeypatch.setattr(
		frappe, "log_error",
		lambda *a, **k: log_error_calls.append((a, k)),
		raising=False,
	)

	recording = {
		"uuid": "ix_type_err",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 10,
		"calls": [{
			"query": "SELECT 1",
			"normalized_query": "SELECT ?",
			"duration": 5.0,
			"stack": [],
		}],
	}
	result = index_suggestions.analyze([recording], empty_context)
	assert log_error_calls == []
	soft = [w for w in result.warnings if "exceeds the DBOptimizer heuristic" in w]
	assert len(soft) == 1


def test_get_query_type_helper_direct():
	"""Direct unit test of the _get_query_type helper — covers the
	regex edge cases without the full analyzer path."""
	from frappe_profiler.analyzers.index_suggestions import _get_query_type

	assert _get_query_type("SELECT 1") == "SELECT"
	assert _get_query_type("  select *  from foo") == "SELECT"
	assert _get_query_type("BEGIN") == "BEGIN"
	assert _get_query_type("commit") == "COMMIT"
	assert _get_query_type("SAVEPOINT sp1") == "SAVEPOINT"
	assert _get_query_type("RELEASE SAVEPOINT sp1") == "RELEASE"
	assert _get_query_type("SET autocommit = 0") == "SET"
	assert _get_query_type("SHOW TABLES") == "SHOW"
	assert _get_query_type("/* comment */ SELECT 1") == "SELECT"
	assert _get_query_type("/* multi\nline\ncomment */ select 1") == "SELECT"
	# Empty / None / garbage:
	assert _get_query_type("") == ""
	assert _get_query_type(None) == ""
	assert _get_query_type("   ") == ""
	assert _get_query_type("/* only comment */") == ""
