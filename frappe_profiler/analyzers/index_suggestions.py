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

# v0.5.1: prefix length for text/blob column indexes. MariaDB requires
# a length hint for TEXT/BLOB columns and for VARCHARs that would
# exceed the 767-byte index-key limit. 255 is a safe default for
# common Frappe Data / Small Text columns; developers can adjust the
# prefix length based on the actual column's selectivity profile.
TEXT_INDEX_PREFIX_LENGTH = 255

# Column types that CANNOT be usefully indexed with a simple
# single-column btree index. JSON requires a functional or generated-
# column approach, which is outside the scope of a heuristic suggestion.
_UNINDEXABLE_TYPES = frozenset({"json", "geometry"})

# Column types that REQUIRE a prefix length when indexing.
_PREFIX_REQUIRED_TYPES = frozenset({
	"text", "tinytext", "mediumtext", "longtext",
	"blob", "tinyblob", "mediumblob", "longblob",
})


def _get_indexed_columns(table: str) -> set[str]:
	"""Return the set of columns on ``table`` that already have at
	least one index WHERE this column is the leftmost of the index key.

	Non-leftmost composite columns are NOT counted — MariaDB's btree
	can't use the index when a query filters on just that column, so
	a Missing Index suggestion is still actionable.

	Returns an empty set on any DB error (table missing, access denied,
	no real Frappe site), which conservatively keeps all suggestions.
	"""
	try:
		import frappe
		rows = frappe.db.sql(f"SHOW INDEX FROM `{table}`", as_dict=True) or []
	except Exception:
		return set()

	indexed: set[str] = set()
	for row in rows:
		# SHOW INDEX returns "Seq_in_index" — 1-indexed. We only count
		# columns at position 1 because a composite index
		# (a, b, c) only accelerates queries filtering on a, (a,b),
		# or (a,b,c); a query on just b or c wouldn't use it.
		try:
			seq = int(row.get("Seq_in_index") or row.get("seq_in_index") or 0)
		except (TypeError, ValueError):
			seq = 0
		if seq == 1:
			col = row.get("Column_name") or row.get("column_name")
			if col:
				indexed.add(col)
	return indexed


def _get_column_types(table: str) -> dict[str, str]:
	"""Return ``{column_name: data_type_lower}`` for ``table`` or an
	empty dict on DB error. Used to check indexability and to pick
	the right DDL shape (plain index vs. prefix index)."""
	try:
		import frappe
		rows = frappe.db.sql(
			"""
			SELECT column_name, data_type
			FROM information_schema.columns
			WHERE table_schema = DATABASE() AND table_name = %s
			""",
			(table,),
			as_dict=True,
		) or []
	except Exception:
		return {}

	out: dict[str, str] = {}
	for r in rows:
		name = r.get("column_name") or r.get("COLUMN_NAME")
		dtype = (r.get("data_type") or r.get("DATA_TYPE") or "").lower()
		if name:
			out[name] = dtype
	return out


