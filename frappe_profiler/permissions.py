# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Server-side permission gates for profiler artifacts.

Currently exposes one gate: a `has_permission` for the File DocType that
double-checks downloads of the raw profiler report. The UI hides the
"Download Raw Report" button from non-admin users, but a malicious user
who guessed the file URL could try to fetch it directly — this gate
makes that fail.
"""

import frappe

PROFILER_SESSION_DOCTYPE = "Profiler Session"


def file_has_permission(doc, ptype=None, user=None):
	"""Gate downloads of the raw profiler report.

	Allows access to:
	  - Anyone who passes the underlying parent permission check (handled
	    by Frappe's built-in private file logic — Profiler User with
	    if_owner=1, System Manager always)
	  - Plus an additional check for raw_report_file specifically: only
	    System Manager OR the recording user, even if some other role
	    accidentally got read access to the parent.

	Return None to defer to Frappe's standard permission logic; return
	False to deny.
	"""
	if not doc:
		return None

	# Only intercept files attached to a Profiler Session.
	if doc.attached_to_doctype != PROFILER_SESSION_DOCTYPE:
		return None

	# Only intercept the raw_report_file field. Safe report uses standard
	# parent-doc permission only.
	if doc.attached_to_field != "raw_report_file":
		return None

	user = user or frappe.session.user
	roles = frappe.get_roles(user)

	if "System Manager" in roles or "Administrator" in roles:
		return None  # defer to standard checks

	# Otherwise the user must be the recording user
	if not doc.attached_to_name:
		return False
	recording_user = frappe.db.get_value(
		PROFILER_SESSION_DOCTYPE,
		doc.attached_to_name,
		"user",
	)
	if recording_user != user:
		return False

	return None  # passed our gate; let standard checks run
