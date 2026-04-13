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
