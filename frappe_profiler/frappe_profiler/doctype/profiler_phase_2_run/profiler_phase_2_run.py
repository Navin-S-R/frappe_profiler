# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ProfilerPhase2Run(Document):
	# Child table on Profiler Session — orchestration logic lives in
	# frappe_profiler.line_profile.capture and .analyzer.
	pass
