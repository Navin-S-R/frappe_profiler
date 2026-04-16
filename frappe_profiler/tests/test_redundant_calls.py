# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for the redundant_calls analyzer.

v0.5.2: the analyzer now requires a caller_stack on each sidecar
entry and filters findings whose callsite is inside frappe/*
framework code (user can't act on framework loops). The fixture
builder in this file sets a DEFAULT user-code stack so existing
tests keep verifying the core aggregation logic; two new tests
exercise the framework-filter + no-stack fallback paths.
"""

import json

from frappe_profiler.analyzers import redundant_calls
from frappe_profiler.analyzers.base import AnalyzeContext


# Canonical user-code caller stack used by the default fixture. Passing
# this through walk_callsite yields ``apps/myapp/controllers/bulk.py:42``
# as the blame frame, so findings built from this fixture are kept.
_USER_CALLER_STACK = [
	{"filename": "frappe/app.py", "lineno": 120, "function": "application"},
	{"filename": "frappe/handler.py", "lineno": 46, "function": "handle"},
	{"filename": "apps/myapp/controllers/bulk.py", "lineno": 42, "function": "do_import"},
]

# Framework-only stack — walk_callsite returns None for this, so
# findings built from it get filtered out.
_FRAMEWORK_CALLER_STACK = [
	{"filename": "frappe/app.py", "lineno": 120, "function": "application"},
	{"filename": "frappe/model/document.py", "lineno": 500, "function": "save"},
	{"filename": "frappe/cache_manager.py", "lineno": 30, "function": "get_doctype_map"},
]


def _sidecar_entry(fn_name, raw, safe, caller_stack=None):
	"""Build a sidecar entry. ``caller_stack`` defaults to the user-
	code stack so existing tests don't have to specify it."""
	return {
		"fn_name": fn_name,
		"identifier_raw": raw,
		"identifier_safe": safe,
		"caller_stack": caller_stack if caller_stack is not None else _USER_CALLER_STACK,
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
	# technical_detail carries both forms AND the callsite (v0.5.2)
	td = json.loads(rc[0]["technical_detail_json"])
	assert "identifier_raw" in td
	assert "identifier_safe" in td
	assert "callsite" in td
	assert td["callsite"]["filename"] == "apps/myapp/controllers/bulk.py"
	assert td["callsite"]["lineno"] == 42
	# Description surfaces the callsite so users can navigate
	assert "apps/myapp/controllers/bulk.py:42" in rc[0]["customer_description"]


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


# ---------------------------------------------------------------------------
# v0.5.2: callsite-based filtering
# ---------------------------------------------------------------------------


def test_framework_callsite_filters_finding():
	"""Loop is inside frappe/model/document.py (framework) → user
	can't act on it → finding must be suppressed. A real production
	report had 'Redundant cache lookup: 93bf3d83c65a (174 times)'
	firing because Frappe itself loops over cached role lookups.
	Users can't fix that."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry(
				"cache_get",
				"role_permissions:Administrator",
				"hash-fw",
				caller_stack=_FRAMEWORK_CALLER_STACK,
			)
			for _ in range(20)  # above cache threshold (10)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)

	# No finding — the callsite was framework-only.
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc == [], (
		f"Framework-only callsite must not produce a Redundant Call "
		f"finding (user can't act on it). Got: "
		f"{[f['title'] for f in rc]}"
	)
	# And the suppression is surfaced as a warning so the user
	# understands WHY they see no Redundant Call entries despite
	# having hot cache loops.
	assert any(
		"Frappe framework code" in w and "Suppressed" in w
		for w in ctx.warnings
	), f"Expected framework-filter warning; got: {ctx.warnings}"


def test_user_callsite_finding_is_kept():
	"""Negative case: a genuine user-code loop (e.g. apps/myapp/…
	iterating get_doc in bulk processing) must still produce a
	finding, with the callsite visible in the detail."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			_sidecar_entry(
				"get_doc",
				("Customer", f"C-{i}"),
				("Customer", "userloop_hash"),
			)
			for i in range(10)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	td = json.loads(rc[0]["technical_detail_json"])
	assert td["callsite"]["filename"] == "apps/myapp/controllers/bulk.py"


def test_missing_caller_stack_is_dropped_with_warning():
	"""v0.5.2 requires caller_stack. Sidecar entries without it
	(recorded pre-v0.5.2 or capture-time error) are dropped rather
	than emitted as findings with no navigable callsite."""
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [
			# NOTE: explicitly no caller_stack (use dict literal, not
			# the _sidecar_entry helper that defaults it).
			{
				"fn_name": "get_doc",
				"identifier_raw": ("Item", "X"),
				"identifier_safe": ("Item", "hash"),
			}
			for _ in range(10)
		],
		"pyi_session": None,
	}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = redundant_calls.analyze([recording], ctx)
	rc = [f for f in result.findings if f["finding_type"] == "Redundant Call"]
	assert rc == []
	# Warning explains why the candidate was dropped so the user
	# knows to re-run the session on the upgraded profiler.
	assert any("no captured caller stack" in w for w in ctx.warnings), (
		f"Expected no-caller-stack warning; got: {ctx.warnings}"
	)
