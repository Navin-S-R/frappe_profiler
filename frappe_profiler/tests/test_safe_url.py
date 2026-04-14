# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for renderer._safe_url Safe-mode URL redaction (v0.5.0).

Mirrors how SQL normalization works in top_queries.py: full text stored,
redacted form emitted in Safe Report mode. Preserves enough structure
for reviewers to understand what endpoint was called without leaking
the specific docname or filter literals.
"""


def test_strips_docname_from_app_path():
	from frappe_profiler.renderer import _safe_url

	assert _safe_url("/app/sales-invoice/SI-2026-00123/edit") == "/app/sales-invoice/<name>/edit"


def test_strips_docname_without_trailing_segment():
	from frappe_profiler.renderer import _safe_url

	assert _safe_url("/app/sales-invoice/SI-2026-00123") == "/app/sales-invoice/<name>"


def test_redacts_source_name_query():
	from frappe_profiler.renderer import _safe_url

	result = _safe_url("/api/method/erpnext.make_delivery?source_name=SI-2026-00123")
	assert "SI-2026-00123" not in result
	# urlencode encodes '?' as '%3F' — either form is fine as long as
	# the docname is gone.
	assert "source_name=%3F" in result or "source_name=?" in result


def test_redacts_filters_query():
	from frappe_profiler.renderer import _safe_url

	result = _safe_url(
		"/api/method/frappe.client.get_list"
		"?doctype=Customer&filters=%5B%5B%22name%22%2C%22like%22%2C%22ACME%25%22%5D%5D"
	)
	assert "ACME" not in result


def test_method_url_passes_through():
	from frappe_profiler.renderer import _safe_url

	# Method names are code identifiers, not PII — same treatment as
	# SQL table names after normalization.
	assert _safe_url("/api/method/frappe.client.save") == "/api/method/frappe.client.save"


def test_non_app_path_passes_through():
	from frappe_profiler.renderer import _safe_url

	assert _safe_url("/assets/frappe/css/desk.css") == "/assets/frappe/css/desk.css"


def test_empty_string_noop():
	from frappe_profiler.renderer import _safe_url

	assert _safe_url("") == ""


def test_none_is_safe():
	from frappe_profiler.renderer import _safe_url

	# Defensive: None input should not raise — some captured URLs can
	# be None if the browser passed a non-string input to fetch().
	assert _safe_url(None) in (None, "")


def test_preserves_fragment():
	from frappe_profiler.renderer import _safe_url

	# Fragments (#something) are client-side anchors; preserve them so
	# the URL still identifies the same UI state.
	assert "#details" in _safe_url("/app/customer/ACME-001#details")
