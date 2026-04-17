# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for the v0.5.2 collapsible 'Analyzer notes' section.

Pre-v0.5.2 analyzer suppression warnings rendered as a single
concatenated <p> in the header — a wall of text with "Suppressed N
... Skipped M ... Suppressed K ..." all running together. v0.5.2
splits the stored newline-joined string into bullets and renders
them in a collapsed-by-default section at the bottom of the report.

These tests pin the template wiring so a future edit can't
accidentally revert to the header paragraph.
"""

import json
import types


def _fake_session_doc(warnings_str=None):
	doc = types.SimpleNamespace()
	doc.title = "Test session"
	doc.session_uuid = "test-uuid"
	doc.user = "alice@example.com"
	doc.status = "Ready"
	doc.started_at = "2026-04-14 10:00:00"
	doc.stopped_at = "2026-04-14 10:02:00"
	doc.notes = None
	doc.top_severity = "Medium"
	doc.total_duration_ms = 2000
	doc.total_query_time_ms = 500
	doc.total_queries = 20
	doc.total_requests = 2
	doc.summary_html = None
	doc.top_queries_json = "[]"
	doc.table_breakdown_json = "[]"
	doc.hot_frames_json = "[]"
	doc.session_time_breakdown_json = "{}"
	doc.total_python_ms = 100
	doc.total_sql_ms = 500
	doc.analyzer_warnings = warnings_str
	doc.compared_to_session = None
	doc.is_baseline = 0
	doc.v5_aggregate_json = "{}"
	doc.actions = []
	doc.findings = []
	return doc


def test_warnings_render_as_bulleted_list_in_collapsed_section():
	"""Three warnings in the stored string must render as three <li>
	elements inside a <details class='section' id='analyzer-notes'>
	block — not as a single <p> like the pre-v0.5.2 layout."""
	from frappe_profiler import renderer

	warnings_str = "\n".join([
		"Suppressed 52 SQL findings from framework code.",
		"Suppressed 22 EXPLAIN rows whose table was a SQL alias.",
		"Skipped 127 non-SELECT statements for index suggestions.",
	])
	doc = _fake_session_doc(warnings_str)
	html = renderer.render(doc, recordings=[], mode="safe")

	# Collapsible section with id="analyzer-notes" exists.
	assert 'id="analyzer-notes"' in html, (
		"Analyzer notes section must be present as a collapsible block"
	)
	# No `open` attribute on the Analyzer notes section — collapsed
	# by default so it doesn't add visual weight to the report.
	analyzer_section_idx = html.find('id="analyzer-notes"')
	section_tag_start = html.rfind("<details", 0, analyzer_section_idx)
	section_tag = html[section_tag_start:analyzer_section_idx + 30]
	assert " open>" not in section_tag, (
		"Analyzer notes must be collapsed by default — found `open` attr"
	)
	# All three warnings present as <li> items.
	for msg in (
		"Suppressed 52 SQL findings from framework code.",
		"Suppressed 22 EXPLAIN rows whose table was a SQL alias.",
		"Skipped 127 non-SELECT statements for index suggestions.",
	):
		# The <li>...</li> form confirms each warning is its own bullet.
		assert f"<li>{msg}</li>" in html, f"Warning missing as <li>: {msg!r}"


def test_header_shows_pointer_not_wall_of_text():
	"""Pre-v0.5.2 the header emitted a <p class='small muted'><strong>
	Warnings:</strong> ...long string...</p>. v0.5.2 replaces that
	with a short pointer that links to the collapsible section."""
	from frappe_profiler import renderer

	warnings_str = "A.\nB.\nC."
	doc = _fake_session_doc(warnings_str)
	html = renderer.render(doc, recordings=[], mode="safe")

	# The header must NOT dump the full warnings string inline.
	# Check for the pre-v0.5.2 shape specifically — a <p> tag in the
	# header area containing a concatenated warning fragment.
	assert "<strong>Warnings:</strong> A." not in html, (
		"Header still emits pre-v0.5.2 Warnings: <full string> paragraph"
	)
	# New shape: a pointer with count + anchor link.
	assert 'href="#analyzer-notes"' in html, (
		"Header pointer must link to the Analyzer notes anchor"
	)
	assert "3 suppressions" in html, (
		"Header pointer must summarize the count (e.g. '3 suppressions')"
	)


def test_no_section_when_no_warnings():
	"""Clean session (no warnings) → no section rendered, no stray
	empty header pointer."""
	from frappe_profiler import renderer

	doc = _fake_session_doc(None)
	html = renderer.render(doc, recordings=[], mode="safe")

	assert 'id="analyzer-notes"' not in html, (
		"Empty analyzer_warnings must not render the notes section"
	)
	assert 'href="#analyzer-notes"' not in html, (
		"Empty analyzer_warnings must not render the header pointer"
	)


def test_single_warning_uses_singular_word():
	"""Cosmetic: 1 warning → 'suppression' (singular), not 'suppressions'.
	Saves the report from saying '1 suppressions' which reads wrong."""
	from frappe_profiler import renderer

	doc = _fake_session_doc("Only one warning fired.")
	html = renderer.render(doc, recordings=[], mode="safe")

	# Header pointer exists (count + singular noun). Whitespace around
	# the Jinja `{{ }}` output collapses differently across Jinja
	# versions so check for "1 suppression" (any trailing whitespace)
	# and that "1 suppressions" doesn't appear.
	import re
	assert re.search(r"\b1 suppression\b", html), (
		f"Single warning must use singular wording — header pointer "
		f"not found. Searched for '1 suppression' as a word."
	)
	assert "1 suppressions" not in html, (
		"Single warning must NOT emit plural 'suppressions'"
	)


def test_blank_lines_in_warning_string_skipped():
	"""analyzer_warnings stored as a newline-joined string may end up
	with empty lines (e.g. if a warning contained '\\n\\n'). The
	renderer's split must filter blanks so the <ul> doesn't show
	empty bullets."""
	from frappe_profiler import renderer

	doc = _fake_session_doc("first\n\n\nsecond\n")
	html = renderer.render(doc, recordings=[], mode="safe")

	assert "<li>first</li>" in html
	assert "<li>second</li>" in html
	assert "<li></li>" not in html, (
		"Empty <li> must not appear — blank lines in the stored string "
		"must be stripped before rendering"
	)
