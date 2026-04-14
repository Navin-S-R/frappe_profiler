# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for findings, aggregation, and analyzer entry point of call_tree."""

from frappe_profiler.analyzers import call_tree
from frappe_profiler.analyzers.base import AnalyzeContext


def _node(function, filename, cumulative_ms, children=None, self_ms=0):
	return {
		"function": function,
		"filename": filename,
		"lineno": 1,
		"self_ms": self_ms,
		"cumulative_ms": cumulative_ms,
		"kind": "python",
		"children": children or [],
	}


# ---------------------------------------------------------------------------
# Pruning + soft cap
# ---------------------------------------------------------------------------


def test_prune_drops_nodes_below_threshold():
	tree = _node("<root>", "", 1000, [
		_node("big", "u.py", 800, []),
		_node("medium", "u.py", 50, []),
		_node("tiny1", "u.py", 1, []),
		_node("tiny2", "u.py", 1, []),
	])
	# Threshold: max(2, 1000 * 0.005) = 5ms
	pruned = call_tree._prune(tree, action_wall_time_ms=1000, threshold_pct=0.005)
	functions = [c["function"] for c in pruned["children"]]
	assert "big" in functions
	assert "medium" in functions
	assert any("[other:" in f for f in functions)
	assert "tiny1" not in functions
	assert "tiny2" not in functions


def test_prune_never_drops_sql_leaves():
	"""SQL leaves are always preserved regardless of size."""
	tree = _node("<root>", "", 1000, [
		_node("big", "u.py", 800, [
			{"function": "<sql>", "filename": "u.py:5", "kind": "sql",
			 "self_ms": 0.1, "cumulative_ms": 0.1, "query_count": 1,
			 "partial_match": False, "children": []},
		]),
	])
	pruned = call_tree._prune(tree, action_wall_time_ms=1000, threshold_pct=0.005)
	big = pruned["children"][0]
	# The 0.1ms sql leaf is below threshold but must survive
	assert any(c.get("kind") == "sql" for c in big["children"])


def test_soft_cap_preserves_hot_path():
	"""Cap drops cold siblings, never the hot descending path."""
	deep = _node("hot5", "u.py", 90, [])
	for level in (4, 3, 2, 1):
		deep = _node(f"hot{level}", "u.py", 90, [
			deep,
			_node(f"cold{level}", "u.py", 1, []),
		])
	tree = _node("<root>", "", 100, [deep])
	capped = call_tree._soft_cap_nodes(tree, max_nodes=6)

	collected = []

	def walk(n):
		collected.append(n["function"])
		for c in n.get("children", []):
			walk(c)
	walk(capped)

	for level in range(1, 6):
		assert f"hot{level}" in collected, f"hot{level} dropped from capped tree"


# ---------------------------------------------------------------------------
# Per-action findings (F1 Slow Hot Path + F3 Hook Bottleneck)
# ---------------------------------------------------------------------------


