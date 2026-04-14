# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Whitelisted HTTP API for the profiler.

These endpoints are how the floating widget (Phase 5) and any custom
integrations talk to the profiler. They are decorated with
`@frappe.whitelist()` so they are reachable as
`/api/method/frappe_profiler.api.<name>`.

Phase 1 surface:
    start(label)         — begin a session for the calling user
    stop()               — end the current user's session
    status()             — is the calling user currently recording?
    get_active_session() — full metadata for the calling user's session

The actual analyze pipeline (read recordings, run analyzers, render
report, attach files) is added in Phase 3+. For now, `stop()` simply
clears the active flag and marks the DocType as `Stopping` — a future
phase will hook the analyze step in.
"""

import frappe
from frappe.utils import now_datetime

from frappe_profiler import session

# Roles allowed to call the profiler API. System Manager is always allowed
# (Frappe's superuser role); Profiler User is our dedicated role created
# on install via install.after_install. Adding Administrator explicitly
# because frappe.get_roles("Administrator") doesn't include "System Manager".
ALLOWED_ROLES = {"System Manager", "Profiler User", "Administrator"}


def _require_user() -> str:
	"""Return the calling user, or throw if Guest."""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw("You must be logged in to use the profiler.")
	return user


def _require_profiler_user() -> str:
	"""Return the calling user, or throw if they lack the profiler role.

	The HTTP-level enforcement that the floating widget's role check is
	mirroring. Without this, any authenticated user could POST to
	/api/method/frappe_profiler.api.start and start a session on
	themselves — the role check in the widget JS would be purely
	cosmetic.
	"""
	user = _require_user()
	if user == "Administrator":
		return user
	roles = set(frappe.get_roles(user))
	if not (ALLOWED_ROLES & roles):
		frappe.throw(
			"You need the Profiler User or System Manager role to use the profiler.",
			frappe.PermissionError,
		)
	return user


@frappe.whitelist()
def start(label: str = "", capture_python_tree: bool = True) -> dict:
	"""Begin a profiling session for the calling user.

	If the user already has an active session, that session is stopped
	first (its DocType row is marked `Stopping` and its Redis pointer
	cleared). This makes start() idempotent from the user's perspective:
	clicking Start twice never produces two parallel sessions.

	Args:
	    label: Human-readable session label.
	    capture_python_tree: v0.3.0+. When True (default), pyinstrument
	        captures a Python call tree per recording and sidecar wraps
	        capture frappe.get_doc / cache.get_value / has_permission
	        argument identities. When False, only the existing SQL
	        recorder runs — same overhead profile as v0.2.0.
	"""
	user = _require_profiler_user()

	# v0.3.0: clear any in-flight capture state from a previous request
	# on this worker BEFORE we look at session state, so leaked state
	# from a concurrent request doesn't influence the new session.
	from frappe_profiler import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# If the user is already recording, gracefully stop the previous one.
	previous = session.get_active_session_for(user)
	if previous:
		_stop_session(user, previous)

	session_uuid = frappe.generate_hash(length=16)
	now = now_datetime()
	title = (label or "").strip() or f"Profiling session @ {now.strftime('%Y-%m-%d %H:%M:%S')}"

	# Create the DocType row in Recording state.
	doc = frappe.get_doc(
		{
			"doctype": "Profiler Session",
			"session_uuid": session_uuid,
			"title": title,
			"user": user,
			"status": "Recording",
			"started_at": now,
		}
	).insert(ignore_permissions=True)

	# Store metadata in Redis (used by the analyze pipeline later).
	session.set_session_meta(
		session_uuid,
		{
			"session_uuid": session_uuid,
			"docname": doc.name,
			"user": user,
			"label": title,
			"started_at": now.isoformat(),
			"capture_python_tree": bool(capture_python_tree),
		},
	)

	# Flip the user's active flag last — once this is set, the next
	# request from this user will start being recorded.
	session.set_active_session(user, session_uuid)

	return {
		"session_uuid": session_uuid,
		"docname": doc.name,
		"title": title,
		"started_at": now.isoformat(),
	}


@frappe.whitelist()
def stop() -> dict:
	"""End the calling user's active profiling session.

	Phase 1 behavior: clears the Redis active pointer and marks the
	DocType row as `Stopping`. The analyze pipeline (Phase 3) will pick
	up `Stopping` rows, run analyzers, render reports, and transition
	them to `Ready`.
	"""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return {"stopped": False, "reason": "no active session"}

	docname = _stop_session(user, active)
	return {"stopped": True, "session_uuid": active, "docname": docname}


def _stop_session(user: str, session_uuid: str) -> str | None:
	"""Internal: clear the active pointer, mark the DocType as Stopping,
	and enqueue the analyze job.

	Returns the docname of the affected Profiler Session, or None if the
	row couldn't be found (Redis/MariaDB state drift — e.g. after a
	Redis flush). Composed from three small helpers for clarity.
	"""
	# v0.3.0: stop any in-flight pyinstrument session and clear capture
	# state on this worker before flipping the active flag, so a previous
	# in-flight capture from the same worker doesn't leak into a new
	# session started immediately after.
	from frappe_profiler import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	_clear_active(user)
	docname = _mark_stopping(user, session_uuid)
	if not docname:
		return None
	_enqueue_analyze(session_uuid)
	return docname


def _clear_active(user: str) -> None:
	"""Remove the user's active session pointer from Redis.

	Idempotent; safe to call even if no session is active. Once this
	returns, no further requests from this user will activate recording.
	"""
	session.clear_active_session(user)


def _mark_stopping(user: str, session_uuid: str) -> str | None:
	"""Transition the Profiler Session row to the Stopping state.

	Returns the docname on success or None if no matching row exists.
	"""
	docname = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid, "user": user},
		"name",
	)
	if not docname:
		return None

	frappe.db.set_value(
		"Profiler Session",
		docname,
		{"status": "Stopping", "stopped_at": now_datetime()},
	)
	frappe.db.commit()
	return docname


def _enqueue_analyze(session_uuid: str) -> None:
	"""Enqueue the background analyze job on the long queue.

	We use queue="long" because EXPLAIN against many queries on a busy
	production database can take 30+ seconds for a heavy session. The
	long queue's 25-minute timeout gives us headroom.
	"""
	frappe.enqueue(
		"frappe_profiler.analyze.run",
		queue="long",
		session_uuid=session_uuid,
	)


@frappe.whitelist()
def status() -> dict:
	"""Return whether the calling user has an active profiling session."""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return {"active": False}

	meta = session.get_session_meta(active) or {}
	return {
		"active": True,
		"session_uuid": active,
		"docname": meta.get("docname"),
		"label": meta.get("label"),
		"started_at": meta.get("started_at"),
	}


@frappe.whitelist()
def get_active_session() -> dict | None:
	"""Return full metadata for the calling user's active session, or None."""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return None
	return session.get_session_meta(active)


