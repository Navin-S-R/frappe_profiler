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

# v0.5.1: Skip-list for instrumentation-noise endpoints. Any HTTP request
# whose ``cmd`` starts with one of these prefixes is NOT recorded against
# the user's active profiler session, even though everything else about
# the request (auth, user, active session) would normally mean "record me".
#
# Why: these are the profiler's own widget-polling calls and Frappe's own
# Recorder doctype whitelisted methods. They fire during an active session
# but represent instrumentation overhead, not application work the user is
# trying to optimize. Capturing them pollutes:
#
#   - per_action rows (N extra "frappe_profiler.api.status" rows per session)
#   - top_queries / table_breakdown (queries from status polling / recorder
#     lookups obscure the real hot spots)
#   - the auto-generated "Steps to Reproduce" bullet list — the widget
#     polls every ~2s while Recording, so a 30-second flow ends up with
#     a reproducer list that's 90% status polls
#   - total wall-clock / total query totals on the Profiler Session form
#
# Filtered at capture time (before_request) rather than at display time so
# ALL downstream analyzers see a clean recording list, not just auto-notes.
_IGNORED_CMD_PREFIXES = (
	# The profiler's own whitelisted API — widget poll, metrics submit,
	# retry, fetch, etc. None of these represent real application work.
	"frappe_profiler.api.",
	# Frappe's built-in Recorder doctype. If the user has the Recorder UI
	# open in another tab while profiling (not uncommon — devs often have
	# both tools handy), its whitelisted calls (export_data, delete,
	# get_request_details, pluck, start, stop) would otherwise be captured
	# and attributed to the profiling session. They're the recorder's own
	# plumbing, not user code.
	"frappe.core.doctype.recorder.recorder.",
)


def _extract_cmd_from_request() -> str:
	"""Resolve the whitelisted-method name for the current HTTP request,
	or "" if we can't determine one.

	Two sources, checked in order:

	1. ``frappe.local.form_dict.cmd`` — set by ``make_form_dict`` for
	   legacy ``?cmd=foo.bar`` RPC calls. Available at before_request
	   time because ``make_form_dict`` runs BEFORE the hook dispatcher.

	2. ``frappe.local.request.path`` parsed for ``/method/<name>`` — the
	   only source for modern ``/api/method/foo.bar`` and
	   ``/api/v2/method/foo.bar`` URLs, because Frappe's REST API routing
	   (``handle_rpc_call`` in ``frappe/api/v1.py`` and ``v2.py``) only
	   calls ``frappe.form_dict.cmd = method`` AFTER the before_request
	   hooks have already fired. Before v0.5.1 the skip filter missed
	   every modern REST call because it only checked form_dict.cmd,
	   which was empty at hook time for these URLs.

	Works for both v1 and v2 API paths because both route shapes use
	``.../method/<name>`` — we find the substring ``/method/`` and take
	everything after it.

	Returns "" when neither source produces a non-empty cmd — the caller
	treats that as "don't skip" so that request-path-less contexts
	(OPTIONS preflights, health checks, pre-init edge cases) fall
	through to the normal path rather than being filtered.
	"""
	# Source 1: form_dict.cmd (legacy ?cmd=foo.bar, always set if present)
	try:
		form_dict = getattr(frappe.local, "form_dict", None)
		if isinstance(form_dict, dict):
			cmd = form_dict.get("cmd")
			if cmd:
				return cmd
	except Exception:
		pass

	# Source 2: parse out of request.path for /api/method/<foo> and
	# /api/v2/method/<foo>. Both use the substring "/method/".
	try:
		request = getattr(frappe.local, "request", None)
		if request is None:
			return ""
		path = getattr(request, "path", "") or ""
		marker = "/method/"
		idx = path.find(marker)
		if idx < 0:
			return ""
		rest = path[idx + len(marker):]
		# Defensive: strip any trailing slash and query string (werkzeug
		# already strips the query from .path, but trailing slashes are
		# not uncommon on REST URLs).
		rest = rest.split("?", 1)[0].rstrip("/")
		return rest
	except Exception:
		return ""


def _should_skip_request() -> bool:
	"""Return True if the current HTTP request is profiler / Frappe-recorder
	instrumentation noise that should not be captured into the session.

	Delegates to ``_extract_cmd_from_request`` for the method-name
	resolution (handles both legacy ``?cmd=foo`` and modern
	``/api/method/foo`` URL shapes), then does a prefix match against
	``_IGNORED_CMD_PREFIXES``. Non-method URLs (``/app/...``,
	``/api/resource/...``, static files) resolve to "" and fall through
	as 'not noise', which is the intended behavior — we only skip
	endpoints that the profiler / recorder EXPOSES via whitelisted
	methods, not general page loads or REST resource access.

	Defensive: any exception resolving the cmd falls through to False
	rather than raising, because crashing here would take down every
	request for every user with an active profiler session.
	"""
	cmd = _extract_cmd_from_request()
	if not cmd:
		return False
	for prefix in _IGNORED_CMD_PREFIXES:
		if cmd.startswith(prefix):
			return True
	return False


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

		# v0.5.1: Skip profiler / Frappe-recorder instrumentation noise.
		# Checked AFTER the active-session lookup (two Redis GETs gating a
		# string-prefix check is free) so there's zero cost on the 99.9%
		# path. See _IGNORED_CMD_PREFIXES for the full rationale.
		if _should_skip_request():
			return

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
		#
		# Gated on `profiler_session_id` specifically (not just the
		# recorder UUID) — the standalone Frappe Recorder UI may be
		# activated globally on this site, which sets frappe.local._recorder
		# and gives us a recording UUID, but THAT recording doesn't belong
		# to any profiler session. Injecting the header for non-session
		# traffic would:
		#   1. Pollute every response across the site with a useless header
		#   2. Cause profiler_frontend.js to buffer XHR timings tagged to
		#      a recording UUID that has no session to flush them to
		try:
			profiler_session_id = getattr(
				frappe.local, "profiler_session_id", None
			)
			if recording_uuid_for_dump and profiler_session_id:
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


_CORRELATION_HEADER_NAME = "X-Profiler-Recording-Id"


def _inject_correlation_header(recording_uuid: str) -> None:
	"""Attach X-Profiler-Recording-Id to the outgoing response + expose it
	via Access-Control-Expose-Headers. Called from after_request during
	an active profiler session. Idempotent and safe to call in non-HTTP
	contexts (no-op if frappe.local has no response_headers)."""
	headers = getattr(frappe.local, "response_headers", None)
	if headers is None:
		return

	headers[_CORRELATION_HEADER_NAME] = recording_uuid

	try:
		existing = headers.get("Access-Control-Expose-Headers") or ""
	except Exception:
		existing = ""

	# Token-by-token check, NOT a substring `in` check. A naive `in`
	# would falsely match when another app has already added
	# "X-Profiler-Recording-Id-Legacy" or similar — our real header
	# would then NOT be appended, the browser would refuse to surface
	# it to JavaScript, and the entire frontend correlation feature
	# would silently break. Split on commas and compare case-insensitively.
	tokens = {t.strip().lower() for t in existing.split(",") if t.strip()}
	if _CORRELATION_HEADER_NAME.lower() not in tokens:
		merged = (
			existing + ", " + _CORRELATION_HEADER_NAME
			if existing
			else _CORRELATION_HEADER_NAME
		)
		headers["Access-Control-Expose-Headers"] = merged
