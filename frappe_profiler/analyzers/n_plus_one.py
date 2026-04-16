# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: true N+1 query detection by callsite.

The single highest-leverage insight in the report. Groups queries by
(normalized_query, callsite_file, callsite_line) — i.e. queries that have
the same SQL shape AND were issued from the same line of Python code.
A group with more than `MIN_OCCURRENCES` queries is almost always a Python
loop fetching its data one row at a time.

This is what makes the recorder's stack capture worth its overhead. The
existing recorder counts "exact_copies" and "normalized_copies" but doesn't
attribute them to a callsite, so it can't tell the difference between
"the same query ran 50 times because it's in a loop" and "the same query
ran 50 times from 50 different places in the code". Our grouping by
callsite makes the distinction.
"""

import json
from collections import defaultdict

from frappe_profiler.analyzers.base import (
	FRAMEWORK_PREFIXES,  # noqa: F401  (kept for any external importers)
	SEVERITY_ORDER,
	AnalyzerResult,
	short_filename,
	walk_callsite,
)

# A group must have at least this many occurrences to be flagged.
# Default is intentionally conservative — many legitimate Frappe patterns
# run 5-9 of the same query (child table operations), and we don't want
# to flag those. Can be overridden per site via
# site_config.json: profiler_n_plus_one_threshold
DEFAULT_MIN_OCCURRENCES = 10

# A group is also required to spend at least this much total time before
# being flagged. Prevents tiny (<1ms each) queries from generating noisy
# findings even when they repeat many times.
DEFAULT_MIN_TOTAL_TIME_MS = 20

# Severity heuristics
HIGH_OCCURRENCES = 50
HIGH_TOTAL_TIME_MS = 200
MEDIUM_OCCURRENCES = 20

def _get_threshold() -> int:
	try:
		import frappe

		v = frappe.conf.get("profiler_n_plus_one_threshold")
		if v is not None:
			return int(v)
	except Exception:
		pass
	return DEFAULT_MIN_OCCURRENCES


def _get_min_total_time() -> float:
	try:
		import frappe

		v = frappe.conf.get("profiler_n_plus_one_min_total_ms")
		if v is not None:
			return float(v)
	except Exception:
		pass
	return DEFAULT_MIN_TOTAL_TIME_MS


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	groups: dict[tuple, list[dict]] = defaultdict(list)
	min_occurrences = _get_threshold()
	min_total_time = _get_min_total_time()

	for action_idx, recording in enumerate(recordings):
		for call in recording.get("calls") or []:
			normalized = call.get("normalized_query") or ""
			callsite = walk_callsite(call.get("stack"))
			if not normalized or not callsite:
				continue
			key = (normalized, callsite["filename"], callsite["lineno"])
			groups[key].append(
				{
					"duration": call.get("duration", 0),
					"action_idx": action_idx,
					"function": callsite.get("function") or "",
				}
			)

	findings = []
	for (normalized, filename, lineno), occurrences in groups.items():
		count = len(occurrences)
		if count < min_occurrences:
			continue

		total_time = sum(o["duration"] for o in occurrences)
		# Also require a minimum total time so we don't flag 10 × 0.1 ms
		# queries as an N+1 — those are not worth reporting.
		if total_time < min_total_time:
			continue

		function_name = occurrences[0]["function"]
		action_idx = occurrences[0]["action_idx"]

		# v0.5.1: shorten filename in the TITLE only. Deeply-nested module
		# paths (e.g. jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/
		# doctype/parent_manufacturing_order/parent_manufacturing_order.py)
		# push the 140-char Profiler Finding.title limit and crash analyze
		# with CharacterLengthExceededError. customer_description and
		# technical_detail_json keep the full filename for navigation.
		short_fn = short_filename(filename)

		# v0.5.1: framework-level repetition gets a separate finding
		# type. When the N+1 is blamed on frappe/* code (e.g. the query
		# builder resolving table metadata 138× in one session),
		# there's usually nothing the user can do — the framework is
		# iterating over inputs as designed. Flagging it as a normal
		# "N+1 Query" would tell the user to "refactor to fetch data
		# in one query" for code they don't own.
		#
		# Emit as "Framework N+1" with Low severity and a customer
		# description that's transparent about the limited action
		# available. Contributors who DO want to act on framework N+1s
		# can still see them in the findings list — they're just
		# distinct from application-level N+1s.
		is_framework = _is_framework_callsite(filename)
		if is_framework:
			findings.append(_build_framework_finding(
				short_fn=short_fn,
				filename=filename,
				lineno=lineno,
				function_name=function_name,
				normalized=normalized,
				count=count,
				total_time=total_time,
				action_idx=action_idx,
			))
		else:
			findings.append(_build_user_finding(
				short_fn=short_fn,
				filename=filename,
				lineno=lineno,
				function_name=function_name,
				normalized=normalized,
				count=count,
				total_time=total_time,
				action_idx=action_idx,
			))

	# Sort: highest severity first, then highest impact within severity
	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))

	return AnalyzerResult(findings=findings)


def _severity(count: int, total_time_ms: float) -> str:
	if count >= HIGH_OCCURRENCES or total_time_ms > HIGH_TOTAL_TIME_MS:
		return "High"
	if count >= MEDIUM_OCCURRENCES:
		return "Medium"
	return "Low"


def _is_framework_callsite(filename: str) -> bool:
	"""True when the blamed file is inside Frappe / frappe_profiler.

	Used to route N+1 findings into the separate "Framework N+1"
	bucket (Low severity, transparent description) rather than the
	normal "N+1 Query" one. The callsite walker already prefers
	user-code frames over framework frames, so this only fires when
	EVERY frame in the stack was in the framework — which happens
	for framework background tasks, migrations, and
	framework-internal query loops like frappe.query_builder.utils
	building a SELECT for each input.
	"""
	if not filename:
		return False
	norm = filename.replace("\\", "/")
	return "frappe/" in norm or "frappe_profiler/" in norm


def _build_user_finding(
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	count: int,
	total_time: float,
	action_idx: int,
) -> dict:
	"""Build the classic user-code N+1 finding — High/Medium/Low
	severity by count & impact, actionable fix hint."""
	return {
		"finding_type": "N+1 Query",
		"severity": _severity(count, total_time),
		"title": f"Same query ran {count}× at {short_fn}:{lineno}",
		"customer_description": (
			f"We noticed the same query was repeated {count} times in a row "
			f"from the same line of code ({filename}:{lineno}), costing about "
			f"{total_time:.0f}ms in total. This is usually a Python loop that "
			"should fetch its data in one query instead of one-at-a-time. "
			"Typical fix: a few hours of dev work."
		),
		"technical_detail_json": json.dumps(
			{
				"callsite": {
					"filename": filename,
					"lineno": lineno,
					"function": function_name,
				},
				"normalized_query": normalized,
				"occurrences": count,
				"total_time_ms": round(total_time, 2),
				"average_time_ms": round(total_time / count, 2) if count else 0,
				"fix_hint": (
					"This is a classic N+1 pattern. The Python code at "
					f"{filename}:{lineno} is running the same query in a loop. "
					"Refactor to fetch all needed data in a single query — for "
					"Frappe specifically, that's usually frappe.get_all() with a "
					"name-IN filter, or a JOIN against the source table — instead "
					"of one row at a time."
				),
			},
			default=str,
		),
		"estimated_impact_ms": round(total_time, 2),
		"affected_count": count,
		"action_ref": str(action_idx),
	}


def _build_framework_finding(
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	count: int,
	total_time: float,
	action_idx: int,
) -> dict:
	"""Build the framework-level N+1 finding — always Low severity,
	description acknowledges the user can rarely fix framework code.

	Still includes the technical detail (callsite, query, impact) so
	contributors who WANT to optimize the framework can find it.
	"""
	return {
		"finding_type": "Framework N+1",
		"severity": "Low",
		"title": (
			f"Framework query repeated {count}× at {short_fn}:{lineno}"
		),
		"customer_description": (
			f"Frappe's own code at **{filename}:{lineno}** issued the "
			f"same query {count} times in this session, totalling "
			f"{total_time:.0f}ms. This is typically the framework "
			"resolving metadata, permissions, or building queries for "
			"different inputs — it's rarely something you can change "
			"in your application code. Listed here for transparency, "
			"not as an action item. If the cumulative cost is high, "
			"the fix usually lives in the Frappe codebase itself."
		),
		"technical_detail_json": json.dumps(
			{
				"callsite": {
					"filename": filename,
					"lineno": lineno,
					"function": function_name,
				},
				"normalized_query": normalized,
				"occurrences": count,
				"total_time_ms": round(total_time, 2),
				"average_time_ms": round(total_time / count, 2) if count else 0,
				"is_framework": True,
				"fix_hint": (
					"This repetition is inside Frappe framework code at "
					f"{filename}:{lineno}. Application developers can "
					"rarely change it. If this is a hot spot in your "
					"profile, consider (1) whether your usage pattern is "
					"triggering unnecessary framework work (e.g. loading "
					"DocType meta in a loop instead of once), (2) whether "
					"a Frappe upgrade has already optimized it, or (3) "
					"raising it as an upstream issue."
				),
			},
			default=str,
		),
		"estimated_impact_ms": round(total_time, 2),
		"affected_count": count,
		"action_ref": str(action_idx),
	}
