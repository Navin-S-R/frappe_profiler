# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for frappe_profiler.analyzers.table_breakdown."""

import pytest

# sql_metadata is a frappe dependency; skip gracefully if the test env
# doesn't have it (unlikely since it ships with frappe)
sql_metadata = pytest.importorskip("sql_metadata")

from frappe_profiler.analyzers import table_breakdown


def test_single_table_aggregated(clean_recording, empty_context):
	result = table_breakdown.analyze([clean_recording], empty_context)
	breakdown = result.aggregate["table_breakdown"]
	# Both queries in clean_recording touch tabCustomer
	tables = [b["table"] for b in breakdown]
	assert "tabCustomer" in tables
	customer_row = next(b for b in breakdown if b["table"] == "tabCustomer")
	assert customer_row["queries"] == 2
	# Sum of 18 + 20 = 38 ms
	assert customer_row["duration_ms"] == pytest.approx(38.0, abs=0.5)


def test_sorted_by_duration_desc(empty_context):
	recording = {
		"uuid": "tb1",
		"path": "/",
		"cmd": None,
		"method": "GET",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [
			{
				"query": "SELECT * FROM tabSmall",
				"normalized_query": "SELECT * FROM tabSmall",
				"duration": 10.0,
				"stack": [],
			},
			{
				"query": "SELECT * FROM tabBig",
				"normalized_query": "SELECT * FROM tabBig",
				"duration": 200.0,
				"stack": [],
			},
			{
				"query": "SELECT * FROM tabMedium",
				"normalized_query": "SELECT * FROM tabMedium",
				"duration": 50.0,
				"stack": [],
			},
		],
	}
	result = table_breakdown.analyze([recording], empty_context)
	breakdown = result.aggregate["table_breakdown"]
	# Should be sorted by duration desc
	assert breakdown[0]["table"] == "tabBig"
	assert breakdown[1]["table"] == "tabMedium"
	assert breakdown[2]["table"] == "tabSmall"


def test_empty_recordings(empty_context):
	result = table_breakdown.analyze([], empty_context)
	assert result.aggregate["table_breakdown"] == []
	assert result.findings == []


def test_no_findings_emitted(clean_recording, empty_context):
	"""table_breakdown is informational only, no findings."""
	result = table_breakdown.analyze([clean_recording], empty_context)
	assert result.findings == []
