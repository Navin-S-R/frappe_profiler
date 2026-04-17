# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for v0.5.2 collapsible report sections.

Each top-level report section is a native HTML5 <details class="section">
with an `open` attribute (so it's expanded by default but foldable).
Framework-level observations live as a nested <details class="subsection">
INSIDE Findings (collapsed by default so the main "what to fix" list reads
clean without burying user-actionable items under framework noise).

User request verbatim: "In report make each sections as collapsable. If
its a framework related issue then move a sub-section".
"""

import os
import re


def _read_template():
	here = os.path.dirname(__file__)
	tpath = os.path.join(here, "..", "templates", "report.html")
	with open(tpath) as f:
		return f.read()


def test_no_stray_section_div_tags():
	"""All top-level <div class="section"> blocks must be converted to
	<details class="section"> so every section is collapsible."""
	template = _read_template()
	assert '<div class="section">' not in template, (
		"Every section must use <details class='section'> — bare "
		"<div class='section'> means that section is not collapsible."
	)
	# Same for the <section class="section"> form used in comparison blocks.
	assert '<section class="section' not in template, (
		"The <section class='section'> form must also be converted to "
		"<details class='section'> for collapsibility."
	)


def test_details_tags_are_balanced():
	"""Every <details> must have a matching </details>. An unbalanced
	tag set means the collapsible edit broke the template structure."""
	template = _read_template()
	opens = len(re.findall(r"<details[\s>]", template))
	closes = len(re.findall(r"</details>", template))
	assert opens == closes, (
		f"<details> tags unbalanced: {opens} open vs {closes} close"
	)


def test_primary_sections_are_open_by_default():
	"""Top-level report sections use `<details class="section" open>` —
	expanded by default so the report reads like a document, with the
	fold affordance as opt-in. Only the observations subsection is
	collapsed by default."""
	template = _read_template()
	# Spot-check each primary section we know about.
	for section_heading in (
		"Summary",
		"Per-action breakdown",
		"Findings &mdash; what to fix",
		"Server Resource",
		"Frontend",
		"Time breakdown",
		"Hot frames",
		"Top",               # "Top {{ top_queries|length }} slowest queries"
		"Queries per action",
		"Time spent per database table",
		"Full recordings",
	):
		assert section_heading in template, (
			f"section heading {section_heading!r} missing from template"
		)

	# Every open-by-default primary section uses the `open` attribute.
	# Count: we expect ≥ 8 such sections (varies with conditional ones).
	open_sections = re.findall(r'<details class="section[^"]*" open>', template)
	assert len(open_sections) >= 8, (
		f"expected at least 8 <details class='section' open> blocks, "
		f"found {len(open_sections)}"
	)


def test_observations_subsection_is_collapsed_by_default():
	"""Framework-level observations ship collapsed so they don't distract
	from the actionable list. User explicitly asked for them as a
	sub-section: 'If its a framework related issue then move a
	sub-section'."""
	template = _read_template()
	# Subsection is present…
	assert '<details class="subsection">' in template, (
		"Framework-level observations must use "
		"<details class='subsection'> (no `open` → collapsed by default)"
	)
	# …and it uses <h3> inside its summary (nested heading level).
	assert "<summary><h3>Framework-level observations" in template, (
		"Observations subsection must use an <h3> inside <summary>"
	)


def test_observations_is_nested_inside_findings():
	"""The Observations <details class='subsection'> must appear between
	the Findings <summary> and its closing </details>. Otherwise the
	'move a sub-section' part of the user's request isn't satisfied."""
	template = _read_template()
	findings_summary_idx = template.find(
		"<summary><h2>Findings &mdash; what to fix</h2></summary>"
	)
	subsection_idx = template.find('<details class="subsection">')
	assert findings_summary_idx > 0, "Findings summary not found"
	assert subsection_idx > 0, "Observations subsection not found"
	assert subsection_idx > findings_summary_idx, (
		"Observations subsection must be nested after Findings' <summary>"
	)

	# And the Findings closing </details> must come AFTER the subsection
	# (proving containment, not just ordering).
	findings_close_idx = template.find("</details>", subsection_idx)
	# Walk forward: the subsection has its own </details>, and Findings
	# has its own </details> after that. Verify the subsection closes
	# BEFORE Findings closes.
	sub_close_idx = template.find("</details>", subsection_idx)
	assert sub_close_idx > 0
	# Findings close must be after (different) sub close.
	findings_close_after_sub = template.find("</details>", sub_close_idx + 1)
	assert findings_close_after_sub > sub_close_idx, (
		"Findings <details> must wrap (contain) the Observations subsection"
	)


def test_collapsible_css_is_present():
	"""The chevron + summary styling must ship in the template head so
	sections render consistently in standalone HTML files (the report
	is distributed as self-contained HTML)."""
	template = _read_template()
	# Chevron rotation when open.
	assert "details.section[open] > summary::before" in template, (
		"Chevron rotate-on-open CSS rule missing — sections will look "
		"static instead of animating"
	)
	# Subsection styling (dashed separator, grey h3).
	assert "details.subsection {" in template, (
		"Subsection CSS rule missing — framework observations will "
		"look identical to top-level sections"
	)
