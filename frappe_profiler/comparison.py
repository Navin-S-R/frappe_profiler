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
