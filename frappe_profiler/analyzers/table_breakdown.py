# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: per-table time and query count breakdown.

Aggregates time spent and query count by SQL table touched. Useful context
for the developer — "where is the time actually going?". Doesn't generate
findings on its own; just produces a sorted list for the report renderer.

Uses `sql_metadata.Parser` (already a frappe dependency) to extract table
names from each query. A query that touches multiple tables (JOIN) is
counted against each table, so the breakdown sums to MORE than the total
query time. The renderer should make this clear.
"""

from collections import defaultdict

from frappe_profiler.analyzers.base import AnalyzerResult

DEFAULT_TOP_N = 15


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	try:
		from sql_metadata import Parser  # noqa: F401
	except Exception:
		return AnalyzerResult(
			warnings=["sql_metadata not available — table breakdown skipped"]
		)

	stats: dict[str, dict] = defaultdict(lambda: {"duration": 0.0, "count": 0})

	for recording in recordings:
		for call in recording.get("calls") or []:
			query = call.get("normalized_query") or call.get("query") or ""
			if not query:
				continue
			duration = call.get("duration", 0)
			for table in _extract_tables(query):
				stats[table]["duration"] += duration
				stats[table]["count"] += 1

	breakdown = sorted(
		(
			{
				"table": t,
				"duration_ms": round(s["duration"], 2),
				"queries": s["count"],
			}
			for t, s in stats.items()
		),
		key=lambda x: x["duration_ms"],
		reverse=True,
	)[:DEFAULT_TOP_N]

	return AnalyzerResult(aggregate={"table_breakdown": breakdown})


def _extract_tables(query: str) -> list[str]:
	"""Return the unique tables touched by a SQL query, in order."""
	if not query:
		return []
	try:
		from sql_metadata import Parser

		parsed = Parser(query)
		raw = parsed.tables or []
	except Exception:
		return []

	seen = set()
	out = []
	for t in raw:
		if t and t not in seen:
			seen.add(t)
			out.append(t)
	return out
