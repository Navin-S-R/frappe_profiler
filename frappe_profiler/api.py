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

	# v0.5.0: mirror for infra capture.
	from frappe_profiler import infra_capture

	infra_capture._force_stop_inflight(frappe.local)

	# If the user is already recording, gracefully stop the previous one.
	previous = session.get_active_session_for(user)
	if previous:
		_stop_session(user, previous)

	session_uuid = frappe.generate_hash(length=16)
	now = now_datetime()
	title = (label or "").strip() or f"Profiling session @ {now.strftime('%Y-%m-%d %H:%M:%S')}"

	# v0.4.0: auto-inherit baseline if one is pinned for this label
	auto_baseline = None
	try:
		auto_baseline = frappe.cache.get_value(_baseline_key(title))
	except Exception:
		pass

	# Create the DocType row in Recording state.
	doc_fields = {
		"doctype": "Profiler Session",
		"session_uuid": session_uuid,
		"title": title,
		"user": user,
		"status": "Recording",
		"started_at": now,
	}
	if auto_baseline:
		doc_fields["compared_to_session"] = auto_baseline
	doc = frappe.get_doc(doc_fields).insert(ignore_permissions=True)

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

	Clears the Redis active pointer, marks the Profiler Session row as
	``Stopping``, and either enqueues the analyze job or runs it inline
	(v0.5.0: scheduler-aware fallback — see ``_enqueue_analyze``).

	Returns:
	    dict with ``stopped``, ``session_uuid``, ``docname``, and
	    ``ran_inline``. The widget reads ``ran_inline`` to decide whether
	    to transition through "Analyzing…" or jump straight to "Ready".
	"""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return {"stopped": False, "reason": "no active session"}

	docname, ran_inline = _stop_session(user, active)
	return {
		"stopped": True,
		"session_uuid": active,
		"docname": docname,
		"ran_inline": ran_inline,
	}


def _stop_session(user: str, session_uuid: str) -> tuple[str | None, bool]:
	"""Internal: clear the active pointer, mark the DocType as Stopping,
	and enqueue (or inline-run) the analyze job.

	Returns a tuple ``(docname, ran_inline)``:
	    docname     — the Profiler Session docname, or None if not found
	    ran_inline  — True if analyze was executed synchronously
	                  (scheduler-disabled fallback), False otherwise

	v0.5.0 adds the scheduler-disabled safety cap: when
	``is_scheduler_disabled()`` is True and the session's recording count
	exceeds ``profiler_inline_analyze_limit`` (default 50), we refuse to
	run analyze inline because gunicorn would likely kill the request
	mid-flight. Instead the session is marked Failed with an actionable
	error message and the user is directed to re-enable the scheduler
	and use the Retry Analyze button.
	"""
	# v0.3.0: stop any in-flight pyinstrument session and clear capture
	# state on this worker before flipping the active flag, so a previous
	# in-flight capture from the same worker doesn't leak into a new
	# session started immediately after.
	from frappe_profiler import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# v0.5.0: clear any leaked infra start snapshot from a previous
	# session on the same worker.
	from frappe_profiler import infra_capture

	infra_capture._force_stop_inflight(frappe.local)

	_clear_active(user)
	docname = _mark_stopping(user, session_uuid)
	if not docname:
		return None, False

	# v0.5.0: if analyze would have to run inline (scheduler disabled),
	# refuse large sessions to stay within gunicorn's 120s timeout.
	from frappe.utils.scheduler import is_scheduler_disabled

	would_run_inline = False
	try:
		would_run_inline = bool(is_scheduler_disabled())
	except Exception:
		pass

	if would_run_inline:
		cap = frappe.conf.get("profiler_inline_analyze_limit") or 50
		count = session.recording_count(session_uuid)
		if count > cap:
			frappe.db.set_value(
				"Profiler Session",
				docname,
				{
					"status": "Failed",
					"analyze_error": (
						f"Scheduler is disabled and this session has "
						f"{count} recordings, exceeding the inline "
						f"analyze cap of {cap}. Re-enable the scheduler "
						f"(bench enable-scheduler) and click Retry "
						f"Analyze on this session's form view."
					),
				},
			)
			frappe.db.commit()
			return docname, False

	ran_inline = _enqueue_analyze(session_uuid)
	return docname, ran_inline


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


def _enqueue_analyze(session_uuid: str) -> bool:
	"""Enqueue analyze on the long queue, or run inline if no worker
	will consume it.

	When `bench disable-scheduler` is in effect (or the "Enable
	Scheduler" toggle in System Settings is off), many deployments
	don't have a `bench worker` processing the RQ queue either, so
	an enqueued analyze job would sit forever and the session would
	hang in the "Stopping" state. In that case we fall back to
	``frappe.enqueue(now=True)`` which executes analyze synchronously
	inside the current request. See v0.5.0 design spec §6.

	Returns True if analyze ran inline (the caller must report this
	to the client via ``ran_inline: True`` so the UI can skip the
	"Analyzing…" state). Returns False if the job was pushed to the
	queue as usual.
	"""
	from frappe.utils.scheduler import is_scheduler_disabled

	run_inline = False
	try:
		run_inline = bool(is_scheduler_disabled())
	except Exception:
		frappe.log_error(title="frappe_profiler scheduler check")

	if run_inline:
		frappe.logger().warning(
			f"frappe_profiler: scheduler disabled; running analyze "
			f"inline for session {session_uuid}. Stop API will block "
			f"until analyze completes."
		)

	frappe.enqueue(
		"frappe_profiler.analyze.run",
		queue="long",
		session_uuid=session_uuid,
		now=run_inline,
	)
	return run_inline


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


# ---------------------------------------------------------------------------
# v0.5.0: frontend metrics receiver
# ---------------------------------------------------------------------------

SOFT_CAP_FRONTEND_XHR = 1000
SOFT_CAP_FRONTEND_VITALS = 200


@frappe.whitelist(methods=["POST"])
def submit_frontend_metrics(payload: str) -> dict:
	"""Receive a batch of frontend metrics (XHR timings + Web Vitals).

	Called by profiler_frontend.js at stop time (via frappe.call) and
	at beforeunload (via navigator.sendBeacon). Payload is a JSON
	string because sendBeacon sends raw Blob — Frappe's dict auto-
	parsing doesn't kick in for sendBeacon, so we take a string and
	parse explicitly.

	Idempotent: multiple submits for the same session merge into one
	Redis blob so a sendBeacon flush followed by a normal stop-time
	flush doesn't duplicate data.
	"""
	user = _require_profiler_user()

	try:
		if isinstance(payload, str):
			data = frappe.parse_json(payload)
		else:
			data = payload
	except Exception:
		return {"accepted": False, "reason": "invalid json"}

	if not isinstance(data, dict):
		return {"accepted": False, "reason": "invalid payload"}

	session_uuid = data.get("session_uuid")
	if not session_uuid:
		return {"accepted": False, "reason": "missing session_uuid"}

	# Ownership check: only the user who owns the session can write to
	# its frontend blob. Silent-drop on missing meta because a
	# beforeunload beacon can legitimately arrive after the session has
	# already been stopped and its meta deleted — no log spam.
	meta = session.get_session_meta(session_uuid) or {}
	if not meta or meta.get("user") != user:
		return {"accepted": False, "reason": "session not found"}

	# Tail-preferring cap on the incoming payload. End-of-flow is where
	# the slow thing probably happened, so on overflow we keep the most
	# recent entries, not the oldest.
	xhr = (data.get("xhr") or [])[-SOFT_CAP_FRONTEND_XHR:]
	vitals = (data.get("vitals") or [])[-SOFT_CAP_FRONTEND_VITALS:]

	key = f"profiler:frontend:{session_uuid}"
	existing = frappe.cache.get_value(key) or {"xhr": [], "vitals": []}

	merged_xhr = (existing.get("xhr") or []) + xhr
	merged_vitals = (existing.get("vitals") or []) + vitals

	blob = {
		"xhr": merged_xhr[-SOFT_CAP_FRONTEND_XHR:],
		"vitals": merged_vitals[-SOFT_CAP_FRONTEND_VITALS:],
	}

	frappe.cache.set_value(
		key,
		blob,
		expires_in_sec=session.SESSION_TTL_SECONDS,
	)
	return {
		"accepted": True,
		"xhr_count": len(blob["xhr"]),
		"vitals_count": len(blob["vitals"]),
	}


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


# v0.4.0: baseline-pinning cache key prefix.
BASELINE_CACHE_PREFIX = "profiler:baseline:"


def _baseline_key(label: str) -> str:
	return f"{BASELINE_CACHE_PREFIX}{label}"


def _require_session_owner_or_sysmanager(session_uuid: str) -> dict:
	"""Permission gate shared by all v0.4.0 baseline / pdf endpoints.

	Returns the session row dict on success; throws on failure.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")
	row = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		["name", "user", "status", "title"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")
	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only modify your own sessions.", frappe.PermissionError
		)
	return row


@frappe.whitelist()
def pin_baseline(session_uuid: str) -> dict:
	"""Pin this session as the baseline for its label.

	Writes the docname to a site-level cache key
	`profiler:baseline:<label>`. Clears the is_baseline flag on any
	previously-pinned session for the same label. Sets is_baseline=1 on
	the target.
	"""
	row = _require_session_owner_or_sysmanager(session_uuid)
	if row["status"] != "Ready":
		frappe.throw(f"Cannot pin session in '{row['status']}' state")

	label = row["title"] or ""
	key = _baseline_key(label)

	# Clear flag on any previously-pinned session for the same label
	previous = frappe.cache.get_value(key)
	if previous and previous != row["name"]:
		try:
			frappe.db.set_value("Profiler Session", previous, "is_baseline", 0)
		except Exception:
			pass

	# Set the new baseline
	frappe.cache.set_value(key, row["name"])
	frappe.db.set_value("Profiler Session", row["name"], "is_baseline", 1)
	frappe.db.commit()

	# Re-render any dependent sessions in the background (best-effort)
	try:
		frappe.enqueue(
			"frappe_profiler.api._rerender_dependents",
			queue="short",
			label=label,
			baseline_docname=row["name"],
		)
	except Exception:
		pass

	return {"pinned": True, "session_uuid": session_uuid, "docname": row["name"]}


@frappe.whitelist()
def unpin_baseline(session_uuid: str) -> dict:
	"""Clear the baseline flag for this session and remove cache entry if active."""
	row = _require_session_owner_or_sysmanager(session_uuid)

	label = row["title"] or ""
	key = _baseline_key(label)

	current = frappe.cache.get_value(key)
	if current == row["name"]:
		frappe.cache.delete_value(key)
	frappe.db.set_value("Profiler Session", row["name"], "is_baseline", 0)
	frappe.db.commit()

	return {"unpinned": True, "session_uuid": session_uuid}


def _rerender_dependents(label: str, baseline_docname: str) -> None:
	"""Re-render reports for any Ready sessions whose label matches AND whose
	compared_to_session is NULL or matches the new baseline.

	Background job triggered by pin_baseline. Best-effort.
	"""
	try:
		dependent_names = frappe.get_all(
			"Profiler Session",
			filters={"title": label, "status": "Ready"},
			pluck="name",
		)
	except Exception:
		return

	for name in dependent_names:
		if name == baseline_docname:
			continue
		try:
			doc = frappe.get_doc("Profiler Session", name)
			if not doc.compared_to_session:
				doc.db_set("compared_to_session", baseline_docname, update_modified=False)
			from frappe_profiler.analyze import _render_and_attach_reports

			_render_and_attach_reports(name, recordings=[])
		except Exception:
			frappe.log_error(title=f"frappe_profiler rerender {name}")


@frappe.whitelist()
def set_comparison(session_uuid: str, compared_to: str) -> dict:
	"""Set compared_to_session on a single session for a one-off comparison.

	The current session must be Ready; the compared_to session is looked up
	by docname.
	"""
	row = _require_session_owner_or_sysmanager(session_uuid)
	if not compared_to:
		frappe.throw("compared_to is required")
	target_status = frappe.db.get_value("Profiler Session", compared_to, "status")
	if not target_status:
		frappe.throw(f"No Profiler Session found with name {compared_to}")
	if target_status != "Ready":
		frappe.throw(f"Compared-to session must be Ready, got '{target_status}'")

	frappe.db.set_value("Profiler Session", row["name"], "compared_to_session", compared_to)
	frappe.db.commit()
	return {"set": True, "session_uuid": session_uuid, "compared_to": compared_to}


@frappe.whitelist()
def download_pdf(session_uuid: str) -> dict:
	"""Return the URL of the safe-report PDF, generating it on first call.

	Permission: recording user, System Manager, or Administrator.
	Mirrors retry_analyze / export_session permission gating.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw("session_uuid is required")

	row = frappe.db.get_value(
		"Profiler Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")
	if row["status"] != "Ready":
		frappe.throw(f"Cannot generate PDF for session in '{row['status']}' state")

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			"You can only download PDFs for your own sessions.",
			frappe.PermissionError,
		)

	from frappe_profiler import pdf_export

	url = pdf_export.get_or_generate_pdf(session_uuid)
	return {"file_url": url}


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

	# v0.4.0: clear the cached PDF so the next download regenerates from
	# the fresh HTML produced by the re-run analyze.
	try:
		from frappe_profiler import pdf_export

		pdf_export.clear_cached_pdf(session_uuid)
	except Exception:
		pass

	frappe.enqueue(
		"frappe_profiler.analyze.run",
		queue="long",
		session_uuid=session_uuid,
	)

	return {"retried": True, "session_uuid": session_uuid, "docname": doc["name"]}
