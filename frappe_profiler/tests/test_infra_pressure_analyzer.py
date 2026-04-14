# frappe_profiler/tests/test_infra_pressure_analyzer.py
# Copyright (c) 2026, Frappe Profiler contributors

"""Tests for v0.5.0 infra_pressure analyzer."""

import json
import os

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_session():
    with open(os.path.join(FIXTURES_DIR, "infra_pressure_session.json")) as f:
        return json.load(f)


def _empty_context():
    from frappe_profiler.analyzers.base import AnalyzeContext
    return AnalyzeContext(session_uuid="test", docname="test")


def _synth_infra(**overrides):
    """Return a minimal infra dict with every Balanced-tier key set to a
    non-breaching value, then apply overrides."""
    base = {
        "sys_cpu_percent": 20,
        "worker_rss_bytes": 500_000_000,
        "sys_swap_used_bytes": 0,
        "sys_mem_available_bytes": 1_000_000_000,
        "sys_mem_total_bytes": 16_000_000_000,
        "sys_load_avg_1min": 1.0,
        "db_threads_connected": 5,
        "db_threads_running": 1,
        "db_slow_queries_total": 0,
        "redis_instantaneous_ops_per_sec": 100,
        "rq_queue_default": 0,
        "rq_queue_short": 0,
        "rq_queue_long": 0,
    }
    base.update(overrides)
    return base


def test_resource_contention_fires_on_sustained_cpu():
    from frappe_profiler.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    findings = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert len(findings) == 1
    assert findings[0]["severity"] in ("High", "Medium")
    # 2 of 3 actions breached CPU_HIGH_PCT (92, 88 > 85); 35 is fine.
    assert findings[0]["affected_count"] == 2


def test_memory_pressure_does_not_fire_on_small_delta():
    """The fixture session has 520M→680M→520M RSS. End-start delta is 0
    and the intermediate spike (160M) is below the 200MB threshold. Swap
    peaks at 50MB which is below the 100MB warn threshold. So neither
    arm of Memory Pressure should fire."""
    from frappe_profiler.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    mp = [f for f in result.findings if f["finding_type"] == "Memory Pressure"]
    assert mp == []


def test_memory_pressure_fires_on_large_rss_delta():
    """Synthetic recordings where RSS grows by 300MB — must fire Medium
    (delta > 200MB threshold but < 500MB critical)."""
    from frappe_profiler.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(worker_rss_bytes=400_000_000)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(worker_rss_bytes=700_000_000)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    mp = [f for f in result.findings if f["finding_type"] == "Memory Pressure"]
    assert len(mp) == 1
    assert mp[0]["severity"] == "Medium"


def test_memory_pressure_fires_high_on_swap():
    """Any swap above the warn threshold fires High severity."""
    from frappe_profiler.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(sys_swap_used_bytes=200_000_000)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_swap_used_bytes=200_000_000)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    mp = [f for f in result.findings if f["finding_type"] == "Memory Pressure"]
    assert len(mp) == 1
    assert mp[0]["severity"] == "High"


def test_db_pool_saturation_does_not_fire_below_threshold():
    """With threads_running max 10/15 ≈ 0.67, DB Pool Saturation should
    NOT fire (threshold is 0.9)."""
    from frappe_profiler.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    pool = [f for f in result.findings if f["finding_type"] == "DB Pool Saturation"]
    assert pool == []


def test_aggregate_includes_timeline_and_summary():
    from frappe_profiler.analyzers import infra_pressure

    session = _load_session()
    result = infra_pressure.analyze(session["recordings"], _empty_context())

    agg = result.aggregate
    assert "infra_timeline" in agg
    assert "infra_summary" in agg
    assert len(agg["infra_timeline"]) == 3
    assert agg["infra_summary"]["cpu_peak"] == 92.0
    assert agg["infra_summary"]["cpu_avg"] == pytest.approx((92.0 + 88.0 + 35.0) / 3, abs=0.01)
    assert agg["infra_summary"]["load_peak"] == 5.1
    assert agg["infra_summary"]["rq_peak_depth"]["default"] == 8


def test_min_actions_affected_guard():
    """A single spiky action must not fire Resource Contention on its own."""
    from frappe_profiler.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(sys_cpu_percent=95)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_cpu_percent=20)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    rc = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert rc == []


def test_severity_escalates_on_critical_cpu():
    from frappe_profiler.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a", "infra": _synth_infra(sys_cpu_percent=97)},
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_cpu_percent=99)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    rc = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert len(rc) == 1
    assert rc[0]["severity"] == "High"


def test_empty_recordings_is_safe():
    from frappe_profiler.analyzers import infra_pressure

    result = infra_pressure.analyze([], _empty_context())
    assert result.findings == []
    assert result.aggregate["infra_timeline"] == []


def test_recordings_without_infra_are_ignored():
    """Not every recording has an infra dict (e.g. if the session was
    started before v0.5.0 rolled out). The analyzer should skip them
    cleanly rather than crashing."""
    from frappe_profiler.analyzers import infra_pressure

    recordings = [
        {"uuid": "r1", "action_label": "a"},  # no infra
        {"uuid": "r2", "action_label": "b", "infra": _synth_infra(sys_cpu_percent=95)},
    ]
    result = infra_pressure.analyze(recordings, _empty_context())
    # Only one action had infra — no sustained-breach finding possible.
    rc = [f for f in result.findings if f["finding_type"] == "Resource Contention"]
    assert rc == []
    # Timeline should only include the action with infra.
    assert len(result.aggregate["infra_timeline"]) == 1
