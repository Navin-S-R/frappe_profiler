# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for frappe_profiler.analyzers.per_action."""

from frappe_profiler.analyzers import per_action


def test_save_sales_invoice_action_label(n_plus_one_recording, empty_context):
	"""frappe.client.save + Sales Invoice in form_dict → 'Save Sales Invoice'."""
	result = per_action.analyze([n_plus_one_recording], empty_context)
	assert len(result.actions) == 1
	assert result.actions[0]["action_label"] == "Save Sales Invoice"


def test_action_aggregates_match_calls(n_plus_one_recording, empty_context):
	"""Per-action totals should match sum of calls."""
	result = per_action.analyze([n_plus_one_recording], empty_context)
	action = result.actions[0]
	# 12 calls in the n_plus_one fixture
	assert action["queries_count"] == 12
	# Sum of durations from the fixture (first call 1.5 + 10 N+1 ~9ms + update 2.1)
	assert 90 < action["query_time_ms"] < 110
	# Slowest should be one of the N+1 queries at ~11.4ms
	assert action["slowest_query_ms"] > 10


def test_non_frappe_client_cmd_falls_back_to_cmd(empty_context):
	"""Unknown cmd should be shown verbatim, not as a 'Save X' label."""
	recording = {
		"uuid": "t1",
		"path": "/api/method/erpnext.accounts.utils.get_balance_on",
		"method": "GET",
		"cmd": "erpnext.accounts.utils.get_balance_on",
		"event_type": "HTTP Request",
		"duration": 15.0,
		"calls": [],
		"form_dict": {},
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "erpnext.accounts.utils.get_balance_on"


def test_http_fallback_when_no_cmd(empty_context):
	"""Requests without a cmd should fall back to METHOD + path."""
	recording = {
		"uuid": "t2",
		"path": "/api/resource/Lead/LEAD-001",
		"method": "GET",
		"cmd": None,
		"event_type": "HTTP Request",
		"duration": 8.0,
		"calls": [],
		"form_dict": {},
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "GET /api/resource/Lead/LEAD-001"


def test_background_job_label(empty_context):
	"""Background jobs should be labeled with 'Job: <last component>'."""
	recording = {
		"uuid": "j1",
		"path": "erpnext.accounts.doctype.gl_entry.gl_entry.post_gl_entries",
		"method": None,
		"cmd": None,
		"event_type": "Background Job",
		"duration": 320.0,
		"calls": [],
		"form_dict": None,
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "Job: post_gl_entries"
	assert result.actions[0]["event_type"] == "Background Job"


def test_submit_cmd(empty_context):
	recording = {
		"uuid": "t3",
		"cmd": "frappe.client.submit",
		"method": "POST",
		"path": "/api/method/frappe.client.submit",
		"event_type": "HTTP Request",
		"duration": 500,
		"calls": [],
		"form_dict": {"doctype": "Delivery Note"},
	}
	result = per_action.analyze([recording], empty_context)
	assert result.actions[0]["action_label"] == "Submit Delivery Note"


# ---------------------------------------------------------------------------
# v0.5.1: frappe.desk.form.save.savedocs humanization
# ---------------------------------------------------------------------------
# The Desk's canonical "save this form" endpoint. Pre-v0.5.1 the cmd
# fell through the verb_for_cmd map and produced the raw cmd string
# in the Steps to Reproduce list — useless. v0.5.1 maps the `action`
# and `__islocal` payload fields to "Create/Save/Submit/Cancel/Update
# <DocType>" for a human-readable reproducer.


def _savedocs(doc, action="Save"):
	"""Build a savedocs recording with the given embedded doc + action."""
	import json
	return {
		"uuid": "sd",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 300,
		"calls": [],
		"form_dict": {
			"doc": json.dumps(doc),
			"action": action,
		},
	}


def test_savedocs_create_new_doc(empty_context):
	"""__islocal=1 → 'Create <DocType>' reads better than 'Save'."""
	rec = _savedocs({"doctype": "Sales Invoice", "__islocal": 1}, action="Save")
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "Create Sales Invoice"


def test_savedocs_save_existing_doc(empty_context):
	"""No __islocal (or __islocal=0) → 'Save <DocType>'."""
	rec = _savedocs(
		{"doctype": "Sales Invoice", "name": "SINV-00042"},
		action="Save",
	)
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "Save Sales Invoice"


def test_savedocs_submit_action(empty_context):
	"""action=Submit → 'Submit <DocType>' regardless of __islocal."""
	rec = _savedocs(
		{"doctype": "Sales Invoice", "name": "SINV-00042"},
		action="Submit",
	)
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "Submit Sales Invoice"


def test_savedocs_cancel_action(empty_context):
	rec = _savedocs(
		{"doctype": "Sales Invoice", "name": "SINV-00042"},
		action="Cancel",
	)
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "Cancel Sales Invoice"


def test_savedocs_without_doctype_falls_back(empty_context):
	"""If the payload is malformed (no parseable doctype), don't
	emit an ugly 'Save <empty>' label — fall back to the cmd string."""
	rec = {
		"uuid": "sd",
		"cmd": "frappe.desk.form.save.savedocs",
		"method": "POST",
		"path": "/api/method/frappe.desk.form.save.savedocs",
		"event_type": "HTTP Request",
		"duration": 100,
		"calls": [],
		"form_dict": {"action": "Save"},  # no doc
	}
	result = per_action.analyze([rec], empty_context)
	label = result.actions[0]["action_label"]
	# Falls through to the raw cmd (or could be the path if cmd wasn't
	# set) — just verify we DON'T silently emit "Save " with trailing
	# whitespace or similar.
	assert label and not label.endswith(" ")


def test_desk_getdoc_is_open_doctype(empty_context):
	"""Opening a doc form in the Desk → 'Open <DocType> <name>'."""
	rec = {
		"uuid": "gd",
		"cmd": "frappe.desk.form.load.getdoc",
		"method": "GET",
		"path": "/api/method/frappe.desk.form.load.getdoc",
		"event_type": "HTTP Request",
		"duration": 50,
		"calls": [],
		"form_dict": {"doctype": "Customer", "name": "CUST-001"},
	}
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "Open Customer CUST-001"


def test_desk_search_link_is_search_doctype(empty_context):
	rec = {
		"uuid": "sl",
		"cmd": "frappe.desk.search.search_link",
		"method": "GET",
		"path": "/api/method/frappe.desk.search.search_link",
		"event_type": "HTTP Request",
		"duration": 30,
		"calls": [],
		"form_dict": {"doctype": "Item", "txt": "ITEM-"},
	}
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "Search Item"


def test_client_insert_is_create(empty_context):
	"""frappe.client.insert is the REST way to create a doc — 'Create'
	reads better than 'Save' (which v0.4.x used)."""
	rec = {
		"uuid": "ci",
		"cmd": "frappe.client.insert",
		"method": "POST",
		"path": "/api/method/frappe.client.insert",
		"event_type": "HTTP Request",
		"duration": 200,
		"calls": [],
		"form_dict": {"doctype": "Lead"},
	}
	result = per_action.analyze([rec], empty_context)
	assert result.actions[0]["action_label"] == "Create Lead"


def test_empty_recordings_list(empty_context):
	result = per_action.analyze([], empty_context)
	assert result.actions == []
	assert result.findings == []


def test_action_emits_no_findings(n_plus_one_recording, empty_context):
	"""per_action only builds Profiler Action rows, no findings."""
	result = per_action.analyze([n_plus_one_recording], empty_context)
	assert result.findings == []
