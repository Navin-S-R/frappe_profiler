# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for v0.5.1 instrumentation-noise skip list.

The profiler filters its own whitelisted API endpoints and Frappe's
built-in Recorder doctype whitelisted methods out of the recording
stream at `before_request` time. Without this filter, a recording
session captures its own widget-polling calls (`frappe_profiler.api
.status` fires every ~2 seconds while Recording), which then pollute:

  - per_action rows
  - top_queries / table_breakdown
  - auto-generated "Steps to Reproduce" bullet list
  - total wall-clock / query totals

The check lives in `_should_skip_request()`, gated on
`frappe.form_dict.cmd` so path-based URLs (e.g. `/app/recorder` page
loads) aren't affected.
"""

import types

import frappe


def _set_fake_local(cmd=None):
	"""Install a minimal SimpleNamespace as `frappe.local` with a
	form_dict that holds the given cmd. Returns the namespace so the
	caller can attach more attributes."""
	local = types.SimpleNamespace()
	local.form_dict = {"cmd": cmd} if cmd is not None else {}
	frappe.local = local
	return local


def test_should_skip_frappe_profiler_status_poll():
	"""The widget polls `frappe_profiler.api.status` every ~2 seconds
	while a session is Recording — this is the canonical case."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	_set_fake_local(cmd="frappe_profiler.api.status")
	assert _should_skip_request() is True


def test_should_skip_all_frappe_profiler_api_methods():
	"""Any `frappe_profiler.api.*` whitelisted method is instrumentation
	noise. Covers start, stop, status, submit_frontend_metrics,
	retry_analyze, analyze_fetch, pin_baseline, download_pdf — all of
	them match the prefix."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	for cmd in (
		"frappe_profiler.api.start",
		"frappe_profiler.api.stop",
		"frappe_profiler.api.status",
		"frappe_profiler.api.submit_frontend_metrics",
		"frappe_profiler.api.retry_analyze",
		"frappe_profiler.api.analyze_fetch",
		"frappe_profiler.api.pin_baseline",
		"frappe_profiler.api.download_pdf",
	):
		_set_fake_local(cmd=cmd)
		assert _should_skip_request() is True, (
			f"{cmd} must be skipped — it's profiler instrumentation, "
			"not application work"
		)


def test_should_skip_frappe_builtin_recorder_methods():
	"""If the user has Frappe's built-in Recorder UI open in another tab
	while profiling, its whitelisted calls should not contaminate the
	profiler session."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	for cmd in (
		"frappe.core.doctype.recorder.recorder.export_data",
		"frappe.core.doctype.recorder.recorder.delete",
		"frappe.core.doctype.recorder.recorder.get_request_details",
		"frappe.core.doctype.recorder.recorder.pluck",
		"frappe.core.doctype.recorder.recorder.start",
		"frappe.core.doctype.recorder.recorder.stop",
	):
		_set_fake_local(cmd=cmd)
		assert _should_skip_request() is True, (
			f"{cmd} must be skipped — it's the Frappe recorder's own "
			"plumbing"
		)


def test_should_not_skip_regular_application_calls():
	"""The common case: a real application whitelisted method. Must NOT
	be skipped, or the profiler would stop capturing anything useful."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	for cmd in (
		"frappe.client.save",
		"frappe.client.get_list",
		"frappe.client.set_value",
		"erpnext.selling.doctype.sales_invoice.sales_invoice.make_sales_return",
		"my_custom_app.api.do_thing",
	):
		_set_fake_local(cmd=cmd)
		assert _should_skip_request() is False, (
			f"{cmd} must NOT be skipped — it's real application work"
		)


def test_should_not_skip_page_loads_without_cmd():
	"""Page loads (`/app/recorder`, `/api/resource/Sales Invoice/…`) have
	no cmd in form_dict. They must fall through as 'not noise' even
	when the URL happens to contain the word 'recorder' — the skip
	list matches cmd prefixes, not URL substrings."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	# Empty form_dict entirely (page load with no POST body)
	_set_fake_local()
	assert _should_skip_request() is False

	# form_dict with some other field but no cmd
	local = _set_fake_local()
	local.form_dict = {"doctype": "Recorder", "name": "REC-001"}
	assert _should_skip_request() is False


def test_should_skip_is_defensive_against_missing_local():
	"""Edge case: `frappe.local` might not have `form_dict` during
	startup / health checks / OPTIONS preflights. Must return False
	(don't crash the request) rather than raising."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	# frappe.local with no form_dict attribute at all
	frappe.local = types.SimpleNamespace()
	assert _should_skip_request() is False

	# frappe.local.form_dict is not a dict (unusual but possible)
	frappe.local = types.SimpleNamespace(form_dict="not-a-dict")
	assert _should_skip_request() is False


def test_should_skip_is_prefix_match_not_exact():
	"""Prefix match, so a future method like `frappe_profiler.api
	.retry_analyze_background` would still be caught. Exact match
	would miss that."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	_set_fake_local(cmd="frappe_profiler.api.some_future_method")
	assert _should_skip_request() is True

	_set_fake_local(cmd="frappe.core.doctype.recorder.recorder.new_method")
	assert _should_skip_request() is True


def test_should_not_match_similar_but_different_prefixes():
	"""Guard against over-matching. A hypothetical
	`frappe_profiler_extensions.api.foo` starts with `frappe_profiler`
	but is NOT our API — the prefix ends with a dot for a reason.
	Same for `frappe.core.doctype.recorder_archive.*`."""
	from frappe_profiler.hooks_callbacks import _should_skip_request

	# Almost-but-not-quite prefixes must NOT match
	_set_fake_local(cmd="frappe_profiler_extras.api.foo")
	assert _should_skip_request() is False

	_set_fake_local(cmd="frappe.core.doctype.recorder_archive.foo")
	assert _should_skip_request() is False


def test_before_request_early_exits_on_skipped_cmd():
	"""Source-inspection check: before_request must call
	_should_skip_request AND return early BEFORE setting
	`frappe.local.profiler_session_id`. If the flag gets set, the
	after_request hook will register the recording even though we
	meant to skip it."""
	import inspect
	from frappe_profiler import hooks_callbacks

	src = inspect.getsource(hooks_callbacks.before_request)

	# The skip call must appear in before_request.
	assert "_should_skip_request()" in src

	# The skip check must occur BEFORE the profiler_session_id assignment —
	# setting the flag first would cause after_request to register the
	# recording anyway, defeating the filter.
	skip_idx = src.find("_should_skip_request()")
	flag_idx = src.find("frappe.local.profiler_session_id = session_uuid")
	assert skip_idx > 0, "before_request must call _should_skip_request()"
	assert flag_idx > 0, "before_request must set profiler_session_id"
	assert skip_idx < flag_idx, (
		"_should_skip_request() must be checked BEFORE "
		"profiler_session_id is set, otherwise after_request will "
		"register the filtered recording anyway"
	)
