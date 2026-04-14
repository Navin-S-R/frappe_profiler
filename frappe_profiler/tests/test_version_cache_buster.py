# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Test that hooks.py uses __version__ for the asset cache buster."""

from frappe_profiler import __version__, hooks


def test_app_include_js_contains_version():
	assert f"v={__version__}" in hooks.app_include_js


def test_app_include_css_contains_version():
	assert f"v={__version__}" in hooks.app_include_css


def test_app_include_paths_are_correct():
	assert hooks.app_include_js.startswith("/assets/frappe_profiler/js/floating_widget.js")
	assert hooks.app_include_css.startswith("/assets/frappe_profiler/css/floating_widget.css")
