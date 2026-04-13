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


def test_empty_recordings_list(empty_context):
	result = per_action.analyze([], empty_context)
	assert result.actions == []
	assert result.findings == []


def test_action_emits_no_findings(n_plus_one_recording, empty_context):
	"""per_action only builds Profiler Action rows, no findings."""
	result = per_action.analyze([n_plus_one_recording], empty_context)
	assert result.findings == []