def test_emit_slow_hot_path_finding_above_thresholds():
	tree = _node("<root>", "", 1000, [
		_node("my_app.heavy_thing", "apps/my_app/x.py", 600, []),
		_node("other", "apps/my_app/y.py", 100, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="Sales Invoice submit",
		action_wall_time_ms=1000,
	)
	slow = [f for f in findings if f["finding_type"] == "Slow Hot Path"]
	assert len(slow) == 1
	assert slow[0]["severity"] == "High"
	assert "my_app.heavy_thing" in slow[0]["title"]
	assert slow[0]["estimated_impact_ms"] == 600
	assert slow[0]["action_ref"] == "0"


def test_no_finding_below_pct_threshold():
	# 20% < 25% threshold
	tree = _node("<root>", "", 1000, [
		_node("my_app.thing", "apps/my_app/x.py", 200, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="x", action_wall_time_ms=1000,
	)
	assert findings == []


def test_no_finding_below_absolute_threshold():
	# 30% but only 150ms
	tree = _node("<root>", "", 500, [
		_node("my_app.thing", "apps/my_app/x.py", 150, []),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="x", action_wall_time_ms=500,
	)
	assert findings == []


def test_hook_bottleneck_emitted_instead_of_slow_hot_path():
	"""F3 takes precedence when ancestor is Document.run_method."""
	tree = _node("<root>", "", 1000, [
		_node("frappe.model.document.Document.run_method",
		      "apps/frappe/model/document.py", 800, [
			_node("erpnext.selling.doctype.sales_invoice.sales_invoice.validate",
			      "apps/erpnext/x.py", 700, []),
		]),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="SI submit", action_wall_time_ms=1000,
	)
	# Should emit F3 (Hook Bottleneck), NOT F1 (Slow Hot Path)
	# Note: the run_method node itself starts with "frappe." so it matches
	# the "framework" prefix check in _walk_for_findings. The ACTUAL hook
	# (validate) is its child, and validate's ancestor chain contains
	# Document.run_method, so validate gets the Hook Bottleneck label.
	assert any(f["finding_type"] == "Hook Bottleneck" for f in findings)
	assert not any(f["finding_type"] == "Slow Hot Path" for f in findings)


def test_slow_hot_path_suppressed_when_dominated_by_sql():
	"""F1 is suppressed when >80% of subtree time is a single SQL leaf."""
	sql_leaf = {
		"function": "<sql>", "filename": "u.py:5", "kind": "sql",
		"self_ms": 700, "cumulative_ms": 700,
		"query_normalized": "SELECT 1", "query_count": 1,
		"partial_match": False, "children": [],
	}
	tree = _node("<root>", "", 1000, [
		_node("my_app.thing", "apps/my_app/x.py", 800, [sql_leaf]),
	])
	findings = call_tree._emit_per_action_findings(
		tree, action_idx=0, action_label="x", action_wall_time_ms=1000,
	)
	# 700/800 = 87.5% — suppressed (top_queries handles it)
	assert findings == []


# ---------------------------------------------------------------------------
# Cross-action aggregation
# ---------------------------------------------------------------------------


def test_repeated_hot_frame_finding_aggregates_across_actions():
	# Same frame in 4 different actions, total 800ms
	per_action_trees = []
	for i in range(4):
		t = _node("<root>", "", 500, [
			_node("my_app.discounts.calc", "apps/my_app/d.py", 200, []),
		])
		per_action_trees.append(t)

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]
	assert len(repeated) == 1
	assert "my_app.discounts.calc" in repeated[0]["title"]
	assert repeated[0]["estimated_impact_ms"] == 800


def test_repeated_hot_frame_no_finding_when_below_thresholds():
	# Only 2 actions (need 3) and only 200ms total (need 500)
	per_action_trees = [
		_node("<root>", "", 500, [_node("my_app.tiny", "apps/my_app/t.py", 100, [])]),
		_node("<root>", "", 500, [_node("my_app.tiny", "apps/my_app/t.py", 100, [])]),
	]
	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	assert findings == []


def test_repeated_hot_frame_does_not_collapse_same_named_wrappers():
	"""Pass-7 architect-review regression guard: the dedup key must include
	filename so different functions sharing a name (35 different ``wrapper``
	decorators, 20 different ``handle`` methods) don't all collapse into one
	misleading 'Repeated Hot Frame' finding.

	Real-world failure: a user's session captured this finding:
	    'High — wrapper appeared in 11 actions and consumed 3534ms total'
	Every one of those 11 was a DIFFERENT decorator wrapper from a
	different module (functools, werkzeug, cached_property, etc.). The
	finding was actionable only in the sense of 'optimize... something
	called wrapper?' — which is nothing the user can actually do.
	"""
	per_action_trees = []
	# Four different 'wrapper' functions in four different files.
	# Each appears across enough actions and ms to trigger aggregation,
	# but only WITHIN its own file. No cross-file collapse.
	for i in range(4):
		per_action_trees.append(_node("<root>", "", 500, [
			_node("wrapper", "my_app/a.py", 50, []),
			_node("wrapper", "my_app/b.py", 50, []),
			_node("wrapper", "my_app/c.py", 50, []),
			_node("wrapper", "my_app/d.py", 50, []),
		]))

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)

	# The leaderboard must have 4 separate entries for the 4 different
	# wrappers, not a single collapsed 'wrapper' entry.
	wrapper_entries = [r for r in leaderboard if "wrapper" in r["function"]]
	assert len(wrapper_entries) == 4, (
		f"Expected 4 distinct wrapper entries (one per file), got "
		f"{len(wrapper_entries)}. Keys: {[r['function'] for r in leaderboard]}"
	)
	# Each entry's total_ms must be 200 (4 actions × 50ms), NOT 800
	# (which would indicate collapsed aggregation).
	for entry in wrapper_entries:
		assert entry["total_ms"] == 200, (
			f"Each wrapper's per-file total should be 200ms; got "
			f"{entry['total_ms']} for key {entry['function']}"
		)


def test_repeated_hot_frame_skips_pure_helpers_only():
	"""v0.5.1: Repeated Hot Frame aggregator uses the NARROWER
	_is_pure_helper_frame filter, not the broad _is_framework_frame.
	Pure plumbing helpers (frappe/handler.py, frappe/utils/, werkzeug,
	rq, pyinstrument) are suppressed — those are unoptimizable.
	"""
	per_action_trees = []
	for _ in range(5):
		per_action_trees.append(_node("<root>", "", 500, [
			# frappe/handler.py — pure request-dispatch plumbing
			_node("handle", "apps/frappe/handler.py", 250, []),
			# frappe/utils/data.py — type conversion helper
			_node("cint", "apps/frappe/frappe/utils/data.py", 100, []),
			# werkzeug — infra lib
			_node(
				"inner",
				"env/lib/python3.14/site-packages/werkzeug/wsgi.py",
				80,
				[],
			),
		]))

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)

	# None of the pure-helper frames should appear as findings.
	suppressed = ("handle", "cint", "inner")
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]
	for f in repeated:
		for name in suppressed:
			assert name not in f["title"], (
				f"pure helper '{name}' leaked into Repeated Hot Frame "
				f"findings: {f['title']}"
			)

	# And not in the leaderboard either.
	for row in leaderboard:
		for name in suppressed:
			assert name not in row["function"], (
				f"pure helper '{name}' leaked into hot-frames leaderboard: {row}"
			)


def test_repeated_hot_frame_keeps_frappe_application_code():
	"""v0.5.1 correction (requested in user review): the Repeated Hot
	Frame aggregator must KEEP most of frappe/* — only pure helpers
	(utils, handler, app.py) get suppressed. Application-layer Frappe
	code like Document.run_method, permissions.has_permission, and
	naming.make_autoname are LEGITIMATE optimization targets even
	though they're inside frappe/*:

	  - Document.run_method runs user-defined doc-event hooks. A slow
	    hook bubbles up here and the user CAN optimize it.
	  - permissions.has_permission evaluates user-defined permission
	    rules (including custom Permission Query Conditions). Slow
	    permissions are the user's fault.
	  - make_autoname runs a naming series; if the user configured a
	    ledger-heavy naming series it's slow-by-choice and fixable.

	A naive 'skip all frappe/*' filter would hide all of these — wrong.
	"""
	per_action_trees = []
	for _ in range(5):
		per_action_trees.append(_node("<root>", "", 2000, [
			# Document.run_method in frappe/model/document.py — keep.
			_node(
				"Document.run_method",
				"apps/frappe/frappe/model/document.py",
				600,
				[],
			),
			# permissions.has_permission — keep.
			_node(
				"has_permission",
				"apps/frappe/frappe/permissions.py",
				200,
				[],
			),
		]))

	findings, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)

	# Document.run_method: 5 actions × 600ms = 3000ms, far above threshold.
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]
	run_method_hits = [f for f in repeated if "run_method" in f["title"]]
	assert len(run_method_hits) == 1, (
		"Document.run_method in frappe/model/document.py MUST be kept "
		"in the Repeated Hot Frame aggregator. It runs user hooks and "
		"is a legitimate optimization target."
	)

	# And both keepers should be in the leaderboard.
	leaderboard_fns = [r["function"] for r in leaderboard]
	assert any("run_method" in f for f in leaderboard_fns), (
		f"Document.run_method missing from leaderboard: {leaderboard_fns}"
	)
	assert any("has_permission" in f for f in leaderboard_fns), (
		f"has_permission missing from leaderboard: {leaderboard_fns}"
	)


