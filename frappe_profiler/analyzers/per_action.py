# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Analyzer: per-action breakdown.

Produces one Profiler Action row per recording, with a humanized label
derived best-effort from the recording's path/cmd/form_dict. The action
row is the unit the customer sorts/filters by in the report — "which step
of my flow took the longest?".

Label detection strategy:
    1. Background jobs: "Job: <last component of method name>"
    2. frappe.client.{save,insert,submit,cancel,delete}: "<Verb> <DocType>"
    3. frappe.client.get_list: "List <DocType>"
    4. Other cmd: use the cmd verbatim
    5. Fallback: "<METHOD> <path>"
"""

import json

from frappe_profiler.analyzers.base import AnalyzerResult


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	actions = [_build_action(r) for r in recordings]
	return AnalyzerResult(actions=actions)


def _build_action(recording: dict) -> dict:
	calls = recording.get("calls") or []
	durations = [c.get("duration", 0) for c in calls]
	return {
		"action_label": _label(recording),
		"event_type": recording.get("event_type") or "HTTP Request",
		"http_method": recording.get("method") or "",
		"path": (recording.get("path") or "")[:500],
		"recording_uuid": recording.get("uuid") or "",
		"duration_ms": round(recording.get("duration", 0), 2),
		"queries_count": len(calls),
		"query_time_ms": round(sum(durations), 2),
		"slowest_query_ms": round(max(durations, default=0), 2),
	}


def _label(recording: dict) -> str:
	if recording.get("event_type") == "Background Job":
		path = recording.get("path") or "Background Job"
		# Trim long module paths to the last component for readability
		short = path.split(".")[-1] if "." in path else path
		return f"Job: {short}"

	cmd = recording.get("cmd") or ""
	form_dict = recording.get("form_dict") or {}

	verb_for_cmd = {
		"frappe.client.save": "Save",
		"frappe.client.insert": "Save",
		"frappe.client.insert_many": "Save",
		"frappe.client.submit": "Submit",
		"frappe.client.cancel": "Cancel",
		"frappe.client.delete": "Delete",
		"frappe.client.set_value": "Update",
	}
	if cmd in verb_for_cmd:
		doctype = _extract_doctype(form_dict)
		if doctype:
			return f"{verb_for_cmd[cmd]} {doctype}"

	if cmd == "frappe.client.get_list":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		if doctype:
			return f"List {doctype}"

	if cmd:
		return cmd

	method = recording.get("method") or "GET"
	path = recording.get("path") or "/"
	return f"{method} {path}"


def _extract_doctype(form_dict) -> str | None:
	"""Extract a DocType name from form_dict.

	form_dict.doctype takes precedence; otherwise we look at form_dict.doc
	(which may be a JSON-encoded dict).
	"""
	if not isinstance(form_dict, dict):
		return None
	if form_dict.get("doctype"):
		return form_dict["doctype"]
	doc = form_dict.get("doc")
	if isinstance(doc, str):
		try:
			doc = json.loads(doc)
		except Exception:
			return None
	if isinstance(doc, dict) and doc.get("doctype"):
		return doc["doctype"]
	return None
