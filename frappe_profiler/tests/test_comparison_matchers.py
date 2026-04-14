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