def test_repeated_hot_frame_keeps_user_code_finding():
	"""Companion positive test to the two above: user-code frames are
	still aggregated correctly and still fire findings.
	"""
	per_action_trees = []
	for _ in range(5):
		per_action_trees.append(_node("<root>", "", 1000, [
			_node("compute_taxes", "erpnext/accounts/tax.py", 200, []),
		]))

	findings, _ = call_tree._aggregate_hot_frames(per_action_trees)
	repeated = [f for f in findings if f["finding_type"] == "Repeated Hot Frame"]
	assert len(repeated) == 1
	# The finding key now includes the filename prefix.
	assert "compute_taxes" in repeated[0]["title"]
	assert "tax.py" in repeated[0]["title"] or "accounts/tax" in repeated[0]["title"]


def test_hot_frames_leaderboard_top_20_sorted_desc():
	per_action_trees = []
	for i in range(25):
		per_action_trees.append(_node("<root>", "", 1000, [
			_node(f"my_app.func_{i:02d}", f"apps/my_app/f{i}.py", float(100 + i), []),
		]))
	_, leaderboard = call_tree._aggregate_hot_frames(per_action_trees)
	assert len(leaderboard) <= 20
	for i in range(len(leaderboard) - 1):
		assert leaderboard[i]["total_ms"] >= leaderboard[i + 1]["total_ms"]


