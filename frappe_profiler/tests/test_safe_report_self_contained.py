# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""CRITICAL acceptance test: the safe report HTML is fully self-contained.

The whole point of v0.4.0's "make it usable" theme is that the customer
can download the report as a file and hand it to any software company.
That only works if the file opens correctly with no internet — no CDN
fonts, no remote images, no remote scripts, no @import of external CSS.

This test renders a fixture session and asserts no http:// or https://
substrings appear in the output. If this test ever fires, we've
regressed the load-bearing self-containment guarantee.
"""

import json
from types import SimpleNamespace

from frappe_profiler import renderer


def _fake_doc():
	return SimpleNamespace(
		name="PS-test",
		session_uuid="test-uuid",
		title="self-contained audit",
		user="tester@example.com",
		status="Ready",
		started_at="2026-04-14T00:00:00",
		stopped_at="2026-04-14T00:00:05",
		total_duration_ms=5000,
		total_requests=1,
		total_queries=100,
		total_query_time_ms=200,
		total_python_ms=800,
		total_sql_ms=200,
		analyze_duration_ms=100,
		top_severity="None",
		summary_html="<p>summary</p>",
		top_queries_json="[]",
		table_breakdown_json="[]",
		hot_frames_json=json.dumps([
			{"function": "erpnext.x", "total_ms": 100, "occurrences": 1, "distinct_actions": 1, "action_refs": [0]},
		]),
		session_time_breakdown_json=json.dumps({
			"sql_ms": 200, "python_ms": 800, "by_app": {"erpnext": 600},
		}),
		analyzer_warnings=None,
		actions=[],
		findings=[],
		compared_to_session=None,
	)


def test_safe_report_contains_no_external_urls():
	"""Match actual fetch points only — script src, link href, img src,
	@import url(), CSS url(). XML namespaces (xmlns="http://www.w3.org/...")
	are NOT fetches and are excluded."""
	doc = _fake_doc()
	html = renderer.render_safe(doc, recordings=[])
	import re

	# Collect ALL http/https occurrences and filter out namespace declarations
	all_urls = re.findall(r'https?://[^\s"\'<>)]+', html)
	fetch_urls = [
		u for u in all_urls
		if not u.startswith(("http://www.w3.org/", "https://www.w3.org/"))
	]
	assert not fetch_urls, (
		f"Safe report contains external URL references (would fail offline): {fetch_urls}"
	)
	assert "@import url(" not in html, "Safe report uses @import for external CSS"


def test_safe_report_contains_no_external_script_tags():
	doc = _fake_doc()
	html = renderer.render_safe(doc, recordings=[])
	import re
	external_scripts = re.findall(r'<script[^>]*src=["\']http', html)
	assert not external_scripts, f"Found external script src: {external_scripts}"


def test_safe_report_contains_no_external_link_stylesheets():
	doc = _fake_doc()
	html = renderer.render_safe(doc, recordings=[])
	import re
	external_css = re.findall(r'<link[^>]*rel=["\']stylesheet["\'][^>]*href=["\']http', html)
	assert not external_css, f"Found external stylesheet: {external_css}"
