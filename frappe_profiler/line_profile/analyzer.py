# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Phase-2 results processor.

Turns the aggregated `results_json` blob (one entry per picked function with
per-line timings) into report-friendly findings + per-function aggregate.

The pure ``analyze()`` function is the unit-testable core. The orchestrator
``run_analyze()`` (added in a later commit) wraps it with Redis I/O and
DocType persistence, mirroring the phase-1 ``analyze.run`` ↔ analyzers/*
split.
"""

import json

from frappe_profiler.analyzers.base import AnalyzerResult

# Severity thresholds. A "hot line" is one that concentrates a large
# fraction of its function's wall time on a single source line.
HOT_LINE_HIGH_FRACTION = 0.50
HOT_LINE_HIGH_MIN_MS = 100.0

HOT_LINE_MEDIUM_FRACTION = 0.25
HOT_LINE_MEDIUM_MIN_MS = 50.0

# Per-function summary keeps the top-N lines by total_ms in the aggregate
# so the renderer can show a compact "where the time went" panel without
# pulling the full per-line data on the form.
HOT_LINES_IN_SUMMARY = 5


def _classify_hot_line(line_ms: float, total_ms: float) -> str | None:
	"""Return 'High', 'Medium', or None for a candidate hot line.

	The line must concentrate enough fraction AND have enough absolute time
	to be worth flagging — a single line that's 100% of a 30ms function
	isn't actionable noise, but 60% of a 300ms function is.
	"""
	if total_ms <= 0:
		return None
	fraction = line_ms / total_ms
	if fraction >= HOT_LINE_HIGH_FRACTION and line_ms >= HOT_LINE_HIGH_MIN_MS:
		return "High"
	if fraction >= HOT_LINE_MEDIUM_FRACTION and line_ms >= HOT_LINE_MEDIUM_MIN_MS:
		return "Medium"
	return None


def _function_invoked(fn: dict) -> bool:
	"""A function is 'invoked' if at least one line has hits > 0 OR
	total_ms > 0. line_profiler may report empty stats (no lines), or the
	full line list with hits=0 — both mean the picked function was never
	executed during the recording."""
	lines = fn.get("lines") or []
	if not lines:
		return False
	return any((line.get("hits") or 0) > 0 or (line.get("total_ms") or 0) > 0 for line in lines)


def _hot_line_finding(fn: dict, line: dict, severity: str) -> dict:
	dotted_path = fn["dotted_path"]
	lineno = line["lineno"]
	content = line["content"]
	total_ms = line["total_ms"]
	hits = line.get("hits") or 0

	return {
		"finding_type": "Hot Line",
		"severity": severity,
		"title": (
			f"{dotted_path}:{lineno} consumed {total_ms:.0f}ms "
			f"({hits} hits) — single hottest line"
		),
		"customer_description": (
			f"The line **{dotted_path}:{lineno}** is the dominant time sink in "
			f"this function ({total_ms:.0f}ms across {hits} executions). "
			"Optimizing it directly will move the needle on the function's "
			"total cost — line-level timing makes the fix targetable."
		),
		"technical_detail_json": json.dumps({
			"dotted_path": dotted_path,
			"file": fn.get("file"),
			"lineno": lineno,
			"line_content": content,
			"total_ms": round(total_ms, 2),
			"hits": hits,
			"per_hit_us": round(line.get("per_hit_us") or 0, 2),
		}, default=str),
		"estimated_impact_ms": round(total_ms, 2),
		"affected_count": hits,
		"action_ref": None,
	}


def _not_invoked_finding(fn: dict) -> dict:
	dotted_path = fn["dotted_path"]
	return {
		"finding_type": "Function Not Invoked",
		"severity": "Low",
		"title": f"{dotted_path} was picked but never invoked during phase 2",
		"customer_description": (
			f"The function **{dotted_path}** was instrumented for phase 2 but "
			"no calls into it were recorded. Either the flow you reproduced "
			"didn't exercise it, or the function name in the picker doesn't "
			"resolve to the code path you intended."
		),
		"technical_detail_json": json.dumps({
			"dotted_path": dotted_path,
			"file": fn.get("file"),
		}, default=str),
		"estimated_impact_ms": 0.0,
		"affected_count": 0,
		"action_ref": None,
	}


def _summary_for_function(fn: dict, invoked: bool) -> dict:
	lines = fn.get("lines") or []
	total_ms = sum((line.get("total_ms") or 0) for line in lines)
	hot_lines: list[dict] = []
	if invoked:
		ranked = sorted(lines, key=lambda l: (l.get("total_ms") or 0), reverse=True)
		for line in ranked[:HOT_LINES_IN_SUMMARY]:
			hot_lines.append({
				"lineno": line["lineno"],
				"content": line.get("content", ""),
				"total_ms": round(line.get("total_ms") or 0, 2),
				"hits": line.get("hits") or 0,
			})
	return {
		"dotted_path": fn["dotted_path"],
		"qualname": fn.get("qualname") or fn["dotted_path"].rsplit(".", 1)[-1],
		"file": fn.get("file"),
		"total_ms": round(total_ms, 2),
		"hot_lines": hot_lines,
	}


def analyze(results_json: list[dict]) -> AnalyzerResult:
	"""Pure: turn the merged phase-2 ``results_json`` into an AnalyzerResult.

	Input shape (one entry per picked function)::

	    [{
	        "dotted_path": "my_app.tasks.heavy",
	        "qualname":    "heavy",
	        "file":        "/abs/path.py",
	        "lines": [{"lineno", "content", "content_hash", "hits",
	                   "total_ms", "per_hit_us"}, ...],
	    }, ...]

	Output:
	  - findings: ``Hot Line`` (High/Medium) per function with a dominant
	    line, ``Function Not Invoked`` (Low) per pick that recorded nothing.
	  - aggregate: ``{phase2_functions: [per-function summary, ...]}`` —
	    each summary carries top-5 lines by ``total_ms``.
	  - warnings: human-readable strings for the report's warnings panel.
	"""
	findings: list[dict] = []
	summaries: list[dict] = []
	warnings: list[str] = []

	for fn in results_json:
		invoked = _function_invoked(fn)
		summaries.append(_summary_for_function(fn, invoked))

		if not invoked:
			findings.append(_not_invoked_finding(fn))
			warnings.append(
				f"Function {fn['dotted_path']} was picked but never invoked"
			)
			continue

		lines = fn["lines"]
		total_ms = sum((line.get("total_ms") or 0) for line in lines)
		hottest = max(lines, key=lambda l: l.get("total_ms") or 0)
		severity = _classify_hot_line(hottest.get("total_ms") or 0, total_ms)
		if severity:
			findings.append(_hot_line_finding(fn, hottest, severity))

	return AnalyzerResult(
		findings=findings,
		aggregate={"phase2_functions": summaries},
		warnings=warnings,
	)
