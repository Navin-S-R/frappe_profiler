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


def test_build_donut_data_hides_sub_millisecond_buckets():
	"""v0.5.1: buckets under 1ms round to "0ms" at display time and
	just add visual clutter. Production report showed seven "Python
	(…) — 0ms" entries (inspect.py, functools.py, <built-in>,
	MySQLdb, etc.) — all noise. Sub-ms buckets must be hidden."""
	breakdown = {
		"sql_ms": 148,
		"python_ms": 0.8,  # tiny Python total
		"by_app": {
			"frappe": 0.3,         # below 1ms — HIDE
			"erpnext": 0.2,        # below 1ms — HIDE
			"[other]": 0.3,        # below 1ms — HIDE
		},
	}
	slices = renderer.build_donut_data(breakdown, mode="raw", allowed_prefixes=())
	labels = [s[0] for s in slices]
	# Only SQL appears — all Python buckets are too small.
	assert labels == ["SQL"], (
		f"Sub-ms Python buckets must not render; got labels: {labels}"
	)


def test_build_donut_data_safe_mode_also_hides_tiny_buckets():
	"""Same threshold applies in safe mode: allowed apps with <1ms
	don't get their own slice, and the custom-apps rollup ignores
	sub-ms custom apps too."""
	breakdown = {
		"sql_ms": 100,
		"python_ms": 0.5,
		"by_app": {
			"frappe": 0.3,          # <1ms — HIDE
			"my_custom": 0.2,       # <1ms — would roll to custom but still <1 total
		},
	}
	slices = renderer.build_donut_data(breakdown, mode="safe", allowed_prefixes=())
	labels = [s[0] for s in slices]
	assert labels == ["SQL"]
	assert "Python (frappe)" not in labels
	assert "Python (custom apps)" not in labels


def test_build_donut_data_renders_buckets_above_threshold():
	"""Positive case — buckets above 1ms are still rendered. The
	threshold only kills noise, not real signal."""
	breakdown = {
		"sql_ms": 200,
		"python_ms": 50,
		"by_app": {
			"frappe": 30,
			"erpnext": 15,
			"[other]": 5,
		},
	}
	slices = renderer.build_donut_data(breakdown, mode="raw", allowed_prefixes=())
	labels = [s[0] for s in slices]
	assert "SQL" in labels
	assert "Python (frappe)" in labels
	assert "Python (erpnext)" in labels
	assert "Python ([other])" in labels


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
