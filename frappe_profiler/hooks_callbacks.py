# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Frappe lifecycle hook callbacks.

These functions are wired into `before_request` / `after_request` (and in
Phase 2, `before_job` / `after_job`) via `hooks.py`. Their job is to:

1. Decide whether the current request belongs to an active profiler session.
2. If yes, activate `frappe.recorder` for this request only (per-user
   isolation — other users' concurrent traffic is NOT recorded).
3. After the request, register the new recording UUID with the session
   so the analyze pipeline can find all recordings that belong to a flow.

Activation strategy
-------------------
We do NOT call `frappe.recorder.start()`, because that flips a global flag
that records every request from every user. Instead, we directly invoke
`frappe.recorder.record(force=True)` only when the current user has an
active profiler session.

Hook order
----------
Frappe loads `frappe`'s own hooks first, then app hooks. So on each request:

  1. `frappe.recorder.record()`        — frappe's own (no-op without flag)
  2. `frappe_profiler.hooks_callbacks.before_request()` — ours, may activate
  3. <request handling>
  4. `frappe.recorder.dump()`          — frappe's own, dumps the recorder
                                          we activated
  5. `frappe_profiler.hooks_callbacks.after_request()` — ours, registers
                                          the new UUID with the session

Step 4 happens before step 5 because frappe's `after_request` runs before
ours (loaded first). That ordering is essential — we need the recording
to be dumped to Redis before we ask the analyze pipeline to fetch it later.
"""

import frappe
import frappe.recorder  # imported at module top so function-local `import frappe.recorder` doesn't rebind `frappe` as a local variable (Python scope rule: any `import frappe.X` inside a function makes `frappe` a function-local for the entire scope, breaking earlier `frappe.local` reads — caused by Python 3.14 stricter scope detection on a pre-existing pattern)

from frappe_profiler import capture as _capture
from frappe_profiler import session


def before_request(*args, **kwargs):
	"""Activate the recorder if the current user has an active profiler session.

	Runs on every HTTP request. The hot path (no active session) is one
	Redis GET — `get_active_session_for(user)` — and an early return.
	"""
	try:
		# Round 2 fix #6: if the analyze pipeline itself is running (it
		# does DB writes to the Profiler Session doctype, which may
		# trigger nested queries), don't recursively activate recording
		# on those internal operations. The flag is set at the top of
		# analyze.run() and cleared in its finally block.
		if getattr(frappe.local, "profiler_analyzing", False):
			return

		user = getattr(frappe.session, "user", None)
		if not user or user == "Guest":
			return

		session_uuid = session.get_active_session_for(user)
		if not session_uuid:
			return  # 99.9% of requests exit here

		# Tag the request so after_request can pick it up.
		frappe.local.profiler_session_id = session_uuid

		# If frappe's own recorder already activated (someone has the
		# standalone Recorder UI running globally), piggyback on its
		# instance — do NOT create a second Recorder because that would
		# overwrite frappe.local._recorder and orphan the first one's
		# SQL patch, corrupting both recordings.
		if getattr(frappe.local, "_recorder", None) is not None:
			return

		# Force-activate the existing recorder for THIS request only.
		# We pass force=True so the recorder runs regardless of the
		# global RECORDER_INTERCEPT_FLAG, leaving the standalone
		# Recorder UI's flag untouched.
		# (frappe.recorder is imported at module top — see comment there.)
		frappe.recorder.record(force=True)

		# v0.3.0: gate the new pyinstrument + sidecar capture on the
		# session's capture_python_tree flag. Setting
		# _profiler_active_session_id is what activates the wraps
		# (they gate on its presence in their hot path). When the
		# flag is False we leave it unset and SQL-only capture proceeds.
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("capture_python_tree", True):
			frappe.local._profiler_active_session_id = session_uuid
			_capture._start_pyi_session(
				local_proxy=frappe.local,
				interval_ms=int(
					frappe.conf.get("profiler_sampler_interval_ms")
					or _capture.DEFAULT_SAMPLER_INTERVAL_MS
				),
			)

		# v0.5.0: snapshot infra metrics at the start of the request.
		# The matching after_request snapshot diffs against this and
		# stores the result under profiler:infra:<recording_uuid>.
		try:
			from frappe_profiler import infra_capture
			frappe.local.profiler_infra_start = infra_capture.snapshot()
		except Exception:
			frappe.log_error(title="frappe_profiler infra start snapshot")
	except Exception:
		# Never let a profiler bug break a customer request. Log and move on.
		frappe.log_error(title="frappe_profiler before_request")


def after_request(*args, **kwargs):
	"""Register the dumped recording UUID with the active profiler session.

	Runs after `frappe.recorder.dump()` (which writes the recording into
	`RECORDER_REQUEST_HASH`). We just need to remember which session this
	recording belongs to.
	"""
	try:
		session_uuid = getattr(frappe.local, "profiler_session_id", None)
		if not session_uuid:
			return

		recorder = getattr(frappe.local, "_recorder", None)
		if recorder is None:
			return

		recording_uuid = getattr(recorder, "uuid", None)
		if not recording_uuid:
			return

		# Pass the current user so register_recording can refresh the
		# active-session TTL (Round 2 fix #2) without re-reading meta.
		user = getattr(frappe.session, "user", None)
		registered = session.register_recording(
			session_uuid, recording_uuid, user=user
		)
		if not registered:
			# Cap hit — the recording is in RECORDER_REQUEST_HASH but not
			# registered against our session. The cap_warning is already
			# written to session meta; log here so the drop is visible
			# in the error log / journalctl for debugging.
			frappe.logger().warning(
				f"frappe_profiler: recording cap hit for session "
				f"{session_uuid}, dropped {recording_uuid}"
			)
	except Exception:
		frappe.log_error(title="frappe_profiler after_request")
	finally:
		# v0.3.0: dump pyinstrument session and sidecar log to Redis under
		# per-recording-UUID keys. Best-effort — failures here log but
		# never break the request.
		recording_uuid_for_dump = getattr(
			getattr(frappe.local, "_recorder", None), "uuid", None
		)
		_dump_capture_state_to_redis(recording_uuid=recording_uuid_for_dump)

		# v0.5.0: write the infra diff to Redis for this recording.
		# Consumed by the infra_pressure analyzer at analyze time.
		try:
			start_snap = getattr(frappe.local, "profiler_infra_start", None)
			if start_snap and recording_uuid_for_dump:
				from frappe_profiler import infra_capture
				end_snap = infra_capture.snapshot()
				frappe.cache.set_value(
					f"profiler:infra:{recording_uuid_for_dump}",
					infra_capture.diff(start_snap, end_snap),
					expires_in_sec=session.SESSION_TTL_SECONDS,
				)
		except Exception:
			frappe.log_error(title="frappe_profiler infra end snapshot")

		# v0.5.0: correlation header for profiler_frontend.js. Must set
		# Access-Control-Expose-Headers or browsers will refuse to surface
		# the custom header to JavaScript, even for same-origin requests.
		try:
			if recording_uuid_for_dump:
				_inject_correlation_header(recording_uuid_for_dump)
		except Exception:
			frappe.log_error(title="frappe_profiler header injection")

		# Clear the per-request markers so they don't leak across requests
		# (frappe.local is per-request anyway, but explicit is good).
		if hasattr(frappe.local, "profiler_session_id"):
			del frappe.local.profiler_session_id
		if hasattr(frappe.local, "profiler_infra_start"):
			try:
				delattr(frappe.local, "profiler_infra_start")
			except AttributeError:
				pass


# ---------------------------------------------------------------------------
# Background job hooks (Phase 2)
# ---------------------------------------------------------------------------
# These mirror the request hooks above but use the `_profiler_session_id`
# kwarg injected by the frappe.enqueue monkey-patch in __init__.py to
# decide whether to activate recording for this job.
#
# Hook order on each job (frappe loads first, our app loads after):
#
#   1. frappe.recorder.record       (frappe's own — no-op without flag)
#   2. frappe.monitor.start         (frappe's own — unrelated)
#   3. frappe_profiler.before_job   (ours — may activate via force=True)
#   4. <method runs>
#   5. frappe.recorder.dump         (frappe's own — dumps the recorder we activated)
#   6. frappe.monitor.stop          (frappe's own — unrelated)
#   7. frappe.utils.file_lock.release_document_locks
#   8. frappe_profiler.after_job    (ours — registers UUID with session)
#
# The kwargs dict is passed by reference from frappe.utils.background_jobs.execute_job,
# so popping `_profiler_session_id` here removes it from the dict that the
# user's method will receive. This is essential — without popping, methods
# whose signatures don't include **kwargs would crash with an unexpected
# keyword argument error.


def before_job(method=None, kwargs=None, **rest):
	"""Activate the recorder if this job belongs to an active profiler session."""
	try:
		# Round 2 fix #6: don't recurse into our own analyze job. The
		# analyze.run job sets frappe.local.profiler_analyzing = True
		# at its top, so we exit here if this job IS our analyze.
		if getattr(frappe.local, "profiler_analyzing", False):
			return

		if kwargs is None:
			return
		if not isinstance(kwargs, dict):
			# Unexpected — frappe should always pass a dict. Log once so
			# we can debug if it ever happens in practice.
			frappe.log_error(
				title="frappe_profiler before_job unexpected kwargs",
				message=f"kwargs type: {type(kwargs).__name__}, method: {method}",
			)
			return

		# Pop our marker so the user's method doesn't see it. This MUST
		# happen regardless of whether we proceed to activate recording —
		# if we leave the marker in kwargs, the user's method will be
		# called with an unexpected keyword argument and crash.
		session_uuid = kwargs.pop("_profiler_session_id", None)
		if not session_uuid:
			return

		# frappe.set_user(user) was already called by execute_job before
		# this hook fires, so frappe.session.user is the originating user.
		user = getattr(frappe.session, "user", None)
		if not user or user == "Guest":
			return

		# Verify the session is still active for this user. If the user
		# stopped the session (or started a new one) between enqueue and
		# run, the session ID in our kwargs no longer matches the active
		# pointer — drop the recording silently.
		active = session.get_active_session_for(user)
		if active != session_uuid:
			return

		# All checks passed — activate the recorder for this job.
		frappe.local.profiler_session_id = session_uuid

		# Same clobber protection as before_request: if the standalone
		# recorder already activated (global flag + frappe's own
		# before_job hook), piggyback on its instance.
		if getattr(frappe.local, "_recorder", None) is not None:
			return

		# (frappe.recorder is imported at module top — see comment there.)
		frappe.recorder.record(force=True)

		# v0.3.0: gate the new pyinstrument + sidecar capture on the
		# session's capture_python_tree flag. Mirrors before_request.
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("capture_python_tree", True):
			frappe.local._profiler_active_session_id = session_uuid
			_capture._start_pyi_session(
				local_proxy=frappe.local,
				interval_ms=int(
					frappe.conf.get("profiler_sampler_interval_ms")
					or _capture.DEFAULT_SAMPLER_INTERVAL_MS
				),
			)

		# v0.5.0: snapshot infra metrics at job start. Mirrors before_request.
		try:
			from frappe_profiler import infra_capture
			frappe.local.profiler_infra_start = infra_capture.snapshot()
		except Exception:
			frappe.log_error(title="frappe_profiler infra start snapshot (job)")
	except Exception:
		frappe.log_error(title="frappe_profiler before_job")


def after_job(method=None, kwargs=None, result=None, **rest):
	"""Register the dumped recording UUID with the active profiler session."""
	try:
		session_uuid = getattr(frappe.local, "profiler_session_id", None)
		if not session_uuid:
			return

		recorder = getattr(frappe.local, "_recorder", None)
		if recorder is None:
			return

		recording_uuid = getattr(recorder, "uuid", None)
		if not recording_uuid:
			return

		user = getattr(frappe.session, "user", None)
		registered = session.register_recording(
			session_uuid, recording_uuid, user=user
		)
		if not registered:
			frappe.logger().warning(
				f"frappe_profiler: recording cap hit for session "
				f"{session_uuid}, dropped job {recording_uuid}"
			)
	except Exception:
		frappe.log_error(title="frappe_profiler after_job")
	finally:
		recording_uuid_for_dump = getattr(
			getattr(frappe.local, "_recorder", None), "uuid", None
		)
		_dump_capture_state_to_redis(recording_uuid=recording_uuid_for_dump)

		# v0.5.0: write the infra diff to Redis for this job's recording.
		# No correlation header to inject — background jobs have no HTTP
		# response, and no browser to correlate with.
		try:
			start_snap = getattr(frappe.local, "profiler_infra_start", None)
			if start_snap and recording_uuid_for_dump:
				from frappe_profiler import infra_capture
				end_snap = infra_capture.snapshot()
				frappe.cache.set_value(
					f"profiler:infra:{recording_uuid_for_dump}",
					infra_capture.diff(start_snap, end_snap),
					expires_in_sec=session.SESSION_TTL_SECONDS,
				)
		except Exception:
			frappe.log_error(title="frappe_profiler infra end snapshot (job)")

		if hasattr(frappe.local, "profiler_session_id"):
			del frappe.local.profiler_session_id
		if hasattr(frappe.local, "profiler_infra_start"):
			try:
				delattr(frappe.local, "profiler_infra_start")
			except AttributeError:
				pass


def _dump_capture_state_to_redis(recording_uuid: str | None) -> None:
	"""Serialize the in-flight pyinstrument session and sidecar list to Redis.

	Called from after_request / after_job after the recorder has dumped
	its own SQL recording. The two new Redis keys are:

	  profiler:tree:<recording_uuid>      → pickle.dumps(pyi.last_session)
	  profiler:sidecar:<recording_uuid>   → list[dict]

	Both inherit the same TTL semantics as RECORDER_REQUEST_HASH (cleaned
	up at the end of analyze, or expire naturally if analyze never runs).

	Best-effort: failures here log but never break the request.
	"""
	import pickle

	from frappe_profiler.session import SESSION_TTL_SECONDS

	if not recording_uuid:
		# No recorder ran on this request — nothing to dump.
		_clear_capture_locals()
		return

	prof = getattr(frappe.local, "profiler_pyinstrument", None)
	if prof is not None:
		try:
			prof.stop()
			tree_blob = pickle.dumps(prof.last_session)
			frappe.cache.set_value(
				f"profiler:tree:{recording_uuid}",
				tree_blob,
				expires_in_sec=SESSION_TTL_SECONDS,
			)
		except Exception:
			frappe.log_error(title="frappe_profiler pyi dump")

	sidecar = getattr(frappe.local, "profiler_sidecar", None)
	if sidecar:
		try:
			# If the sidecar was truncated, append a marker entry so the
			# analyze pipeline can surface a warning even if the
			# truncation flag itself is lost across the Redis hop.
			payload = list(sidecar)
			if getattr(frappe.local, "profiler_sidecar_truncated", False):
				payload.append({"_truncated": True})
			frappe.cache.set_value(
				f"profiler:sidecar:{recording_uuid}",
				payload,
				expires_in_sec=SESSION_TTL_SECONDS,
			)
		except Exception:
			frappe.log_error(title="frappe_profiler sidecar dump")

	_clear_capture_locals()


def _clear_capture_locals() -> None:
	"""Clear all v0.3.0 capture state from frappe.local. Idempotent."""
	for attr in (
		"profiler_pyinstrument",
		"profiler_sidecar",
		"profiler_sidecar_truncated",
		"_profiler_active_session_id",
		"_profiler_in_wrap",
	):
		if hasattr(frappe.local, attr):
			try:
				delattr(frappe.local, attr)
			except AttributeError:
				pass


# ---------------------------------------------------------------------------
# v0.5.0: correlation header for frontend metrics
# ---------------------------------------------------------------------------
# The X-Profiler-Recording-Id header is read by the browser-side
# profiler_frontend.js shim to tie each XHR timing back to a specific
# server recording. Without the Access-Control-Expose-Headers entry,
# browsers refuse to surface custom response headers to JavaScript
# even for same-origin requests — it's the most common frontend
# instrumentation failure mode.


def _inject_correlation_header(recording_uuid: str) -> None:
	"""Attach X-Profiler-Recording-Id to the outgoing response + expose it
	via Access-Control-Expose-Headers. Called from after_request during
	an active profiler session. Idempotent and safe to call in non-HTTP
	contexts (no-op if frappe.local has no response_headers)."""
	headers = getattr(frappe.local, "response_headers", None)
	if headers is None:
		return

	headers["X-Profiler-Recording-Id"] = recording_uuid

	try:
		existing = headers.get("Access-Control-Expose-Headers") or ""
	except Exception:
		existing = ""
	if "X-Profiler-Recording-Id" not in existing:
		merged = (
			existing + ", X-Profiler-Recording-Id"
			if existing
			else "X-Profiler-Recording-Id"
		)
		headers["Access-Control-Expose-Headers"] = merged
