# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: aggregated index suggestions across the session.

Wraps the existing
`frappe.core.doctype.recorder.recorder._optimize_query` (which uses the
`DBOptimizer` heuristic) to identify a single best missing index per query.
We run it across the union of unique normalized queries in the session,
then dedupe and aggregate suggestions by (table, column) so a customer
sees one suggestion per missing index — with the cumulative time it would
save and the queries that would benefit.

This is the per-session aggregation that the existing per-request
optimizer doesn't do. Per-request, you only see the index for THAT
request's queries; per-session, you see the index that would help across
the whole flow.
"""

import json
import re
from collections import Counter, defaultdict

from frappe_profiler.analyzers.base import SEVERITY_ORDER, AnalyzerResult


def _scrub_literals(text: str) -> str:
	"""Best-effort removal of SQL literals from a query before logging.

	The error log is typically admin-only but can sometimes be exposed
	to non-admins via permission misconfig or support workflows. Since
	this query string is going into a log that might be shared, we
	replace obvious string literals and long numeric sequences with ?
	placeholders.

	This is paranoid belt-and-suspenders — the upstream query should
	already be normalized by mark_duplicates by the time we see it, but
	if normalization was skipped or the query contained an unusual
	pattern, we scrub here too.
	"""
	if not text:
		return text
	# Replace 'single-quoted' strings
	text = re.sub(r"'[^']*'", "'?'", text)
	# Replace "double-quoted" strings (but not the identifier quotes `)
	text = re.sub(r'"[^"]*"', '"?"', text)
	# Replace long numeric sequences (likely IDs or amounts)
	text = re.sub(r"\b\d{4,}\b", "?", text)
	return text

HIGH_IMPACT_MS = 500
MEDIUM_IMPACT_MS = 100
MAX_EXAMPLE_QUERIES = 3
# Log the first N failures to the Frappe Error Log; beyond that, we just
# count. Prevents the Error Log from being flooded by one bad session
# with thousands of unparseable queries.
MAX_LOGGED_FAILURES = 3


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	try:
		from frappe.core.doctype.recorder.recorder import _optimize_query
	except Exception:
		return AnalyzerResult(
			warnings=["Could not import frappe.core.doctype.recorder.recorder._optimize_query"]
		)

	# Aggregate by unique normalized query so we only ask the optimizer once
	# per shape, regardless of how many times the shape was executed.
	unique_queries: dict[str, dict] = {}
	for recording in recordings:
		for call in recording.get("calls") or []:
			normalized = call.get("normalized_query")
			if not normalized:
				continue
			entry = unique_queries.setdefault(
				normalized,
				{"duration": 0.0, "count": 0, "raw": call.get("query") or normalized},
			)
			entry["duration"] += call.get("duration", 0)
			entry["count"] += 1

	# Run the optimizer on each unique query and aggregate suggestions.
	# Track per-exception-type failures so we can surface a warning if
	# the optimizer couldn't analyze a lot of queries — otherwise the
	# customer reads "no index suggestions" as "no missing indexes"
	# when the real answer might be "we couldn't analyze your queries".
	suggestion_buckets: dict[tuple, dict] = defaultdict(
		lambda: {"duration": 0.0, "count": 0, "queries": []}
	)
	failures_by_type: Counter = Counter()
	logged = 0
	for normalized, info in unique_queries.items():
		try:
			# DBOptimizer parses with sql_metadata which doesn't care about
			# literal values, so passing the normalized query is fine.
			index = _optimize_query(info["raw"])
		except Exception as e:
			failures_by_type[type(e).__name__] += 1
			if logged < MAX_LOGGED_FAILURES:
				try:
					import frappe

					# Scrub literals defensively — the query should already
					# be normalized, but if it isn't (edge case), we don't
					# want literal customer data landing in the Error Log.
					scrubbed = _scrub_literals(normalized[:1000])
					frappe.log_error(
						title="frappe_profiler optimizer failure",
						message=f"{type(e).__name__}: {e}\n\nQuery: {scrubbed}",
					)
				except Exception:
					pass
				logged += 1
			continue
		if not index or not index.table or not index.column:
			continue
		key = (index.table, index.column)
		bucket = suggestion_buckets[key]
		bucket["duration"] += info["duration"]
		bucket["count"] += info["count"]
		if len(bucket["queries"]) < MAX_EXAMPLE_QUERIES:
			bucket["queries"].append(normalized[:300])

	findings = []
	for (table, column), bucket in suggestion_buckets.items():
		impact_ms = bucket["duration"]
		findings.append(
			{
				"finding_type": "Missing Index",
				"severity": _severity(impact_ms),
				"title": f"Add index on {table}({column})",
				"customer_description": (
					f"Adding an index to the **{column}** column of the "
					f"**{table}** table would speed up {bucket['count']} "
					f"queries in this session, saving roughly "
					f"{impact_ms:.0f}ms total. Ask your developer to add this "
					"index in a database migration."
				),
				"technical_detail_json": json.dumps(
					{
						"table": table,
						"column": column,
						"suggested_ddl": f"ALTER TABLE `{table}` ADD INDEX `idx_{column}` (`{column}`);",
						"affected_query_count": bucket["count"],
						"estimated_savings_ms": round(impact_ms, 2),
						"example_queries": bucket["queries"],
						"validation_note": (
							"The developer must validate this suggestion against "
							"the actual schema and production query patterns "
							"before applying. The optimizer uses heuristics; an "
							"actual EXPLAIN ANALYZE on a representative query is "
							"recommended."
						),
					},
					default=str,
				),
				"estimated_impact_ms": round(impact_ms, 2),
				"affected_count": bucket["count"],
				"action_ref": "",
			}
		)

	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))

	warnings: list[str] = []
	total_failures = sum(failures_by_type.values())
	if total_failures:
		failure_summary = ", ".join(
			f"{n}× {name}" for name, n in failures_by_type.most_common(3)
		)
		warnings.append(
			f"Could not analyze {total_failures} of {len(unique_queries)} "
			f"queries for index suggestions ({failure_summary}). "
			"The report may be missing some optimization opportunities — "
			"see Error Log for the first few failed queries."
		)

	return AnalyzerResult(findings=findings, warnings=warnings)


def _severity(impact_ms: float) -> str:
	if impact_ms > HIGH_IMPACT_MS:
		return "High"
	if impact_ms > MEDIUM_IMPACT_MS:
		return "Medium"
	return "Low"
