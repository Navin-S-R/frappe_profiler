# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for the redundant_calls analyzer."""

import json

from frappe_profiler.analyzers import redundant_calls
from frappe_profiler.analyzers.base import AnalyzeContext


def _sidecar_entry(fn_name, raw, safe):
	return {
		"fn_name": fn_name,
		"identifier_raw": raw,
		"identifier_safe": safe,
	}


def test_emits_finding_when_get_doc_threshold_exceeded():
	# 8 identical get_doc calls (threshold = 5)
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "ITEM-X"), ("Item", "abc123hash"))
			for _ in range(8)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.actions = [{"action_label": "test", "duration_ms": 100}]
	result = redundant_calls.analyze([recording], ctx)

	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	assert rc[0]["affected_count"] == 8
	# Title in the safe form (no plaintext name)
	assert "Item" in rc[0]["title"]
	# technical_detail carries both forms
	td = json.loads(rc[0]["technical_detail_json"])
	assert "identifier_raw" in td
	assert "identifier_safe" in td


def test_no_finding_below_threshold():
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "hash1")),
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "hash1")),
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	assert result.findings == []


def test_high_severity_at_5x_threshold():
	# 25 calls = 5x threshold of 5 → High
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "hash1"))
			for _ in range(25)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc[0]["severity"] == "High"


def test_cache_get_threshold_separate_from_doc_threshold():
	# 8 cache_get calls — under cache threshold of 10
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("cache_get", "user_lang:x", "hashx")
			for _ in range(8)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	assert result.findings == []  # below cache threshold


def test_truncation_marker_emits_warning():
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry("get_doc", ("Item", "X"), ("Item", "h1"))
			for _ in range(3)
		] + [{"_truncated": True}],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	redundant_calls.analyze([recording], ctx)
	assert any("truncated" in w.lower() for w in ctx.warnings)


def test_identifier_safe_is_used_as_bucket_key():
	"""Two different raw values with different safe values must not merge."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": (
			[_sidecar_entry("get_doc", ("Item", "A"), ("Item", "hashA")) for _ in range(6)]
			+ [_sidecar_entry("get_doc", ("Item", "B"), ("Item", "hashB")) for _ in range(6)]
		),
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	# Two separate findings because the safe hashes are different
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 2
