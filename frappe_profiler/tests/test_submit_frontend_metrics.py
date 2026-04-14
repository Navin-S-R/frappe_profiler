# frappe_profiler/tests/test_submit_frontend_metrics.py
# Copyright (c) 2026, Frappe Profiler contributors

"""Tests for api.submit_frontend_metrics (v0.5.0).

The endpoint accepts a JSON string payload (because sendBeacon sends
raw Blob, not form-encoded), validates session ownership, merges
against any existing Redis blob, and enforces soft caps server-side.
"""

import json

import pytest


@pytest.fixture
def mock_frappe(monkeypatch):
    """Install the minimal Frappe stubs needed to exercise the endpoint
    without a real site."""
    import frappe

    cache_store = {}

    class FakeCache:
        def get_value(self, key):
            return cache_store.get(key)

        def set_value(self, key, value, expires_in_sec=None):
            cache_store[key] = value

        def delete_value(self, key):
            cache_store.pop(key, None)

    class FakeSession:
        user = "alice@example.com"

    monkeypatch.setattr(frappe, "cache", FakeCache(), raising=False)
    monkeypatch.setattr(frappe, "session", FakeSession(), raising=False)
    monkeypatch.setattr(frappe, "parse_json", json.loads, raising=False)
    monkeypatch.setattr(
        frappe, "get_roles",
        lambda user=None: ["System Manager"],
        raising=False,
    )
    monkeypatch.setattr(
        frappe, "log_error", lambda *a, **k: None, raising=False
    )

    class PermError(Exception):
        pass

    frappe.PermissionError = PermError

    def fake_throw(msg, exc=None):
        raise (exc or Exception)(msg)

    monkeypatch.setattr(frappe, "throw", fake_throw, raising=False)

    return {"cache_store": cache_store}


def test_submit_rejects_invalid_json(mock_frappe):
    from frappe_profiler import api

    result = api.submit_frontend_metrics(payload="not-json")
    assert result["accepted"] is False
    assert result["reason"] == "invalid json"


def test_submit_rejects_missing_session_uuid(mock_frappe):
    from frappe_profiler import api

    result = api.submit_frontend_metrics(payload=json.dumps({"xhr": []}))
    assert result["accepted"] is False
    assert result["reason"] == "missing session_uuid"


def test_submit_rejects_unknown_session(mock_frappe):
    from frappe_profiler import api

    result = api.submit_frontend_metrics(
        payload=json.dumps({"session_uuid": "nope", "xhr": [], "vitals": []})
    )
    assert result["accepted"] is False
    assert result["reason"] == "session not found"


def test_submit_rejects_cross_user_session(mock_frappe):
    from frappe_profiler import api

    mock_frappe["cache_store"]["profiler:session:S1:meta"] = {
        "user": "bob@example.com",
        "docname": "PS-0001",
    }

    result = api.submit_frontend_metrics(
        payload=json.dumps({"session_uuid": "S1", "xhr": [], "vitals": []})
    )
    assert result["accepted"] is False
    assert result["reason"] == "session not found"


def test_submit_accepts_and_stores(mock_frappe):
    from frappe_profiler import api

    mock_frappe["cache_store"]["profiler:session:S2:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-0002",
    }

    payload = json.dumps({
        "session_uuid": "S2",
        "xhr": [
            {"recording_id": "r1", "url": "/api/method/foo",
             "method": "GET", "duration_ms": 120, "status": 200,
             "response_size_bytes": 512, "transport": "fetch",
             "timestamp": 1700000000000},
        ],
        "vitals": [
            {"name": "lcp", "value_ms": 1800, "page_url": "/app",
             "timestamp": 1700000000000},
        ],
    })

    result = api.submit_frontend_metrics(payload=payload)
    assert result["accepted"] is True
    assert result["xhr_count"] == 1
    assert result["vitals_count"] == 1

    stored = mock_frappe["cache_store"]["profiler:frontend:S2"]
    assert len(stored["xhr"]) == 1
    assert len(stored["vitals"]) == 1


def test_submit_merges_with_existing_blob(mock_frappe):
    from frappe_profiler import api

    mock_frappe["cache_store"]["profiler:session:S3:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-0003",
    }
    mock_frappe["cache_store"]["profiler:frontend:S3"] = {
        "xhr": [{"recording_id": "r0", "url": "/api/prev",
                 "method": "POST", "duration_ms": 50, "status": 200,
                 "response_size_bytes": 100, "transport": "xhr",
                 "timestamp": 1}],
        "vitals": [],
    }

    payload = json.dumps({
        "session_uuid": "S3",
        "xhr": [
            {"recording_id": "r1", "url": "/api/next",
             "method": "GET", "duration_ms": 200, "status": 200,
             "response_size_bytes": 999, "transport": "fetch",
             "timestamp": 2},
        ],
        "vitals": [],
    })

    result = api.submit_frontend_metrics(payload=payload)
    assert result["accepted"] is True
    stored = mock_frappe["cache_store"]["profiler:frontend:S3"]
    assert len(stored["xhr"]) == 2
    assert stored["xhr"][0]["url"] == "/api/prev"
    assert stored["xhr"][1]["url"] == "/api/next"


def test_submit_enforces_soft_cap_tail_prefer(mock_frappe):
    """Over-cap submissions must keep the most-recent entries (tail),
    not the oldest, because end-of-flow is where slow things happen."""
    from frappe_profiler import api

    mock_frappe["cache_store"]["profiler:session:S4:meta"] = {
        "user": "alice@example.com",
        "docname": "PS-0004",
    }

    # Push 1500 XHRs — 500 over the cap of 1000.
    xhrs = [
        {"recording_id": f"r{i}", "url": f"/api/{i}",
         "method": "GET", "duration_ms": 10, "status": 200,
         "response_size_bytes": 0, "transport": "fetch",
         "timestamp": i}
        for i in range(1500)
    ]
    payload = json.dumps({"session_uuid": "S4", "xhr": xhrs, "vitals": []})

    result = api.submit_frontend_metrics(payload=payload)
    assert result["accepted"] is True
    stored = mock_frappe["cache_store"]["profiler:frontend:S4"]
    assert len(stored["xhr"]) == 1000
    # Tail preferred — the last entry should be index 1499.
    assert stored["xhr"][-1]["timestamp"] == 1499
