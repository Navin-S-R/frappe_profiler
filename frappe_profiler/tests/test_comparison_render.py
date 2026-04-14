# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for v0.4.0 comparison rendering integration."""

import json
from types import SimpleNamespace

from frappe_profiler import renderer


def _fake_doc(**fields):
	defaults = {
		"name": "PS-test",
		"session_uuid": "test-uuid",
		"title": "test session",
		"user": "tester@example.com",
		"status": "Ready",
		"started_at": "2026-04-14T00:00:00",
		"stopped_at": "2026-04-14T00:00:05",
		"total_duration_ms": 5000,
		"total_requests": 1,
		"total_queries": 100,
		"total_query_time_ms": 200,
		"total_python_ms": 800,
		"total_sql_ms": 200,
		"analyze_duration_ms": 100,
		"top_severity": "None",
		"summary_html": "<p>summary</p>",
		"top_queries_json": "[]",
		"table_breakdown_json": "[]",
		"hot_frames_json": "[]",
		"session_time_breakdown_json": "{}",
		"analyzer_warnings": None,
		"actions": [],
		"findings": [],
		"compared_to_session": None,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def test_render_without_baseline_skips_comparison_sections():
	doc = _fake_doc(compared_to_session=None)
	html = renderer.render_safe(doc, recordings=[])
	assert "Compared to baseline" not in html
	assert "Per-action comparison" not in html
	assert "Findings compared to baseline" not in html


def test_render_with_baseline_includes_comparison(monkeypatch):
	import frappe

	baseline = _fake_doc(name="PS-baseline", title="baseline run",
	                     total_duration_ms=4200, total_queries=1076)
	new = _fake_doc(name="PS-new", title="new run",
	                total_duration_ms=2100, total_queries=540,
	                compared_to_session="PS-baseline")

	def fake_get_doc(doctype, name):
		assert doctype == "Profiler Session"
		assert name == "PS-baseline"
		return baseline

	monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)

	html = renderer.render_safe(new, recordings=[])
	assert "Compared to baseline" in html
	assert "baseline run" in html


def test_render_baseline_deleted_gracefully_skips(monkeypatch):
	import frappe

	new = _fake_doc(compared_to_session="PS-deleted")

	def fake_get_doc(doctype, name):
		raise frappe.DoesNotExistError("baseline gone")

	monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)

	# Must not raise; comparison sections skipped
	html = renderer.render_safe(new, recordings=[])
	assert "Compared to baseline" not in html


def test_render_baseline_in_failed_state_skips(monkeypatch):
	import frappe

	failed_baseline = _fake_doc(name="PS-failed", status="Failed")
	new = _fake_doc(compared_to_session="PS-failed")

	monkeypatch.setattr(frappe, "get_doc", lambda d, n: failed_baseline, raising=False)

	html = renderer.render_safe(new, recordings=[])
	assert "Compared to baseline" not in html
