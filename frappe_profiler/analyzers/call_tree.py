# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: reconcile pyinstrument call tree with SQL recordings.

The single highest-value analyzer added in v0.3.0. Walks a per-recording
pyinstrument frame tree, grafts each captured SQL call onto the deepest
matching user-code frame, and produces a unified tree where every node
knows both its Python self-time AND the SQL queries that fired underneath
it.

Findings emitted:
  - Slow Hot Path (F1): subtree consumes >25% of action wall time AND >200ms
  - Hook Bottleneck (F3): same as F1, but the subtree root is a doc-event hook
  - Repeated Hot Frame (F2): same redacted frame in ≥3 actions, ≥500ms total

Aggregates:
  - hot_frames_json: top-20 leaderboard of hottest frames in the session
  - session_time_breakdown_json: SQL ms + per-app Python ms for the donut
"""

import json
from collections import defaultdict

from frappe_profiler.analyzers.base import (
	SEVERITY_ORDER,
	AnalyzerResult,
)


# ---------------------------------------------------------------------------
# Reconciliation primitives (Tasks 14-16)
# ---------------------------------------------------------------------------


def _summarize_explain(explain_result):
	"""Extract the four red-flag fields from an EXPLAIN result list."""
	if not explain_result or not isinstance(explain_result, list):
		return {}
	flags = {}
	for row in explain_result:
		if not isinstance(row, dict):
			continue
		t = (row.get("type") or "").lower()
		if t == "all":
			flags["full_scan"] = True
		extra = (row.get("Extra") or row.get("extra") or "").lower()
		if "using filesort" in extra:
			flags["filesort"] = True
		if "using temporary" in extra:
			flags["temporary"] = True
		filtered = row.get("filtered")
		if isinstance(filtered, (int, float)) and filtered < 10:
			flags["low_filter"] = True
	return flags


def _short_callsite(stack: list) -> str:
	"""Build a short 'file:line' string for the SQL leaf's filename slot."""
	if not stack:
		return ""
	# Walk innermost-to-outermost, return first non-framework frame
	for frame in reversed(stack):
		fn = (frame.get("filename") or "").replace("\\", "/")
		if "/frappe/" in fn or "/frappe_profiler/" in fn:
			continue
		return f"{fn}:{frame.get('lineno', 0)}"
	# Fall back to the innermost frame
	last = stack[-1]
	return f"{last.get('filename', '?')}:{last.get('lineno', 0)}"


def _make_sql_leaf(call: dict, partial_match: bool) -> dict:
	"""Build one SQL leaf node for grafting onto the pyi tree."""
	duration = float(call.get("duration") or 0)
	return {
		"function": "<sql>",
		"filename": _short_callsite(call.get("stack") or []),
		"lineno": None,
		"self_ms": duration,
		"cumulative_ms": duration,
		"kind": "sql",
		"query_normalized": call.get("normalized_query") or call.get("query") or "",
		"query_count": 1,
		"explain_flags": _summarize_explain(call.get("explain_result")),
		"partial_match": partial_match,
		"children": [],
	}


def _empty_root() -> dict:
	return {
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"self_ms": 0.0,
		"cumulative_ms": 0.0,
		"kind": "python",
		"children": [],
	}


def _normalize_dict_tree(node: dict) -> dict:
	"""Recursively ensure dict tree has all required keys."""
	return {
		"function": node.get("function", "<unknown>"),
		"filename": node.get("filename", ""),
		"lineno": node.get("lineno", 0),
		"self_ms": float(node.get("self_ms", 0)),
		"cumulative_ms": float(node.get("cumulative_ms", 0)),
		"kind": node.get("kind", "python"),
		"children": [_normalize_dict_tree(c) for c in node.get("children", [])],
	}


def _walk_pyi_frame(frame) -> dict:
	"""Convert one pyinstrument frame (and its descendants) into our dict shape."""
	# pyinstrument's Frame object exposes:
	#   function, file_path_short, line_no, self_time, time, children
	return {
		"function": getattr(frame, "function", "<unknown>"),
		"filename": (
			getattr(frame, "file_path_short", None)
			or getattr(frame, "file_path", "")
			or ""
		),
		"lineno": getattr(frame, "line_no", 0),
		"self_ms": float(getattr(frame, "self_time", 0)) * 1000,
		"cumulative_ms": float(getattr(frame, "time", 0)) * 1000,
		"kind": "python",
		"children": [_walk_pyi_frame(c) for c in getattr(frame, "children", [])],
	}


def _pyi_to_dict_tree(pyi_session_or_dict) -> dict:
	"""Convert a pyinstrument Session to our dict-tree shape.

	If the input is already a dict (from a JSON fixture), it's returned
	with normalized field names. If it's a pyinstrument Session, we walk
	its `root_frame()` and build the dict tree.

	The returned tree has a single root node with `function="<root>"`
	whose children are the actual top-level frames.
	"""
	# Test fixture path: input is already a dict
	if isinstance(pyi_session_or_dict, dict):
		# Fixture format wraps the root under a "root" key
		if "root" in pyi_session_or_dict:
			return _normalize_dict_tree(pyi_session_or_dict["root"])
		return _normalize_dict_tree(pyi_session_or_dict)

	# Production path: walk a real pyinstrument Session
	try:
		root = pyi_session_or_dict.root_frame()
	except Exception:
		return _empty_root()

	if root is None:
		return _empty_root()

	return _walk_pyi_frame(root)


