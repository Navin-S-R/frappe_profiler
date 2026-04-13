# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Capture module: pyinstrument integration and sidecar wraps.

Owns:
  1. Optional pyinstrument import (degrades gracefully if unavailable)
  2. Per-recording pyinstrument session start/stop helpers
  3. Three monkey-patched sidecar wraps on frappe.get_doc /
     frappe.cache.get_value / frappe.permissions.has_permission for
     argument capture (with PII-safe hashing)

PII safety: argument values that may contain user data are stored in
two forms — `identifier_raw` (used only by the raw report) and
`identifier_safe` (a sha256[:12] hash, used by the safe report and as
the bucket key for redundant-call detection). Doctype names and ptypes
are NOT hashed because they're schema-level identifiers, not data.

Activation gate: the sidecar wraps and pyinstrument start are gated on
the presence of `frappe.local._profiler_active_session_id`. That flag is
set by hooks_callbacks.before_request / before_job only when the session
meta has `capture_python_tree=True`. So the wraps' hot-path check is a
single attribute lookup; they never read Redis.
"""

import hashlib

# Optional dependency — capture degrades gracefully if pyinstrument is
# not installed (e.g. air-gapped environments, broken pip cache).
try:
	import pyinstrument  # noqa: F401

	_PYINSTRUMENT_AVAILABLE = True
except ImportError:
	_PYINSTRUMENT_AVAILABLE = False


def _hash_identifier(value) -> str:
	"""Return a deterministic 12-char sha256 hex prefix of `value`.

	None passes through as None (used by has_permission when name is omitted).
	"""
	if value is None:
		return None
	return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _identify_args(fn_name: str, args: tuple, kwargs: dict):
	"""Build (identifier_raw, identifier_safe) for one captured call.

	Sub-shapes per wrapped function:
	  - get_doc(doctype, name)        → ((doctype, name), (doctype, hash(name)))
	  - cache_get(key)                → (key, hash(key))
	  - has_permission(doctype, name=None, ptype="read")
	                                  → ((doctype, name, ptype),
	                                     (doctype, hash(name), ptype))
	"""
	if fn_name == "get_doc":
		doctype = args[0] if len(args) > 0 else kwargs.get("doctype")
		name = args[1] if len(args) > 1 else kwargs.get("name")
		return (doctype, name), (doctype, _hash_identifier(name))

	if fn_name == "cache_get":
		key = args[0] if len(args) > 0 else kwargs.get("key")
		return key, _hash_identifier(key)

	if fn_name == "has_permission":
		doctype = args[0] if len(args) > 0 else kwargs.get("doctype")
		name = args[1] if len(args) > 1 else kwargs.get("doc_name")
		ptype = args[2] if len(args) > 2 else kwargs.get("ptype", "read")
		return (doctype, name, ptype), (doctype, _hash_identifier(name), ptype)

	# Unknown — return raw passthrough; should not happen in practice.
	return (args, kwargs), (args, kwargs)


# Maximum entries per recording's sidecar list. Above this, additional
# wraps drop their entries silently and set a truncation flag on the
# request-local context. The analyze pipeline surfaces this as a warning.
SIDECAR_CAP_PER_RECORDING = 50_000


def _make_wrap(orig, fn_name: str, local_proxy=None):
	"""Build a sidecar-recording wrapper around `orig`.

	`local_proxy` is the request-local namespace where we read the
	activation flag and append entries. In production this is `frappe.local`;
	tests pass a stand-in object so the wrap can be exercised without a
	Frappe runtime.

	Properties:
	  - Passthrough when no active session (single attribute lookup).
	  - Records entries on success AND on exception (try/finally).
	  - Re-entrant call into another wrap from inside one wrap is a
	    passthrough (prevents double-counting `has_permission` → `get_doc`).
	  - Drops entries past SIDECAR_CAP_PER_RECORDING and flags truncation.
	  - Stores the original on `wrapped._profiler_original` so uninstall
	    can restore it. If `orig` is itself an already-wrapped function
	    (has `_profiler_original`), our wrap chains through `orig` —
	    we never double-wrap.
	"""
	def wrapped(*args, **kwargs):
		active = getattr(local_proxy, "_profiler_active_session_id", None)
		if not active:
			return orig(*args, **kwargs)

		in_wrap = getattr(local_proxy, "_profiler_in_wrap", False)
		if in_wrap:
			return orig(*args, **kwargs)

		# Set re-entrancy flag BEFORE doing any work so nested wrapped
		# calls (e.g. has_permission → get_doc) skip recording.
		local_proxy._profiler_in_wrap = True

		# Build the sidecar entry on a best-effort basis. A failure here
		# (malformed args, exotic types) MUST NOT prevent the user's call
		# from running — observability code never breaks the host call.
		try:
			identifier_raw, identifier_safe = _identify_args(fn_name, args, kwargs)
			entry = {
				"fn_name": fn_name,
				"identifier_raw": identifier_raw,
				"identifier_safe": identifier_safe,
			}
		except Exception:
			entry = None

		try:
			return orig(*args, **kwargs)
		finally:
			local_proxy._profiler_in_wrap = False
			if entry is not None:
				sidecar = getattr(local_proxy, "profiler_sidecar", None)
				if sidecar is None:
					local_proxy.profiler_sidecar = [entry]
				elif len(sidecar) >= SIDECAR_CAP_PER_RECORDING:
					local_proxy.profiler_sidecar_truncated = True
				else:
					sidecar.append(entry)

	wrapped._profiler_original = orig
	return wrapped


# Default pyinstrument sample interval in milliseconds. Overridable via
# site_config.json: profiler_sampler_interval_ms. 1ms is pyinstrument's
# default and balances fidelity vs overhead well.
DEFAULT_SAMPLER_INTERVAL_MS = 1


def _start_pyi_session(local_proxy, interval_ms: int = DEFAULT_SAMPLER_INTERVAL_MS):
	"""Start a pyinstrument profiler scoped to this request.

	Stores the running profiler on `local_proxy.profiler_pyinstrument` so
	`after_request`/`after_job` can stop and serialize it. Returns the
	profiler instance, or None if pyinstrument is not available.

	Note: pyinstrument is imported inside the try-except so a broken
	install (rare, but possible in air-gapped environments) doesn't
	break app load. The module-level _PYINSTRUMENT_AVAILABLE flag is the
	authoritative check.
	"""
	if not _PYINSTRUMENT_AVAILABLE:
		return None
	try:
		from pyinstrument import Profiler

		# pyinstrument expects interval in seconds (float)
		prof = Profiler(interval=interval_ms / 1000.0, async_mode="enabled")
		prof.start()
		local_proxy.profiler_pyinstrument = prof
		return prof
	except Exception:
		# Any failure to start pyinstrument is non-fatal — degrade to
		# SQL-only capture for this recording.
		return None


def _force_stop_inflight_capture(local_proxy):
	"""Stop any in-flight pyinstrument session and clear all capture state.

	Called by api.start() (and the underlying _stop_session) before
	flipping the active flag, so a previous in-flight capture from the
	same worker doesn't leak into the new session.
	"""
	prof = getattr(local_proxy, "profiler_pyinstrument", None)
	if prof is not None:
		try:
			prof.stop()
		except Exception:
			pass
		try:
			delattr(local_proxy, "profiler_pyinstrument")
		except AttributeError:
			pass

	for attr in (
		"_profiler_active_session_id",
		"profiler_sidecar",
		"profiler_sidecar_truncated",
		"_profiler_in_wrap",
	):
		try:
			delattr(local_proxy, attr)
		except AttributeError:
			pass


# ----- Wrap installation on the real frappe modules -----------------------
#
# Installed once at app import time from frappe_profiler/__init__.py.
# install_wraps() is idempotent: calling it twice does not double-wrap,
# and pre-existing wraps from other apps are detected via the
# _profiler_original attribute convention.


def _wrap_targets():
	"""Return the list of (module, attr_name, fn_name) tuples to wrap.

	Lazy so that importing capture.py does not import frappe.permissions
	(which would trigger a circular import at app load on some sites).
	"""
	import frappe
	import frappe.permissions

	return [
		(frappe, "get_doc", "get_doc"),
		(frappe.cache, "get_value", "cache_get"),
		(frappe.permissions, "has_permission", "has_permission"),
	]


def install_wraps():
	"""Install all three sidecar wraps. Idempotent.

	If `frappe.get_doc` is already a `_profiler_is_our_wrap`-tagged wrapper,
	we do not double-wrap.
	"""
	import frappe

	for module, attr, fn_name in _wrap_targets():
		current = getattr(module, attr)
		if getattr(current, "_profiler_is_our_wrap", False):
			continue  # already wrapped by us
		new_wrap = _make_wrap(current, fn_name, local_proxy=frappe.local)
		new_wrap._profiler_is_our_wrap = True
		setattr(module, attr, new_wrap)


def uninstall_wraps():
	"""Restore originals. Used by before_uninstall and tests."""
	for module, attr, fn_name in _wrap_targets():
		current = getattr(module, attr)
		if getattr(current, "_profiler_is_our_wrap", False):
			setattr(module, attr, current._profiler_original)
