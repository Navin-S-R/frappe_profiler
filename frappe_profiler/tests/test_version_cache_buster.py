# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Test that hooks.py uses __version__ for the asset cache buster."""

from frappe_profiler import __version__, hooks


def _js_entries():
	"""Return app_include_js as a list regardless of string/list form."""
	val = hooks.app_include_js
	return val if isinstance(val, list) else [val]


def test_app_include_js_contains_version():
	for entry in _js_entries():
		assert f"v={__version__}" in entry


def test_app_include_css_contains_version():
	assert f"v={__version__}" in hooks.app_include_css


def test_app_include_paths_are_correct():
	entries = _js_entries()
	# floating_widget.js must always be present.
	assert any(
		e.startswith("/assets/frappe_profiler/js/floating_widget.js")
		for e in entries
	)
	# v0.5.0 adds profiler_frontend.js alongside it.
	assert any(
		e.startswith("/assets/frappe_profiler/js/profiler_frontend.js")
		for e in entries
	)
	assert hooks.app_include_css.startswith("/assets/frappe_profiler/css/floating_widget.css")
