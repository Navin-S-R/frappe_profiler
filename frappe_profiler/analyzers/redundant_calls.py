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

from frappe_profiler.analyzers.base import AnalyzerResult, walk_callsite


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


def _customer_description_for(fn_name: str, count: int, callsite: dict | None = None) -> str:
	"""Build the customer description, appending the callsite when
	available. v0.5.2 requires the callsite (file:line) for the user
	to actually navigate to the loop — pre-v0.5.2 the description
	said 'the same callsite' without revealing where."""
	site_hint = ""
	if callsite:
		fn_site = callsite.get("filename") or ""
		ln = callsite.get("lineno")
		if fn_site and ln:
			site_hint = f" The loop is at **{fn_site}:{ln}**."

	if fn_name == "get_doc":
		return (
			f"The same document was fetched **{count} times** from the same "
			"line of code. This is almost always a loop that reloads a "
			"document inside its body — caching the result outside the loop "
			"would eliminate the redundant fetches."
			f"{site_hint}"
		)
	if fn_name == "cache_get":
		return (
			f"The same cache key was looked up **{count} times** from the "
			"same callsite. Cache lookups are cheap individually but add up "
			"in a hot loop; reading once and re-using is the fix."
			f"{site_hint}"
		)
	if fn_name == "has_permission":
		return (
			f"The same permission check ran **{count} times** from the same "
			"callsite. Permission checks involve role lookups and DocType "
			"validation — caching the result for the duration of the action "
			"is the standard fix."
			f"{site_hint}"
		)
	return f"A function was called {count} times redundantly.{site_hint}"


def _to_hashable(value):
	"""Convert nested lists to nested tuples so the value can be a dict key."""
	if isinstance(value, list):
		return tuple(_to_hashable(v) for v in value)
	if isinstance(value, tuple):
		return tuple(_to_hashable(v) for v in value)
	return value


def _is_framework_filename(filename: str) -> bool:
	"""True if the given filename is inside code the user can't
	modify from their application: frappe/*, frappe_profiler/*, OR
	any third-party pip-installed library.

	Used to filter Redundant Call findings whose loop lives in
	framework / infra code. Production report had 3 of 4 redundant
	findings pointing at ``env/lib/python3.14/site-packages/
	werkzeug/serving.py`` — werkzeug's WSGI loop. 'Cache looked up
	25 times in serving.py:370' isn't a real loop — it's once per
	request across 25 requests — and even if it were, the user
	can't patch werkzeug.
	"""
	if not filename:
		return False
	norm = filename.replace("\\", "/")
	# Frappe-ecosystem framework
	if "frappe/" in norm or "frappe_profiler/" in norm:
		return True
	# v0.5.2: third-party libs. Any pip-installed package lives
	# under site-packages/ or dist-packages/ (Debian). Also catch
	# common ones by name in case sys.path was manipulated to
	# bypass the site-packages indirection.
	if "site-packages/" in norm or "dist-packages/" in norm:
		return True
	for lib in ("werkzeug/", "gunicorn/", "/rq/", "pyinstrument/",
	            "pytz/", "dateutil/", "MySQLdb/", "pymysql/"):
		if lib in norm:
			return True
	return False