def _classify_column(
	table: str,
	column: str,
	indexed_cache: dict[str, set[str]],
	type_cache: dict[str, dict[str, str]],
) -> tuple[str, str | None]:
	"""Classify a suggested (table, column) pair.

	Returns ``(status, ddl_or_reason)`` where status is one of:

	  - ``"actionable"``   — column exists, is not indexed, can be
	                         indexed; second element is the DDL string
	  - ``"already_indexed"`` — column exists and is already the
	                         leftmost of at least one index; drop the
	                         suggestion, second element explains
	  - ``"unindexable"``  — column is JSON / geometry or doesn't exist
	                         on the table; drop the suggestion, second
	                         element explains
	  - ``"unknown"``      — information_schema lookup failed (likely
	                         no real DB, dev environment); KEEP the
	                         suggestion with a plain DDL (legacy
	                         behavior) so we don't silently suppress
	                         the old pipeline
	"""
	# Memoize per-table lookups so a suggestion with N columns on
	# the same table only pays one round trip.
	if table not in indexed_cache:
		indexed_cache[table] = _get_indexed_columns(table)
	if table not in type_cache:
		type_cache[table] = _get_column_types(table)

	indexed = indexed_cache[table]
	types = type_cache[table]

	# Fallback: both lookups empty → we probably have no real DB
	# (unit test, pre-migrate site). Return "unknown" so the caller
	# keeps the suggestion as-is using the legacy plain-index DDL.
	if not types and not indexed:
		return "unknown", (
			f"ALTER TABLE `{table}` ADD INDEX `idx_{column}` (`{column}`);"
		)

	# Column doesn't exist on the table.
	if types and column not in types:
		return "unindexable", (
			f"column `{column}` does not exist on table `{table}` "
			f"— the optimizer's suggestion is likely a parse error; "
			f"verify the query and try again"
		)

	# Column is already the leftmost of at least one index.
	if column in indexed:
		return "already_indexed", (
			f"column `{column}` on `{table}` is already indexed "
			f"(leftmost of an existing btree); no action needed"
		)

	dtype = types.get(column, "") if types else ""

	if dtype in _UNINDEXABLE_TYPES:
		return "unindexable", (
			f"column `{column}` on `{table}` has type `{dtype}`, "
			f"which cannot be indexed with a simple btree. Consider "
			f"a functional index or a generated column."
		)

	if dtype in _PREFIX_REQUIRED_TYPES:
		return "actionable", (
			f"ALTER TABLE `{table}` ADD INDEX `idx_{column}` "
			f"(`{column}`({TEXT_INDEX_PREFIX_LENGTH}));"
		)

	# Regular indexable type (varchar, int, datetime, etc.) — and
	# even if we don't recognise the dtype (empty string because
	# information_schema lookup returned no row), fall through to
	# plain DDL rather than silently drop.
	return "actionable", (
		f"ALTER TABLE `{table}` ADD INDEX `idx_{column}` (`{column}`);"
	)


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

	# v0.5.1: verify each suggested (table, column) against
	# information_schema before emitting a finding. Pre-v0.5.1 the
	# analyzer blindly trusted the DBOptimizer heuristic and produced
	# false-positive findings for:
	#   1. columns already indexed (primary keys like `name`, framework
	#      columns `parent`/`owner`/`modified`/`creation`, and every
	#      Link / Data field with search_index: 1)
	#   2. columns with types that can't be btree-indexed (JSON, geometry)
	#   3. TEXT/BLOB columns where plain `ADD INDEX (col)` fails in
	#      MariaDB because the column exceeds the 767-byte key limit
	#   4. columns that don't exist on the table (rare, usually a
	#      sql_metadata parse error)
	#
	# We now classify each suggestion and drop / adjust accordingly.
	indexed_cache: dict[str, set[str]] = {}
	type_cache: dict[str, dict[str, str]] = {}
	drop_already_indexed = 0
	drop_unindexable = 0
	unindexable_reasons: list[str] = []

	findings = []
	for (table, column), bucket in suggestion_buckets.items():
		status, ddl_or_reason = _classify_column(
			table, column, indexed_cache, type_cache
		)

		if status == "already_indexed":
			drop_already_indexed += 1
			continue
		if status == "unindexable":
			drop_unindexable += 1
			unindexable_reasons.append(ddl_or_reason)
			continue

		# status is "actionable" or "unknown" — both keep the
		# suggestion. "unknown" uses the legacy plain-DDL template,
		# matching pre-v0.5.1 behavior for environments without a
		# live DB connection.
		suggested_ddl = ddl_or_reason
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
						"suggested_ddl": suggested_ddl,
						"affected_query_count": bucket["count"],
						"estimated_savings_ms": round(impact_ms, 2),
						"example_queries": bucket["queries"],
						"verified_not_indexed": status == "actionable",
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
	if drop_already_indexed:
		warnings.append(
			f"Suppressed {drop_already_indexed} index suggestion(s) whose "
			"target column was already indexed (would have been a false "
			"positive — the DBOptimizer heuristic doesn't check existing "
			"indexes)."
		)
	if drop_unindexable:
		# Keep the warning short but log the reasons so curious users
		# can check Error Log for the exact column/type mismatches.
		warnings.append(
			f"Suppressed {drop_unindexable} index suggestion(s) whose "
			"target column can't be btree-indexed directly (JSON / "
			"nonexistent / etc.)."
		)
		try:
			import frappe
			for reason in unindexable_reasons[:5]:
				frappe.log_error(
					title="frappe_profiler index_suggestions dropped",
					message=reason,
				)
		except Exception:
			pass

	return AnalyzerResult(findings=findings, warnings=warnings)


def _severity(impact_ms: float) -> str:
	if impact_ms > HIGH_IMPACT_MS:
		return "High"
	if impact_ms > MEDIUM_IMPACT_MS:
		return "Medium"
	return "Low"
