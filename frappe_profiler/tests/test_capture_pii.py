# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for PII identifier hashing in capture.py."""

import pytest

from frappe_profiler import capture


def test_identify_args_get_doc_returns_safe_and_raw():
	raw, safe = capture._identify_args("get_doc", ("User", "customer@example.com"), {})
	assert raw == ("User", "customer@example.com")
	assert safe[0] == "User"  # doctype not hashed
	assert len(safe[1]) == 12  # sha256[:12]
	assert safe[1] != "customer@example.com"  # actually hashed


def test_identify_args_get_doc_deterministic():
	_, safe1 = capture._identify_args("get_doc", ("User", "x@y.com"), {})
	_, safe2 = capture._identify_args("get_doc", ("User", "x@y.com"), {})
	assert safe1 == safe2


def test_identify_args_cache_get_hashes_key():
	# cache_get wraps RedisWrapper.get_value (a class method), so args[0]
	# is the RedisWrapper instance (self) and args[1] is the actual key.
	fake_self = object()
	raw, safe = capture._identify_args(
		"cache_get", (fake_self, "user_lang:admin@example.com"), {},
	)
	assert raw == "user_lang:admin@example.com"
	assert isinstance(safe, str) and len(safe) == 12
	assert "@" not in safe


def test_identify_args_has_permission_keeps_doctype_and_ptype():
	raw, safe = capture._identify_args(
		"has_permission",
		("Sales Invoice", "SI-2026-00042", "read"),
		{},
	)
	assert raw == ("Sales Invoice", "SI-2026-00042", "read")
	assert safe[0] == "Sales Invoice"  # doctype unchanged
	assert len(safe[1]) == 12  # name hashed
	assert safe[2] == "read"  # ptype unchanged


def test_identify_args_handles_missing_name():
	# has_permission can be called with name=None
	raw, safe = capture._identify_args("has_permission", ("Sales Invoice",), {})
	assert raw[0] == "Sales Invoice"
	assert raw[1] is None
	assert safe[1] is None
