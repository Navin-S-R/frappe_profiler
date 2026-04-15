# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Shared types for the analyzer pipeline.

Every analyzer is a pure function with this signature:

    analyze(recordings: list[dict], context: AnalyzeContext) -> AnalyzerResult

The analyzer reads the recording dicts (already enriched by analyze.py with
sqlparse-formatted queries, EXPLAIN output, normalized queries, and
exact/normalized copy counts) and returns:

    actions   — Profiler Action child rows (only per_action populates this)
    findings  — Profiler Finding child rows (each analyzer may emit findings)
    aggregate — top-level dict-shaped data (e.g. top_queries, table_breakdown)
    warnings  — non-fatal issues to surface in the report

Pure means: no Frappe DB access, no Redis access, no I/O. Analyzers operate
only on the data passed in. Side-effects are limited to the AnalyzerResult
they return. The orchestrator (analyze.py) merges all results and persists
them once.

This makes analyzers trivially unit-testable from JSON fixtures and easy to
reason about: each one is a pure data transformation.
"""

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Shared constants and helpers (Round 2 fixes #19 + #20)
# ---------------------------------------------------------------------------
# Severity sort order — lower number is higher severity. Used by every
# analyzer when sorting its findings list. Moved here from per-module
# copies to keep the ordering consistent across the pipeline.
SEVERITY_ORDER: dict[str, int] = {"High": 0, "Medium": 1, "Low": 2}

# Path prefixes we treat as "framework" when picking a representative
# callsite for a query. The goal is to blame the user's business logic,
# not the frappe helper the query was routed through (get_value,
# get_all, db.count etc.). See the detailed explanation in
# analyzers/n_plus_one.py — this is just a shared constant now.
FRAMEWORK_PREFIXES: tuple[str, ...] = (
	"frappe/",
	"frappe_profiler/",
)


def is_profiler_own_query(stack: list | None) -> bool:
	"""Return True if a SQL call's Python stack originates from the
	profiler's own instrumentation.

	Examples of queries that hit this path:

	- ``frappe_profiler/infra_capture.py:176`` — the ``SHOW GLOBAL
	  STATUS`` snapshot run inside every ``before_request`` /
	  ``after_request`` hook. Fired ~2× per captured request.
	- ``frappe_profiler/infra_capture.py`` — the one-shot ``SHOW
	  VARIABLES`` for ``max_connections`` (cached after first call).
	- Anything else the profiler queries as part of its own bookkeeping.

	These queries are real SQL that MariaDB executed, so they show up
	in the recorder's call list with stack traces. The user can't act
	on them, though — they're profiler overhead, not application work.
	Before this helper, n_plus_one would surface them as:

	    "Same query ran 22× at frappe_profiler/infra_capture.py:176"

	and top_queries would include them in the slow-queries leaderboard,
	both with the profiler's own internal file path as the "blame
	frame." Filtering them out here keeps the findings user-actionable.

	The rule (walk innermost → outermost):

	- If we find a user frame (not in ``frappe/`` and not in
	  ``frappe_profiler/``) → return False. The query came from user
	  code routed through framework helpers — keep it.
	- If we exhaust the stack seeing only ``frappe/`` and
	  ``frappe_profiler/`` frames AND at least one was
	  ``frappe_profiler/`` → return True. The deepest non-frappe frame
	  is inside the profiler, so the query originated there.
	- If we exhaust with only ``frappe/`` frames → return False. This
	  is a legitimate framework query (migration, fixture, internal
	  bg task) — the ``walk_callsite`` fallback still surfaces it.
	"""
	if not stack:
		return False
	has_profiler_frame = False
	for frame in reversed(stack):
		if not isinstance(frame, dict):
			continue
		filename = frame.get("filename") or ""
		if not filename:
			continue
		if filename.startswith("frappe_profiler/"):
			has_profiler_frame = True
			continue
		if filename.startswith("frappe/"):
			# Keep walking — the profiler or user code may be further out.
			continue
		# Non-framework frame — this is user code; the query's origin
		# is the user's business logic, not our instrumentation.
		return False
	return has_profiler_frame


def walk_callsite(stack: list | None) -> dict | None:
	"""Return the deepest non-framework frame that issued a query, or None.

	Shared implementation of the "skip frappe frames" callsite walker.
	The recorder builds `stack` outermost-to-innermost (after stripping
	its own frames), so the LAST entry is the closest /apps/ frame to
	the SQL call — but that's often a frappe framework helper. We walk
	from innermost toward outermost and return the first frame whose
	filename isn't inside a framework directory.

	Returns a dict with keys `filename`, `lineno`, `function` — or None
	if the stack is empty / malformed / belongs to profiler
	instrumentation. Falls back to the innermost frame if every frame
	is in ``frappe/`` (legitimate for queries issued from inside
	frappe migrations, fixtures, etc.) so we never silently drop a
	legitimate framework finding.

	v0.5.1: stacks whose deepest non-frappe frame is inside
	``frappe_profiler/`` (as detected by ``is_profiler_own_query``)
	return None instead of falling back to the profiler frame. The
	caller's ``if not callsite: continue`` guard then drops the query
	— otherwise the profiler's own ``SHOW GLOBAL STATUS`` snapshots
	show up as "Same query ran 22× at frappe_profiler/infra_capture
	.py:176" findings, which are noise the user can't act on.
	"""
	if not stack:
		return None

	for frame in reversed(stack):
		if not isinstance(frame, dict):
			continue
		filename = frame.get("filename") or ""
		lineno = frame.get("lineno")
		if not filename or lineno is None:
			continue
		if any(filename.startswith(prefix) for prefix in FRAMEWORK_PREFIXES):
			continue
		return frame

	# Fallback: every frame was in the framework. If the profiler itself
	# is in the stack, this is our own instrumentation — drop it.
	if is_profiler_own_query(stack):
		return None

	# Pure frappe/* fallback: return the deepest frame so legitimate
	# framework queries (migrations, fixtures, background tasks) still
	# produce a finding.
	last = stack[-1] if isinstance(stack[-1], dict) else None
	if last and last.get("filename") and last.get("lineno") is not None:
		return last
	return None


def walk_callsite_str(stack: list | None) -> str | None:
	"""String-form convenience wrapper: 'filename:lineno' or None."""
	frame = walk_callsite(stack)
	if not frame:
		return None
	return f"{frame.get('filename', '?')}:{frame.get('lineno', '?')}"


# ---------------------------------------------------------------------------
# Filename display helper (v0.5.1)
# ---------------------------------------------------------------------------
# Used by analyzers that embed filenames in user-visible finding TITLES.
#
# Frappe's DocType Data field caps at 140 characters. Apps with deeply-
# nested module paths push titles over that limit and crash the analyze
# pipeline with CharacterLengthExceededError. A production session on
# jewellery_erpnext hit this with an N+1 title:
#
#   Same query ran 65× at jewellery_erpnext/jewellery_erpnext/jewellery_
#   erpnext/doctype/parent_manufacturing_order/parent_manufacturing_order
#   .py:503
#
# That's 144 chars — just past the 140 limit. Shortening the filename to
# its last 2 path segments yields:
#
#   Same query ran 65× at parent_manufacturing_order/parent_manufacturing
#   _order.py:503
#
# ~90 chars — well under the limit — and still uniquely identifies the
# file for navigation. The full absolute path remains in the finding's
# technical_detail_json so the developer can jump to it directly.
#
# Analyzers should use this for TITLES only; customer_description and
# technical_detail_json can keep the full path for disambiguation.


def short_filename(filename: str, keep_segments: int = 2) -> str:
	"""Return the last ``keep_segments`` path components of ``filename``.

	Examples::

	    short_filename("frappe/model/document.py")                    → "model/document.py"
	    short_filename("a/b/c/d/e.py")                                → "d/e.py"
	    short_filename("erpnext.py")                                  → "erpnext.py"
	    short_filename("/Users/.../apps/frappe/frappe/handler.py")    → "frappe/handler.py"
	    short_filename("")                                            → ""

	The returned value is always <=  sum of the last N segment lengths
	plus (N - 1) slashes, which for typical Python files is 40-60 chars.
	"""
	if not filename:
		return ""
	norm = filename.replace("\\", "/")
	parts = [p for p in norm.split("/") if p]
	if not parts:
		return ""
	if len(parts) <= keep_segments:
		return "/".join(parts)
	return "/".join(parts[-keep_segments:])


@dataclass
class AnalyzerResult:
	"""Output from a single analyzer."""

	actions: list[dict] = field(default_factory=list)
	findings: list[dict] = field(default_factory=list)
	aggregate: dict[str, Any] = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)


@dataclass
class AnalyzeContext:
	"""Shared state across the analyzer pipeline.

	Holds the accumulated outputs from each analyzer as the orchestrator
	walks through them. The orchestrator calls `merge()` after each
	analyzer to fold its result into the context.
	"""

	session_uuid: str
	docname: str

	actions: list[dict] = field(default_factory=list)
	findings: list[dict] = field(default_factory=list)
	aggregate: dict[str, Any] = field(default_factory=dict)
	warnings: list[str] = field(default_factory=list)

	def merge(self, result: AnalyzerResult) -> None:
		"""Fold an analyzer's output into the context."""
		if result.actions:
			self.actions.extend(result.actions)
		if result.findings:
			self.findings.extend(result.findings)
		if result.aggregate:
			self.aggregate.update(result.aggregate)
		if result.warnings:
			self.warnings.extend(result.warnings)