# ---------------------------------------------------------------------------
# Donut + analyzer entry point
# ---------------------------------------------------------------------------


def test_donut_bucketing_by_top_level_module():
	per_action_trees = [
		_node("<root>", "", 1000, [
			_node("erpnext.selling.validate", "apps/erpnext/x.py", 400, [], self_ms=400),
			_node("my_app.discounts.calc", "apps/my_app/d.py", 300, [], self_ms=300),
			_node("frappe.model.save", "apps/frappe/m.py", 200, [], self_ms=200),
		]),
	]
	breakdown = call_tree._build_session_breakdown(per_action_trees, sql_total_ms=100)
	assert breakdown["sql_ms"] == 100
	assert breakdown["python_ms"] == 900
	by_app = breakdown["by_app"]
	assert by_app.get("erpnext") == 400
	assert by_app.get("my_app") == 300
	assert by_app.get("frappe") == 200


def test_analyze_entry_point_reads_pyi_session_and_emits_findings():
	context = AnalyzeContext(session_uuid="test", docname="PS-001")
	# Pre-populate context.actions as if per_action ran
	context.actions = [
		{"action_label": "SI submit", "duration_ms": 1000, "queries_count": 0},
	]

	# Recording with a pyi tree dict and a slow hot path
	recording = {
		"uuid": "rec-1",
		"calls": [],
		"sidecar": [],
		"pyi_session": {
			"function": "<root>",
			"filename": "",
			"lineno": 0,
			"self_ms": 0,
			"cumulative_ms": 1000,
			"children": [
				{
					"function": "my_app.heavy",
					"filename": "apps/my_app/h.py",
					"lineno": 1,
					"self_ms": 600,
					"cumulative_ms": 600,
					"children": [],
				},
			],
		},
	}

	result = call_tree.analyze([recording], context)
	# Should emit at least one Slow Hot Path finding
	assert any(f["finding_type"] == "Slow Hot Path" for f in result.findings)
	# Should produce hot_frames + breakdown aggregates
	assert "hot_frames" in result.aggregate
	assert "session_time_breakdown" in result.aggregate
	# Should have written call_tree_json onto the action
	assert context.actions[0].get("call_tree_json") is not None


def test_analyze_handles_missing_pyi_session():
	"""Recording with no pyi tree should not crash; produces empty tree."""
	context = AnalyzeContext(session_uuid="test", docname="PS-001")
	context.actions = [
		{"action_label": "x", "duration_ms": 100, "queries_count": 5},
	]
	recording = {
		"uuid": "rec-1",
		"calls": [{"query": "SELECT 1", "duration": 5}],
		"sidecar": [],
		"pyi_session": None,
	}
	result = call_tree.analyze([recording], context)
	# No findings (no tree, nothing to find)
	assert result.findings == []
	# But sql_total_ms still counted
	assert result.aggregate["session_time_breakdown"]["sql_ms"] == 5
	# action.call_tree_json is None
	assert context.actions[0]["call_tree_json"] is None
