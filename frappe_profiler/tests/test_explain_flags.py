# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for frappe_profiler.analyzers.explain_flags.

Exercises all four red-flag patterns: Full Table Scan, Filesort,
Temporary Table, and Low Filter Ratio (the one we just added in fix #2).
"""

from frappe_profiler.analyzers import explain_flags


def test_full_table_scan_detected(full_scan_recording, empty_context):
	result = explain_flags.analyze([full_scan_recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) >= 1
	# Fixture rows examined is 50000 > HIGH_ROWS_EXAMINED, so severity High
	assert any(f["severity"] == "High" for f in scans)


def test_filesort_detected(full_scan_recording, empty_context):
	result = explain_flags.analyze([full_scan_recording], empty_context)
	filesorts = [f for f in result.findings if f["finding_type"] == "Filesort"]
	assert len(filesorts) >= 1


def test_temporary_table_detected(full_scan_recording, empty_context):
	result = explain_flags.analyze([full_scan_recording], empty_context)
	temps = [f for f in result.findings if f["finding_type"] == "Temporary Table"]
	assert len(temps) >= 1


def test_low_filter_ratio_detected(empty_context):
	"""Fix #2: filtered < 10 check must now fire."""
	recording = {
		"uuid": "lf1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 300,
		"calls": [
			{
				"query": "SELECT * FROM tabLead WHERE status = ?",
				"normalized_query": "SELECT * FROM tabLead WHERE STATUS = ?",
				"duration": 250.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabLead",
						"type": "ref",
						"key": "status",
						"rows": 5000,
						"filtered": 2.0,  # Only 2% of scanned rows match
						"Extra": "Using where",
					}
				],
			}
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	low_filter = [f for f in result.findings if f["finding_type"] == "Low Filter Ratio"]
	assert len(low_filter) == 1
	assert "tabLead" in low_filter[0]["title"]


def test_low_filter_ratio_ignores_small_result_sets(empty_context):
	"""If only 50 rows were examined, low filter ratio isn't worth flagging."""
	recording = {
		"uuid": "lf2",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 10,
		"calls": [
			{
				"query": "SELECT * FROM tabSmallTable WHERE x = ?",
				"normalized_query": "SELECT * FROM tabSmallTable WHERE x = ?",
				"duration": 2.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabSmallTable",
						"type": "ALL",
						"rows": 50,  # below LOW_FILTERED_MIN_ROWS=100
						"filtered": 5.0,
						"Extra": "",
					}
				],
			}
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	low_filter = [f for f in result.findings if f["finding_type"] == "Low Filter Ratio"]
	assert low_filter == []


def test_clean_query_no_findings(clean_recording, empty_context):
	"""A query using an index with good selectivity should emit nothing."""
	result = explain_flags.analyze([clean_recording], empty_context)
	assert result.findings == []


def test_findings_deduplicated_by_table(empty_context):
	"""Multiple queries with the same full-scan pattern on the same table
	should collapse to one finding with aggregated impact.
	"""
	recording = {
		"uuid": "dedup1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": f"SELECT * FROM tabBigTable WHERE name = '{i}'",
				"normalized_query": "SELECT * FROM tabBigTable WHERE NAME = ?",
				"duration": 80.0,
				"stack": [],
				"explain_result": [
					{"table": "tabBigTable", "type": "ALL", "rows": 50000, "Extra": ""}
				],
			}
			for i in range(5)
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1  # deduplicated
	assert scans[0]["affected_count"] == 5
	assert scans[0]["estimated_impact_ms"] == 400.0  # 5 × 80ms


def test_empty_recordings(empty_context):
	assert explain_flags.analyze([], empty_context).findings == []
