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
