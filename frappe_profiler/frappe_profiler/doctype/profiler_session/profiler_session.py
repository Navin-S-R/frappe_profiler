# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ProfilerSession(Document):
	# Phase 0 — scaffold only.
	# Lifecycle methods (validate, on_update, on_trash) will be added in
	# Phase 1 alongside the session API and Redis state tracking.
	pass
