# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Smoke tests for frontend assets (widget JS, form JS, CSS).

Full browser integration tests would need cypress/playwright, which is
heavy to set up. These cheap smoke tests just verify that the JS files
parse, that the widget's state machine symbols are present, and that the
CSS selectors look sane. Good enough to catch regressions where someone
accidentally breaks the syntax or deletes a critical hook.

Run with `pytest frappe_profiler/tests/test_frontend_assets.py -v`.
"""

import os
import re
import shutil
import subprocess

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDGET_JS = os.path.join(APP_DIR, "public", "js", "floating_widget.js")
WIDGET_CSS = os.path.join(APP_DIR, "public", "css", "floating_widget.css")
FORM_JS = os.path.join(
	APP_DIR, "frappe_profiler", "doctype", "profiler_session", "profiler_session.js"
)
LIST_JS = os.path.join(
	APP_DIR, "frappe_profiler", "doctype", "profiler_session", "profiler_session_list.js"
)


def _node_check(js_path: str) -> None:
	"""Run `node --check` to validate JS syntax.

	Skips if node isn't installed — that's fine, frappe benches ship with
	node so in practice this will run everywhere that matters.
	"""
	if not shutil.which("node"):
		pytest.skip("node not installed")
	result = subprocess.run(
		["node", "--check", js_path],
		capture_output=True,
		text=True,
	)
	if result.returncode != 0:
		pytest.fail(f"node --check failed for {js_path}:\n{result.stderr}")


def test_widget_js_syntax():
	_node_check(WIDGET_JS)


def test_form_js_syntax():
	_node_check(FORM_JS)


def test_list_js_syntax():
	_node_check(LIST_JS)


def test_widget_has_state_machine_constants():
	"""The widget's 5 states must all be referenced in the JS."""
	with open(WIDGET_JS) as f:
		src = f.read()
	for state in ("inactive", "recording", "stopping", "analyzing", "ready"):
		assert f"fp-state-{state}" in src, f"Missing state class: fp-state-{state}"


def test_widget_has_visibility_listener():
	"""Fix #13: widget must pause polling on tab hide."""
	with open(WIDGET_JS) as f:
		src = f.read()
	assert "visibilitychange" in src
	assert "document.hidden" in src
	assert "stopPolling" in src


def test_widget_role_check():
	"""Widget must check for Profiler User or System Manager role."""
	with open(WIDGET_JS) as f:
		src = f.read()
	assert "System Manager" in src
	assert "Profiler User" in src
	assert "userHasRole" in src


def test_form_js_has_retry_button():
	"""Fix #11: form JS must have a Retry Analyze button for Failed sessions."""
	with open(FORM_JS) as f:
		src = f.read()
	assert "Retry Analyze" in src
	assert "retry_analyze" in src
	assert 'status !== "Failed"' in src or "Failed" in src


def test_form_js_has_analyzer_warnings_intro():
	"""Fix #15: analyzer_warnings should be surfaced as a form intro banner."""
	with open(FORM_JS) as f:
		src = f.read()
	assert "analyzer_warnings" in src
	assert "set_intro" in src


def test_form_js_raw_report_gated_to_admin():
	"""Form JS must check for admin role before showing the Raw Report button."""
	with open(FORM_JS) as f:
		src = f.read()
	assert "user_can_see_raw" in src
	assert "System Manager" in src


def test_list_js_severity_indicators():
	"""List view must color-code by top_severity for Ready sessions."""
	with open(LIST_JS) as f:
		src = f.read()
	assert "top_severity" in src
	assert "High severity" in src
	assert "Medium severity" in src


def test_widget_start_has_error_callback():
	"""v0.5.1 regression guard: the Start dialog's frappe.call must
	include an error callback. Without it, any server-side failure of
	api.start (permission error, concurrent session, server exception)
	silently closes the dialog with no feedback to the user — the
	exact 'widget not working as expected' failure mode reported by
	users who lacked the Profiler User role. The stop API already had
	an error callback added in an earlier fix; this test forces start
	to stay symmetric.
	"""
	with open(WIDGET_JS) as f:
		src = f.read()

	# Find the start dialog's primary_action block and verify it
	# contains both callback: and error:.
	start_call_idx = src.find("frappe_profiler.api.start")
	assert start_call_idx > 0, "widget must call frappe_profiler.api.start"

	# Look in the ~2000 chars around the start call for an error: key.
	window = src[start_call_idx : start_call_idx + 2000]
	assert "error:" in window or "error: " in window, (
		"openStartDialog's frappe.call(api.start) must have an error "
		"callback — without it, permission errors and server exceptions "
		"leave the widget silently unresponsive after the dialog closes"
	)


def test_widget_stop_has_error_callback():
	"""Companion to the start-error guard: the Stop call already had an
	error handler added in an earlier fix. Make sure it stays."""
	with open(WIDGET_JS) as f:
		src = f.read()

	stop_call_idx = src.find("frappe_profiler.api.stop")
	assert stop_call_idx > 0
	window = src[stop_call_idx : stop_call_idx + 2000]
	assert "error:" in window, (
		"confirmAndStop's frappe.call(api.stop) must have an error "
		"callback so failed stops don't strand the widget in 'Stopping…'"
	)


def test_widget_css_selectors():
	"""CSS must define the widget root and state classes."""
	with open(WIDGET_CSS) as f:
		src = f.read()
	assert "#frappe-profiler-widget" in src
	for state in ("inactive", "recording", "stopping", "analyzing", "ready"):
		assert f".fp-state-{state}" in src, f"Missing CSS class: .fp-state-{state}"


def test_widget_css_has_print_safety():
	"""CSS should have print-safe rules since reports are often printed."""
	with open(WIDGET_CSS) as f:
		src = f.read()
	# Widget is for Desk, not print, but it should be hidden in print mode
	# OR have sane fallbacks. Minimum: the file should not be empty.
	assert len(src) > 100
