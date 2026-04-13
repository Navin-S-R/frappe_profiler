# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for v0.3.0 renderer helpers (redact_frame_name, build_donut_data, etc)."""

from frappe_profiler import renderer


# ---------------------------------------------------------------------------
# redact_frame_name
# ---------------------------------------------------------------------------


def test_redact_frame_name_keeps_frappe_in_safe_mode():
	node = {"function": "frappe.model.document.Document.save",
	        "filename": "apps/frappe/x.py", "lineno": 100}
	out = renderer.redact_frame_name(node, mode="safe", allowed_prefixes=())
	assert out == "frappe.model.document.Document.save"


def test_redact_frame_name_keeps_erpnext_in_safe_mode():
	node = {"function": "erpnext.selling.doctype.sales_invoice.validate",
	        "filename": "apps/erpnext/x.py", "lineno": 50}
	out = renderer.redact_frame_name(node, mode="safe", allowed_prefixes=())
	assert out == "erpnext.selling.doctype.sales_invoice.validate"


def test_redact_frame_name_collapses_custom_app_in_safe_mode():
	node = {"function": "my_acme_app.discounts.pricing.calculate_secret_pricing",
	        "filename": "apps/my_acme_app/x.py", "lineno": 50}
	out = renderer.redact_frame_name(node, mode="safe", allowed_prefixes=())
	assert out == "my_acme_app:discounts"


def test_redact_frame_name_respects_extra_allowed_prefixes():
	node = {"function": "my_open_source_app.module.function",
	        "filename": "apps/my_open_source_app/x.py", "lineno": 50}
	out = renderer.redact_frame_name(
		node, mode="safe", allowed_prefixes=("my_open_source_app.",),
	)
	assert out == "my_open_source_app.module.function"


def test_redact_frame_name_raw_mode_shows_everything():
	node = {"function": "my_acme_app.discounts.pricing.calculate_secret_pricing",
	        "filename": "apps/my_acme_app/discounts/pricing.py", "lineno": 50}
	out = renderer.redact_frame_name(node, mode="raw", allowed_prefixes=())
	assert "my_acme_app.discounts.pricing.calculate_secret_pricing" in out
	assert "pricing.py:50" in out


def test_redact_frame_name_handles_missing_keys_gracefully():
	# Old fixture data may have nodes missing some keys, or non-dict input
	out = renderer.redact_frame_name({}, mode="safe", allowed_prefixes=())
	assert out == "<unknown>"


def test_redact_frame_name_passes_through_special_markers():
	for marker in ("<root>", "<sql>", "[other: 5 frames]", "[3 more frames omitted]"):
		node = {"function": marker, "filename": "", "lineno": 0}
		out = renderer.redact_frame_name(node, mode="safe", allowed_prefixes=())
		assert out == marker


# ---------------------------------------------------------------------------
# build_donut_data
# ---------------------------------------------------------------------------


def test_build_donut_data_safe_mode_collapses_custom_apps():
	breakdown = {
		"sql_ms": 100,
		"python_ms": 500,
		"by_app": {
			"erpnext": 200,
			"frappe": 100,
			"my_acme_app": 200,
		},
	}
	slices = renderer.build_donut_data(breakdown, mode="safe", allowed_prefixes=())
	labels = [s[0] for s in slices]
	assert "SQL" in labels
	assert "Python (frappe)" in labels
	assert "Python (erpnext)" in labels
	# Custom app name is collapsed
	assert "Python (custom apps)" in labels
	assert "Python (my_acme_app)" not in labels


def test_build_donut_data_raw_mode_shows_app_names():
	breakdown = {
		"sql_ms": 100,
		"python_ms": 500,
		"by_app": {
			"erpnext": 200,
			"my_acme_app": 200,
		},
	}
	slices = renderer.build_donut_data(breakdown, mode="raw", allowed_prefixes=())
	labels = [s[0] for s in slices]
	assert "Python (my_acme_app)" in labels


def test_build_donut_data_handles_none():
	# Backward compat: old session has no breakdown
	slices = renderer.build_donut_data(None, mode="safe", allowed_prefixes=())
	assert slices == []


def test_build_donut_data_handles_empty_breakdown():
	slices = renderer.build_donut_data({}, mode="safe", allowed_prefixes=())
	assert slices == []


# ---------------------------------------------------------------------------
# build_hot_frames_table
# ---------------------------------------------------------------------------


def test_build_hot_frames_table_applies_redaction():
	rows = [
		{"function": "my_acme_app.x.y.z", "total_ms": 500, "occurrences": 5,
		 "distinct_actions": 3, "action_refs": [0, 1, 2]},
	]
	out_safe = renderer.build_hot_frames_table(rows, mode="safe", allowed_prefixes=())
	display = out_safe[0]["display_name"]
	assert display == "my_acme_app:x"

	out_raw = renderer.build_hot_frames_table(rows, mode="raw", allowed_prefixes=())
	assert "my_acme_app.x.y.z" in out_raw[0]["display_name"]


def test_build_hot_frames_table_handles_none():
	# Backward compat
	rows = renderer.build_hot_frames_table(None, mode="safe", allowed_prefixes=())
	assert rows == []
