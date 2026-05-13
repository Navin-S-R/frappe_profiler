# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""v0.6.0: add the AI "suggest a fix" fields.

Additive only:
  - Profiler Settings: ai_section + ai_enabled / ai_provider / ai_base_url /
    ai_model / ai_api_key (Password)
  - Profiler Finding: llm_fix_json (Long Text)

``bench migrate`` already auto-adds the columns from the updated .json files;
this patch just reloads the two DocTypes so that happens deterministically
during the patch run (matching the pattern of the other ``add_*_fields``
patches in this app). Idempotent — safe to re-run.
"""

import frappe


def execute():
	for doctype in ("profiler_settings", "profiler_finding"):
		try:
			frappe.reload_doc("frappe_profiler", "doctype", doctype)
		except Exception:
			frappe.log_error(title=f"v0.6.0 patch: reload {doctype} (add AI fix fields)")
	frappe.db.commit()