@frappe.whitelist()
def health() -> dict:
	"""Lightweight health/metrics endpoint for ops scrapers.

	Returns a small structured dict with counts by session status and
	analyze-pipeline performance over the last 24 hours. Intended to be
	polled from Prometheus/Grafana/Datadog via a custom scraper, or
	called manually by an admin to sanity-check the profiler's health.

	Permission: any role that can use the profiler (Profiler User or
	System Manager). Doesn't expose session contents — just aggregate
	counts.
	"""
	_require_profiler_user()

	# Count by status
	rows = (
		frappe.db.sql(
			"SELECT status, COUNT(*) FROM `tabProfiler Session` GROUP BY status",
			as_list=True,
		)
		or []
	)
	by_status = {row[0]: int(row[1]) for row in rows}

	# Analyze performance over the last 24 hours. Only count Ready
	# sessions — Failed sessions don't have a meaningful analyze time.
	recent = frappe.db.sql(
		"""
		SELECT
			COUNT(*),
			COALESCE(AVG(analyze_duration_ms), 0),
			COALESCE(MAX(analyze_duration_ms), 0)
		FROM `tabProfiler Session`
		WHERE status = 'Ready'
		  AND modified > NOW() - INTERVAL 1 DAY
		"""
	)
	count, avg_ms, max_ms = recent[0] if recent else (0, 0, 0)

	# Count by top severity for Ready sessions (useful signal for
	# "are customers finding issues?")
	severity_rows = (
		frappe.db.sql(
			"""
			SELECT COALESCE(top_severity, 'None'), COUNT(*)
			FROM `tabProfiler Session`
			WHERE status = 'Ready'
			GROUP BY top_severity
			""",
			as_list=True,
		)
		or []
	)
	by_severity = {row[0] or "None": int(row[1]) for row in severity_rows}

	return {
		"by_status": by_status,
		"by_top_severity_ready": by_severity,
		"last_24h": {
			"sessions_ready": int(count or 0),
			"analyze_avg_ms": round(float(avg_ms or 0), 2),
			"analyze_max_ms": round(float(max_ms or 0), 2),
		},
	}


# v0.4.0: onboarding toast state endpoints. Used by floating_widget.js
# to decide whether to render the one-time onboarding toast.
ONBOARDING_CACHE_PREFIX = "profiler:onboarding_seen:"
ONBOARDING_CACHE_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year


@frappe.whitelist()
def check_onboarding_seen() -> dict:
	"""Has the current user dismissed the onboarding toast?

	Also returns True if the user has any existing Ready Profiler Session
	row (they're an experienced user; suppress the toast).
	"""
	user = _require_user()
	# Suppress for experienced users — anyone with at least one Ready session
	try:
		existing = frappe.db.count(
			"Profiler Session",
			filters={"user": user, "status": "Ready"},
		)
		if existing and existing > 0:
			return {"seen": True}
	except Exception:
		pass
	flag = frappe.cache.get_value(f"{ONBOARDING_CACHE_PREFIX}{user}")
	return {"seen": bool(flag)}


