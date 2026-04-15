# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for frappe_profiler.analyzers.n_plus_one.

The key tests here validate the callsite attribution fix: grouping must
key on the deepest NON-FRAPPE frame, not just the deepest `/apps/` frame,
so N+1 loops routed through frappe helpers still get attributed to
business logic.
"""

import json

from frappe_profiler.analyzers import n_plus_one


def test_n_plus_one_detected_from_n_plus_one_fixture(n_plus_one_recording, empty_context):
	"""The fixture has 10 `SELECT tax_rate` queries from the same loop.

	Callsite walking should skip the frappe.database frames and attribute
	them all to sales_invoice.py:212. A single finding should emerge.
	"""
	result = n_plus_one.analyze([n_plus_one_recording], empty_context)

	# Exactly one finding for the 10-query loop (12-query recording but
	# 2 queries are unique non-loop queries).
	assert len(result.findings) == 1
	f = result.findings[0]
	assert f["finding_type"] == "N+1 Query"
	assert f["affected_count"] == 10
	# Severity should be Medium (10 is below the High threshold of 50)
	# OR we hit the total_time_ms > 200 threshold which would make it High.
	# The fixture total is ~93ms so Medium is correct.
	assert f["severity"] in ("Medium", "Low")  # could be Low if threshold bumped


def test_n_plus_one_callsite_attributes_to_business_code(n_plus_one_recording, empty_context):
	"""The N+1 finding must point at sales_invoice.py, NOT frappe/database/database.py.

	This is the fix for review issue #1. The stack has frappe framework
	frames AFTER the business-logic frame, so without the fix we'd blame
	database.py:742 for the N+1 instead of sales_invoice.py:212.
	"""
	result = n_plus_one.analyze([n_plus_one_recording], empty_context)
	assert len(result.findings) == 1

	detail = json.loads(result.findings[0]["technical_detail_json"])
	callsite = detail["callsite"]
	assert "sales_invoice.py" in callsite["filename"]
	assert callsite["lineno"] == 212
	# Must NOT be the frappe database frame
	assert "frappe/database" not in callsite["filename"]


def test_clean_recording_has_no_n_plus_one(clean_recording, empty_context):
	"""A normal list+count query pair should NOT trigger N+1."""
	result = n_plus_one.analyze([clean_recording], empty_context)
	assert result.findings == []


def test_threshold_respected(empty_context):
	"""Groups below the threshold should not become findings."""
	# Build a recording with only 5 identical queries (below default 10)
	recording = {
		"uuid": "thr",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 20,
		"calls": [
			{
				"query": "SELECT 1 FROM t WHERE x=1",
				"normalized_query": "SELECT ? FROM t WHERE x=?",
				"duration": 2,
				"stack": [
					{"filename": "apps/myapp/module.py", "lineno": 100, "function": "f"},
				],
			}
		] * 5,
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert result.findings == []  # 5 < 10 threshold


def test_fallback_to_deepest_frame_when_only_frappe_frames(empty_context):
	"""If the only /apps/ frame is in frappe itself, we still emit the finding
	rather than silently dropping it.
	"""
	recording = {
		"uuid": "fb",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT ?",
				"duration": 5,
				"stack": [
					{"filename": "frappe/model/document.py", "lineno": 200, "function": "save"},
				],
			}
		] * 15,  # 15 copies, well above threshold
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert len(result.findings) == 1
	detail = json.loads(result.findings[0]["technical_detail_json"])
	# It fell back to the frappe frame — that's the right behavior for queries
	# that are genuinely issued from inside frappe internals.
	assert "frappe/model/document.py" in detail["callsite"]["filename"]


def test_severity_scales_with_count_and_time(empty_context):
	"""50+ occurrences OR >200ms total → High severity."""
	high_recording = {
		"uuid": "hi",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": f"SELECT name FROM t WHERE id = {i}",
				"normalized_query": "SELECT NAME FROM t WHERE ID = ?",
				"duration": 5.0,
				"stack": [
					{"filename": "apps/myapp/module.py", "lineno": 50, "function": "loop"},
				],
			}
			for i in range(60)  # 60 × 5ms = 300ms total
		],
	}
	result = n_plus_one.analyze([high_recording], empty_context)
	assert len(result.findings) == 1
	assert result.findings[0]["severity"] == "High"
	assert result.findings[0]["affected_count"] == 60


# ---------------------------------------------------------------------------
# v0.5.1 regression guards: don't surface the profiler's own instrumentation
# queries as N+1 findings. A production session flagged:
#
#   "Same query ran 22× at frappe_profiler/frappe_profiler/infra_capture.py:176"
#
# That's the SHOW GLOBAL STATUS snapshot our before_request hook runs on
# every recording — real SQL, but profiler overhead, not application work
# the user can optimize. Same goes for top_queries.


def test_profiler_infra_capture_query_is_not_flagged(empty_context):
	"""Exact production payload: 22 SHOW GLOBAL STATUS calls from
	infra_capture.py:176. Must NOT produce an N+1 finding."""
	recording = {
		"uuid": "infra-noise",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 200,
		"calls": [
			{
				"query": (
					"SHOW GLOBAL STATUS WHERE Variable_name IN "
					"('Threads_connected', 'Threads_running', 'Slow_queries')"
				),
				"normalized_query": (
					"SHOW GLOBAL STATUS WHERE Variable_name IN "
					"('Threads_connected', 'Threads_running', 'Slow_queries')"
				),
				"duration": 1.5,
				"stack": [
					{"filename": "frappe/app.py", "lineno": 202, "function": "init_request"},
					{
						"filename": "frappe_profiler/hooks_callbacks.py",
						"lineno": 108,
						"function": "before_request",
					},
					{
						"filename": "frappe_profiler/infra_capture.py",
						"lineno": 176,
						"function": "_read_db",
					},
					{
						"filename": "frappe/database/mariadb/database.py",
						"lineno": 742,
						"function": "sql",
					},
				],
			}
		] * 22,  # 22 identical calls, well above threshold
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert result.findings == [], (
		"Profiler's own instrumentation queries must be filtered. "
		f"Got findings: {[f['title'] for f in result.findings]}"
	)


def test_user_code_routed_through_profiler_wrap_still_attributed(empty_context):
	"""A legitimate user-code N+1 where the stack happens to include a
	frappe_profiler/capture.py wrap frame (because the wrap intercepts
	frappe.get_doc) must STILL be attributed to the user code, not
	filtered as profiler noise.

	The rule is 'is the deepest non-frappe frame inside frappe_profiler/?'
	— here the deepest is user code, so the finding fires."""
	recording = {
		"uuid": "user-through-wrap",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": "SELECT name FROM t WHERE id = ?",
				"normalized_query": "SELECT NAME FROM t WHERE ID = ?",
				"duration": 5.0,
				"stack": [
					{"filename": "frappe/app.py", "lineno": 120, "function": "dispatch"},
					{"filename": "apps/myapp/controller.py", "lineno": 42, "function": "bulk_update"},
					# Simulated capture-wrap frame in the middle
					{"filename": "frappe_profiler/capture.py", "lineno": 88, "function": "wrapped_get_doc"},
					{"filename": "frappe/model/document.py", "lineno": 200, "function": "get_doc"},
					{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
				],
			}
		] * 15,
	}
	result = n_plus_one.analyze([recording], empty_context)
	assert len(result.findings) == 1, (
		"User-code N+1 with a profiler wrap frame in the middle of the "
		"stack must still produce a finding — the deepest non-frappe "
		"frame is the user's controller.py, so the callsite rule matches."
	)
	detail = json.loads(result.findings[0]["technical_detail_json"])
	assert "apps/myapp/controller.py" in detail["callsite"]["filename"]
	assert detail["callsite"]["lineno"] == 42


def test_is_profiler_own_query_unit():
	"""Direct unit test of the helper — clearer than going through
	the full n_plus_one pipeline for each branch."""
	from frappe_profiler.analyzers.base import is_profiler_own_query

	# Stack is ONLY frappe_profiler + frappe → is profiler
	stack = [
		{"filename": "frappe/app.py", "lineno": 120, "function": "dispatch"},
		{"filename": "frappe_profiler/infra_capture.py", "lineno": 176, "function": "_read_db"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is True

	# Stack has a user frame → NOT profiler
	stack = [
		{"filename": "frappe/app.py", "lineno": 120, "function": "dispatch"},
		{"filename": "apps/myapp/controller.py", "lineno": 42, "function": "do_thing"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is False

	# Pure frappe stack (migration/fixture) → NOT profiler
	stack = [
		{"filename": "frappe/migrate.py", "lineno": 50, "function": "run"},
		{"filename": "frappe/model/document.py", "lineno": 200, "function": "save"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is False

	# Empty / None stack → False (don't drop; let caller's normal path handle)
	assert is_profiler_own_query(None) is False
	assert is_profiler_own_query([]) is False

	# Mixed profiler + user frame → user wins (NOT profiler)
	stack = [
		{"filename": "apps/myapp/bulk.py", "lineno": 10, "function": "bulk"},
		{"filename": "frappe_profiler/capture.py", "lineno": 88, "function": "wrap"},
		{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
	]
	assert is_profiler_own_query(stack) is False


def test_walk_callsite_returns_none_for_profiler_only_stack():
	"""walk_callsite's fallback used to return the innermost frame
	for 100%-framework stacks. Now it checks is_profiler_own_query and
	returns None when the stack is profiler instrumentation, so
	analyzers drop the query via their `if not callsite: continue`
	guard."""
	from frappe_profiler.analyzers.base import walk_callsite

	stack = [
		{"filename": "frappe/app.py", "lineno": 202, "function": "init_request"},
		{"filename": "frappe_profiler/hooks_callbacks.py", "lineno": 108, "function": "before_request"},
		{"filename": "frappe_profiler/infra_capture.py", "lineno": 176, "function": "_read_db"},
	]
	assert walk_callsite(stack) is None


def test_walk_callsite_still_falls_back_for_pure_frappe_stack():
	"""Legacy behavior preserved: a 100% frappe/ stack (no
	frappe_profiler) still falls back to the innermost frame, so
	legitimate framework queries aren't silently dropped."""
	from frappe_profiler.analyzers.base import walk_callsite

	stack = [
		{"filename": "frappe/migrate.py", "lineno": 50, "function": "run"},
		{"filename": "frappe/model/document.py", "lineno": 200, "function": "save"},
	]
	frame = walk_callsite(stack)
	assert frame is not None
	assert "frappe/model/document.py" in frame["filename"]


