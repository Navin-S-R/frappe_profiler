# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for frappe_profiler.analyzers.top_queries."""

from frappe_profiler.analyzers import top_queries


def test_top_queries_sorted_by_duration_desc(full_scan_recording, empty_context):
	result = top_queries.analyze([full_scan_recording], empty_context)
	top = result.aggregate["top_queries"]
	assert len(top) == 3
	# Sorted desc
	assert top[0]["duration_ms"] >= top[1]["duration_ms"] >= top[2]["duration_ms"]


def test_slow_query_finding_for_long_queries(full_scan_recording, empty_context):
	"""Queries > 200ms should emit Slow Query findings."""
	result = top_queries.analyze([full_scan_recording], empty_context)
	# The full_scan fixture has 850ms and 920ms queries → two slow findings
	slow = [f for f in result.findings if f["finding_type"] == "Slow Query"]
	assert len(slow) >= 2
	# Severity High for >500ms
	assert all(f["severity"] == "High" for f in slow)


def test_top_callsite_from_business_code(full_scan_recording, empty_context):
	"""Top queries should report the business-logic callsite, not frappe internals."""
	result = top_queries.analyze([full_scan_recording], empty_context)
	top = result.aggregate["top_queries"]
	# All fixture frames are in erpnext — should see erpnext paths, not frappe
	for q in top:
		if q["callsite"]:
			assert "erpnext/" in q["callsite"]


def test_clean_recording_emits_no_slow_query_findings(clean_recording, empty_context):
	"""Normal fast queries shouldn't generate findings."""
	result = top_queries.analyze([clean_recording], empty_context)
	assert result.findings == []


def test_empty_recordings(empty_context):
	result = top_queries.analyze([], empty_context)
	assert result.aggregate.get("top_queries") == []
	assert result.findings == []
