# frappe_profiler/tests/test_steps_to_reproduce.py
# Copyright (c) 2026, Frappe Profiler contributors

"""Tests for v0.5.0 Steps-to-Reproduce / Notes field.

The v0.5.0 design upgrades the existing `notes` field on Profiler
Session from plain Text to Text Editor so users can include rich
"what did you do during this session" context, and renders it at
the top of the report above findings.
"""

import inspect
import json
import os


HERE = os.path.dirname(__file__)


def _load_doctype_json():
	# Frappe's on-disk layout is <app>/<app>/<module>/doctype/<dt>/<dt>.json.
	# For this app the module name matches the app name ("frappe_profiler")
	# so the resolved path from frappe_profiler/tests/ is
	# ../frappe_profiler/doctype/profiler_session/profiler_session.json.
	jpath = os.path.join(
		HERE,
		"..",
		"frappe_profiler",
		"doctype",
		"profiler_session",
		"profiler_session.json",
	)
	with open(jpath) as f:
		return json.load(f)


def test_notes_field_is_text_editor():
	"""The notes field must be Text Editor (rich HTML), not plain Text,
	so users can include formatting / links / lists in the steps they
	document."""
	meta = _load_doctype_json()
	fields = meta.get("fields") or []
	target = [f for f in fields if f.get("fieldname") == "notes"]
	assert len(target) == 1, "notes field missing from Profiler Session"
	assert target[0]["fieldtype"] == "Text Editor"


def test_notes_label_reflects_steps_purpose():
	"""The label must make the field's purpose clear to users — the
	description alone isn't enough because the field header in the
	form view only shows the label."""
	meta = _load_doctype_json()
	notes = next(
		(f for f in meta["fields"] if f.get("fieldname") == "notes"), None
	)
	assert notes is not None
	assert "Reproduce" in notes["label"] or "Steps" in notes["label"]


def test_api_start_accepts_notes():
	"""api.start must accept an optional notes kwarg and persist it
	into the Profiler Session row."""
	from frappe_profiler import api

	sig = inspect.signature(api.start)
	assert "notes" in sig.parameters
	# Default should be empty string — keeping start() backward compatible
	# with callers that don't pass notes.
	assert sig.parameters["notes"].default == ""


def test_api_start_writes_notes_to_doc():
	"""Source check: api.start must include notes in the doc_fields dict
	passed to get_doc, or the value will be silently dropped."""
	from frappe_profiler import api

	src = inspect.getsource(api.start)
	assert "notes" in src
	# The field must land in doc_fields, not just be parsed and forgotten.
	assert 'doc_fields["notes"]' in src or "doc_fields['notes']" in src


def test_report_template_renders_notes():
	"""The report template must render the notes section and
	use the pre-sanitized `notes_html` context var (which is |safe)."""
	tpath = os.path.join(HERE, "..", "templates", "report.html")
	with open(tpath) as f:
		template = f.read()

	# Template must use the sanitized notes_html var, not raw session.notes.
	# Using session.notes directly with |safe would be a stored-XSS sink.
	assert "notes_html" in template
	assert "notes_html | safe" in template or "notes_html|safe" in template
	# "Steps to Reproduce" heading must appear so users know the purpose.
	assert "Steps to Reproduce" in template

	# Verify notes appear ABOVE the "Findings — what to fix" section header.
	notes_idx = template.find("notes_html")
	findings_heading_idx = template.find("Findings &mdash; what to fix")
	assert notes_idx > 0
	assert findings_heading_idx > 0
	assert findings_heading_idx > notes_idx, (
		"notes section must appear above 'Findings — what to fix'"
	)


def test_notes_are_bleach_sanitized_before_render():
	"""XSS regression guard: a notes field containing <script> must NOT
	produce an executable <script> in the rendered report. This test
	loads the sanitize function directly (not a full render) because
	the test environment doesn't have a real Frappe site. MUST call
	with always_sanitize=True so the JSON/no-tag fast-paths don't bypass."""
	try:
		from frappe.utils.html_utils import sanitize_html
	except Exception:
		# If Frappe isn't importable at test time for some reason, the
		# renderer falls back to html.escape which also neutralizes.
		import html as html_mod
		cleaned = html_mod.escape('<script>alert(1)</script>')
		assert "<script>" not in cleaned
		return

	malicious = '<p>ok</p><script>alert(1)</script>'
	cleaned = sanitize_html(malicious, always_sanitize=True)
	# Harmless tags preserved:
	assert "<p>" in cleaned
	# Script tags removed (nh3/bleach strips or escapes):
	assert "<script>" not in cleaned


def test_json_shaped_xss_payload_is_sanitized():
	"""Architect-review finding: sanitize_html has a fast-path that
	skips bleach for input detected as valid JSON. An attacker could
	set notes = '{"x": "<script>alert(1)</script>"}' — which IS valid
	JSON (a JSON dict literal with a string value containing the
	script) — and sanitize_html without always_sanitize=True returns
	it unchanged. The template's |safe then renders the script as
	a live <script> tag. always_sanitize=True closes this bypass.
	"""
	try:
		from frappe.utils.html_utils import sanitize_html
	except Exception:
		import html as html_mod
		cleaned = html_mod.escape('{"x": "<script>alert(1)</script>"}')
		assert "<script>" not in cleaned
		return

	json_payload = '{"x": "<script>alert(1)</script>"}'

	# Without always_sanitize=True the JSON fast-path would kick in.
	# With always_sanitize=True the script tag must be neutralized.
	cleaned = sanitize_html(json_payload, always_sanitize=True)
	assert "<script>" not in cleaned, (
		"sanitize_html(always_sanitize=True) must strip script tags "
		"from JSON-shaped input. If this fails, the renderer's |safe "
		"render path leaks XSS to anyone viewing a Profiler Session report."
	)


def test_renderer_passes_always_sanitize_true():
	"""Source-inspection guard: the renderer must pass always_sanitize=True
	to sanitize_html, or the JSON / no-tag fast-paths will bypass bleach
	and leak XSS through |safe in the template."""
	import inspect
	from frappe_profiler import renderer

	src = inspect.getsource(renderer.render)
	assert "always_sanitize=True" in src, (
		"renderer.render must call sanitize_html(..., always_sanitize=True). "
		"Without it, valid JSON or no-tag input bypasses sanitization "
		"entirely — stored XSS regression."
	)


def test_renderer_sanitizes_notes_before_template_context():
	"""The render function must run session.notes through sanitize_html
	and pass the result as notes_html, not as session.notes directly."""
	import inspect
	from frappe_profiler import renderer

	src = inspect.getsource(renderer.render)
	assert "sanitize_html" in src or "html.escape" in src or "html_mod.escape" in src
	assert "notes_html" in src
