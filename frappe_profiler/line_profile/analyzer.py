# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Phase-2 results processor.

Turns the aggregated `results_json` blob (one entry per picked function with
per-line timings) into report-friendly findings + per-function aggregate.

Two functions, mirroring the phase-1 ``analyze.run`` ↔ ``analyzers/*``
split:

- ``analyze(results_json)`` is the **pure** classifier — testable in
  isolation, no Frappe / Redis access.
- ``run_analyze(session_uuid, run_uuid)`` is the **impure** RQ entry
  point — pulls samples from Redis, calls aggregate_samples, calls
  analyze, persists to the Profiler Phase 2 Run row, propagates findings
  to the parent Session, triggers re-render, publishes realtime events.
"""

import json
import traceback

from frappe_profiler.analyzers.base import AnalyzerResult

try:
	import frappe  # type: ignore[import-not-found]
	_FRAPPE_AVAILABLE = True
except ImportError:
	frappe = None  # type: ignore[assignment]
	_FRAPPE_AVAILABLE = False

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


# ---------------------------------------------------------------------------
# Orchestrator (impure — frappe required)
# ---------------------------------------------------------------------------


def _publish(event: str, payload: dict) -> None:
	"""Best-effort realtime event for the floating widget + form."""
	try:
		frappe.publish_realtime(event, payload, user=payload.get("user"))
	except Exception:
		pass


def run_analyze(session_uuid: str, run_uuid: str) -> None:
	"""RQ entry point. Reads phase-2 samples from Redis, builds the
	results_json, persists findings to the parent Profiler Session, marks
	the Phase 2 Run as Ready (or Failed), and triggers re-render.

	On any uncaught exception: rollback, mark Failed, publish failed event,
	re-raise so RQ logs it.
	"""
	if not _FRAPPE_AVAILABLE:
		raise RuntimeError("frappe not importable — run under bench")

	from frappe_profiler.line_profile import capture

	# Lookup the Phase 2 Run row + parent Session docname
	run_row = _find_run_row(session_uuid, run_uuid)
	if run_row is None:
		raise RuntimeError(
			f"Phase 2 Run {run_uuid} not found on session {session_uuid}"
		)
	parent_docname = run_row.parent

	try:
		_publish("phase_2_run_analyzing", {
			"session_uuid": session_uuid,
			"run_uuid": run_uuid,
		})

		# Drain samples + load picks meta with source snapshot.
		samples = capture.read_all_samples(run_uuid)
		picks = capture.read_picks_meta(run_uuid)

		# Aggregate raw samples into the analyzer's input shape, then run
		# the pure classifier.
		results_json = capture.aggregate_samples(samples, picks)
		result = analyze(results_json)
		total_ms = sum(s.get("total_ms") or 0 for s in result.aggregate.get("phase2_functions", []))

		# Persist to the run row + propagate findings to the parent session.
		_persist_run(parent_docname, run_uuid, results_json, result, total_ms)

		# Re-render the parent session's safe + raw reports so the new
		# phase-2 panel appears. Reuses the existing regenerate_reports
		# code path so we don't duplicate the renderer entry point.
		_regenerate_parent_reports(parent_docname)

		# Done — drop ephemeral Redis state.
		capture.cleanup_run(run_uuid)

		_publish("phase_2_run_ready", {
			"session_uuid": session_uuid,
			"run_uuid": run_uuid,
			"parent": parent_docname,
		})

	except Exception as exc:
		try:
			frappe.db.rollback()
		except Exception:
			pass
		_mark_run_failed(parent_docname, run_uuid, str(exc), traceback.format_exc())
		_publish("phase_2_run_failed", {
			"session_uuid": session_uuid,
			"run_uuid": run_uuid,
			"error": str(exc),
		})
		raise


def _find_run_row(session_uuid: str, run_uuid: str):
	"""Return the Profiler Phase 2 Run child row whose parent session
	matches session_uuid. None if not found."""
	parent_docname = frappe.db.get_value(
		"Profiler Session", {"session_uuid": session_uuid}, "name"
	)
	if not parent_docname:
		return None
	matches = frappe.get_all(
		"Profiler Phase 2 Run",
		filters={"parent": parent_docname, "run_uuid": run_uuid},
		fields=["name", "parent"],
		limit=1,
	)
	if not matches:
		return None
	# Return a tiny shape with .parent for the caller's convenience.
	row = matches[0]

	class _Row:
		pass

	r = _Row()
	r.name = row["name"]
	r.parent = row["parent"]
	return r


def _persist_run(
	parent_docname: str,
	run_uuid: str,
	results_json: list,
	result: AnalyzerResult,
	total_ms: float,
) -> None:
	"""Write results back to the run row + append findings to the parent
	session's findings child table."""
	parent = frappe.get_doc("Profiler Session", parent_docname)

	# Update the matching child row in place.
	for child in (parent.phase_2_runs or []):
		if child.run_uuid == run_uuid:
			child.results_json = json.dumps(results_json, default=str)
			child.warnings_json = json.dumps(result.warnings, default=str)
			child.total_ms = round(total_ms, 2)
			child.status = "Ready"
			child.ended_at = frappe.utils.now_datetime()
			break

	# Promote findings into the unified Session.findings table so the
	# existing finding rendering / filtering picks them up alongside
	# phase-1 findings.
	for finding in result.findings:
		parent.append("findings", finding)

	parent.flags.ignore_validate_update_after_submit = True
	parent.save(ignore_permissions=True)
	frappe.db.commit()


def _mark_run_failed(parent_docname: str, run_uuid: str, error: str, tb: str) -> None:
	"""Best-effort: set the run row's status to Failed with the error
	message in warnings_json. Tolerant of missing parent / row so the
	caller can still re-raise cleanly."""
	if not parent_docname:
		return
	try:
		parent = frappe.get_doc("Profiler Session", parent_docname)
		for child in (parent.phase_2_runs or []):
			if child.run_uuid == run_uuid:
				child.status = "Failed"
				child.warnings_json = json.dumps([
					f"phase 2 analyze failed: {error}",
					tb,
				], default=str)
				child.ended_at = frappe.utils.now_datetime()
				break
		parent.flags.ignore_validate_update_after_submit = True
		parent.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		# Truly best-effort — don't mask the original exception.
		try:
			frappe.db.rollback()
		except Exception:
			pass


def _regenerate_parent_reports(parent_docname: str) -> None:
	"""Trigger re-render of the parent Profiler Session's HTML reports.

	Phase 1's existing ``api.regenerate_reports`` performs the same job
	for phase-1 findings; we reuse that pathway so phase-2 doesn't get a
	bespoke re-render path that drifts from the canonical one.
	"""
	try:
		from frappe_profiler import api as profiler_api

		profiler_api.regenerate_reports(parent_docname)  # type: ignore[attr-defined]
	except Exception as exc:
		# Re-render failure is non-fatal: data is persisted, the customer
		# just needs to click "Regenerate Reports" manually. Surface the
		# error in the run row for debuggability.
		frappe.log_error(
			title="phase 2 re-render failed",
			message=f"{parent_docname}: {exc}\n{traceback.format_exc()}",
		)
