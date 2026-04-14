# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Redis state for the profiler session lifecycle.

This module is intentionally pure state-management — no business logic, no
DocType I/O, no recorder coupling. It owns three Redis key shapes:

    profiler:active:<user_email>          → string, value=session_uuid, TTL
    profiler:session:<uuid>:meta          → hash with {started_at, user, label}
    profiler:session:<uuid>:recordings    → set of recording UUIDs

The active key has a TTL that matches the recorder's auto-disable so a
forgotten Stop button can never run forever. The meta and recordings keys
have no TTL — they live until the analyze pipeline finalizes the session
into a `Profiler Session` DocType row and explicitly deletes them.
"""

import frappe

# Match frappe.recorder.RECORDER_AUTO_DISABLE so a forgotten session
# auto-stops at the same point as the underlying recorder would.
SESSION_TTL_SECONDS = 10 * 60

# Hard cap on the number of recordings registered against a single session.
# Prevents pathological flows from filling Redis. Configurable per site
# via site_config.json: profiler_max_recordings_per_session
MAX_RECORDINGS_PER_SESSION = 200


def _active_key(user: str) -> str:
	return f"profiler:active:{user}"


def _meta_key(session_uuid: str) -> str:
	return f"profiler:session:{session_uuid}:meta"


def _recordings_key(session_uuid: str) -> str:
	return f"profiler:session:{session_uuid}:recordings"


# ----- active session pointer (per-user) -----------------------------------


def get_active_session_for(user: str) -> str | None:
	"""Return the active profiler session UUID for the given user, or None."""
	if not user or user == "Guest":
		return None
	value = frappe.cache.get_value(_active_key(user))
	if isinstance(value, bytes):
		return value.decode()
	return value


def set_active_session(user: str, session_uuid: str) -> None:
	"""Mark the user as currently recording into the given session.

	The active key carries a TTL so a forgotten Stop button auto-clears.
	"""
	frappe.cache.set_value(
		_active_key(user),
		session_uuid,
		expires_in_sec=SESSION_TTL_SECONDS,
	)


def clear_active_session(user: str) -> None:
	"""Clear the active session pointer for the user.

	Idempotent — safe to call when no session is active.
	"""
	frappe.cache.delete_value(_active_key(user))


# ----- session metadata ----------------------------------------------------


def set_session_meta(session_uuid: str, meta: dict) -> None:
	"""Store session metadata.

	Recognized keys (consumers may add more, but these are the canonical):
	  - session_uuid, docname, user, label, started_at  (set by api.start)
	  - cap_warning                                     (set by register_recording)
	  - capture_python_tree (bool)                      (v0.3.0+, set by api.start)

	The v0.3.0 capture_python_tree flag is read by hooks_callbacks
	before_request/before_job to decide whether to set
	frappe.local._profiler_active_session_id. When False, the new
	pyinstrument capture and sidecar wraps stay inert; SQL recording
	via frappe.recorder proceeds as usual.
	"""
	frappe.cache.set_value(_meta_key(session_uuid), meta)


def get_session_meta(session_uuid: str) -> dict | None:
	return frappe.cache.get_value(_meta_key(session_uuid))


# ----- session → recording UUIDs (set, append-only during recording) ------


def register_recording(
	session_uuid: str,
	recording_uuid: str,
	user: str | None = None,
) -> bool:
	"""Append a recording UUID to the session's set of recordings.

	Atomic via Redis SADD. Safe to call from multiple workers concurrently.

	Enforces MAX_RECORDINGS_PER_SESSION as a soft cap: if the cap is hit,
	the new recording is dropped and a flag is set on the session meta so
	the analyze pipeline can surface a warning to the customer. Returns
	True if registered, False if capped.

	Also refreshes the user's active-session TTL (see Round 2 fix #2):
	without this refresh, a long flow (e.g. 45 minutes of profiling)
	would silently stop at the 10-minute TTL boundary because the
	profiler:active:<user> key expired. By bumping the TTL on every
	register_recording, an actively-used session stays alive as long as
	there's traffic. If the user stops making requests, the key expires
	naturally 10 minutes later and the janitor cleans up.
	"""
	import frappe

	cap = frappe.conf.get("profiler_max_recordings_per_session") or MAX_RECORDINGS_PER_SESSION

	if recording_count(session_uuid) >= cap:
		# Set a one-time warning flag on the session meta
		meta = get_session_meta(session_uuid) or {}
		if not meta.get("cap_warning"):
			meta["cap_warning"] = (
				f"Hit the session recording cap ({cap}). "
				"Some recordings were dropped. Restart with a shorter flow."
			)
			set_session_meta(session_uuid, meta)
		return False

	frappe.cache.sadd(_recordings_key(session_uuid), recording_uuid)

	# Refresh the active-session TTL so long flows don't silently expire.
	# If the caller didn't pass a user, fall back to reading it from the
	# session meta — one extra Redis roundtrip in exchange for a safer
	# default.
	if not user:
		meta = get_session_meta(session_uuid) or {}
		user = meta.get("user")
	if user:
		frappe.cache.set_value(
			_active_key(user),
			session_uuid,
			expires_in_sec=SESSION_TTL_SECONDS,
		)

	return True


def get_recordings(session_uuid: str) -> list[str]:
	"""Return all recording UUIDs that belong to this session."""
	members = frappe.cache.smembers(_recordings_key(session_uuid)) or set()
	return sorted(m.decode() if isinstance(m, bytes) else m for m in members)


def recording_count(session_uuid: str) -> int:
	"""Return the count of recordings registered to this session."""
	return len(get_recordings(session_uuid))


# ----- cleanup -------------------------------------------------------------


def delete_session_state(session_uuid: str) -> None:
	"""Delete all Redis state for a finalized session.

	Called by the analyze pipeline once the session has been persisted to
	the `Profiler Session` DocType. Idempotent.
	"""
	frappe.cache.delete_value(_meta_key(session_uuid))
	frappe.cache.delete_value(_recordings_key(session_uuid))
	# v0.5.0: also clean up the frontend metrics blob written by
	# api.submit_frontend_metrics. Per-recording infra keys
	# (profiler:infra:<recording_uuid>) are cleaned up alongside
	# RECORDER_REQUEST_HASH entries when the analyze pipeline walks
	# the recording UUIDs, so they don't need a separate sweep here.
	frappe.cache.delete_value(f"profiler:frontend:{session_uuid}")
