# frappe_profiler/tests/test_scheduler_inline_fallback.py
# Copyright (c) 2026, Frappe Profiler contributors

"""Tests for v0.5.0 scheduler-aware analyze enqueue.

When bench disable-scheduler is in effect (is_scheduler_disabled() returns
True), _enqueue_analyze must pass now=True to frappe.enqueue so analyze
runs synchronously instead of being pushed to a queue no worker will
consume. Also verifies the recording-count safety cap prevents inline
analyze from exceeding the gunicorn request timeout on huge sessions.
"""

import inspect

from frappe_profiler import api


def test_enqueue_analyze_checks_scheduler():
    src = inspect.getsource(api._enqueue_analyze)
    assert "is_scheduler_disabled" in src
    assert "now=" in src


def test_stop_returns_ran_inline_flag():
    src = inspect.getsource(api.stop)
    # stop() must propagate whether analyze ran inline so the widget
    # can transition directly to Ready without passing through Analyzing.
    assert "ran_inline" in src


def test_stop_session_honors_inline_analyze_limit():
    src = inspect.getsource(api._stop_session)
    assert "profiler_inline_analyze_limit" in src


def test_enqueue_analyze_passes_now_when_scheduler_disabled(monkeypatch):
    """Full-stack check: when is_scheduler_disabled() is True, frappe.enqueue
    should be called with now=True."""
    import frappe_profiler.api as api_mod

    captured = {}

    def fake_enqueue(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    def fake_is_disabled():
        return True

    import sys, types
    fake_mod = types.ModuleType("frappe.utils.scheduler")
    fake_mod.is_scheduler_disabled = fake_is_disabled
    monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", fake_mod)

    fake_frappe_utils = types.ModuleType("frappe.utils")
    fake_frappe_utils.scheduler = fake_mod
    monkeypatch.setitem(sys.modules, "frappe.utils", fake_frappe_utils)

    # Patch the frappe.enqueue that the module will call
    import frappe
    monkeypatch.setattr(frappe, "enqueue", fake_enqueue)
    monkeypatch.setattr(frappe, "logger", lambda: types.SimpleNamespace(warning=lambda *a, **k: None))

    ran_inline = api_mod._enqueue_analyze("test-uuid-123")

    assert captured["kwargs"].get("now") is True
    assert captured["kwargs"].get("session_uuid") == "test-uuid-123"
    assert ran_inline is True


def test_stop_session_blocks_huge_inline_analyze(monkeypatch):
    """Behavioral test for the inline-analyze recording cap.

    When scheduler is disabled AND recording_count > profiler_inline_analyze_limit,
    _stop_session must mark the Profiler Session as Failed with a helpful
    analyze_error and NOT call _enqueue_analyze. This prevents gunicorn from
    killing a 200-recording inline analyze partway through, which would
    leave the session half-analyzed.
    """
    import sys
    import types

    import frappe
    from frappe_profiler import api as api_mod
    from frappe_profiler import session as session_mod

    # Stub scheduler_disabled = True
    fake_sched_mod = types.ModuleType("frappe.utils.scheduler")
    fake_sched_mod.is_scheduler_disabled = lambda: True
    monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", fake_sched_mod)

    fake_frappe_utils = types.ModuleType("frappe.utils")
    fake_frappe_utils.scheduler = fake_sched_mod
    monkeypatch.setitem(sys.modules, "frappe.utils", fake_frappe_utils)

    # Stub frappe.conf, frappe.db, frappe.cache, session helpers
    class FakeConf:
        def get(self, key, default=None):
            return {"profiler_inline_analyze_limit": 50}.get(key, default)

    set_calls = []

    class FakeDB:
        def set_value(self, doctype, name, field_or_dict, value=None):
            set_calls.append((doctype, name, field_or_dict, value))
            return None

        def commit(self):
            pass

        def get_value(self, *a, **kw):
            return "PS-0001"

    monkeypatch.setattr(frappe, "conf", FakeConf(), raising=False)
    monkeypatch.setattr(frappe, "db", FakeDB(), raising=False)
    monkeypatch.setattr(
        frappe, "local", types.SimpleNamespace(), raising=False
    )
    monkeypatch.setattr(
        session_mod, "get_active_session_for", lambda user: "test-uuid-huge",
    )
    monkeypatch.setattr(session_mod, "clear_active_session", lambda user: None)
    monkeypatch.setattr(
        session_mod, "recording_count", lambda session_uuid: 75
    )

    # Stub capture._force_stop_inflight_capture and infra_capture._force_stop_inflight
    from frappe_profiler import capture
    monkeypatch.setattr(
        capture, "_force_stop_inflight_capture", lambda local_proxy: None
    )
    from frappe_profiler import infra_capture
    monkeypatch.setattr(
        infra_capture, "_force_stop_inflight", lambda local_proxy: None
    )

    # Spy on _enqueue_analyze to make sure it's NOT called
    enqueue_calls = []
    monkeypatch.setattr(
        api_mod, "_enqueue_analyze",
        lambda session_uuid: enqueue_calls.append(session_uuid) or False,
    )

    docname, ran_inline = api_mod._stop_session("alice@example.com", "test-uuid-huge")

    assert docname == "PS-0001"
    assert ran_inline is False
    assert enqueue_calls == []  # did NOT enqueue
    # Session must be marked Failed with the cap message
    status_calls = [
        c for c in set_calls
        if c[0] == "Profiler Session"
        and isinstance(c[2], dict)
        and c[2].get("status") == "Failed"
    ]
    assert len(status_calls) == 1
    error_msg = status_calls[0][2].get("analyze_error", "")
    assert "75" in error_msg
    assert "50" in error_msg
    assert "enable-scheduler" in error_msg
