# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for renderer._render_phase2_panel — exercises the phase-2 HTML
without spinning up Frappe / Jinja. We feed a minimal session-doc-shaped
object directly to the helper.
"""

import json
import re
from types import SimpleNamespace

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
		assert renderer._render_phase2_panel(session) == ""

	def test_phase_2_runs_attribute_missing_returns_empty(self):
		session = SimpleNamespace()
		assert renderer._render_phase2_panel(session) == ""


class TestRenderPhase2PanelSingleRun:
	def _session(self, results):
		return SimpleNamespace(phase_2_runs=[_run("r1", "Ready", results)])

	def test_function_dotted_path_appears_in_output(self):
		session = self._session([
			_function("my_app.x.compute", [_line(1, "x = 1", 5, 10.0)]),
		])

		html = renderer._render_phase2_panel(session)

		assert "my_app.x.compute" in html
		assert "Phase 2: Line-Level Drilldown" in html

	def test_source_always_rendered(self):
		# v0.6.0 Round 7: safe-mode source toggle removed. Source is
		# always rendered now.
		session = self._session([
			_function("my_app.x", [_line(1, "literal_value = 'foo'", 1, 5.0)]),
		])

		html = renderer._render_phase2_panel(session)

		assert "literal_value" in html

	def test_zero_invocation_function_shows_warning(self):
		session = self._session([_function("my_app.never_runs", [])])

		html = renderer._render_phase2_panel(session)

		assert "never invoked" in html.lower()


class TestRenderPhase2PanelDiff:
	def test_function_in_two_runs_shows_diff_section(self):
		fn_run1 = _function("my_app.x", [_line(11, "    a = compute()", 100, 800.0)])
		fn_run2 = _function("my_app.x", [_line(11, "    a = compute()", 100, 200.0)])

		session = SimpleNamespace(phase_2_runs=[
			_run("r1", "Ready", [fn_run1], total_ms=800),
			_run("r2", "Ready", [fn_run2], total_ms=200),
		])

		html = renderer._render_phase2_panel(session)

		assert "Cross-Run Comparison" in html
		# Delta should be -600 (faster after fix); shown on a row
		assert "-600.00" in html or "-600" in html

	def test_function_in_one_run_no_diff_section(self):
		fn = _function("my_app.x", [_line(11, "compute()", 100, 100.0)])
		session = SimpleNamespace(phase_2_runs=[_run("r1", "Ready", [fn])])

		html = renderer._render_phase2_panel(session)

		assert "Cross-Run Comparison" not in html


class TestRenderPhase2PanelAutoExpandChain:
	"""When a curated pick was auto-expanded into a chain, the run's
	picks_json marks descendant functions with source='auto_expand'.
	The renderer should indent those function headers and prefix with
	an arrow so the chain reads top-down as a stack."""

	def _run_with_chain(self, root_path, descendant_path):
		# picks_json captures the source of each pick.
		picks = [
			{"dotted_path": root_path, "source": "curated"},
			{"dotted_path": descendant_path, "source": "auto_expand"},
		]
		results = [
			_function(root_path, [_line(1, "self.descendant()", 1, 100.0)]),
			_function(descendant_path, [_line(5, "compute()", 1, 95.0)]),
		]
		return SimpleNamespace(
			run_uuid="r1",
			status="Ready",
			started_at="2026-05-07 12:00:00",
			ended_at="2026-05-07 12:01:00",
			total_ms=195.0,
			picks_json=json.dumps(picks),
			results_json=json.dumps(results),
		)

	def test_root_pick_renders_flush_left(self):
		session = SimpleNamespace(phase_2_runs=[
			self._run_with_chain("my_app.x.root_fn", "my_app.x.descendant"),
		])

		html = renderer._render_phase2_panel(session)

		# rfind targets the function-table header (the descendant appears
		# earlier in the run's "Picks:" summary line as well).
		root_idx = html.rfind("my_app.x.root_fn")
		assert root_idx > -1
		nearby = html[max(0, root_idx - 200):root_idx]
		assert "margin: 12px 0 12px 24px" not in nearby
		assert "↳" not in nearby

	def test_auto_expanded_descendant_renders_indented(self):
		session = SimpleNamespace(phase_2_runs=[
			self._run_with_chain("my_app.x.root_fn", "my_app.x.descendant"),
		])

		html = renderer._render_phase2_panel(session)

		desc_idx = html.rfind("my_app.x.descendant")
		assert desc_idx > -1
		nearby = html[max(0, desc_idx - 300):desc_idx]
		assert "margin: 12px 0 12px 24px" in nearby
		assert "↳" in nearby

	def test_no_picks_json_falls_back_to_curated_no_indent(self):
		# Older runs may not carry source markers; renderer should treat
		# everything as curated (no indent) rather than break.
		results = [_function("my_app.x.fn", [_line(1, "x = 1", 1, 100.0)])]
		run = SimpleNamespace(
			run_uuid="r1",
			status="Ready",
			started_at="2026-05-07 12:00:00",
			ended_at="2026-05-07 12:01:00",
			total_ms=100.0,
			picks_json="",
			results_json=json.dumps(results),
		)
		session = SimpleNamespace(phase_2_runs=[run])

		html = renderer._render_phase2_panel(session)

		fn_idx = html.rfind("my_app.x.fn")
		assert fn_idx > -1
		nearby = html[max(0, fn_idx - 200):fn_idx]
		assert "↳" not in nearby


class TestRenderPhase2PanelSelfContainment:
	def test_no_external_urls_in_output(self):
		# Critical: safe-report self-containment invariant. The phase-2
		# panel must not introduce any http:// / https:// references or
		# external <script>/<link> elements that would make the safe
		# report fetch resources at view time.
		fn = _function("my_app.x", [_line(11, "compute()", 100, 100.0)])
		session = SimpleNamespace(phase_2_runs=[_run("r1", "Ready", [fn])])

		html = renderer._render_phase2_panel(session)

		# No protocol-prefixed URLs (excluding xmlns-style namespaces, none
		# of which we use in this panel).
		assert not re.search(r"https?://", html), "phase-2 panel must not introduce external URLs"
		# No <script src=...> or <link href=...> with external URLs
		assert "<script src=" not in html
		assert "<link " not in html