@frappe.whitelist()
def mark_onboarding_seen() -> dict:
	"""Mark the onboarding toast as dismissed for the current user."""
	user = _require_user()
	frappe.cache.set_value(
		f"{ONBOARDING_CACHE_PREFIX}{user}",
		"1",
		expires_in_sec=ONBOARDING_CACHE_TTL_SECONDS,
	)
	return {"seen": True}


@frappe.whitelist()
def export_session(session_uuid: str) -> dict:
	"""Export a Profiler Session as a structured JSON blob.

	Lets dev shops (or automation) consume the profiler's output
	programmatically without parsing the HTML report. Returns the full
	session including all child rows, top queries, table breakdown, and
	finding technical details — effectively everything from the safe
	report in a machine-friendly shape.

	Permission model: mirrors the "raw report" gate — only the recording
	user or a System Manager can export. Other Profiler Users get a
	permission error even if they somehow guessed the uuid.
	"""
	import json

	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	row = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		"name",
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")

	doc = frappe.get_doc("Profiler Session", row["name"])

	# Permission gate (same logic as retry_analyze)
	roles = set(frappe.get_roles(user))
	if doc.user != user and "System Manager" not in roles and user != "Administrator":
		frappe.throw("You can only export your own sessions.", frappe.PermissionError)

	def _parse_json_field(value):
		if not value:
			return []
		try:
			return json.loads(value)
		except Exception:
			return []

	return {
		"schema_version": 1,
		"exported_at": frappe.utils.now_datetime().isoformat(),
		"session": {
			"session_uuid": doc.session_uuid,
			"title": doc.title,
			"user": doc.user,
			"status": doc.status,
			"started_at": str(doc.started_at) if doc.started_at else None,
			"stopped_at": str(doc.stopped_at) if doc.stopped_at else None,
			"total_duration_ms": doc.total_duration_ms,
			"total_requests": doc.total_requests,
			"total_queries": doc.total_queries,
			"total_query_time_ms": doc.total_query_time_ms,
			"analyze_duration_ms": getattr(doc, "analyze_duration_ms", None),
			"top_severity": getattr(doc, "top_severity", None),
			"analyzer_warnings": doc.analyzer_warnings,
			# v0.3.0 fields
			"total_python_ms": getattr(doc, "total_python_ms", None),
			"total_sql_ms": getattr(doc, "total_sql_ms", None),
		},
		"actions": [
			{
				"idx": a.idx,
				"action_label": a.action_label,
				"event_type": a.event_type,
				"http_method": a.http_method,
				"path": a.path,
				"recording_uuid": a.recording_uuid,
				"duration_ms": a.duration_ms,
				"queries_count": a.queries_count,
				"query_time_ms": a.query_time_ms,
				"slowest_query_ms": a.slowest_query_ms,
				# v0.3.0: include the call tree (or its overflow marker)
				"call_tree": _parse_json_field(getattr(a, "call_tree_json", None)),
			}
			for a in (doc.actions or [])
		],
		"findings": [
			{
				"idx": f.idx,
				"finding_type": f.finding_type,
				"severity": f.severity,
				"title": f.title,
				"customer_description": f.customer_description,
				"technical_detail": _parse_json_field(f.technical_detail_json),
				"estimated_impact_ms": f.estimated_impact_ms,
				"affected_count": f.affected_count,
				"action_ref": f.action_ref,
			}
			for f in (doc.findings or [])
		],
		"top_queries": _parse_json_field(doc.top_queries_json),
		"table_breakdown": _parse_json_field(doc.table_breakdown_json),
		# v0.3.0 top-level aggregates
		"hot_frames": _parse_json_field(getattr(doc, "hot_frames_json", None)),
		"session_time_breakdown": _parse_json_field(
			getattr(doc, "session_time_breakdown_json", None)
		),
	}


@frappe.whitelist()
def retry_analyze(session_uuid: str) -> dict:
	"""Retry the analyze job for a Failed session.

	Allows the recording user or a System Manager to recover from
	transient analyzer errors (worker crash, DB timeout, etc.) without
	dropping into a Frappe console. The session must be in `Failed`
	state — retrying a Ready or Recording session is a no-op.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	doc = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not doc:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")

	# Permission check: recording user OR System Manager only
	roles = set(frappe.get_roles(user))
	if doc["user"] != user and "System Manager" not in roles and user != "Administrator":
		frappe.throw("You can only retry your own sessions.", frappe.PermissionError)

	if doc["status"] != "Failed":
		return {
			"retried": False,
			"reason": f"Session is in '{doc['status']}' state, not Failed.",
		}

	# Reset to Stopping so the analyze pipeline runs through its usual
	# state transitions.
	frappe.db.set_value(
		"Profiler Session",
		doc["name"],
		{"status": "Stopping", "analyzer_warnings": None},
	)
	frappe.db.commit()

	frappe.enqueue(
		"frappe_profiler.analyze.run",
		queue="long",
		session_uuid=session_uuid,
	)

	return {"retried": True, "session_uuid": session_uuid, "docname": doc["name"]}
