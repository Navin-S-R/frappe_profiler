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

from frappe_profiler.analyzers.base import SEVERITY_ORDER, AnalyzerResult, walk_callsite

# A query is "high severity" full-scan if it touched more than this many rows.
HIGH_ROWS_EXAMINED = 10000

# A query is flagged as "low filter ratio" if MariaDB's `filtered` column
# says it reads more than 10x what it returns AND it touches more than 100
# rows (the 100 floor prevents noise from tiny queries).
LOW_FILTERED_THRESHOLD = 10  # percent
LOW_FILTERED_MIN_ROWS = 100

# Filesort / Temporary Table findings need a row floor for the same
# reason Low Filter Ratio does: sorting 1 row or materializing a 5-row
# intermediate is free, and flagging those fills the report with noise.
# A real production run surfaced "Filesort on tabCustom DocPerm" from a
# SELECT * FROM tabCustom DocPerm WHERE parent=? ORDER BY creation ASC
# query where EXPLAIN reported rows=1 (a single-parent lookup with the
# `parent` index already doing const-ref access). The filesort is on
# one row — actionable only in the abstract. 100 rows is the same
# floor LOW_FILTERED_MIN_ROWS uses for the same reason.
MIN_ROWS_TO_FLAG_SORT = 100


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	# (finding_type, table) → aggregated finding dict
	buckets: dict[tuple, dict] = {}
	row_errors = 0
	first_error_reasons: list[str] = []
	# v0.5.2: track drops so we can surface why fewer findings than
	# the raw EXPLAIN data would suggest.
	drop_alias = 0
	drop_framework_callsite = 0

	for action_idx, recording in enumerate(recordings):
		for call in recording.get("calls") or []:
			normalized = call.get("normalized_query") or call.get("query") or ""
			explain_rows = call.get("explain_result") or []
			if not isinstance(explain_rows, list):
				continue
			query_duration = call.get("duration", 0)
			# v0.5.2: pull the call's stack once per call so _inspect_row
			# can consult the blame frame. If the user has no agency
			# over where this SQL is issued (the loop lives in
			# frappe/*), the find-an-index finding is noise.
			call_stack = call.get("stack") or []

			# Resolve the call's user-blame frame ONCE. If it's inside
			# frappe/* framework code (or no user frame exists at
			# all), every Full Scan / Filesort / Temp Table / Low
			# Filter finding from this call lives inside framework
			# code and isn't user-actionable. Skip the whole call's
			# EXPLAIN rows rather than emit N findings the user
			# can't act on.
			if _is_framework_origin(call_stack):
				drop_framework_callsite += 1
				continue

			for row in explain_rows:
				if not isinstance(row, dict):
					continue
				# v0.5.1: per-row try/except for resilience (see previous comment).
				try:
					skipped = _inspect_row(
						row, normalized, action_idx, query_duration, buckets,
					)
					if skipped == "alias":
						drop_alias += 1
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
	if drop_framework_callsite:
		warnings.append(
			f"Suppressed SQL findings from {drop_framework_callsite} "
			"call(s) whose callsite was inside Frappe framework code. "
			"The loop that issues those queries lives inside frappe/* "
			"— application developers can't add an index to fix them "
			"from their code. If one of these is a hot spot, raise it "
			"upstream in the Frappe repo."
		)
	if drop_alias:
		warnings.append(
			f"Suppressed {drop_alias} EXPLAIN row(s) whose `table` value "
			"was a SQL alias (a / c / p / addr / ...) rather than a "
			"real table name. 'Full table scan on a' isn't actionable "
			"without knowing which table 'a' aliases — the per-query "
			"detail in the Top Queries section shows the actual SQL "
			"if you want to investigate."
		)
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


