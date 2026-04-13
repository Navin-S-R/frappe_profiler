# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: top N slowest queries across the session + slow-query findings.

Builds the `top_queries` aggregate (used by the report renderer to draw
the slowest-queries leaderboard) and emits a `Slow Query` finding for any
single query > 200ms.
"""

import json

from frappe_profiler.analyzers.base import AnalyzerResult, walk_callsite_str

DEFAULT_TOP_N = 20
SLOW_QUERY_MS = 200
HIGH_SEVERITY_MS = 500
MAX_FINDINGS = 5  # only flag the top 5 slow queries to avoid noise


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	all_queries = []
	for action_idx, recording in enumerate(recordings):
		for call in recording.get("calls") or []:
			all_queries.append(
				{
					"normalized_query": (call.get("normalized_query") or call.get("query") or "")[:500],
					"duration_ms": round(call.get("duration", 0), 2),
					"action_idx": action_idx,
					"recording_uuid": recording.get("uuid"),
					"callsite": walk_callsite_str(call.get("stack")),
				}
			)

	all_queries.sort(key=lambda q: q["duration_ms"], reverse=True)
	top = all_queries[:DEFAULT_TOP_N]

	findings = []
	for q in top[:MAX_FINDINGS]:
		if q["duration_ms"] <= SLOW_QUERY_MS:
			break  # the rest are below threshold; sorted desc so we can break
		findings.append(
			{
				"finding_type": "Slow Query",
				"severity": "High" if q["duration_ms"] > HIGH_SEVERITY_MS else "Medium",
				"title": f"Slow query: {q['duration_ms']:.0f}ms",
				"customer_description": (
					f"A single query took {q['duration_ms']:.0f}ms to run. "
					"This is one of the slowest queries in the session and is "
					"a likely candidate for optimization."
				),
				"technical_detail_json": json.dumps(
					{
						"normalized_query": q["normalized_query"],
						"callsite": q["callsite"],
						"recording_uuid": q["recording_uuid"],
						"fix_hint": (
							"Investigate this query — it may need an index, a "
							"refactored WHERE clause, or a different access pattern. "
							"Run EXPLAIN ANALYZE on a representative production query "
							"to see the actual cost."
						),
					},
					default=str,
				),
				"estimated_impact_ms": q["duration_ms"],
				"affected_count": 1,
				"action_ref": str(q["action_idx"]),
			}
		)

	return AnalyzerResult(findings=findings, aggregate={"top_queries": top})


# Callsite walking is shared across analyzers via base.walk_callsite_str.