# ---------------------------------------------------------------------------
# Frame matching and graft-point selection (Task 15)
# ---------------------------------------------------------------------------


# Module path prefixes treated as "framework" — graft points roll back
# past these so SQL leaves attach under user code, not under helpers.
_FRAMEWORK_PATH_FRAGMENTS = ("/frappe/", "/frappe_profiler/")
_FRAMEWORK_FUNCTION_PREFIXES = ("frappe.", "frappe_profiler.")


def _is_framework_frame(node_or_frame: dict) -> bool:
	"""True if a tree node or stack frame looks like framework code."""
	fn = node_or_frame.get("function") or ""
	for prefix in _FRAMEWORK_FUNCTION_PREFIXES:
		if fn.startswith(prefix):
			return True
	filename = (node_or_frame.get("filename") or "").replace("\\", "/")
	for frag in _FRAMEWORK_PATH_FRAGMENTS:
		if frag in filename:
			return True
	return False


def _frames_match(node: dict, frame: dict) -> bool:
	"""Fuzzy match: same function name, ignoring filename prefix differences."""
	return (node.get("function") or "") == (frame.get("function") or "")


def _find_graft_point(root: dict, stack: list):
	"""Walk a recorder stack against the pyi tree; return (graft_node, partial).

	The graft node is the deepest node that *matches* a frame in the
	recorder stack. If matching descends past framework frames, the graft
	point is rolled back to the deepest non-framework ancestor.

	Returns (graft_point, partial_match: bool) where partial_match is
	True if the stack had more frames than the tree could match.
	"""
	if not stack:
		return root, False

	current = root
	matched_user_node = root
	last_matched_index = -1

	for i, frame in enumerate(stack):
		# Look for a child of `current` that matches this frame
		next_child = None
		for child in current.get("children", []):
			if _frames_match(child, frame):
				next_child = child
				break
		if next_child is None:
			# No further match — stop descending
			break
		current = next_child
		last_matched_index = i
		# Track the deepest non-framework match — that's where we graft.
		if not _is_framework_frame(current):
			matched_user_node = current

	# partial_match is True if the recorder stack had unconsumed frames
	partial = last_matched_index < (len(stack) - 1)

	# If the only matches were framework frames, fall back to root
	# (matched_user_node remains root in that case).
	return matched_user_node, partial


def _coalesce_sql_siblings(node: dict) -> None:
	"""Recursively merge identical SQL leaf siblings under each parent.

	Coalescing key: (query_normalized, partial_match). Coalesced nodes
	get an `is_n_plus_one_hint=True` flag the renderer uses to show
	`<sql> ×N` instead of N separate rows. The n_plus_one analyzer still
	emits its own finding — this is just a renderer hint.
	"""
	children = node.get("children") or []
	if not children:
		return

	# Bucket SQL children by (normalized_query, partial_match)
	merged: dict = {}
	non_sql: list = []
	for child in children:
		if child.get("kind") == "sql":
			key = (child.get("query_normalized", ""), child.get("partial_match", False))
			if key in merged:
				existing = merged[key]
				existing["self_ms"] += child.get("self_ms", 0)
				existing["cumulative_ms"] += child.get("cumulative_ms", 0)
				existing["query_count"] += child.get("query_count", 1)
				existing["is_n_plus_one_hint"] = True
			else:
				# Shallow copy so we don't mutate the original
				merged[key] = dict(child)
		else:
			non_sql.append(child)

	# Recurse into non-SQL children
	for child in non_sql:
		_coalesce_sql_siblings(child)

	node["children"] = non_sql + list(merged.values())


def reconcile(pyi_session_or_dict, sql_calls: list, action_wall_time_ms: float) -> dict:
	"""Build a unified call tree from a pyinstrument session + SQL calls.

	This is the main reconciliation entry point. Returns a dict tree with
	SQL leaves grafted at the deepest user-code ancestor of each call's
	stack, identical SQL siblings coalesced, and partial-match flags set
	where the recorder stack ran past the pyi tree's visible depth.

	Time-accounting invariant: pyinstrument has already counted SQL time
	in every Python ancestor's cumulative_ms. We DO NOT subtract — the
	leaf's self_ms is informational only. The renderer always reads
	cumulative_ms from the node itself, never sums children.
	"""
	tree = _pyi_to_dict_tree(pyi_session_or_dict)

	for call in sql_calls or []:
		stack = call.get("stack") or []
		graft_point, partial = _find_graft_point(tree, stack)
		leaf = _make_sql_leaf(call, partial_match=partial)
		graft_point.setdefault("children", []).append(leaf)

	_coalesce_sql_siblings(tree)
	return tree
