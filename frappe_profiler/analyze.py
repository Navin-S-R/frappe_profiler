# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Background-job entry point: analyze a finished session.

Triggered by `api.stop()` via `frappe.enqueue("frappe_profiler.analyze.run", ...)`.
Reads all recordings for the session from Redis, runs the six analyzers,
persists the results into the Profiler Session DocType, and publishes a
realtime notification so the UI can navigate to the report.

State transitions on the Profiler Session row:
    Stopping  →  Analyzing  →  Ready    (happy path)
    Stopping  →  Analyzing  →  Failed   (uncaught exception)

The Redis state for the session is cleaned up at the end of a successful
run — the source recordings are deleted from RECORDER_REQUEST_HASH and the
profiler:session:* keys are removed. Failed runs do NOT clean up, so a
developer can manually retry the analyze.
"""

import html
import json
import time

import sqlparse

import frappe
from frappe.database.utils import is_query_type
from frappe.recorder import (
	RECORDER_REQUEST_HASH,
	RECORDER_REQUEST_SPARSE_HASH,
	mark_duplicates,
)

from frappe_profiler import renderer, session
from frappe_profiler.analyzers import (
	explain_flags,
	index_suggestions,
	n_plus_one,
	per_action,
	table_breakdown,
	top_queries,
)
from frappe_profiler.analyzers.base import AnalyzeContext

# per_action is first because it builds the Profiler Action rows that the
# rest of the analyzers reference via action_ref. The remainder are
# independent and could in principle be parallelized.
_BUILTIN_ANALYZERS = [
	per_action.analyze,
	top_queries.analyze,
	n_plus_one.analyze,
	explain_flags.analyze,
	index_suggestions.analyze,
	table_breakdown.analyze,
]

# Backward-compat alias: the old name is still the public-facing list
# for code that references `analyze.ANALYZERS` directly.
ANALYZERS = _BUILTIN_ANALYZERS


def _get_analyzers() -> list:
	"""Return the analyzer pipeline: builtins + custom hooks.

	Round 2 fix #13. Third-party Frappe apps can add analyzers via:

	    # hooks.py
	    profiler_analyzers = [
	        "my_app.analyzers.custom.analyze",
	    ]

	Custom analyzers run AFTER the builtins so they can read
	context.actions / context.findings built by earlier analyzers.
	A failing custom analyzer logs via the normal error path but
	doesn't abort the pipeline (same as builtins).
	"""
	analyzers = list(_BUILTIN_ANALYZERS)
	try:
		hook_paths = frappe.get_hooks("profiler_analyzers") or []
	except Exception:
		hook_paths = []

	for dotted in hook_paths:
		if not dotted:
			continue
		try:
			fn = frappe.get_attr(dotted)
			if callable(fn):
				analyzers.append(fn)
			else:
				frappe.log_error(
					title="frappe_profiler analyzer hook",
					message=f"Custom analyzer {dotted} is not callable",
				)
		except Exception:
			frappe.log_error(
				title="frappe_profiler analyzer hook",
				message=f"Failed to load custom analyzer {dotted}",
			)

	return analyzers


def _publish_progress(percent: float, description: str, session_uuid: str):
	"""Emit a progress update for the floating widget and form UI.

	Best-effort — never raises. Subscribed to in the floating widget JS
	via frappe.realtime.on("profiler_progress"). Round 2 fix #17.
	"""
	try:
		frappe.publish_realtime(
			"profiler_progress",
			{
				"session_uuid": session_uuid,
				"percent": round(percent, 1),
				"description": description,
			},
		)
	except Exception:
		pass


def run(session_uuid: str):
	"""Background-job entry point. Called from api.stop() via frappe.enqueue."""
	# Round 2 fix #6: mark this request-context as "analyzing" so our
	# before_request / before_job hooks don't recursively activate the
	# recorder on the DocType writes we're about to do. Without this,
	# if the recording user also has an active profiler session (e.g.
	# multiple sessions started in sequence) we could recurse.
	frappe.local.profiler_analyzing = True

	docname = frappe.db.get_value("Profiler Session", {"session_uuid": session_uuid}, "name")
	if not docname:
		frappe.log_error(
			title="frappe_profiler analyze",
			message=f"No Profiler Session found for uuid {session_uuid}",
		)
		return

	analyze_start = time.monotonic()

	try:
		# Phase: Analyzing
		frappe.db.set_value("Profiler Session", docname, "status", "Analyzing")
		frappe.db.commit()
		_publish_progress(5, "Fetching recordings", session_uuid)

		recording_uuids = session.get_recordings(session_uuid)
		# v0.3.0: _fetch_recordings is now a generator. Materialize here
		# for the analyzer pipeline (which makes multiple passes), but
		# each recording's pyi_session is dropped after call_tree finishes
		# with it (handled in the analyzer itself).
		recordings = list(_fetch_recordings(recording_uuids))

		if not recordings:
			_finalize_with_empty_session(docname)
			session.delete_session_state(session_uuid)
			_publish_progress(100, "Complete (no data)", session_uuid)
			return

		_publish_progress(20, "Running EXPLAIN on queries", session_uuid)
		enrichment_warnings = _enrich_recordings(recordings)

		context = AnalyzeContext(session_uuid=session_uuid, docname=docname)
		context.warnings.extend(enrichment_warnings)

		# Surface any caps the session hit during recording
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("cap_warning"):
			context.warnings.append(meta["cap_warning"])

		_publish_progress(50, "Running analyzers", session_uuid)
		analyzers = _get_analyzers()
		for i, analyzer in enumerate(analyzers):
			analyzer_name = getattr(analyzer, "__module__", "<unknown>")
			try:
				result = analyzer(recordings, context)
				context.merge(result)
			except Exception:
				context.warnings.append(f"Analyzer {analyzer_name} failed (see error log)")
				frappe.log_error(title=f"frappe_profiler analyzer {analyzer_name}")
			# Progress between 50-75% spread across analyzers
			pct = 50 + (25 * (i + 1) / len(analyzers))
			_publish_progress(pct, f"Ran {analyzer_name.split('.')[-1]}", session_uuid)

		# How long did analyze take so far (before report rendering)?
		analyze_elapsed_ms = (time.monotonic() - analyze_start) * 1000

		_publish_progress(80, "Writing session data", session_uuid)
		_persist(docname, context, recordings, analyze_elapsed_ms)

		_publish_progress(90, "Rendering reports", session_uuid)
		# Render and attach the safe + raw HTML reports to the DocType.
		# IMPORTANT: this must run BEFORE _cleanup_redis, because raw mode
		# reads raw SQL, headers, form_dict, and full stack traces from the
		# in-memory recordings list (not from the DocType, which only has
		# normalized data).
		_render_and_attach_reports(docname, recordings)

		_cleanup_redis(session_uuid, recording_uuids)

		# Phase: Ready
		frappe.db.set_value("Profiler Session", docname, "status", "Ready")
		frappe.db.commit()
		_publish_progress(100, "Report ready", session_uuid)

		# Notify the UI so the floating widget can navigate the user to the report.
		try:
			user = frappe.db.get_value("Profiler Session", docname, "user")
			frappe.publish_realtime(
				"profiler_session_ready",
				{"session_uuid": session_uuid, "docname": docname},
				user=user,
			)
		except Exception:
			pass  # realtime is best-effort, never block the analyze

	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"frappe_profiler analyze {session_uuid}")
		try:
			frappe.db.set_value("Profiler Session", docname, "status", "Failed")
			frappe.db.commit()
		except Exception:
			pass
		raise
	finally:
		# Round 2 fix #6: always clear the analyzing flag so subsequent
		# requests in the same worker process can profile normally.
		if hasattr(frappe.local, "profiler_analyzing"):
			del frappe.local.profiler_analyzing


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_recordings(recording_uuids: list[str]):
	"""Stream recording dicts from Redis, one at a time.

	v0.3.0 changes:
	  - This is now a generator (was: list-returning) so the analyze
	    pipeline can drop unpruned pyi_session blobs from memory between
	    recordings rather than holding all 200 in RAM at once.
	  - For each recording, also loads the per-recording pyi tree pickle
	    from `profiler:tree:<uuid>` and the sidecar log from
	    `profiler:sidecar:<uuid>`. Both are best-effort — failures
	    log a warning and yield None for the missing piece.

	Yields recording dicts shaped like:
	    {
	      ...existing recorder fields (uuid, calls, etc.)...
	      "pyi_session": <pyinstrument.session.Session or dict or None>,
	      "sidecar": <list[dict]>,
	    }
	"""
	import pickle

	for uuid in recording_uuids:
		rec = frappe.cache.hget(RECORDER_REQUEST_HASH, uuid)
		if not rec:
			continue

		# Load the pyinstrument tree pickle (best-effort)
		pyi_session = None
		try:
			tree_blob = frappe.cache.get_value(f"profiler:tree:{uuid}")
			if tree_blob:
				pyi_session = pickle.loads(tree_blob)
		except Exception:
			frappe.log_error(
				title="frappe_profiler analyze",
				message=f"Failed to deserialize pyi tree for {uuid}",
			)
			pyi_session = None

		# Load the sidecar argument log (best-effort)
		sidecar = []
		try:
			loaded = frappe.cache.get_value(f"profiler:sidecar:{uuid}")
			if isinstance(loaded, list):
				sidecar = loaded
		except Exception:
			frappe.log_error(
				title="frappe_profiler analyze",
				message=f"Failed to load sidecar for {uuid}",
			)
			sidecar = []

		rec["pyi_session"] = pyi_session
		rec["sidecar"] = sidecar
		yield rec


# Per-recording enrichment caps. On a session that hit the 200-recording
# cap with 5000 queries each, running EXPLAIN on every query would be
# nearly a million DB calls and would exceed the RQ long-queue timeout.
# We cap at a reasonable upper bound and dedupe EXPLAIN by normalized
# query shape so we only EXPLAIN each distinct query once per session.
MAX_QUERIES_ENRICHED_PER_RECORDING = 2000

# Cross-session EXPLAIN cache (Round 2 fix #12). On a stable schema, two
# consecutive analyze runs often see the same query shapes. Caching the
# EXPLAIN result for an hour lets the second run skip the DB roundtrip
# entirely. Override per site via:
#   site_config.json: profiler_explain_cache_ttl_seconds (default 3600)
# Set to 0 to disable the cross-session cache entirely (falls back to
# per-session dedup only).
DEFAULT_EXPLAIN_CACHE_TTL = 3600


def _enrich_recordings(recordings: list[dict]) -> list[str]:
	"""Mirror frappe.recorder.post_process for our recordings only.

	The vanilla post_process operates on every recording in
	RECORDER_REQUEST_HASH globally and starts a read-only DB transaction
	that would block our subsequent DocType writes. This version is
	scoped to our session's recordings and leaves the transaction state
	alone.

	Idempotent: safe to call on already-enriched recordings.

	Returns a list of warning strings that should be surfaced in the
	report — things like "we truncated X queries because the session hit
	the per-recording cap".
	"""
	# Flush any pending transaction state so EXPLAIN sees a consistent
	# snapshot (Round 2 fix #4). The caller commits right before invoking
	# us so this is usually a no-op, but it's a cheap defensive check
	# that protects against future refactors that might leave dirty
	# state in the transaction.
	try:
		frappe.db.commit()
	except Exception:
		pass

	warnings: list[str] = []
	truncated_queries = 0
	# Cache EXPLAIN results by normalized query shape so we don't re-run
	# EXPLAIN on the same query hundreds of times within a single session.
	# This is the in-memory first tier; the second tier is the
	# cross-session frappe.cache lookup below.
	explain_cache: dict[str, list] = {}

	# Cross-session EXPLAIN cache config
	cache_ttl = int(
		frappe.conf.get("profiler_explain_cache_ttl_seconds")
		if frappe.conf.get("profiler_explain_cache_ttl_seconds") is not None
		else DEFAULT_EXPLAIN_CACHE_TTL
	)
	use_shared_cache = cache_ttl > 0

	for recording in recordings:
		calls = recording.get("calls") or []
		if len(calls) > MAX_QUERIES_ENRICHED_PER_RECORDING:
			truncated_queries += len(calls) - MAX_QUERIES_ENRICHED_PER_RECORDING
			calls = calls[:MAX_QUERIES_ENRICHED_PER_RECORDING]
			recording["calls"] = calls

		for call in calls:
			query = call.get("query", "")
			if not query:
				continue

			# sqlparse format (idempotent on already-formatted SQL)
			try:
				call["query"] = sqlparse.format(
					query.strip(),
					keyword_case="upper",
					reindent=True,
					strip_comments=True,
				)
			except Exception:
				pass

			# Skip EXPLAIN if already populated
			if call.get("explain_result"):
				continue

			if not is_query_type(call["query"], ("select", "update", "delete")):
				call["explain_result"] = []
				continue

			# Dedupe EXPLAIN by normalized query shape. The recorder
			# hasn't computed normalized_query yet (mark_duplicates runs
			# below), so compute a temporary shape key here.
			cache_key = _shape_key(call["query"])

			# First tier: in-memory cache for this analyze run
			if cache_key in explain_cache:
				call["explain_result"] = explain_cache[cache_key]
				continue

			# Second tier: cross-session frappe.cache with TTL. Two
			# analyze runs on a stable schema will hit this cache.
			shared_key = f"profiler:explain:{cache_key}"
			if use_shared_cache:
				cached = frappe.cache.get_value(shared_key)
				if cached is not None:
					call["explain_result"] = cached
					explain_cache[cache_key] = cached
					continue

			try:
				result = frappe.db.sql(
					f"EXPLAIN {call['query']}", as_dict=True
				)
				call["explain_result"] = result
				explain_cache[cache_key] = result
				if use_shared_cache:
					try:
						frappe.cache.set_value(
							shared_key, result, expires_in_sec=cache_ttl
						)
					except Exception:
						pass  # shared cache is best-effort
			except Exception:
				call["explain_result"] = []
				explain_cache[cache_key] = []

			if "explain_result" not in call:
				call["explain_result"] = []

		# mark_duplicates adds normalized_query, exact_copies, normalized_copies, index
		try:
			mark_duplicates(recording)
		except Exception:
			pass

	if truncated_queries:
		warnings.append(
			f"Truncated {truncated_queries} queries during enrichment "
			f"(exceeded the {MAX_QUERIES_ENRICHED_PER_RECORDING}-queries-per-"
			"recording cap). The report covers the first part of each "
			"recording. If you need full analysis, re-run the profiler on a "
			"shorter flow."
		)
	return warnings


def _shape_key(query: str) -> str:
	"""Cheap query-shape key for EXPLAIN dedup.

	Lower-cases, collapses whitespace, and truncates. This is NOT the
	proper sqlparse normalization — we just need "are these two queries
	shaped the same" for caching purposes. The REAL normalization happens
	in mark_duplicates afterward.
	"""
	import re

	return re.sub(r"\s+", " ", query.lower().strip())[:500]


def _persist(
	docname: str,
	context: AnalyzeContext,
	recordings: list[dict],
	analyze_elapsed_ms: float = 0,
) -> None:
	"""Write the analyzed data into the Profiler Session DocType row."""
	total_requests = len(recordings)
	total_queries = sum(len(r.get("calls") or []) for r in recordings)
	total_query_time_ms = sum(
		sum(c.get("duration", 0) for c in r.get("calls") or []) for r in recordings
	)
	total_duration_ms = sum(r.get("duration", 0) for r in recordings)

	doc = frappe.get_doc("Profiler Session", docname)
	doc.total_requests = total_requests
	doc.total_queries = total_queries
	doc.total_query_time_ms = round(total_query_time_ms, 2)
	doc.total_duration_ms = round(total_duration_ms, 2)
	doc.analyze_duration_ms = round(analyze_elapsed_ms, 2)
	doc.top_severity = _compute_top_severity(context.findings)
	doc.summary_html = _build_summary_html(context, total_queries)
	doc.top_queries_json = json.dumps(
		context.aggregate.get("top_queries", []), default=str
	)
	doc.table_breakdown_json = json.dumps(
		context.aggregate.get("table_breakdown", []), default=str
	)
	doc.analyzer_warnings = "\n".join(context.warnings) if context.warnings else None

	# Reset and re-populate child tables (in case of re-run)
	doc.set("actions", [])
	for action in context.actions:
		doc.append("actions", action)

	doc.set("findings", [])
	for finding in context.findings:
		doc.append("findings", finding)

	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _compute_top_severity(findings: list[dict]) -> str:
	"""Return the highest severity present in the findings list.

	Populated on each session so the list view can show a color-coded
	"Top Severity" column without loading the child rows.
	"""
	if not findings:
		return "None"
	severities = {f.get("severity") for f in findings}
	for level in ("High", "Medium", "Low"):
		if level in severities:
			return level
	return "None"


def _build_summary_html(context: AnalyzeContext, total_queries: int) -> str:
	"""Plain-language customer summary, generated from the analyzer findings."""
	n_actions = len(context.actions)
	findings = context.findings
	high = sum(1 for f in findings if f.get("severity") == "High")
	medium = sum(1 for f in findings if f.get("severity") == "Medium")
	low = sum(1 for f in findings if f.get("severity") == "Low")

	parts = [
		f"<p>We recorded <strong>{n_actions} actions</strong> with "
		f"<strong>{total_queries} database queries</strong> in this session.</p>"
	]

	if context.actions:
		# Pair the slowest action with its highest-impact finding (if any)
		# so the customer immediately sees "X was slow because of Y".
		slowest_idx = max(
			range(len(context.actions)),
			key=lambda i: context.actions[i].get("duration_ms", 0),
		)
		slowest = context.actions[slowest_idx]
		slowest_label = html.escape(str(slowest.get("action_label") or "?"))
		slowest_ms = slowest.get("duration_ms", 0)

		# Two-step lookup (Round 2 fix #16):
		#   1. Prefer a finding tied to this specific action (via action_ref)
		#   2. If none, fall back to the highest-impact finding overall
		#      with a "affecting multiple actions" caveat. This handles
		#      session-wide findings (Missing Index, etc.) that don't
		#      attribute to a single action.
		tied_finding = None
		for f in findings:
			ref = f.get("action_ref")
			if ref and str(ref) == str(slowest_idx):
				if not tied_finding or (f.get("estimated_impact_ms") or 0) > (
					tied_finding.get("estimated_impact_ms") or 0
				):
					tied_finding = f

		overall_finding = None
		if findings:
			overall_finding = max(
				findings, key=lambda f: f.get("estimated_impact_ms") or 0
			)

		if tied_finding:
			parts.append(
				f"<p>The slowest action was <strong>{slowest_label}</strong> "
				f"at {slowest_ms:.0f}ms. The biggest contributor we found was "
				f"<strong>{html.escape(tied_finding.get('title') or '')}</strong> "
				f"({tied_finding.get('severity', '?')} severity, ~"
				f"{tied_finding.get('estimated_impact_ms', 0):.0f}ms). "
				"See the Findings section below for what to ask your developer to fix.</p>"
			)
		elif overall_finding:
			parts.append(
				f"<p>The slowest action was <strong>{slowest_label}</strong> "
				f"at {slowest_ms:.0f}ms. The highest-impact issue in this session "
				f"(affecting multiple actions) was "
				f"<strong>{html.escape(overall_finding.get('title') or '')}</strong> "
				f"({overall_finding.get('severity', '?')} severity, ~"
				f"{overall_finding.get('estimated_impact_ms', 0):.0f}ms total). "
				"See the Findings section below.</p>"
			)
		else:
			parts.append(
				f"<p>The slowest action was "
				f"<strong>{slowest_label}</strong> at {slowest_ms:.0f}ms.</p>"
			)

	if not findings:
		parts.append(
			"<p>We analyzed your flow for: "
			"<strong>repeated queries (N+1 patterns)</strong>, "
			"<strong>full table scans</strong>, "
			"<strong>filesort operations</strong>, "
			"<strong>temporary table creation</strong>, "
			"<strong>low filter ratios</strong>, "
			"<strong>missing indexes</strong>, and "
			"<strong>individually slow queries</strong> (&gt;200ms). "
			"Nothing significant was found.</p>"
			"<p>Either your flow is already well-optimized, or the data "
			"volume is too small to surface bottlenecks at this scale. "
			"Try running the profiler again with a larger dataset for "
			"more insight.</p>"
		)
	else:
		severity_parts = []
		if high:
			severity_parts.append(f"<strong>{high} high-severity</strong>")
		if medium:
			severity_parts.append(f"{medium} medium-severity")
		if low:
			severity_parts.append(f"{low} low-severity")
		joined = ", ".join(severity_parts) if severity_parts else "no"
		parts.append(
			f"<p>We identified {joined} performance issues — see the Findings "
			"section below for details and what to ask your developer to fix.</p>"
		)

	return "\n".join(parts)


def _finalize_with_empty_session(docname: str) -> None:
	"""Mark a session Ready when it had no recordings."""
	doc = frappe.get_doc("Profiler Session", docname)
	doc.status = "Ready"
	doc.total_requests = 0
	doc.total_queries = 0
	doc.summary_html = (
		"<p>No traffic was recorded during this session. Either no requests "
		"were made, or the session was stopped before any flow was performed.</p>"
	)
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _render_and_attach_reports(docname: str, recordings: list[dict]) -> None:
	"""Render the safe and raw HTML reports and attach both to the DocType.

	Both files are stored as PRIVATE attachments on the Profiler Session.
	Frappe enforces "user must have read permission on attached_to_doctype"
	for private files — combined with the `if_owner=1` permission rule on
	Profiler Session for the Profiler User role, this means non-admin
	users can only download reports for their own sessions.

	The Phase 5 UI will additionally hide the "Download Raw" button from
	users without System Manager role.
	"""
	# Re-fetch the doc so child rows persisted by _persist are visible.
	doc = frappe.get_doc("Profiler Session", docname)

	# Render both modes. Safe doesn't strictly need recordings (only reads
	# from the doc) but we pass them anyway for symmetry.
	try:
		safe_html = renderer.render_safe(doc, recordings)
		safe_url = _save_report_file(
			docname=docname,
			filename=f"profiler_safe_report_{doc.session_uuid}.html",
			attached_to_field="safe_report_file",
			content=safe_html,
		)
		if safe_url:
			frappe.db.set_value("Profiler Session", docname, "safe_report_file", safe_url)
	except Exception:
		frappe.log_error(title="frappe_profiler render safe report")

	try:
		raw_html = renderer.render_raw(doc, recordings)
		raw_url = _save_report_file(
			docname=docname,
			filename=f"profiler_raw_report_{doc.session_uuid}.html",
			attached_to_field="raw_report_file",
			content=raw_html,
		)
		if raw_url:
			frappe.db.set_value("Profiler Session", docname, "raw_report_file", raw_url)
	except Exception:
		frappe.log_error(title="frappe_profiler render raw report")

	frappe.db.commit()


def _save_report_file(*, docname: str, filename: str, attached_to_field: str, content: str) -> str | None:
	"""Insert a private File attached to the Profiler Session.

	Returns the file_url for the new file, or None on failure.
	"""
	try:
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": filename,
				"attached_to_doctype": "Profiler Session",
				"attached_to_name": docname,
				"attached_to_field": attached_to_field,
				"content": content.encode("utf-8"),
				"is_private": 1,
			}
		)
		file_doc.insert(ignore_permissions=True)
		return file_doc.file_url
	except Exception:
		frappe.log_error(title=f"frappe_profiler save_report_file {filename}")
		return None


def _cleanup_redis(session_uuid: str, recording_uuids: list[str]) -> None:
	"""Delete Redis state for this finalized session.

	The Profiler Session DocType row is now the durable record. Redis is
	freed so subsequent sessions can use it. Best-effort: a failure here
	does not abort the analyze.
	"""
	try:
		session.delete_session_state(session_uuid)
	except Exception:
		frappe.log_error(title="frappe_profiler cleanup session_state")

	for uuid in recording_uuids:
		try:
			frappe.cache.hdel(RECORDER_REQUEST_HASH, uuid)
		except Exception:
			pass
		try:
			frappe.cache.hdel(RECORDER_REQUEST_SPARSE_HASH, uuid)
		except Exception:
			pass
