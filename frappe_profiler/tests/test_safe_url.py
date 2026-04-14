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


def test_app_list_route_not_redacted_as_docname():
	"""Regression guard (v0.5.1 architect review): the Frappe URL
	/app/<doctype>/view/list must NOT have `view` replaced with <name>.
	`view` is a reserved second segment for list routes, not a docname.
	"""
	from frappe_profiler.renderer import _safe_url

	assert _safe_url("/app/sales-invoice/view/list") == "/app/sales-invoice/view/list"
	assert _safe_url("/app/sales-invoice/view/report/Monthly+Sales") == "/app/sales-invoice/view/report/Monthly+Sales"


def test_app_new_route_not_redacted():
	"""/app/<doctype>/new/<name> — `new` is a reserved segment."""
	from frappe_profiler.renderer import _safe_url

	assert _safe_url("/app/sales-invoice/new") == "/app/sales-invoice/new"


def test_app_tree_route_not_redacted():
	from frappe_profiler.renderer import _safe_url

	assert _safe_url("/app/item-group/view/tree") == "/app/item-group/view/tree"


def test_query_string_denylist_redacts_unknown_keys():
	"""v0.5.1 switches from allowlist to denylist: any QS key NOT in
	_QS_SAFE_KEYS gets its value redacted. This protects against custom
	filter keys added by third-party apps that we've never seen."""
	from frappe_profiler.renderer import _safe_url

	# `my_custom_filter` is not in _QS_SAFE_KEYS, so its value must redact.
	result = _safe_url("/api/method/foo?my_custom_filter=secret-customer-id-123")
	assert "secret-customer-id-123" not in result
	assert "my_custom_filter" in result  # key name preserved

	# `customer_email` is not in _QS_SAFE_KEYS — redact.
	result2 = _safe_url("/api/method/foo?customer_email=alice@example.com")
	assert "alice@example.com" not in result2


def test_query_string_safe_keys_pass_through():
	"""Schema references, pagination, and format flags are code-level
	and NOT redacted."""
	from frappe_profiler.renderer import _safe_url

	# doctype name is a code identifier — pass through.
	result = _safe_url("/api/method/frappe.client.get_list?doctype=Customer&limit=20&order_by=name")
	assert "doctype=Customer" in result
	assert "limit=20" in result
	assert "order_by=name" in result


def test_query_string_mixed_safe_and_unsafe_keys():
	from frappe_profiler.renderer import _safe_url

	# doctype is safe; filters is NOT (even though the old allowlist had
	# it explicitly — denylist is more conservative and redacts it anyway).
	result = _safe_url('/api/method/frappe.client.get_list?doctype=Sales+Invoice&filters=%5B%5B%22name%22%2C%22like%22%2C%22ACME%25%22%5D%5D')
	assert "doctype=Sales+Invoice" in result
	assert "ACME" not in result
