# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""CRITICAL acceptance test: no plaintext PII in safe-mode finding output.

This test is a load-bearing safety gate. If it fails, do NOT merge —
the safe report is leaking user-identifying data, which breaks the
core product promise.
"""

import json
import os

from frappe_profiler.analyzers import redundant_calls
from frappe_profiler.analyzers.base import AnalyzeContext

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

PII_SUBSTRINGS = [
	"alice@example.com",
	"bob@example.com",
	"@example.com",
]


def test_redundant_call_finding_title_contains_no_plaintext_pii():
	with open(os.path.join(FIXTURES, "sidecar_with_pii.json")) as f:
		sidecar = json.load(f)

	recording = {"uuid": "r1", "calls": [], "sidecar": sidecar, "pyi_session": None}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.actions = [{"action_label": "test", "duration_ms": 100}]
	result = redundant_calls.analyze([recording], ctx)

	assert len(result.findings) >= 2  # one for get_doc, one for cache_get

	for f in result.findings:
		title = f["title"]
		for pii in PII_SUBSTRINGS:
			assert pii not in title, (
				f"PII LEAK: {pii!r} found in safe finding title: {title!r}"
			)


def test_technical_detail_safe_form_contains_no_plaintext_pii():
	"""technical_detail_json carries BOTH safe and raw — but the safe key
	must contain only the hashed form."""
	with open(os.path.join(FIXTURES, "sidecar_with_pii.json")) as f:
		sidecar = json.load(f)

	recording = {"uuid": "r1", "calls": [], "sidecar": sidecar, "pyi_session": None}
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.actions = [{"action_label": "test", "duration_ms": 100}]
	result = redundant_calls.analyze([recording], ctx)

	for f in result.findings:
		td = json.loads(f["technical_detail_json"])
		safe = td.get("identifier_safe")
		safe_str = json.dumps(safe)
		for pii in PII_SUBSTRINGS:
			assert pii not in safe_str, (
				f"PII LEAK: {pii!r} found in identifier_safe: {safe_str!r}"
			)
		# But identifier_raw should contain plaintext (raw is internal-only)
		raw = td.get("identifier_raw")
		raw_str = json.dumps(raw)
		# At least one PII substring must appear in raw — proves the test
		# is wired correctly (otherwise it would trivially pass).
		assert any(pii in raw_str for pii in PII_SUBSTRINGS), (
			"identifier_raw should contain plaintext (it's internal-only)"
		)
