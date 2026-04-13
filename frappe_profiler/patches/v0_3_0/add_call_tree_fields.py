# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""v0.3.0: add Profiler Action and Profiler Session fields for call tree capture.

The DocType JSON files declare the new fields, but Frappe's automatic schema
sync only adds columns when the DocType is reloaded. This patch reloads both
DocTypes so the columns appear in the database without requiring a manual
bench reload.

The new columns are nullable; existing rows get NULL for the new fields and
render correctly via the renderer's backward-compat fallbacks.
"""

import frappe


def execute():
	frappe.reload_doc("frappe_profiler", "doctype", "profiler_action")
	frappe.reload_doc("frappe_profiler", "doctype", "profiler_session")
