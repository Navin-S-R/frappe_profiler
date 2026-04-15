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
		severity = _severity(count, total_time)

		# v0.5.1: shorten filename in the TITLE only. Deeply-nested module
		# paths (e.g. jewellery_erpnext/jewellery_erpnext/jewellery_erpnext/
		# doctype/parent_manufacturing_order/parent_manufacturing_order.py)
		# push the 140-char Profiler Finding.title limit and crash analyze
		# with CharacterLengthExceededError. customer_description and
		# technical_detail_json keep the full filename for navigation.
		short_fn = short_filename(filename)
		findings.append(
			{
				"finding_type": "N+1 Query",
				"severity": severity,
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
		)

	# Sort: highest severity first, then highest impact within severity
	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))

	return AnalyzerResult(findings=findings)


def _severity(count: int, total_time_ms: float) -> str:
	if count >= HIGH_OCCURRENCES or total_time_ms > HIGH_TOTAL_TIME_MS:
		return "High"
	if count >= MEDIUM_OCCURRENCES:
		return "Medium"
	return "Low"
