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

	# v0.5.1: frappe.desk.form.save.savedocs is the Desk's canonical
	# "save this form" endpoint. Its payload carries:
	#   - doc:    JSON-encoded doc (has "doctype", "name", "__islocal")
	#   - action: "Save" | "Submit" | "Cancel" | "Update"
	# Pre-v0.5.1 this cmd fell through the verb_for_cmd map and the
	# label came out as the raw cmd string "frappe.desk.form.save.
	# savedocs" — useless in the Steps to Reproduce panel. Combine
	# the action + __islocal flag + doctype to build a human phrase.
	if cmd == "frappe.desk.form.save.savedocs":
		action = ""
		if isinstance(form_dict, dict):
			action = form_dict.get("action") or ""
		doctype, is_new = _extract_doc_info(form_dict)
		if doctype:
			if action == "Submit":
				return f"Submit {doctype}"
			if action == "Cancel":
				return f"Cancel {doctype}"
			if action == "Update":
				return f"Update {doctype}"
			# action == "Save" (or empty): distinguish create vs save
			# using the __islocal flag pyinstrument captures on the
			# posted doc payload. "Create" reads better than "Save" for
			# a brand-new record.
			return f"{'Create' if is_new else 'Save'} {doctype}"

	verb_for_cmd = {
		"frappe.client.save": "Save",
		"frappe.client.insert": "Create",
		"frappe.client.insert_many": "Create many",
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

	# v0.5.1: common Desk navigation / form-open operations. Humanize
	# so the reproducer reads "Open Sales Invoice" instead of
	# "frappe.desk.form.load.getdoc".
	if cmd == "frappe.desk.form.load.getdoc":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		name = form_dict.get("name") if isinstance(form_dict, dict) else None
		if doctype and name:
			return f"Open {doctype} {name}"
		if doctype:
			return f"Open {doctype}"

	if cmd == "frappe.desk.form.load.getdoctype":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		if doctype:
			return f"Load {doctype} form"

	if cmd == "frappe.desk.search.search_link":
		doctype = form_dict.get("doctype") if isinstance(form_dict, dict) else None
		if doctype:
			return f"Search {doctype}"

	if cmd:
		return cmd

	method = recording.get("method") or "GET"
	path = recording.get("path") or "/"
	return f"{method} {path}"


def _extract_doc_info(form_dict) -> tuple[str | None, bool]:
	"""Return (doctype, is_new) from a savedocs payload.

	The Desk's savedocs endpoint posts a JSON-encoded `doc` field that
	contains the doctype plus a ``__islocal: 1`` marker for brand-new
	(never-persisted) documents. We use that marker to distinguish
	"Create Sales Invoice" (a new doc) from "Save Sales Invoice"
	(an existing doc). Returns (None, False) when the payload is
	unparseable.
	"""
	if not isinstance(form_dict, dict):
		return None, False
	doctype = form_dict.get("doctype")
	doc = form_dict.get("doc")
	if isinstance(doc, str):
		try:
			doc = json.loads(doc)
		except Exception:
			doc = None
	is_new = False
	if isinstance(doc, dict):
		doctype = doctype or doc.get("doctype")
		# __islocal is sent as 1 for unsaved docs, absent/0 otherwise.
		# Cast defensively — Frappe sends both int 1 and string "1"
		# depending on the client.
		raw_islocal = doc.get("__islocal")
		is_new = bool(raw_islocal) and raw_islocal not in ("0", 0, False)
	return doctype, is_new


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
