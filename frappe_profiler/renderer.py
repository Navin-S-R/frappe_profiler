# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""HTML report renderer for a Profiler Session.

Two modes from a single Jinja template:

    safe — Normalized SQL only (literals replaced with `?`), no headers,
           no form data, no full stack traces. The customer can hand this
           to a third-party dev shop without leaking PII.

    raw  — Full data: raw SQL with literal values, request headers, form
           data, and the complete stack trace for every query. Stays on
           the customer's site only — gated to System Manager + the
           recording user via Frappe's file permission system.

The template is loaded directly from the file system (not via Frappe's
Jinja environment) so the renderer is unit-testable in isolation and
doesn't depend on a running site.

Both modes render from the same template; the `mode` context variable
toggles redaction-sensitive sections.
"""

import functools
import json
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from frappe_profiler.analyzers.base import SEVERITY_ORDER

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


# ---------------------------------------------------------------------------
# v0.5.0: URL redaction for Safe Report mode
# ---------------------------------------------------------------------------
# Mirrors how SQL normalization works in top_queries.py: full text in Raw,
# redacted form in Safe, both derived from the same stored blob. Applied
# to frontend XHR URLs and page paths in the Frontend panel.

_DOCNAME_PATH_RE = re.compile(r"^/app/([^/]+)/([^/?#]+)")

# Frappe's /app/<doctype>/<second> URL scheme uses reserved second-segment
# keywords for list/view/new/report/calendar/etc. These aren't docnames
# and must NOT be replaced with <name> — that would mangle list routes
# into `/app/sales-invoice/<name>/list` which is semantically wrong.
# v0.5.1 fix for the false-positive flagged in the architect review.
_APP_RESERVED_SEGMENTS = frozenset({
	"view",         # list/report view suffix
	"new",          # new-doc form
	"edit",         # edit form (rare)
	"list",         # direct list route
	"report",       # report view
	"tree",         # tree view
	"dashboard",    # dashboard view
	"calendar",     # calendar view
	"kanban",       # kanban board
	"gantt",        # gantt chart
	"image",        # image gallery
	"inbox",        # inbox view
	"print",        # print format preview
})

# Query string values are redacted by default in Safe mode (denylist
# strategy: redact everything that isn't explicitly marked safe). This
# is the opposite of the old allowlist approach — safer for custom
# Frappe apps that add their own filter keys, because we never miss
# a PII leak just because we didn't know about a new key name.
#
# Safe keys are schema references, pagination/sort flags, and format
# hints — values that can't contain customer data.
_QS_SAFE_KEYS = frozenset({
	# Schema references (code identifiers, not PII)
	"doctype", "fieldtype", "fieldname", "parenttype", "parentfield",
	# Pagination
	"limit", "offset", "limit_start", "limit_page_length", "start", "page_length",
	# Sorting
	"order_by", "sort_by", "sort_order", "sort",
	# Format hints
	"as_array", "as_list", "as_dict", "format", "json", "csv",
	# Extension flags
	"with_childnames", "with_comment_count", "debug", "ignore_permissions",
	# Cache behavior
	"cache", "refresh", "force", "cmd",
})


def _safe_url(url: str | None) -> str:
	"""Redact PII from a captured URL for Safe Report mode.

	Redaction rules:
	  /app/<doctype>/<name>/...   ->  /app/<doctype>/<name>/...  (strip docname,
	                                  unless the second segment is a Frappe
	                                  reserved keyword like `view`, `list`,
	                                  `new`, etc. — those are route hints,
	                                  not docnames).
	  ?<key>=<value>              ->  ?<key>=?  (for every key NOT in
	                                  _QS_SAFE_KEYS — denylist strategy).

	Method URLs (/api/method/frappe.client.save) pass through because
	method names are code identifiers, not PII — same status as SQL
	table names after normalization.

	Non-string / None inputs return "" rather than raising.
	"""
	if not url or not isinstance(url, str):
		return ""

	parsed = urlparse(url)

	# Docname path redaction with reserved-segment guard.
	m = _DOCNAME_PATH_RE.match(parsed.path)
	if m and m.group(2) not in _APP_RESERVED_SEGMENTS:
		path = _DOCNAME_PATH_RE.sub(r"/app/\1/<name>", parsed.path)
	else:
		path = parsed.path

	# Query string denylist: redact everything not explicitly safe.
	if parsed.query:
		pairs = parse_qsl(parsed.query, keep_blank_values=True)
		redacted_pairs = [
			(key, value if key in _QS_SAFE_KEYS else "?")
			for key, value in pairs
		]
		query = urlencode(redacted_pairs, doseq=True)
	else:
		query = ""

	return urlunparse((
		parsed.scheme,
		parsed.netloc,
		path,
		parsed.params,
		query,
		parsed.fragment,
	))

# ---------------------------------------------------------------------------
# Sensitive field redaction (Round 2, fix #1)
# ---------------------------------------------------------------------------
# Even though the raw report is permission-gated to System Manager + the
# recording user, once downloaded to local disk it can leak to backup
# systems, screen shares, email, etc. We redact known-sensitive field
# names from headers and form_dict BEFORE they hit the template, keeping
# the field NAME (useful signal for the developer) but replacing the
# VALUE with "[REDACTED]".
#
# Patterns are case-insensitive, matched against the full field name.
# Add new patterns here when you see leaks in real reports.

_SENSITIVE_FIELD_PATTERNS = [
	re.compile(p, re.IGNORECASE)
	for p in (
		# Auth
		r"password",
		r"passwd",
		r"^pwd$",
		r"secret",
		r"token",
		r"api[-_]?key",
		r"^authorization$",
		r"^auth$",
		r"bearer",
		# Session / cookies
		r"^cookie$",
		r"^set[-_]cookie$",
		r"^sid$",
		r"session[-_]?id",
		r"csrf",
		r"x[-_]frappe[-_]csrf",
		# Two-factor / OTP
		r"otp",
		r"verification[-_]?code",
		# Cards / payment
		r"card[-_]?number",
		r"cvv",
		r"ccv",
		# Personal identifiers (conservative — false positives are fine)
		r"ssn",
		r"aadhar",
		r"aadhaar",
		r"pan[-_]?number",
	)
]

_REDACTED = "[REDACTED]"


def _redact_value(key: Any, value: Any, depth: int = 0) -> Any:
	"""Recursively redact sensitive values in a dict or list.

	Rule: scalar values at sensitive keys are replaced with [REDACTED],
	but containers (dicts and lists) are always recursed into so nested
	non-sensitive fields are preserved. This gives the dev shop maximum
	useful context (e.g. `auth: {username: "alice"}` stays visible even
	though the parent key is "auth") while still scrubbing credentials.

	Max depth 4 prevents runaway recursion on circular structures (Frappe
	form_dicts are usually flat or one level deep, so 4 is plenty).
	"""
	if depth > 4:
		return value

	# Recurse into containers regardless of the parent key. Nested
	# sensitive children will still be caught at their own level.
	if isinstance(value, dict):
		return {k: _redact_value(k, v, depth + 1) for k, v in value.items()}
	if isinstance(value, (list, tuple)):
		return [_redact_value(key, v, depth + 1) for v in value]

	# For scalar values only: check if the key is sensitive.
	key_str = str(key).lower()
	for pattern in _SENSITIVE_FIELD_PATTERNS:
		if pattern.search(key_str):
			return _REDACTED
	return value


def redact_sensitive(obj: Any) -> Any:
	"""Top-level redaction entry point — returns a copy of obj with all
	sensitive fields blanked out. Used by the renderer to sanitize
	recording.headers and recording.form_dict before they hit the raw
	report template.
	"""
	if obj is None:
		return None
	if isinstance(obj, dict):
		return {k: _redact_value(k, v) for k, v in obj.items()}
	return obj


@functools.lru_cache(maxsize=1)
def _get_jinja_env() -> Environment:
	"""Build and cache the Jinja environment.

	Autoescape is on for HTML so user-provided strings (action labels,
	finding titles, etc.) can never inject markup into the report.
	"""
	return Environment(
		loader=FileSystemLoader(_TEMPLATES_DIR),
		autoescape=select_autoescape(["html"]),
		trim_blocks=True,
		lstrip_blocks=True,
	)


def render(
	session_doc: Any,
	recordings: list[dict] | None = None,
	*,
	mode: str = "safe",
	generated_at: str | None = None,
) -> str:
	"""Render a Profiler Session to standalone HTML.

	Args:
	    session_doc: The Profiler Session DocType row (loaded via
	        frappe.get_doc). Provides totals, summary_html, and the
	        actions/findings child rows.
	    recordings: The in-memory recordings list. Used by BOTH modes for
	        the per-action query drill-down (safe mode shows normalized
	        queries; raw mode shows raw SQL + headers + form_dict +
	        stacks). Can be None in safe mode if you only want the summary
	        view, but the per-action breakdown will be omitted.
	    mode: "safe" or "raw". Raw mode requires recordings.
	    generated_at: ISO timestamp of when this report was generated;
	        defaults to now() if not provided.

	Returns:
	    Standalone HTML as a string. Inline CSS, no external assets, no
	    JavaScript. Self-contained for emailing or attaching to a ticket.
	"""
	if mode not in ("safe", "raw"):
		raise ValueError(f"Invalid renderer mode: {mode!r} (expected 'safe' or 'raw')")

	if mode == "raw" and recordings is None:
		raise ValueError("raw mode requires the recordings list")

	template = _get_jinja_env().get_template("report.html")

	# Build the per-action drill-down structure. We pair each Profiler
	# Action child row with its source recording so the template can show
	# full SQL / headers / form_dict for that action.
	#
	# CRITICAL: Even though the raw report is permission-gated, we still
	# run the sensitive-field redactor on headers and form_dict before
	# they hit the template. This is defense-in-depth — once the HTML is
	# downloaded, Frappe's permission system no longer protects it.
	# See _SENSITIVE_FIELD_PATTERNS at the top of this file for what's
	# redacted.
	recordings_by_uuid: dict[str, dict] = {}
	if recordings:
		for r in recordings:
			uid = r.get("uuid")
			if not uid:
				continue
			# Shallow copy so we don't mutate the caller's recording dict.
			# (The analyze pipeline may still use the un-redacted data
			# after rendering completes.)
			redacted = dict(r)
			redacted["headers"] = redact_sensitive(r.get("headers"))
			redacted["form_dict"] = redact_sensitive(r.get("form_dict"))
			recordings_by_uuid[uid] = redacted

	actions = [_action_to_dict(a) for a in (session_doc.actions or [])]
	all_findings = [_finding_to_dict(f) for f in (session_doc.findings or [])]

	try:
		top_queries = json.loads(session_doc.top_queries_json or "[]")
	except Exception:
		top_queries = []
	try:
		table_breakdown = json.loads(session_doc.table_breakdown_json or "[]")
	except Exception:
		table_breakdown = []

	# Sort all findings: highest severity first, then highest impact.
	all_findings.sort(
		key=lambda f: (
			SEVERITY_ORDER.get(f["severity"], 3),
			-(f["estimated_impact_ms"] or 0),
		)
	)

	# v0.5.2: split findings into two buckets per user feedback
	# ("In Findings — what to fix, Show only the valid fixes").
	#
	# ACTIONABLE: findings with a concrete fix the user can ship —
	# add an index, refactor a loop, trim a response. These go into
	# the main "Findings — what to fix" section so the list reads
	# as a punchlist.
	#
	# OBSERVATIONS: informational findings that surface signal but
	# don't prescribe a fix the user can act on (framework N+1 where
	# the loop lives inside Frappe, system-level CPU/memory/queue
	# pressure, repeated hot frames that need further investigation).
	# These go into a separate "Observations" section — still
	# visible for users who want the full picture, but no longer
	# cluttering the action list.
	actionable_findings = [
		f for f in all_findings if f["finding_type"] in _ACTIONABLE_FINDING_TYPES
	]
	observational_findings = [
		f for f in all_findings if f["finding_type"] not in _ACTIONABLE_FINDING_TYPES
	]
	# Back-compat: some template paths still reference `findings`.
	# Point it at the actionable list so the main section shows
	# only the punchlist. Observations are exposed separately.
	findings = actionable_findings

	# v0.3.0: load donut + hot frames data from the new fields. Each
	# helper degrades to empty/None if the field is missing (old session).
	allowed_prefixes = ()
	try:
		import frappe

		allowed_prefixes = tuple(
			frappe.conf.get("profiler_safe_extra_allowed_apps") or ()
		)
	except Exception:
		pass

	try:
		_breakdown = json.loads(getattr(session_doc, "session_time_breakdown_json", None) or "{}")
	except Exception:
		_breakdown = {}
	try:
		_hot_frames_raw = json.loads(getattr(session_doc, "hot_frames_json", None) or "[]")
	except Exception:
		_hot_frames_raw = []

	donut_slices = build_donut_data(_breakdown, mode=mode, allowed_prefixes=allowed_prefixes)
	donut_svg = build_donut_svg(donut_slices)  # v0.4.0: PDF fallback
	hot_frames_rows = build_hot_frames_table(
		_hot_frames_raw, mode=mode, allowed_prefixes=allowed_prefixes,
	)

	def _redact_for_template(node):
		return redact_frame_name(node, mode=mode, allowed_prefixes=allowed_prefixes)

	def _from_json(s):
		try:
			return json.loads(s) if s else {}
		except Exception:
			return {}

	# v0.4.0: compute comparison data if a baseline is set
	comparison_data = None
	baseline_uuid = getattr(session_doc, "compared_to_session", None)
	if baseline_uuid:
		try:
			import frappe

			baseline = frappe.get_doc("Profiler Session", baseline_uuid)
			if getattr(baseline, "status", None) == "Ready":
				from frappe_profiler import comparison as _cmp

				comparison_data = _cmp.compute_comparison(
					new_session=session_doc, baseline_session=baseline,
				)
		except Exception:
			# Baseline deleted, in Failed state, or comparison computation
			# failed — log and render without comparison sections.
			try:
				import frappe

				frappe.log_error(title="frappe_profiler comparison render")
			except Exception:
				pass
			comparison_data = None

	# v0.5.0: infra_pressure + frontend_timings aggregates. One JSON field
	# holds both. Empty fallbacks let sessions captured before v0.5.0
	# render cleanly after the upgrade — the new panels just don't appear.
	try:
		v5 = json.loads(getattr(session_doc, "v5_aggregate_json", None) or "{}")
	except Exception:
		v5 = {}

	# v0.5.0: pre-sanitize session.notes before the template uses |safe.
	# The field was upgraded from plain Text to Text Editor in v0.5.0,
	# which means `{{ session.notes | safe }}` would render stored HTML
	# verbatim — a stored-XSS sink if any existing row has script content
	# (plain-text before, live HTML after).
	#
	# CRITICAL: pass always_sanitize=True. Without it, Frappe's
	# sanitize_html has TWO fast-paths that skip bleach:
	#   1. if is_json(html) → returns unchanged  (bypassable with
	#      notes = '{"x":"<script>alert(1)</script>"}' — valid JSON
	#      containing a script tag)
	#   2. if BeautifulSoup.find() returns nothing → returns unchanged
	# Both paths would leak raw input to |safe in the template.
	# always_sanitize=True forces nh3/bleach to run on every input.
	notes_html = getattr(session_doc, "notes", None) or ""
	if notes_html:
		try:
			from frappe.utils.html_utils import sanitize_html
			notes_html = sanitize_html(notes_html, always_sanitize=True)
		except Exception:
			# If sanitize_html blows up for any reason (unexpected input
			# type, nh3/bleach internal error), fall back to HTML-escaping
			# via html.escape so the report NEVER renders unsanitized
			# user input — safe by default.
			import html as html_mod
			notes_html = html_mod.escape(notes_html)

	context = {
		"session": session_doc,
		"actions": actions,
		# v0.5.2: "findings" holds actionable items only (shown in
		# "Findings — what to fix"). The severity counts and the
		# summary reflect ACTIONABLE findings — not the observations
		# — so the session status shows what the user needs to ship,
		# not infra noise.
		"findings": findings,
		"observational_findings": observational_findings,
		"top_queries": top_queries,
		"table_breakdown": table_breakdown,
		"recordings_by_uuid": recordings_by_uuid,
		"mode": mode,
		"generated_at": generated_at or _now_iso(),
		"server_tz": _get_server_timezone(),
		# Severity counts for the summary — actionable only, so the
		# session badge reflects fixable items.
		"severity_counts": {
			"High": sum(1 for f in findings if f["severity"] == "High"),
			"Medium": sum(1 for f in findings if f["severity"] == "Medium"),
			"Low": sum(1 for f in findings if f["severity"] == "Low"),
		},
		# v0.3.0 additions
		"donut_slices": donut_slices,
		"hot_frames_rows": hot_frames_rows,
		"redact_frame_name": _redact_for_template,
		"from_json": _from_json,
		# v0.4.0 additions
		"comparison": comparison_data,
		"donut_svg": donut_svg,
		# v0.5.0 additions
		"infra_timeline": v5.get("infra_timeline") or [],
		"infra_summary": v5.get("infra_summary") or {},
		"frontend_xhr_matched": v5.get("frontend_xhr_matched") or [],
		"frontend_vitals_by_page": v5.get("frontend_vitals_by_page") or {},
		"frontend_orphans": v5.get("frontend_orphans") or [],
		"frontend_summary": v5.get("frontend_summary") or {},
		"safe_url": _safe_url,
		"notes_html": notes_html,  # sanitized, safe to pass through |safe
	}

	return template.render(**context)


def render_safe(session_doc: Any, recordings: list[dict] | None = None) -> str:
	"""Render the safe (shareable) version of the report."""
	return render(session_doc, recordings, mode="safe")


def render_raw(session_doc: Any, recordings: list[dict]) -> str:
	"""Render the raw (internal) version of the report.

	Requires the in-memory recordings list — raw SQL, headers, form_dict,
	and full stack traces are NOT stored on the DocType.
	"""
	return render(session_doc, recordings, mode="raw")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _action_to_dict(child: Any) -> dict:
	"""Flatten a Profiler Action child row to a plain dict."""
	return {
		"action_label": child.action_label or "",
		"event_type": child.event_type or "",
		"http_method": child.http_method or "",
		"path": child.path or "",
		"recording_uuid": child.recording_uuid or "",
		"duration_ms": child.duration_ms or 0,
		"queries_count": child.queries_count or 0,
		"query_time_ms": child.query_time_ms or 0,
		"slowest_query_ms": child.slowest_query_ms or 0,
	}


def _finding_to_dict(child: Any) -> dict:
	"""Flatten a Profiler Finding child row, parsing the JSON detail blob."""
	try:
		detail = json.loads(child.technical_detail_json or "{}")
	except Exception:
		detail = {}
	return {
		"finding_type": child.finding_type or "",
		"severity": child.severity or "Low",
		"title": child.title or "",
		"customer_description": child.customer_description or "",
		"estimated_impact_ms": child.estimated_impact_ms or 0,
		"affected_count": child.affected_count or 0,
		"action_ref": child.action_ref or "",
		"technical_detail": detail,
	}


def _now_iso() -> str:
	from datetime import datetime

	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_server_timezone() -> str:
	"""Return a human-readable server timezone label.

	Tries frappe's system settings first (more accurate than Python's
	local tz guess). Falls back to the Python datetime tzname.
	"""
	try:
		import frappe

		tz = frappe.db.get_single_value("System Settings", "time_zone")
		if tz:
			return str(tz)
	except Exception:
		pass
	try:
		from datetime import datetime

		name = datetime.now().astimezone().tzname()
		if name:
			return name
	except Exception:
		pass
	return "UTC"


# ---------------------------------------------------------------------------
# v0.3.0: call tree, donut, and hot frames helpers
# ---------------------------------------------------------------------------

HARDCODED_ALLOWED_PREFIXES = ("frappe.", "erpnext.", "payments.", "hrms.")


# v0.5.2: finding types that carry a concrete, user-actionable fix.
# These render in the main "Findings — what to fix" section.
# Everything else (framework-level, system-level, informational)
# renders in a separate "Observations" section below so the action
# list stays tight.
#
# Rule of thumb: if the customer_description ends with a specific
# next step the user can ship in a single PR (add THIS index,
# refactor THIS loop, trim THIS response), it belongs here. If the
# finding is an observation about the system or framework where the
# user has no direct code change to make, it's an Observation.
_ACTIONABLE_FINDING_TYPES = frozenset({
	# SQL — all have concrete DDL / refactor guidance
	"N+1 Query",
	"Missing Index",
	"Full Table Scan",
	"Filesort",
	"Temporary Table",
	"Low Filter Ratio",
	"Slow Query",
	# Python hot paths in user code
	"Slow Hot Path",       # narrowed by call_tree filter to user frames
	"Hook Bottleneck",     # user's own doc-event hook is slow
	"Redundant Call",      # v0.5.2: framework callsites already filtered
	# Frontend — user can trim responses / optimize JS
	"Slow Frontend Render",
	"Heavy Response",
})
# Observation-only finding types (informational, no direct fix):
#   Framework N+1            — loop inside frappe/*
#   Repeated Hot Frame       — function repeated across actions; needs
#                               investigation, not a shippable fix
#   Resource Contention      — system CPU sustained high
#   Memory Pressure          — worker RSS growth / swap
#   DB Pool Saturation       — infra-level
#   Background Queue Backlog — infra-level
#   Network Overhead         — client/proxy territory, not user code


def redact_frame_name(node: dict, mode: str, allowed_prefixes: tuple) -> str:
	"""Apply R2 redaction to a tree node's display name.

	Safe mode:
	  - Frames in frappe./erpnext./payments./hrms. → full name kept
	  - Frames in any allowed_prefixes prefix → full name kept
	  - All other frames → collapsed to "<app>:<top-level-module>"

	Raw mode:
	  - Full function name + filename + line number, no redaction.
	"""
	if not isinstance(node, dict):
		return "<unknown>"

	function = node.get("function") or "<unknown>"
	filename = node.get("filename") or ""
	lineno = node.get("lineno") or 0

	if mode == "raw":
		short_file = filename.split("/")[-1] if filename else "?"
		return f"{function} ({short_file}:{lineno})"

	# Special markers — pass through unchanged
	if function.startswith("[") or function in ("<root>", "<sql>", "<unknown>"):
		return function

	allowed = HARDCODED_ALLOWED_PREFIXES + tuple(allowed_prefixes or ())
	for prefix in allowed:
		if function.startswith(prefix):
			return function

	# Collapse to <app>:<top-level-module>
	parts = function.split(".", 2)
	if len(parts) >= 2:
		return f"{parts[0]}:{parts[1]}"
	return function


# Donut color palette (8 colors; rolls over for more).
_DONUT_COLORS = [
	"#ff6b6b", "#4ecdc4", "#ffd93d", "#6c5ce7",
	"#a8e6cf", "#ff8b94", "#95e1d3", "#ffaaa5",
]


def build_donut_data(breakdown: dict, mode: str, allowed_prefixes: tuple) -> list:
	"""Convert session_time_breakdown_json into ordered (label, ms, color) tuples.

	Safe mode collapses non-allowed app names into a single "Python (custom apps)"
	bucket. Raw mode shows each app by name.

	v0.5.1: hides slices that round to 0ms in display. A session with
	148ms of SQL and only a handful of sub-ms Python self-times was
	rendering seven "Python (…) — 0ms" entries, all noise. The
	threshold catches both (a) genuinely tiny buckets and (b) buckets
	that leaked through imperfect routing (stdlib files, third-party
	libs that the bucketer should have caught but missed).
	"""
	if not breakdown:
		return []

	# v0.5.1: buckets under this threshold display as "0ms" after the
	# integer-ms display formatting, so they're worse than useless —
	# they just add visual clutter. Hide them.
	DONUT_DISPLAY_MIN_MS = 1.0

	allowed = HARDCODED_ALLOWED_PREFIXES + tuple(allowed_prefixes or ())
	allowed_app_names = {p.rstrip(".") for p in allowed}

	slices = []
	sql_ms = breakdown.get("sql_ms", 0)
	if sql_ms >= DONUT_DISPLAY_MIN_MS:
		slices.append(("SQL", sql_ms, _DONUT_COLORS[0]))

	by_app = breakdown.get("by_app", {})
	if mode == "safe":
		# Group: each allowed app gets its own slice; everything else
		# merges into "Python (custom apps)".
		custom_total = 0.0
		for app, ms in by_app.items():
			if app in allowed_app_names:
				if ms >= DONUT_DISPLAY_MIN_MS:
					color = _DONUT_COLORS[(len(slices)) % len(_DONUT_COLORS)]
					slices.append((f"Python ({app})", ms, color))
			else:
				custom_total += ms
		if custom_total >= DONUT_DISPLAY_MIN_MS:
			color = _DONUT_COLORS[(len(slices)) % len(_DONUT_COLORS)]
			slices.append(("Python (custom apps)", custom_total, color))
	else:
		# Raw mode: every app named, subject to the display threshold.
		for app, ms in by_app.items():
			if ms < DONUT_DISPLAY_MIN_MS:
				continue
			color = _DONUT_COLORS[(len(slices)) % len(_DONUT_COLORS)]
			slices.append((f"Python ({app})", ms, color))

	return slices


def build_donut_svg(slices: list) -> str:
	"""Render the donut as an inline SVG pie for PDF mode.

	wkhtmltopdf does not handle conic-gradient reliably; this SVG
	fallback always renders correctly. Each slice becomes a <path>
	element with a precomputed arc.
	"""
	if not slices:
		return ""
	import math

	total = sum(s[1] for s in slices) or 1
	cx, cy, r = 80, 80, 70
	parts = ['<svg width="160" height="160" xmlns="http://www.w3.org/2000/svg">']
	angle_start = -math.pi / 2  # start at 12 o'clock

	for label, ms, color in slices:
		fraction = ms / total
		angle_end = angle_start + fraction * 2 * math.pi
		x1 = cx + r * math.cos(angle_start)
		y1 = cy + r * math.sin(angle_start)
		x2 = cx + r * math.cos(angle_end)
		y2 = cy + r * math.sin(angle_end)
		large_arc = 1 if fraction > 0.5 else 0
		path = (
			f'<path d="M {cx} {cy} L {x1:.1f} {y1:.1f} '
			f'A {r} {r} 0 {large_arc} 1 {x2:.1f} {y2:.1f} Z" '
			f'fill="{color}" stroke="#fff" stroke-width="1"/>'
		)
		parts.append(path)
		angle_start = angle_end

	parts.append("</svg>")
	return "".join(parts)


def build_hot_frames_table(rows: list, mode: str, allowed_prefixes: tuple) -> list:
	"""Apply redaction to the hot frames leaderboard rows.

	Returns a list of dicts ready for the template (display_name, total_ms,
	occurrences, distinct_actions, action_refs).
	"""
	out = []
	for row in rows or []:
		display = redact_frame_name(
			{"function": row.get("function"), "filename": "", "lineno": 0},
			mode=mode,
			allowed_prefixes=allowed_prefixes,
		)
		out.append({
			"display_name": display,
			"total_ms": row.get("total_ms", 0),
			"occurrences": row.get("occurrences", 0),
			"distinct_actions": row.get("distinct_actions", 0),
			"action_refs": row.get("action_refs", []),
		})
	return out
