# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Cached reader for Profiler Settings.

Every request goes through ``hooks_callbacks.before_request`` which
checks ``is_enabled()`` — reading the Single doc directly on every
request would be a DB hit per request. We cache the resolved config
in Redis with a version key; the DocType controller bumps the key
on save.

Falls back to ``frappe.conf`` for thresholds when the setting is
unset or the DocType row doesn't exist yet (fresh install, pre-
migration). That preserves the pre-v0.5.2 behavior where thresholds
lived in ``site_config.json``.
"""

from dataclasses import dataclass, field

# NOTE: frappe is imported lazily inside each function rather than at
# module top. Importing at top forces every unit test that touches
# ``from frappe_profiler import settings`` to run under bench, which
# would exclude pure-logic tests from the plain-pytest path. All
# runtime callers (hooks, analyzers) import inside a Frappe request
# context, so the lazy import is free there.


# Frozen default values. Matches the constants that USED to live in
# individual analyzers before v0.5.2 centralized configuration. Kept
# in this module so analyzers never have to know about DocType shape.
#
# v0.5.2 round 4: cache threshold bumped from 10 → 50. We don't time
# individual cache lookups (sidecar captures name + args, not wall
# time), so every Redundant Cache finding reports impact=0ms. A 10×
# loop at 0ms impact is indistinguishable from framework background
# noise; a 50× loop is a clear pattern. 50 also matches the
# severity=High multiplier, so low-count loops that DID previously
# emit at Medium (with unknown impact) were the worst kind of noise.
_DEFAULTS = {
	"enabled": True,
	"session_retention_days": 30,
	"tracked_apps": (),  # tuple, not list — immutable for caching
	"redundant_doc_threshold": 5,
	"redundant_cache_threshold": 50,
	"redundant_perm_threshold": 10,
	"n_plus_one_min_occurrences": 10,
}

# Keys we also accept from site_config.json for backwards compatibility
# with the pre-v0.5.2 pattern of tuning thresholds without a DocType.
# The DocType wins if both are set.
_SITE_CONFIG_FALLBACK = {
	"redundant_doc_threshold": "profiler_redundant_doc_threshold",
	"redundant_cache_threshold": "profiler_redundant_cache_threshold",
	"redundant_perm_threshold": "profiler_redundant_perm_threshold",
	"n_plus_one_min_occurrences": "profiler_n_plus_one_threshold",
}


@dataclass(frozen=True)
class ProfilerConfig:
	"""Snapshot of resolved profiler configuration.

	Frozen so a single instance can be safely cached and handed to
	multiple analyzers without copy-on-read concerns.
	"""

	enabled: bool = True
	session_retention_days: int = 30
	tracked_apps: tuple[str, ...] = field(default_factory=tuple)
	redundant_doc_threshold: int = 5
	redundant_cache_threshold: int = 10
	redundant_perm_threshold: int = 10
	n_plus_one_min_occurrences: int = 10


_CACHE_KEY = "profiler_settings_cached"


def _read_doctype_row() -> dict | None:
	"""Load the Single doc's field dict, or None if the DocType doesn't
	exist yet (fresh install / pre-migration).

	We use ``get_single_value`` per-field instead of ``get_single`` so
	we can degrade cleanly when ``Profiler Settings`` isn't yet in the
	schema — some deployments install the app but haven't migrated.
	"""
	import frappe
	try:
		# Short-circuit: if the DocType row doesn't exist, fall back
		# to defaults instead of raising from inside get_single_value.
		if not frappe.db.exists("DocType", "Profiler Settings"):
			return None
	except Exception:
		# frappe.db unavailable (e.g. schema still loading) — defaults.
		return None

	try:
		doc = frappe.get_cached_doc("Profiler Settings")
	except Exception:
		return None

	return {
		"enabled": bool(doc.get("enabled", 1)),
		"session_retention_days": int(doc.get("session_retention_days") or 30),
		"tracked_apps": tuple(
			(row.app_name or "").strip()
			for row in (doc.get("tracked_apps") or [])
			if (row.app_name or "").strip()
		),
		"redundant_doc_threshold": int(doc.get("redundant_doc_threshold") or 0) or None,
		"redundant_cache_threshold": int(doc.get("redundant_cache_threshold") or 0) or None,
		"redundant_perm_threshold": int(doc.get("redundant_perm_threshold") or 0) or None,
		"n_plus_one_min_occurrences": int(doc.get("n_plus_one_min_occurrences") or 0) or None,
	}


def _site_conf_fallback(key: str) -> int | None:
	"""Return the site_config.json override for a threshold, if set."""
	conf_key = _SITE_CONFIG_FALLBACK.get(key)
	if not conf_key:
		return None
	import frappe
	try:
		v = frappe.conf.get(conf_key)
		if v is None:
			return None
		return int(v)
	except (TypeError, ValueError, AttributeError):
		return None


def _resolve() -> ProfilerConfig:
	"""Build a fresh config snapshot from DocType + site_config + defaults.

	Precedence: DocType row > site_config.json > hardcoded default.
	"""
	row = _read_doctype_row() or {}

	def _threshold(key: str) -> int:
		# DocType wins if non-zero.
		v = row.get(key)
		if v:
			return int(v)
		# Fallback to site_config.json.
		sc = _site_conf_fallback(key)
		if sc is not None:
			return sc
		# Hardcoded default.
		return int(_DEFAULTS[key])

	return ProfilerConfig(
		enabled=bool(row.get("enabled", _DEFAULTS["enabled"])),
		session_retention_days=int(
			row.get("session_retention_days") or _DEFAULTS["session_retention_days"]
		),
		tracked_apps=tuple(row.get("tracked_apps") or ()),
		redundant_doc_threshold=_threshold("redundant_doc_threshold"),
		redundant_cache_threshold=_threshold("redundant_cache_threshold"),
		redundant_perm_threshold=_threshold("redundant_perm_threshold"),
		n_plus_one_min_occurrences=_threshold("n_plus_one_min_occurrences"),
	)


def get_config() -> ProfilerConfig:
	"""Return the resolved config, cached in Redis until the Single is
	saved (controller's on_update deletes the cache key).

	Fails soft — on ANY exception during lookup (including Frappe not
	being importable in unit-test contexts), returns the hardcoded
	defaults. The profiler must never crash a request because of a
	settings read, especially on bench startup before Redis is warm.
	"""
	try:
		import frappe
	except ImportError:
		# Unit-test path — no bench context.
		return ProfilerConfig()

	try:
		cached = frappe.cache.get_value(_CACHE_KEY)
		if cached is not None:
			return ProfilerConfig(**cached)
	except Exception:
		pass

	try:
		cfg = _resolve()
	except Exception:
		return ProfilerConfig()

	try:
		frappe.cache.set_value(_CACHE_KEY, cfg.__dict__)
	except Exception:
		pass

	return cfg


def is_enabled() -> bool:
	"""Convenience wrapper — hot-path entry point from hooks_callbacks."""
	try:
		return get_config().enabled
	except Exception:
		# Fail open: if we can't read the setting, don't silently
		# disable the profiler — that would be a very confusing
		# support issue ("why isn't recording working"). Default to
		# on, matching the DocType default.
		return True


def get_tracked_apps() -> tuple[str, ...]:
	"""Allowlist of user apps. Empty tuple → no override (use the
	built-in FRAMEWORK_APPS exclusion list).

	Called by ``is_framework_callsite`` to flip the classifier from
	exclusion-mode (framework = frappe/erpnext/…) to inclusion-mode
	(user code = exactly the tracked apps).
	"""
	try:
		return get_config().tracked_apps
	except Exception:
		return ()