def analyze(recordings: list, context) -> AnalyzerResult:
	# Bucket: (fn_name, identifier_safe_tuple) → list of
	# (action_idx, raw, caller_stack)
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
			caller_stack = entry.get("caller_stack") or []
			if fn_name is None or safe is None:
				continue
			try:
				key = (fn_name, _to_hashable(safe))
				buckets[key].append((action_idx, raw, caller_stack))
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
	# v0.5.2: track how many buckets we dropped as framework-only so we
	# can surface a soft warning (same pattern as index_suggestions'
	# drop counts).
	drop_framework_callsite = 0
	# And how many had no caller stack at all (sidecars captured before
	# v0.5.2 when caller_stack wasn't recorded).
	drop_no_caller_stack = 0
	# v0.5.2 round 2: buckets whose count threshold was only reached by
	# summing ACROSS many actions (e.g. "25 calls" that turned out to
	# be 1 call in each of 25 requests — not a loop, just a call that
	# naturally fires once per request).
	drop_cross_request_spread = 0

	for (fn_name, safe_key), occurrences in buckets.items():
		threshold = _threshold_for(fn_name)
		count = len(occurrences)
		if count < threshold:
			continue

		# v0.5.2 round 2: a "redundant call" is a LOOP, meaning the
		# threshold must be reached WITHIN a single action. Cross-
		# request aggregation (25 separate requests each calling cache
		# once) isn't a loop — it's a framework call that naturally
		# fires once per request. Production report had 3 "Redundant
		# cache lookup: … (25 times)" / "(36 times)" findings from
		# werkzeug/serving.py:370 — each was 1 call per request across
		# 25/36 requests, not a repeated in-loop lookup.
		action_counts = Counter(idx for idx, _, _ in occurrences)
		max_in_any_action = action_counts.most_common(1)[0][1]
		if max_in_any_action < threshold:
			drop_cross_request_spread += 1
			continue

		# v0.5.2: callsite-based filtering. Use the first occurrence's
		# stack as the representative (all occurrences of the same
		# (fn_name, identifier) are by definition from the same cache
		# key, and we flag them BECAUSE they all fire from the same
		# repeated loop — so first-occurrence stack is canonical).
		first_stack = occurrences[0][2]
		if not first_stack:
			# Recording captured before v0.5.2 OR stack capture failed.
			# Drop the finding rather than emit a hashed-cache-key-
			# with-no-context row that the user can't act on.
			drop_no_caller_stack += 1
			continue

		callsite = walk_callsite(first_stack)
		if callsite is None or _is_framework_filename(
			callsite.get("filename") or ""
		):
			# Pure framework stack. walk_callsite returns None for
			# profiler-own stacks; for pure frappe/* stacks it falls
			# back to the deepest frame (so legitimate migration /
			# background-task findings don't disappear). Here we
			# ADDITIONALLY filter any callsite that resolves to
			# frappe/* code, because for Redundant Calls the loop
			# inside Frappe's own codebase isn't actionable for
			# application developers. Same rationale as the
			# Framework N+1 filter.
			drop_framework_callsite += 1
			continue

		# Callsite IS user code (or at least contains a user frame).
		# Emit the finding with the callsite in the detail so users
		# can navigate to the loop.

		high_multiplier = _conf_int(
			"profiler_redundant_high_multiplier", DEFAULT_REDUNDANT_HIGH_MULTIPLIER
		)
		# v0.5.2 round 2: severity based on max-in-any-action, not
		# total count. Because count was established above to reflect
		# loop density within a single action, using it for severity
		# misleads ("100 cross-request cache calls" looks worse than
		# "100 cache calls in one loop in one request"). Use
		# max_in_any_action instead.
		severity = (
			"High"
			if max_in_any_action >= threshold * high_multiplier
			else "Medium"
		)

		# Action ref = the action containing the most occurrences
		# (already computed in action_counts above as part of the
		# per-action threshold check).
		top_action_idx, _ = action_counts.most_common(1)[0]

		identifier_safe = safe_key
		identifier_raw = occurrences[0][1]

		findings.append({
			"finding_type": "Redundant Call",
			"severity": severity,
			"title": _title_for(fn_name, identifier_safe, count),
			"customer_description": _customer_description_for(
				fn_name, count, callsite=callsite
			),
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
				# v0.5.2: surface the callsite so developers can
				# actually navigate to the loop. Pre-v0.5.2 the only
				# identifier was a sha256 hash of the cache key —
				# useless for finding the offending code.
				"callsite": {
					"filename": callsite.get("filename"),
					"lineno": callsite.get("lineno"),
					"function": callsite.get("function"),
				},
			}, default=str),
			"estimated_impact_ms": 0,
			"affected_count": count,
			"action_ref": str(top_action_idx),
		})

	if drop_cross_request_spread:
		context.warnings.append(
			f"Suppressed {drop_cross_request_spread} Redundant Call "
			"candidate(s) where the threshold was reached only by "
			"summing across multiple requests (e.g. one cache lookup "
			"per request × 25 requests). That's not a loop — it's a "
			"call that naturally fires once per request. A real "
			"redundant loop has the threshold met WITHIN a single "
			"action."
		)
	if drop_framework_callsite:
		context.warnings.append(
			f"Suppressed {drop_framework_callsite} Redundant Call "
			"finding(s) whose loop lives inside Frappe framework code "
			"or a third-party library (users can't act on those). "
			"The hot ones still show up in the Repeated Hot Frame "
			"leaderboard if they represent significant time."
		)

	if drop_no_caller_stack:
		context.warnings.append(
			f"Skipped {drop_no_caller_stack} Redundant Call candidate(s) "
			"with no captured caller stack. Re-run the session on the "
			"v0.5.2+ profiler to enable callsite-based filtering."
		)

	return AnalyzerResult(findings=findings)
