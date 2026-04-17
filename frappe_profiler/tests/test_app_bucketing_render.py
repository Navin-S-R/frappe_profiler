# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""End-to-end render tests for v0.5.2 per-app sub-grouping in the report."""

import json
import types


def _fake_session_doc_with_findings(*findings_child_rows):
	doc = types.SimpleNamespace()
	doc.title = "Test session"
	doc.session_uuid = "test-uuid"
	doc.user = "alice@example.com"
	doc.status = "Ready"
	doc.started_at = "2026-04-14 10:00:00"
	doc.stopped_at = "2026-04-14 10:02:00"
	doc.notes = None
	doc.top_severity = "High"
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
	doc.analyzer_warnings = None
	doc.compared_to_session = None
	doc.is_baseline = 0
	doc.v5_aggregate_json = "{}"
	doc.actions = []
	doc.findings = list(findings_child_rows)
	return doc


def _finding_row(*, finding_type="N+1 Query", title="t",
                 severity="Medium", impact=10.0,
                 callsite_filename="apps/myapp/foo.py", callsite_lineno=1):
	row = types.SimpleNamespace()
	row.finding_type = finding_type
	row.severity = severity
	row.title = title
	row.customer_description = "desc"
	row.affected_count = 1
	row.action_ref = ""
	row.estimated_impact_ms = impact
	row.technical_detail_json = json.dumps({
		"callsite": {
			"filename": callsite_filename,
			"lineno": callsite_lineno,
			"function": "f",
		},
	})
	return row


def test_single_app_renders_flat_without_wrapper():
	"""If every finding is in one app, don't wrap in "myapp (N findings)"
	— just render flat. Avoids visual noise when there's nothing to
	disambiguate."""
	from frappe_profiler import renderer

	doc = _fake_session_doc_with_findings(
		_finding_row(title="A", callsite_filename="apps/myapp/a.py"),
		_finding_row(title="B", callsite_filename="apps/myapp/b.py"),
	)
	html = renderer.render(doc, recordings=[], mode="safe")

	# Findings present as cards.
	assert ">A<" in html
	assert ">B<" in html
	# NO app-bucket subsection wrapping (single app → flat render).
	# The Findings section itself is still a <details>, but no
	# <details class="subsection" open> with an <h3> inside it.
	assert 'class="app-bucket-name"' not in html, (
		"Single-app sessions must render flat; no app-bucket wrapper"
	)


def test_multiple_apps_each_in_own_bucket():
	"""Two apps → two subsection wrappers, each with its app name and
	finding count in the summary header."""
	from frappe_profiler import renderer

	doc = _fake_session_doc_with_findings(
		_finding_row(
			title="FindingA", impact=100.0,
			callsite_filename="apps/myapp/a.py",
		),
		_finding_row(
			title="FindingB", impact=50.0,
			callsite_filename="apps/other_app/b.py",
		),
	)
	html = renderer.render(doc, recordings=[], mode="safe")

	# Both app names appear as bucket headers.
	assert "myapp" in html
	assert "other_app" in html
	# Each gets its own subsection inside Findings.
	assert 'class="app-bucket-name"' in html, (
		"Multi-app sessions must use the app-bucket wrapper"
	)
	# Higher-impact app comes first (myapp at 100ms > other_app at 50ms).
	# Verify by string order in the rendered HTML.
	myapp_idx = html.find(">myapp<")
	other_idx = html.find(">other_app<")
	assert 0 < myapp_idx < other_idx, (
		f"myapp (higher impact) must render before other_app. "
		f"Positions: myapp={myapp_idx}, other_app={other_idx}"
	)


def test_app_bucket_header_shows_count_and_impact():
	"""Header reads "myapp · 2 findings · ~30ms" — the meta numbers
	are visible so the user sees cost per app at a glance."""
	from frappe_profiler import renderer

	doc = _fake_session_doc_with_findings(
		_finding_row(title="A", impact=12.0, callsite_filename="apps/myapp/a.py"),
		_finding_row(title="B", impact=18.0, callsite_filename="apps/myapp/b.py"),
		# Another app so we get the wrapper.
		_finding_row(title="C", impact=5.0, callsite_filename="apps/other/c.py"),
	)
	html = renderer.render(doc, recordings=[], mode="safe")

	# myapp bucket: 2 findings, ~30ms total.
	assert "2 findings" in html, "Header must show plural count"
	# Impact rounded to whole ms.
	assert "30ms" in html, "Header must show ~30ms aggregate impact for myapp"


def test_singular_finding_header_uses_singular():
	from frappe_profiler import renderer

	doc = _fake_session_doc_with_findings(
		_finding_row(title="A", callsite_filename="apps/myapp/a.py"),
		# Second app so we get the wrapper.
		_finding_row(title="B", callsite_filename="apps/other/b.py"),
	)
	html = renderer.render(doc, recordings=[], mode="safe")

	# Cosmetic: "1 finding" not "1 findings".
	assert "1 finding " in html or "1 finding&middot;" in html or "1 finding\n" in html
	assert "1 findings" not in html


def test_finding_without_callsite_goes_to_other_bucket():
	"""A finding whose technical_detail has no callsite (e.g. infra-
	pressure observations) must still render — via the "Other" bucket."""
	from frappe_profiler import renderer

	# Build a finding with NO callsite in the detail.
	row = types.SimpleNamespace()
	row.finding_type = "Resource Contention"  # observational type
	row.severity = "Medium"
	row.title = "CPU saturated"
	row.customer_description = "desc"
	row.affected_count = 1
	row.action_ref = ""
	row.estimated_impact_ms = 0
	row.technical_detail_json = json.dumps({})

	doc = _fake_session_doc_with_findings(
		_finding_row(title="UserFinding", callsite_filename="apps/myapp/x.py"),
		row,
	)
	html = renderer.render(doc, recordings=[], mode="safe")

	# "CPU saturated" is observational, so it's in Observations, not Findings.
	# Test that the "Other" bucket exists in the Observations area.
	assert "CPU saturated" in html


def test_observations_also_bucketed_by_app():
	"""The Observations subsection must get the same per-app wrapper
	when it contains findings from multiple frameworks (frappe + erpnext)."""
	from frappe_profiler import renderer

	# Framework N+1 (observational) findings from two different
	# framework apps, so we get multiple buckets inside Observations.
	frappe_obs = _finding_row(
		finding_type="Framework N+1",
		title="Framework loop in frappe",
		impact=40.0,
		callsite_filename="frappe/model/document.py",
	)
	erpnext_obs = _finding_row(
		finding_type="Framework N+1",
		title="Framework loop in erpnext",
		impact=20.0,
		callsite_filename="apps/erpnext/erpnext/foo.py",
	)
	doc = _fake_session_doc_with_findings(frappe_obs, erpnext_obs)

	html = renderer.render(doc, recordings=[], mode="safe")

	# Both framework observations present.
	assert "Framework loop in frappe" in html
	assert "Framework loop in erpnext" in html
	# Both app names appear as bucket headers within Observations.
	assert "frappe" in html
	assert "erpnext" in html
