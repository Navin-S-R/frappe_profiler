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
	row_errors = 0
	first_error_reasons: list[str] = []

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
				# v0.5.1: wrap per-row inspection in try/except so a single
				# bad row (e.g. an EXPLAIN JSON nested structure from an
				# unusual MariaDB version, a Decimal value that doesn't
				# coerce cleanly, a field with an unexpected type) doesn't
				# kill the whole analyzer. Previously a single crash in
				# _inspect_row caused the analyze.run outer try/except to
				# log 'analyzer failed' and drop ALL Full Table Scan /
				# Filesort / Temporary Table / Low Filter Ratio findings
				# for the entire session.
				try:
					_inspect_row(row, normalized, action_idx, query_duration, buckets)
				except Exception as e:
					row_errors += 1
					if len(first_error_reasons) < 3:
						first_error_reasons.append(
							f"{type(e).__name__}: {e} "
							f"(row keys: {sorted(list(row.keys()))[:10]})"
						)

	findings = list(buckets.values())
	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))

	warnings: list[str] = []
	if row_errors:
		warnings.append(
			f"explain_flags: could not parse {row_errors} EXPLAIN row(s). "
			f"First reasons: {'; '.join(first_error_reasons)}. "
			"The report may be missing some optimization opportunities."
		)
		# Surface the first few to the Error Log as well so operators can
		# pattern-match across sessions.
		try:
			import frappe

			for reason in first_error_reasons:
				frappe.log_error(
					title="frappe_profiler explain_flags row parse",
					message=reason,
				)
		except Exception:
			pass

	return AnalyzerResult(findings=findings, warnings=warnings)


# ---------------------------------------------------------------------------
# Numeric coercion
# ---------------------------------------------------------------------------
# MariaDB's `rows` and `filtered` columns come back as int/float in the
# typical PyMySQL path, but certain driver versions and EXPLAIN FORMAT
# variants have been observed to return Decimal, str, or even None — any
# of which would crash a Python 3 `>` comparison with a numeric literal.
# v0.5.1 adds explicit coercion helpers so one weird row doesn't take out
# the whole session.


def _to_int(val) -> int:
	"""Coerce EXPLAIN numeric fields to int. Returns 0 on any failure
	(None, unparseable string, unexpected type)."""
	if val is None:
		return 0
	if isinstance(val, bool):
		# bool is a subclass of int — treat False as 0, True as 1
		return int(val)
	if isinstance(val, int):
		return val
	if isinstance(val, float):
		return int(val)
	try:
		return int(val)
	except (TypeError, ValueError):
		try:
			return int(float(val))
		except (TypeError, ValueError):
			return 0


def _to_float(val):
	"""Coerce EXPLAIN `filtered` to float. Returns None on failure so the
	filtered-threshold check can cleanly skip."""
	if val is None:
		return None
	if isinstance(val, bool):
		return float(val)
	if isinstance(val, (int, float)):
		return float(val)
	try:
		return float(val)
	except (TypeError, ValueError):
		return None


def _inspect_row(row, normalized_query, action_idx, query_duration, buckets):
	"""Check one EXPLAIN row against four red-flag patterns."""
	table = row.get("table") or "?"
	# v0.5.1: explicit coercion — see _to_int docstring.
	rows_examined = _to_int(row.get("rows"))
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
	# v0.5.1: coerce explicitly so Decimal/str values from unusual drivers
	# don't silently fall through the isinstance guard.
	filtered = _to_float(row.get("filtered"))
	if (
		filtered is not None
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
