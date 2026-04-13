# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: parse EXPLAIN output for red flags.

The recorder captures `EXPLAIN <query>` for every SELECT/UPDATE/DELETE but
nobody reads the result. This analyzer walks every EXPLAIN row and surfaces
the four most actionable red flags:

    type == "ALL"          → full table scan (no index used)
    Extra: "Using filesort"  → sorting on disk
    Extra: "Using temporary" → temp table created
    filtered < 10            → reading much more than returned

Each match becomes a finding tagged by table. Findings are deduplicated by
(finding_type, table) — if 50 queries hit the same full-scan table, we
report it once with the cumulative impact.
"""

import json
from collections import defaultdict

from frappe_profiler.analyzers.base import SEVERITY_ORDER, AnalyzerResult

# A query is "high severity" full-scan if it touched more than this many rows.
HIGH_ROWS_EXAMINED = 10000

# A query is flagged as "low filter ratio" if MariaDB's `filtered` column
# says it reads more than 10x what it returns AND it touches more than 100
# rows (the 100 floor prevents noise from tiny queries).
LOW_FILTERED_THRESHOLD = 10  # percent
LOW_FILTERED_MIN_ROWS = 100


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	# (finding_type, table) → aggregated finding dict
	buckets: dict[tuple, dict] = {}

	for action_idx, recording in enumerate(recordings):
		for call in recording.get("calls") or []:
			normalized = call.get("normalized_query") or call.get("query") or ""
			explain_rows = call.get("explain_result") or []
			if not isinstance(explain_rows, list):
				continue
			query_duration = call.get("duration", 0)
			for row in explain_rows:
				if not isinstance(row, dict):
					continue
				_inspect_row(row, normalized, action_idx, query_duration, buckets)

	findings = list(buckets.values())
	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))
	return AnalyzerResult(findings=findings)


def _inspect_row(row, normalized_query, action_idx, query_duration, buckets):
	"""Check one EXPLAIN row against four red-flag patterns."""
	table = row.get("table") or "?"
	rows_examined = row.get("rows") or 0
	extra = (row.get("Extra") or row.get("extra") or "").lower()
	type_ = (row.get("type") or "").lower()

	# Full table scan
	if type_ == "all":
		severity = "High" if rows_examined > HIGH_ROWS_EXAMINED else "Medium"
		_upsert(
			buckets,
			finding_type="Full Table Scan",
			table=table,
			severity=severity,
			query_duration=query_duration,
			action_idx=action_idx,
			row=row,
			normalized_query=normalized_query,
			title=f"Full table scan on {table}",
			customer_description=(
				f"A query had to read every row of the **{table}** table "
				f"({rows_examined} rows examined) because no index could "
				"help. This kind of query gets dramatically slower as the "
				"table grows. Adding an appropriate index is usually the fix."
			),
			fix_hint="Add an index on the WHERE/JOIN columns of this query.",
		)

	# Filesort
	if "using filesort" in extra:
		_upsert(
			buckets,
			finding_type="Filesort",
			table=table,
			severity="Medium",
			query_duration=query_duration,
			action_idx=action_idx,
			row=row,
			normalized_query=normalized_query,
			title=f"Filesort on {table}",
			customer_description=(
				f"A query against **{table}** had to sort its results without "
				"the help of an index. For small result sets this is fine, "
				"but on large data it slows the query down significantly. "
				"Adding an index that covers the ORDER BY clause usually fixes it."
			),
			fix_hint="Add an index that covers the ORDER BY columns of this query.",
		)

	# Temporary table
	if "using temporary" in extra:
		_upsert(
			buckets,
			finding_type="Temporary Table",
			table=table,
			severity="Medium",
			query_duration=query_duration,
			action_idx=action_idx,
			row=row,
			normalized_query=normalized_query,
			title=f"Temporary table created for query on {table}",
			customer_description=(
				f"A query against **{table}** had to materialize a temporary "
				"table to compute its results. This usually indicates a "
				"GROUP BY or DISTINCT without a covering index, and gets "
				"more expensive as the data grows."
			),
			fix_hint="Add a covering index for the GROUP BY/DISTINCT columns.",
		)

	# Low filter ratio: MariaDB's `filtered` column reports what percentage
	# of rows examined are actually returned after filtering. Values under
	# 10 mean the query is reading 10x or more of what it needs — the WHERE
	# clause isn't selective enough (or isn't using an index to filter).
	filtered = row.get("filtered")
	if (
		isinstance(filtered, (int, float))
		and filtered < LOW_FILTERED_THRESHOLD
		and rows_examined > LOW_FILTERED_MIN_ROWS
	):
		severity = "Medium" if rows_examined > HIGH_ROWS_EXAMINED else "Low"
		_upsert(
			buckets,
			finding_type="Low Filter Ratio",
			table=table,
			severity=severity,
			query_duration=query_duration,
			action_idx=action_idx,
			row=row,
			normalized_query=normalized_query,
			title=f"Low filter ratio on {table}",
			customer_description=(
				f"A query against **{table}** examined {rows_examined} rows "
				f"but only {filtered:.0f}% of them matched the WHERE clause. "
				"That means the query is reading far more data than it needs. "
				"Usually fixable by adding or reshaping an index so the "
				"filter is applied at the index level instead of per-row."
			),
			fix_hint=(
				"Review the WHERE clause and add an index that matches its "
				"selectivity. Check the key_len in EXPLAIN to confirm the "
				"full index is being used."
			),
		)


def _upsert(
	buckets,
	*,
	finding_type,
	table,
	severity,
	query_duration,
	action_idx,
	row,
	normalized_query,
	title,
	customer_description,
	fix_hint,
):
	"""Insert or merge a finding into the buckets dict.

	Findings of the same (type, table) are merged: counts and impact are
	summed, severity is upgraded to the highest seen.
	"""
	key = (finding_type, table)
	existing = buckets.get(key)
	if existing:
		existing["affected_count"] += 1
		existing["estimated_impact_ms"] += round(query_duration, 2)
		# Upgrade severity if this row is more severe
		if SEVERITY_ORDER.get(severity, 3) < SEVERITY_ORDER.get(existing["severity"], 3):
			existing["severity"] = severity
		return

	buckets[key] = {
		"finding_type": finding_type,
		"severity": severity,
		"title": title,
		"customer_description": customer_description,
		"technical_detail_json": json.dumps(
			{
				"table": table,
				"explain_row": row,
				"normalized_query": normalized_query,
				"fix_hint": fix_hint,
			},
			default=str,
		),
		"estimated_impact_ms": round(query_duration, 2),
		"affected_count": 1,
		"action_ref": str(action_idx),
	}
