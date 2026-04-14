# frappe_profiler/tests/test_correlation_header.py
# Copyright (c) 2026, Frappe Profiler contributors

"""Tests for the X-Profiler-Recording-Id response header injection (v0.5.0).

The correlation header is read by the browser-side profiler_frontend.js
shim to tie each XHR timing back to a specific server recording. The
Access-Control-Expose-Headers entry is load-bearing: without it, browsers
refuse to surface the custom header to JavaScript, even for same-origin
requests. That's the #1 failure mode in frontend instrumentation.
"""

import types

import frappe


def _set_fake_local(headers=None):
    """Replace frappe.local with a minimal SimpleNamespace holding a
    dict-like response_headers. Returns the namespace so tests can
    assert on it."""
    local = types.SimpleNamespace()
    if headers is not None:
        local.response_headers = headers
    frappe.local = local
    return local


def test_inject_correlation_header_sets_both_headers():
    from frappe_profiler.hooks_callbacks import _inject_correlation_header

    headers = {}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    assert headers["X-Profiler-Recording-Id"] == "rec-uuid-abc123"
    assert "X-Profiler-Recording-Id" in headers.get("Access-Control-Expose-Headers", "")


def test_inject_correlation_header_merges_with_existing_expose():
    """If another app has already set Access-Control-Expose-Headers,
    our value must be appended, not overwritten."""
    from frappe_profiler.hooks_callbacks import _inject_correlation_header

    headers = {"Access-Control-Expose-Headers": "X-Some-Other-Header"}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    expose = headers["Access-Control-Expose-Headers"]
    assert "X-Some-Other-Header" in expose
    assert "X-Profiler-Recording-Id" in expose


def test_inject_correlation_header_idempotent():
    """Calling the injector twice must not duplicate the expose entry."""
    from frappe_profiler.hooks_callbacks import _inject_correlation_header

    headers = {}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-1")
    _inject_correlation_header("rec-uuid-2")

    expose = headers["Access-Control-Expose-Headers"]
    assert expose.count("X-Profiler-Recording-Id") == 1


def test_inject_correlation_header_noop_without_response_headers():
    """If frappe.local has no response_headers attribute (non-HTTP context
    like a script shell), the injector must no-op, not raise."""
    from frappe_profiler.hooks_callbacks import _inject_correlation_header

    _set_fake_local()  # no response_headers attribute

    # Must not raise.
    _inject_correlation_header("rec-uuid-abc123")


def test_hooks_callbacks_invokes_snapshot():
    """Source-inspection regression guard: before/after hooks must call
    infra_capture. Source inspection is an established pattern in this
    codebase (see test_api_start_kwargs.py)."""
    import inspect
    from frappe_profiler import hooks_callbacks

    before = inspect.getsource(hooks_callbacks.before_request)
    after = inspect.getsource(hooks_callbacks.after_request)
    before_job = inspect.getsource(hooks_callbacks.before_job)
    after_job = inspect.getsource(hooks_callbacks.after_job)

    assert "infra_capture" in before
    assert "infra_capture" in after
    assert "infra_capture" in before_job
    assert "infra_capture" in after_job
    assert "profiler:infra:" in after
    assert "profiler:infra:" in after_job


def test_stop_session_force_stops_infra_inflight():
    import inspect
    from frappe_profiler import api

    src = inspect.getsource(api._stop_session)
    assert "infra_capture._force_stop_inflight" in src


def test_after_request_injects_correlation_header():
    import inspect
    from frappe_profiler import hooks_callbacks

    src = inspect.getsource(hooks_callbacks.after_request)
    assert "_inject_correlation_header" in src
