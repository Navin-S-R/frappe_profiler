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
		import frappe.recorder

		frappe.recorder.record(force=True)

		# v0.3.0: gate the new pyinstrument + sidecar capture on the
		# session's capture_python_tree flag. Setting
		# _profiler_active_session_id is what activates the wraps
		# (they gate on its presence in their hot path). When the
		# flag is False we leave it unset and SQL-only capture proceeds.
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("capture_python_tree", True):
			frappe.local._profiler_active_session_id = session_uuid
			from frappe_profiler import capture as _capture

			interval_ms = int(
				frappe.conf.get("profiler_sampler_interval_ms")
				or _capture.DEFAULT_SAMPLER_INTERVAL_MS
			)
			_capture._start_pyi_session(
				local_proxy=frappe.local, interval_ms=interval_ms
			)
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
		# Clear the per-request marker so it doesn't leak across requests
		# (frappe.local is per-request anyway, but explicit is good).
		if hasattr(frappe.local, "profiler_session_id"):
			del frappe.local.profiler_session_id


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

		import frappe.recorder

		frappe.recorder.record(force=True)

		# v0.3.0: gate the new pyinstrument + sidecar capture on the
		# session's capture_python_tree flag. Mirrors before_request.
		meta = session.get_session_meta(session_uuid) or {}
		if meta.get("capture_python_tree", True):
			frappe.local._profiler_active_session_id = session_uuid
			from frappe_profiler import capture as _capture

			interval_ms = int(
				frappe.conf.get("profiler_sampler_interval_ms")
				or _capture.DEFAULT_SAMPLER_INTERVAL_MS
			)
			_capture._start_pyi_session(
				local_proxy=frappe.local, interval_ms=interval_ms
			)
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
		if hasattr(frappe.local, "profiler_session_id"):
			del frappe.local.profiler_session_id
