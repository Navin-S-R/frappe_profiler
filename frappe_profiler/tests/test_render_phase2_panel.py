# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for renderer._render_phase2_panel — exercises the phase-2 HTML
without spinning up Frappe / Jinja. We feed a minimal session-doc-shaped
object directly to the helper.
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import patch

from frappe_profiler import renderer


def _run(run_uuid, status, results, picks=None, total_ms=0):
	return SimpleNamespace(
		run_uuid=run_uuid,
		status=status,
		started_at="2026-05-07 12:00:00",
		ended_at="2026-05-07 12:01:00",
		total_ms=total_ms,
		picks_json=json.dumps(picks or []),
		results_json=json.dumps(results),
	)


def _line(lineno, content, hits, total_ms):
	return {
		"lineno": lineno,
		"content": content,
		"content_hash": f"hash_{lineno}",
		"hits": hits,
		"total_ms": total_ms,
		"per_hit_us": (total_ms * 1000.0 / hits) if hits else 0.0,
	}


def _function(dotted_path, lines):
	return {
		"dotted_path": dotted_path,
		"qualname": dotted_path.rsplit(".", 1)[-1],
		"file": "/fake/path.py",
		"lines": lines,
	}


class TestRenderPhase2PanelEmpty:
	def test_no_phase2_runs_returns_empty_string(self):
		session = SimpleNamespace(phase_2_runs=[])
		assert renderer._render_phase2_panel(session, "safe") == ""

	def test_phase_2_runs_attribute_missing_returns_empty(self):
		session = SimpleNamespace()
		assert renderer._render_phase2_panel(session, "safe") == ""


class TestRenderPhase2PanelSingleRun:
	def _session(self, results):
		return SimpleNamespace(phase_2_runs=[_run("r1", "Ready", results)])

	def test_function_dotted_path_appears_in_output(self):
		session = self._session([
			_function("my_app.x.compute", [_line(1, "x = 1", 5, 10.0)]),
		])

		with patch.object(renderer, "_phase2_safe_show_source", return_value=True):
			html = renderer._render_phase2_panel(session, "safe")

		assert "my_app.x.compute" in html
		assert "Phase 2: Line-Level Drilldown" in html

	def test_safe_mode_omits_source_when_setting_off(self):
		session = self._session([
			_function("my_app.x", [_line(1, "secret = 'PASSWORD123'", 1, 5.0)]),
		])

		with patch.object(renderer, "_phase2_safe_show_source", return_value=False):
			html = renderer._render_phase2_panel(session, "safe")

		assert "PASSWORD123" not in html
		assert "&lt;source omitted&gt;" in html
		# lineno + ms still present — that's the timing telemetry the
		# admin chose to share even with source hidden.
		assert "5.00" in html

	def test_raw_mode_always_shows_source(self):
		session = self._session([
			_function("my_app.x", [_line(1, "secret = 'PASSWORD123'", 1, 5.0)]),
		])

		# Raw mode bypasses the toggle.
		with patch.object(renderer, "_phase2_safe_show_source", return_value=False):
			html = renderer._render_phase2_panel(session, "raw")

		assert "PASSWORD123" in html

	def test_zero_invocation_function_shows_warning(self):
		session = self._session([_function("my_app.never_runs", [])])

		with patch.object(renderer, "_phase2_safe_show_source", return_value=True):
			html = renderer._render_phase2_panel(session, "safe")

		assert "never invoked" in html.lower()


class TestRenderPhase2PanelDiff:
	def test_function_in_two_runs_shows_diff_section(self):
		fn_run1 = _function("my_app.x", [_line(11, "    a = compute()", 100, 800.0)])
		fn_run2 = _function("my_app.x", [_line(11, "    a = compute()", 100, 200.0)])

		session = SimpleNamespace(phase_2_runs=[
			_run("r1", "Ready", [fn_run1], total_ms=800),
			_run("r2", "Ready", [fn_run2], total_ms=200),
		])

		with patch.object(renderer, "_phase2_safe_show_source", return_value=True):
			html = renderer._render_phase2_panel(session, "safe")

		assert "Cross-Run Comparison" in html
		# Delta should be -600 (faster after fix); shown on a row
		assert "-600.00" in html or "-600" in html

	def test_function_in_one_run_no_diff_section(self):
		fn = _function("my_app.x", [_line(11, "compute()", 100, 100.0)])
		session = SimpleNamespace(phase_2_runs=[_run("r1", "Ready", [fn])])

		with patch.object(renderer, "_phase2_safe_show_source", return_value=True):
			html = renderer._render_phase2_panel(session, "safe")

		assert "Cross-Run Comparison" not in html


class TestRenderPhase2PanelSelfContainment:
	def test_no_external_urls_in_output(self):
		# Critical: safe-report self-containment invariant. The phase-2
		# panel must not introduce any http:// / https:// references or
		# external <script>/<link> elements that would make the safe
		# report fetch resources at view time.
		fn = _function("my_app.x", [_line(11, "compute()", 100, 100.0)])
		session = SimpleNamespace(phase_2_runs=[_run("r1", "Ready", [fn])])

		with patch.object(renderer, "_phase2_safe_show_source", return_value=True):
			html = renderer._render_phase2_panel(session, "safe")

		# No protocol-prefixed URLs (excluding xmlns-style namespaces, none
		# of which we use in this panel).
		assert not re.search(r"https?://", html), "phase-2 panel must not introduce external URLs"
		# No <script src=...> or <link href=...> with external URLs
		assert "<script src=" not in html
		assert "<link " not in html