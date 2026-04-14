# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Pure-function comparison helpers for frappe_profiler v0.4.0.

Given two Profiler Session docs (the "new" session and the "baseline"),
produce the data structure the renderer embeds into the report's
comparison sections. No Frappe DB access inside this module — callers
pass already-loaded session docs.

Three public functions:
  - compute_comparison(new_session, baseline_session) -> dict
  - match_actions(new_actions, baseline_actions) -> list[dict]
  - match_findings(new_findings, baseline_findings) -> dict
"""

import json
from collections import defaultdict


# Finding types whose technical_detail_json carries a `function` field
_FUNCTION_FINDING_TYPES = {
	"Slow Hot Path",
	"Hook Bottleneck",
	"Slow Query",
	"Repeated Hot Frame",
}

# Finding types whose technical_detail_json carries a `table` field
_TABLE_FINDING_TYPES = {
	"Full Table Scan",
	"Filesort",
	"Temporary Table",
	"Low Filter Ratio",
}


def _parse_td(technical_detail_json):
	"""Parse technical_detail_json safely; return dict or None."""
	if not technical_detail_json:
		return None
	try:
		return json.loads(technical_detail_json)
	except Exception:
		return None


def _extract_callsite_key(finding_type: str, technical_detail_json):
	"""Build the matching key for a finding's callsite.

	Returns a hashable value (string or tuple) used by match_findings
	as part of the composite key. Returns None for unknown finding types
	or malformed JSON; finding pairs with None keys still match each
	other if their (finding_type, action_ref) match.
	"""
	td = _parse_td(technical_detail_json)
	if td is None:
		return None

	if finding_type in _FUNCTION_FINDING_TYPES:
		return td.get("function")

	if finding_type in _TABLE_FINDING_TYPES:
		return td.get("table")

	if finding_type == "N+1 Query":
		return td.get("callsite") or td.get("function")

	if finding_type == "Missing Index":
		columns = td.get("columns")
		if isinstance(columns, list):
			columns = tuple(columns)
		return (td.get("table"), columns)

	if finding_type == "Redundant Call":
		fn_name = td.get("fn_name")
		safe = td.get("identifier_safe")
		first = None
		if isinstance(safe, (list, tuple)) and safe:
			first = safe[0]
		elif isinstance(safe, str):
			first = safe
		return (fn_name, first)

	# Unknown finding type — return None
	return None


def _finding_match_key(finding):
	"""Composite match key: (type, action_ref_str, callsite_key)."""
	return (
		finding.get("finding_type"),
		str(finding.get("action_ref")) if finding.get("action_ref") is not None else None,
		_extract_callsite_key(
			finding.get("finding_type"),
			finding.get("technical_detail_json"),
		),
	)


def _severity_delta_string(baseline_sev, new_sev):
	"""Build a 'High → Medium' style delta string, or None if unchanged."""
	if baseline_sev == new_sev:
		return None
	return f"{baseline_sev} → {new_sev}"


def match_findings(new_findings: list, baseline_findings: list) -> dict:
	"""Bucket findings into fixed / new / unchanged.

	Match key: (finding_type, action_ref_str, callsite_key). Two findings
	are "the same finding" if they share all three.

	Returns:
	    {
	        "fixed":     [<finding dicts in baseline, gone in new>],
	        "new":       [<finding dicts in new, absent in baseline>],
	        "unchanged": [<finding dicts present in both, with delta fields>],
	    }
	Unchanged items are augmented with:
	  - delta_impact_ms: new.estimated_impact_ms - baseline.estimated_impact_ms
	  - delta_impact_pct: percentage of baseline (negative = improved)
	  - delta_severity:   "High → Medium" or None
	  - baseline:         the baseline finding dict (for reference)
	"""
	baseline_by_key = {}
	for f in (baseline_findings or []):
		key = _finding_match_key(f)
		baseline_by_key.setdefault(key, []).append(f)

	fixed = []
	new = []
	unchanged = []

	for f in (new_findings or []):
		key = _finding_match_key(f)
		bucket = baseline_by_key.get(key, [])
		if not bucket:
			new.append(dict(f))
			continue
		baseline_finding = bucket.pop(0)
		new_impact = f.get("estimated_impact_ms") or 0
		baseline_impact = baseline_finding.get("estimated_impact_ms") or 0
		delta = new_impact - baseline_impact
		pct = 0
		if baseline_impact:
			pct = round((delta / baseline_impact) * 100)
		augmented = dict(f)
		augmented["delta_impact_ms"] = round(delta, 2)
		augmented["delta_impact_pct"] = pct
		augmented["delta_severity"] = _severity_delta_string(
			baseline_finding.get("severity"),
			f.get("severity"),
		)
		augmented["baseline"] = baseline_finding
		unchanged.append(augmented)

	# Anything left in baseline_by_key was not matched → fixed
	for remaining in baseline_by_key.values():
		fixed.extend(dict(f) for f in remaining)

	return {"fixed": fixed, "new": new, "unchanged": unchanged}


def _make_action_pair(status, baseline=None, new=None):
	"""Build an action-pair dict with computed deltas (or None for unmatched)."""
	delta_ms = None
	delta_queries = None
	delta_query_time_ms = None
	if status == "matched" and baseline is not None and new is not None:
		delta_ms = round(
			(new.get("duration_ms") or 0) - (baseline.get("duration_ms") or 0), 2
		)
		delta_queries = (new.get("queries_count") or 0) - (baseline.get("queries_count") or 0)
		delta_query_time_ms = round(
			(new.get("query_time_ms") or 0) - (baseline.get("query_time_ms") or 0), 2
		)
	return {
		"status": status,
		"baseline": baseline,
		"new": new,
		"delta_ms": delta_ms,
		"delta_queries": delta_queries,
		"delta_query_time_ms": delta_query_time_ms,
	}


def match_actions(new_actions: list, baseline_actions: list) -> list:
	"""Pair actions from two sessions.

	Matching strategy:
	  1. Exact action_label match. Positional within duplicates: first
	     occurrence of a label in new pairs with first occurrence in
	     baseline.
	  2. Fallback: match unmatched new actions to unmatched baseline
	     actions by path alone (covers session-label renames).
	  3. Anything still unmatched goes to only_in_baseline / only_in_new.

	Returns a flat list of action-pair dicts.
	"""
	# Step 1: build per-label queues for baseline
	baseline_by_label = defaultdict(list)
	for action in (baseline_actions or []):
		baseline_by_label[action.get("action_label")].append(action)

	pairs = []
	new_unmatched = []

	for action in (new_actions or []):
		label = action.get("action_label")
		queue = baseline_by_label.get(label)
		if queue:
			baseline_action = queue.pop(0)
			pairs.append(_make_action_pair("matched", baseline_action, action))
		else:
			new_unmatched.append(action)

	# Step 2: fallback to path matching for any remaining new actions
	baseline_remaining_by_path = defaultdict(list)
	for queue in baseline_by_label.values():
		for action in queue:
			baseline_remaining_by_path[action.get("path")].append(action)

	still_new_unmatched = []
	for action in new_unmatched:
		path = action.get("path")
		queue = baseline_remaining_by_path.get(path)
		if queue:
			baseline_action = queue.pop(0)
			pairs.append(_make_action_pair("matched", baseline_action, action))
		else:
			still_new_unmatched.append(action)

	# Step 3: emit only_in_baseline for anything left
	for queue in baseline_remaining_by_path.values():
		for action in queue:
			pairs.append(_make_action_pair("only_in_baseline", baseline=action))

	# Emit only_in_new for the remaining unmatched new actions
	for action in still_new_unmatched:
		pairs.append(_make_action_pair("only_in_new", new=action))

	return pairs


def _delta_dict(old, new):
	"""Build a per-metric delta dict {old, new, delta, pct}."""
	old = old or 0
	new = new or 0
	delta = new - old
	pct = 0
	if old:
		pct = round((delta / old) * 100)
	return {
		"old": round(old, 2),
		"new": round(new, 2),
		"delta": round(delta, 2),
		"pct": pct,
	}


def _action_to_dict(action):
	"""Coerce a Profiler Action child row (Document or SimpleNamespace) to a dict."""
	if isinstance(action, dict):
		return dict(action)
	return {
		"action_label": getattr(action, "action_label", None),
		"path": getattr(action, "path", None),
		"http_method": getattr(action, "http_method", None),
		"event_type": getattr(action, "event_type", None),
		"duration_ms": getattr(action, "duration_ms", 0),
		"queries_count": getattr(action, "queries_count", 0),
		"query_time_ms": getattr(action, "query_time_ms", 0),
	}


def _finding_to_dict(finding):
	"""Coerce a Profiler Finding child row to a dict."""
	if isinstance(finding, dict):
		return dict(finding)
	return {
		"finding_type": getattr(finding, "finding_type", None),
		"severity": getattr(finding, "severity", None),
		"title": getattr(finding, "title", None),
		"customer_description": getattr(finding, "customer_description", None),
		"technical_detail_json": getattr(finding, "technical_detail_json", None),
		"estimated_impact_ms": getattr(finding, "estimated_impact_ms", 0),
		"affected_count": getattr(finding, "affected_count", 0),
		"action_ref": getattr(finding, "action_ref", None),
	}


def compute_comparison(new_session, baseline_session) -> dict:
	"""Build the full comparison data structure for the renderer.

	Both arguments are Profiler Session docs (real Frappe docs OR
	SimpleNamespace fixtures). They expose:
	  - actions: list of Profiler Action child rows
	  - findings: list of Profiler Finding child rows
	  - total_duration_ms, total_queries, total_query_time_ms,
	    total_python_ms, total_sql_ms
	  - name, title, started_at

	Returns a dict ready for the Jinja template (see spec §5.5).
	"""
	new_actions = [_action_to_dict(a) for a in (new_session.actions or [])]
	baseline_actions = [_action_to_dict(a) for a in (baseline_session.actions or [])]
	new_findings = [_finding_to_dict(f) for f in (new_session.findings or [])]
	baseline_findings = [_finding_to_dict(f) for f in (baseline_session.findings or [])]

	return {
		"baseline_info": {
			"docname": getattr(baseline_session, "name", None),
			"title": getattr(baseline_session, "title", None),
			"started_at": str(getattr(baseline_session, "started_at", "")),
			"duration_ms": getattr(baseline_session, "total_duration_ms", 0),
			"total_queries": getattr(baseline_session, "total_queries", 0),
		},
		"session_delta": {
			"duration_ms": _delta_dict(
				getattr(baseline_session, "total_duration_ms", 0),
				getattr(new_session, "total_duration_ms", 0),
			),
			"total_queries": _delta_dict(
				getattr(baseline_session, "total_queries", 0),
				getattr(new_session, "total_queries", 0),
			),
			"total_query_time_ms": _delta_dict(
				getattr(baseline_session, "total_query_time_ms", 0),
				getattr(new_session, "total_query_time_ms", 0),
			),
			"total_python_ms": _delta_dict(
				getattr(baseline_session, "total_python_ms", 0),
				getattr(new_session, "total_python_ms", 0),
			),
			"total_sql_ms": _delta_dict(
				getattr(baseline_session, "total_sql_ms", 0),
				getattr(new_session, "total_sql_ms", 0),
			),
		},
		"action_pairs": match_actions(new_actions, baseline_actions),
		"finding_diff": match_findings(new_findings, baseline_findings),
	}
