# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: detect redundant frappe.get_doc / cache.get_value / has_permission calls.

Reads the per-recording sidecar argument log captured by the wraps in
capture.py, buckets entries by (fn_name, identifier_safe), and emits
one Redundant Call finding per bucket whose count exceeds a configurable
threshold.

PII safety: the bucket key uses identifier_safe (sha256 hash), so we
never expose plaintext values to the safe-mode finding title. The
finding's technical_detail_json carries BOTH identifier_safe AND
identifier_raw so the renderer can show the appropriate form per mode.
"""

import json
from collections import Counter, defaultdict

from frappe_profiler.analyzers.base import AnalyzerResult


DEFAULT_REDUNDANT_DOC_THRESHOLD = 5
DEFAULT_REDUNDANT_CACHE_THRESHOLD = 10
DEFAULT_REDUNDANT_PERM_THRESHOLD = 10
DEFAULT_REDUNDANT_HIGH_MULTIPLIER = 5


def _conf_int(key: str, default: int) -> int:
	try:
		import frappe

		v = frappe.conf.get(key)
		if v is not None:
			return int(v)
	except Exception:
		pass
	return default


def _threshold_for(fn_name: str) -> int:
	if fn_name == "get_doc":
		return _conf_int("profiler_redundant_doc_threshold", DEFAULT_REDUNDANT_DOC_THRESHOLD)
	if fn_name == "cache_get":
		return _conf_int("profiler_redundant_cache_threshold", DEFAULT_REDUNDANT_CACHE_THRESHOLD)
	if fn_name == "has_permission":
		return _conf_int("profiler_redundant_perm_threshold", DEFAULT_REDUNDANT_PERM_THRESHOLD)
	return 999_999


def _title_for(fn_name: str, identifier_safe, count: int) -> str:
	if fn_name == "get_doc":
		doctype, name_hash = identifier_safe
		return f"Redundant doc fetch: {doctype} {name_hash} ({count} times)"
	if fn_name == "cache_get":
		return f"Redundant cache lookup: {identifier_safe} ({count} times)"
	if fn_name == "has_permission":
		doctype, name_hash, ptype = identifier_safe
		return f"Redundant permission check: {doctype} {name_hash} {ptype} ({count} times)"
	return f"Redundant call: {fn_name} ({count} times)"


def _customer_description_for(fn_name: str, count: int) -> str:
	if fn_name == "get_doc":
		return (
			f"The same document was fetched **{count} times** from the same "
			"line of code. This is almost always a loop that reloads a "
			"document inside its body — caching the result outside the loop "
			"would eliminate the redundant fetches."
		)
	if fn_name == "cache_get":
		return (
			f"The same cache key was looked up **{count} times** from the "
			"same callsite. Cache lookups are cheap individually but add up "
			"in a hot loop; reading once and re-using is the fix."
		)
	if fn_name == "has_permission":
		return (
			f"The same permission check ran **{count} times** from the same "
			"callsite. Permission checks involve role lookups and DocType "
			"validation — caching the result for the duration of the action "
			"is the standard fix."
		)
	return f"A function was called {count} times redundantly."


def _to_hashable(value):
	"""Convert nested lists to nested tuples so the value can be a dict key."""
	if isinstance(value, list):
		return tuple(_to_hashable(v) for v in value)
	if isinstance(value, tuple):
		return tuple(_to_hashable(v) for v in value)
	return value


def analyze(recordings: list, context) -> AnalyzerResult:
	# Bucket: (fn_name, identifier_safe_tuple) → list of (action_idx, raw)
	buckets: dict = defaultdict(list)
	truncation_seen = False
	skipped_unhashable = 0

	for action_idx, recording in enumerate(recordings):
		sidecar = recording.get("sidecar") or []
		for entry in sidecar:
			if not isinstance(entry, dict):
				continue
			if entry.get("_truncated"):
				truncation_seen = True
				continue
			fn_name = entry.get("fn_name")
			safe = entry.get("identifier_safe")
			raw = entry.get("identifier_raw")
			if fn_name is None or safe is None:
				continue
			# Convert lists to tuples so they're hashable as dict keys.
			# Defensive: if the conversion still leaves an unhashable value
			# (e.g. a dict slipped through capture._identify_args because
			# the wrapped function was called with an unexpected shape),
			# skip the entry rather than crash the whole analyzer.
			try:
				key = (fn_name, _to_hashable(safe))
				buckets[key].append((action_idx, raw))
			except TypeError:
				skipped_unhashable += 1
				continue

	if skipped_unhashable:
		context.warnings.append(
			f"redundant_calls: skipped {skipped_unhashable} sidecar entries "
			"with unhashable identifiers (likely dict-arg get_doc on unsaved docs)."
		)

	if truncation_seen:
		context.warnings.append(
			"Sidecar argument log was truncated for at least one recording — "
			"redundant call detection may be incomplete."
		)

	findings: list = []
	for (fn_name, safe_key), occurrences in buckets.items():
		threshold = _threshold_for(fn_name)
		count = len(occurrences)
		if count < threshold:
			continue

		high_multiplier = _conf_int(
			"profiler_redundant_high_multiplier", DEFAULT_REDUNDANT_HIGH_MULTIPLIER
		)
		severity = "High" if count >= threshold * high_multiplier else "Medium"

		# Action ref = the action containing the most occurrences
		action_counts = Counter(idx for idx, _ in occurrences)
		top_action_idx, _ = action_counts.most_common(1)[0]

		identifier_safe = safe_key  # already a tuple from _to_hashable
		# Use the first raw value as the representative
		identifier_raw = occurrences[0][1]

		findings.append({
			"finding_type": "Redundant Call",
			"severity": severity,
			"title": _title_for(fn_name, identifier_safe, count),
			"customer_description": _customer_description_for(fn_name, count),
			"technical_detail_json": json.dumps({
				"fn_name": fn_name,
				"identifier_safe": (
					list(identifier_safe) if isinstance(identifier_safe, tuple) else identifier_safe
				),
				"identifier_raw": (
					list(identifier_raw) if isinstance(identifier_raw, tuple) else identifier_raw
				),
				"count": count,
				"distinct_actions": len(action_counts),
			}, default=str),
			"estimated_impact_ms": 0,  # we don't have per-call timing in the sidecar
			"affected_count": count,
			"action_ref": str(top_action_idx),
		})

	return AnalyzerResult(findings=findings)