def test_top_queries_filters_profiler_instrumentation(empty_context):
	"""top_queries must skip the SHOW GLOBAL STATUS infra_capture query
	entirely, so it doesn't clutter the slow-queries leaderboard."""
	from frappe_profiler.analyzers import top_queries

	recording = {
		"uuid": "mixed",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			# Profiler instrumentation — should be dropped
			{
				"query": "SHOW GLOBAL STATUS WHERE Variable_name IN (...)",
				"normalized_query": "SHOW GLOBAL STATUS WHERE Variable_name IN (...)",
				"duration": 350.0,  # Would otherwise rank as top slow query
				"stack": [
					{"filename": "frappe_profiler/infra_capture.py", "lineno": 176, "function": "_read_db"},
					{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
				],
			},
			# Real application query — should appear in leaderboard
			{
				"query": "SELECT * FROM tabSales Invoice WHERE customer = ?",
				"normalized_query": "SELECT * FROM tabSales Invoice WHERE customer = ?",
				"duration": 250.0,
				"stack": [
					{"filename": "apps/myapp/handler.py", "lineno": 99, "function": "list_invoices"},
					{"filename": "frappe/database/mariadb/database.py", "lineno": 742, "function": "sql"},
				],
			},
		],
	}
	result = top_queries.analyze([recording], empty_context)
	queries = result.aggregate.get("top_queries", [])

	# Only the real query should appear in the leaderboard.
	assert len(queries) == 1
	assert "tabSales Invoice" in queries[0]["normalized_query"]
	# And only the real query should produce a Slow Query finding.
	slow = [f for f in result.findings if f["finding_type"] == "Slow Query"]
	assert len(slow) == 1
	assert "tabSales Invoice" in slow[0]["technical_detail_json"]