def _is_framework_origin(stack: list) -> bool:
	"""Return True when every user-visible frame in a SQL call's stack
	is inside frappe/* or frappe_profiler/*.

	Used by explain_flags to skip Full Scan / Filesort / Temporary
	Table / Low Filter findings whose issuing code lives in the
	framework. Same rationale as the Framework N+1 split: the
	application developer can't add an index for a query that
	Frappe issues — they'd have to patch Frappe itself.

	walk_callsite walks innermost-to-outermost for a non-framework
	frame; its fallback returns the deepest frame if ALL frames are
	framework. So a None return means "profiler-own stack" (already
	filtered elsewhere). A returned frame inside frappe/* means
	"pure frappe stack." We filter both cases.
	"""
	if not stack:
		# No stack captured. Don't filter — fall through to the
		# legacy behavior where every query produces findings.
		# This path is hit on older recordings that pre-date
		# stack-per-call capture.
		return False
	callsite = walk_callsite(stack)
	if callsite is None:
		# Pure-profiler stack → filtered (though those should
		# already be gone at this stage — defensive).
		return True
	filename = (callsite.get("filename") or "").replace("\\", "/")
	return "frappe/" in filename or "frappe_profiler/" in filename


def _is_likely_alias(table: str) -> bool:
	"""Return True when `table` looks like a SQL alias rather than a
	real table name.

	Frappe DocType tables always start with ``tab`` (``tabItem``,
	``tabSales Invoice``, ``tabCustom Field``, etc.), so anything
	that starts with a letter and is short + lowercase-only is
	almost certainly an alias:

	  ``a``   — alias
	  ``c``   — alias
	  ``ap``  — alias
	  ``cd``  — alias
	  ``addr`` — alias (common for Address)
	  ``p``   — alias
	  ``d``   — alias

	These come from EXPLAIN rows for JOIN queries where MariaDB
	uses the aliased name in the `table` column of its output.
	A finding of "Full table scan on a" has no actionable signal
	— the user can't index "a", they'd need the real table name.

	False negatives are acceptable: a legitimate short table name
	like a custom "log" table would be mis-classified as alias
	and filtered. That's rare enough that the noise reduction
	wins. True aliases (single/double letter) are MUCH more common
	than short real table names in a Frappe codebase.
	"""
	if not table:
		return True
	s = str(table).strip()
	# Real Frappe tables — always kept.
	if s.startswith("tab"):
		return False
	# Quoted identifiers (with spaces / capitals) are real tables
	# the user created with a non-standard name.
	if any(ch.isupper() for ch in s) or " " in s:
		return False
	# Non-ASCII characters — assume real table.
	if not s.isascii():
		return False
	# Anything else short + lowercase is probably an alias. 5 chars
	# is the cutoff — "users", "items" would pass; "a", "ap", "addr"
	# would be flagged.
	if len(s) <= 5 and s.replace("_", "").isalpha() and s.islower():
		return True
	# MariaDB's synthetic <derivedN> / <subqueryN> table markers.
	if s.startswith("<") and s.endswith(">"):
		return True
	return False


def _inspect_row(row, normalized_query, action_idx, query_duration, buckets):
	"""Check one EXPLAIN row against four red-flag patterns.

	Returns:
	  - ``"alias"`` when the row's table is a SQL alias (skipped,
	    caller counts it for the warning).
	  - ``None`` on normal processing.
	"""
	table = row.get("table") or "?"

	# v0.5.2: skip SQL aliases (single-letter JOIN aliases,
	# <derivedN> subquery markers). "Full table scan on a" is
	# uninterpretable — the user can't index "a", they'd need
	# the real underlying table name.
	if _is_likely_alias(table):
		return "alias"

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

	# Filesort — only worth flagging when the sort has enough rows to
	# actually matter (see MIN_ROWS_TO_FLAG_SORT). Otherwise "Filesort
	# on tabCustom DocPerm" fires on single-row parent lookups that the
	# user can't act on.
	if "using filesort" in extra and rows_examined >= MIN_ROWS_TO_FLAG_SORT:
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

	# Temporary table — same row floor as Filesort. Materializing a
	# tiny intermediate table is free; flagging it is noise.
	if "using temporary" in extra and rows_examined >= MIN_ROWS_TO_FLAG_SORT:
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
