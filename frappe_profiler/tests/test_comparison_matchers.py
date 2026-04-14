# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for the comparison matchers in comparison.py."""

import json

import pytest

from frappe_profiler import comparison


def test_extract_callsite_key_slow_hot_path():
	td = json.dumps({"function": "my_app.calculate", "filename": "x.py", "lineno": 5})
	key = comparison._extract_callsite_key("Slow Hot Path", td)
	assert key == "my_app.calculate"


def test_extract_callsite_key_full_table_scan():
	td = json.dumps({"table": "tabSales Invoice", "explain_row": {}})
	key = comparison._extract_callsite_key("Full Table Scan", td)
	assert key == "tabSales Invoice"


def test_extract_callsite_key_redundant_call():
	td = json.dumps({
		"fn_name": "get_doc",
		"identifier_safe": ["User", "abc123"],
		"identifier_raw": ["User", "alice@example.com"],
	})
	key = comparison._extract_callsite_key("Redundant Call", td)
	assert key == ("get_doc", "User")


def test_extract_callsite_key_handles_invalid_json():
	# Defensive: malformed technical_detail_json must not crash
	key = comparison._extract_callsite_key("Slow Hot Path", "not-valid-json")
	assert key is None


def test_extract_callsite_key_handles_none():
	key = comparison._extract_callsite_key("Slow Hot Path", None)
	assert key is None


def test_extract_callsite_key_unknown_finding_type_uses_title():
	td = json.dumps({"some_field": "some_value"})
	# Unknown type → fallback to None
	key = comparison._extract_callsite_key("Brand New Finding Type", td)
	assert key is None


def _finding(finding_type, severity, title, td_dict, action_ref=None, impact_ms=100):
	return {
		"finding_type": finding_type,
		"severity": severity,
		"title": title,
		"technical_detail_json": json.dumps(td_dict),
		"action_ref": action_ref,
		"estimated_impact_ms": impact_ms,
	}


def test_match_findings_fixed_bucket():
	baseline = [
		_finding("Slow Hot Path", "High", "old slow path",
		         {"function": "my_app.heavy"}, action_ref="0"),
	]
	new = []
	result = comparison.match_findings(new, baseline)
	assert len(result["fixed"]) == 1
	assert result["fixed"][0]["title"] == "old slow path"
	assert result["new"] == []
	assert result["unchanged"] == []


def test_match_findings_new_bucket():
	baseline = []
	new = [
		_finding("Slow Hot Path", "Medium", "fresh slow path",
		         {"function": "my_app.new_heavy"}, action_ref="0"),
	]
	result = comparison.match_findings(new, baseline)
	assert result["fixed"] == []
	assert len(result["new"]) == 1
	assert result["new"][0]["title"] == "fresh slow path"


def test_match_findings_unchanged_with_delta():
	baseline = [
		_finding("Slow Hot Path", "High", "still slow",
		         {"function": "my_app.heavy"}, action_ref="0", impact_ms=850),
	]
	new = [
		_finding("Slow Hot Path", "Medium", "still slow",
		         {"function": "my_app.heavy"}, action_ref="0", impact_ms=640),
	]
	result = comparison.match_findings(new, baseline)
	assert result["unchanged"]
	pair = result["unchanged"][0]
	assert pair["delta_impact_ms"] == -210
	assert pair["delta_severity"] == "High → Medium"


def test_match_findings_session_wide_finding_with_none_action_ref():
	baseline = [
		_finding("Repeated Hot Frame", "Medium", "frame X",
		         {"function": "frame_x"}, action_ref=None),
	]
	new = [
		_finding("Repeated Hot Frame", "Medium", "frame X",
		         {"function": "frame_x"}, action_ref=None, impact_ms=100),
	]
	result = comparison.match_findings(new, baseline)
	assert len(result["unchanged"]) == 1
