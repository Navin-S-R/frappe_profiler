# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class ProfilerSettings(Document):
	"""Site-wide configuration for Frappe Profiler.

	A Single DocType. The cached reader in ``frappe_profiler.settings``
	is what analyzers and hooks actually call — this controller only
	handles validation + cache invalidation when an admin saves.
	"""

	def validate(self):
		self._normalize_tracked_apps()
		self._warn_on_framework_apps_in_tracked()

	def on_update(self):
		# Settings are read on every request (via the `enabled` gate in
		# hooks_callbacks), so the cache version bumps on every save.
		# The settings module's reader respects the cache version.
		frappe.cache.delete_value("profiler_settings_cached")

	def _normalize_tracked_apps(self):
		"""Trim whitespace and deduplicate app names, preserving order.

		Saves the admin from pasting ``myapp`` and ``myapp `` (trailing
		space) and getting two rows that both fail to match.
		"""
		if not self.tracked_apps:
			return
		seen = set()
		normalized = []
		for row in self.tracked_apps:
			name = (row.app_name or "").strip()
			if not name or name in seen:
				continue
			seen.add(name)
			row.app_name = name
			normalized.append(row)
		# Rebuild the child-table list preserving order.
		self.tracked_apps = normalized

	def _warn_on_framework_apps_in_tracked(self):
		"""Flash a non-blocking warning when the admin adds a
		known-framework app (frappe / erpnext / hrms / …) to Tracked
		Apps.

		Most users misread "Tracked Apps" as "apps to monitor" and
		add frappe + erpnext — which has the OPPOSITE effect of what
		they want: it flips the classifier into inclusion mode where
		framework code becomes "user code", and their actionable
		findings list gets flooded with framework noise.

		We don't HARD-block the save (ERPNext contributors may
		legitimately want framework findings as actionable) — just
		flash a clear warning so the common misconfiguration surfaces
		itself.
		"""
		if not self.tracked_apps:
			return
		# Local import to avoid a top-level dependency on analyzers/.
		from frappe_profiler.analyzers.base import FRAMEWORK_APPS
		offenders = sorted({
			(row.app_name or "").strip()
			for row in self.tracked_apps
			if (row.app_name or "").strip() in FRAMEWORK_APPS
			and (row.app_name or "").strip() != "frappe_profiler"
		})
		if not offenders:
			return
		msg = (
			"<b>Heads up:</b> you added "
			+ ", ".join(f"<code>{a}</code>" for a in offenders)
			+ " to Tracked Apps. These are framework/first-party apps — "
			"adding them here flips the filter into <i>inclusion mode</i>, "
			"so their findings will now show up as <b>actionable</b> "
			"instead of in the collapsed Framework observations section. "
			"<br><br>"
			"If you want the default behavior (frappe + erpnext + stock "
			"apps treated as framework), <b>remove these rows and leave "
			"the table empty</b>. Only add your own custom app here if "
			"you want to narrow the actionable list to just that app."
		)
		frappe.msgprint(
			msg,
			title="Tracked Apps — possible misconfiguration",
			indicator="orange",
		)
