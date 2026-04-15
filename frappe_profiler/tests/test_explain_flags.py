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


def test_rows_as_string_does_not_crash_analyzer(empty_context):
	"""v0.5.1 regression guard: production report showed
	'Analyzer frappe_profiler.analyzers.explain_flags failed' because
	some MariaDB drivers return `rows` as a string (Decimal-to-str
	through certain adapter versions). The `rows_examined > N` int
	comparison then crashed with TypeError in Python 3, taking out
	the whole analyzer and dropping all Full Table Scan / Filesort
	/ Temporary Table / Low Filter Ratio findings for the session.

	_to_int coercion + per-row try/except means a string `rows` is
	either coerced cleanly (if parseable) or treated as 0.
	"""
	recording = {
		"uuid": "str-rows",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 300,
		"calls": [
			{
				"query": "SELECT * FROM tabBig WHERE x = ?",
				"normalized_query": "SELECT * FROM tabBig WHERE x = ?",
				"duration": 250.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabBig",
						"type": "ALL",
						"rows": "50000",  # STRING, not int
						"Extra": "",
					},
				],
			},
		],
	}

	# Must NOT raise. The old code would crash at the string-vs-int
	# comparison in _inspect_row.
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1
	# Coerced correctly → 50000 > HIGH_ROWS_EXAMINED → High severity
	assert scans[0]["severity"] == "High"


def test_rows_as_none_does_not_crash_analyzer(empty_context):
	"""None is a valid value for EXPLAIN.rows in some edge cases
	(subquery materialization, const access, etc.). Must coerce to
	0 and continue, not crash."""
	recording = {
		"uuid": "none-rows",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [
			{
				"query": "SELECT 1",
				"normalized_query": "SELECT 1",
				"duration": 10.0,
				"stack": [],
				"explain_result": [
					{"table": "tabAny", "type": "ALL", "rows": None, "Extra": ""},
				],
			},
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	# rows=0 still triggers Full Table Scan (since type=ALL) but
	# severity drops to Medium (0 < HIGH_ROWS_EXAMINED).
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1
	assert scans[0]["severity"] == "Medium"


def test_malformed_row_is_isolated_not_crashing(empty_context):
	"""A single unparseable row must not kill the whole session's
	explain_flags output. The per-row try/except catches the error,
	counts it, logs a sample to the warnings list, and continues
	with the remaining rows.
	"""
	import unittest.mock as mock

	recording = {
		"uuid": "bad-row",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 200,
		"calls": [
			{
				"query": "SELECT * FROM tabOk WHERE x = ?",
				"normalized_query": "SELECT * FROM tabOk WHERE x = ?",
				"duration": 100.0,
				"stack": [],
				"explain_result": [
					# A legit full-scan row that should still produce a finding
					{"table": "tabOk", "type": "ALL", "rows": 50000, "Extra": ""},
				],
			},
		],
	}

	# Monkeypatch _inspect_row to raise on the first call only,
	# simulating a weird row shape we can't foresee. The per-row
	# try/except should catch it and continue.
	original = explain_flags._inspect_row
	call_count = {"n": 0}

	def sometimes_fail(*args, **kwargs):
		call_count["n"] += 1
		if call_count["n"] == 1:
			raise TypeError("simulated: '<' not supported between str and int")
		return original(*args, **kwargs)

	with mock.patch.object(explain_flags, "_inspect_row", side_effect=sometimes_fail):
		result = explain_flags.analyze([recording, recording], empty_context)

	# Row 1 failed, row 2 succeeded. One Full Table Scan finding from
	# the second recording's row.
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1
	# And the failure was surfaced as a warning.
	assert any("explain_flags: could not parse" in w for w in result.warnings)


def test_filtered_as_string_does_not_crash(empty_context):
	"""`filtered` can also arrive as a string from some drivers.
	Must coerce cleanly, not crash."""
	recording = {
		"uuid": "str-filtered",
		"path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 300,
		"calls": [
			{
				"query": "SELECT * FROM tabLead WHERE status = ?",
				"normalized_query": "SELECT * FROM tabLead WHERE status = ?",
				"duration": 250.0,
				"stack": [],
				"explain_result": [
					{
						"table": "tabLead",
						"type": "ref",
						"key": "status",
						"rows": 5000,
						"filtered": "2.5",  # STRING, not float
						"Extra": "Using where",
					},
				],
			},
		],
	}
	result = explain_flags.analyze([recording], empty_context)
	low_filter = [f for f in result.findings if f["finding_type"] == "Low Filter Ratio"]
	assert len(low_filter) == 1
