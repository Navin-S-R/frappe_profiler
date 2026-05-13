# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Install / uninstall hooks for frappe_profiler.

Currently the only install-time action is creating the `Profiler User`
role used by Phase 1's permission model. Customers grant this role to
non-admin users who should be able to record their own profiling sessions.
"""

import frappe

from frappe_profiler import safe_commit

PROFILER_USER_ROLE = "Profiler User"


def after_install():
	"""Create the Profiler User role and auto-assign it to System Managers."""
	if not frappe.db.exists("Role", PROFILER_USER_ROLE):
		frappe.get_doc(
			{
				"doctype": "Role",
				"role_name": PROFILER_USER_ROLE,
				"desk_access": 1,
				"is_custom": 0,
			}
		).insert(ignore_permissions=True)
		safe_commit()

	# v0.4.0: auto-assign the Profiler User role to every existing
	# System Manager. Idempotent — users who already have it are skipped.
	# Wrapped in try/except so a failure can't abort the install.
	try:
		_assign_profiler_user_to_system_managers()
	except Exception:
		try:
			frappe.log_error(title="frappe_profiler after_install auto-role")
		except Exception:
			pass

	# v0.5.2 round 3: pre-populate Profiler Settings ▸ Tracked Apps
	# with the site's custom apps (anything not in the built-in
	# FRAMEWORK_APPS set). Admin gets a sensible default inclusion-
	# mode allowlist on day one — they don't have to remember that
	# Tracked Apps exists for the framework filter to actually match
	# their mental model of "user code".
	try:
		_seed_tracked_apps_from_installed_apps()
	except Exception:
		try:
			frappe.log_error(title="frappe_profiler after_install tracked-apps seed")
		except Exception:
			pass


def _seed_tracked_apps_from_installed_apps():
	"""Populate Profiler Settings.tracked_apps with every installed
	app that's NOT in the built-in framework allowlist.

	Idempotent: if tracked_apps is already populated (user configured
	it manually before running migrate again), we do nothing.
	"""
	from frappe_profiler.analyzers.base import FRAMEWORK_APPS

	if not frappe.db.exists("DocType", "Profiler Settings"):
		# Migration hasn't created the Single yet — skip silently.
		return

	settings = frappe.get_single("Profiler Settings")
	if settings.tracked_apps:
		# Respect existing config — never overwrite.
		return

	installed = frappe.get_installed_apps() or []
	custom_apps = [a for a in installed if a not in FRAMEWORK_APPS]
	if not custom_apps:
		return

	for app_name in custom_apps:
		settings.append("tracked_apps", {"app_name": app_name})
	settings.save(ignore_permissions=True)
	safe_commit()


def _assign_profiler_user_to_system_managers():
	"""Add Profiler User role to every user who has System Manager.

	Idempotent: existing Profiler Users are left untouched. Never removes
	roles. Safe to call repeatedly.

	v0.6.x: was an N+1 — one ``get_doc("User", name)`` per user in the
	system. Now uses a single ``Has Role`` query to find users with
	System Manager (and read their existing roles in the same fetch), so
	we only ``get_doc`` + ``save`` the subset that actually needs the new
	role added. On a site with 500 users where 5 are System Managers,
	this drops from 500 → ~5 doc loads.
	"""
	# Pull every user-role pair in one query, grouped by user. We only
	# care about two roles, but fetching them both in one round-trip is
	# faster than a per-user introspection.
	role_rows = frappe.get_all(
		"Has Role",
		filters={"role": ("in", ["System Manager", PROFILER_USER_ROLE])},
		fields=["parent", "role"],
	)
	roles_by_user: dict[str, set[str]] = {}
	for row in role_rows:
		roles_by_user.setdefault(row["parent"], set()).add(row["role"])

	for name, role_names in roles_by_user.items():
		if "System Manager" not in role_names:
			continue
		if PROFILER_USER_ROLE in role_names:
			continue
		# Only the users that need a NEW role assigned get loaded as docs
		# (so save() fires the right lifecycle events / hooks).
		user = frappe.get_doc("User", name)
		user.append("roles", {"role": PROFILER_USER_ROLE})
		user.save(ignore_permissions=True)


def on_user_role_change(doc, method=None):
	"""validate hook on User: auto-add Profiler User when System Manager
	is present.

	Wired via hooks.py: doc_events["User"]["validate"]. Silent — never
	raises and never produces a user-facing message. Idempotent.
	"""
	try:
		role_names = {r.role for r in (doc.roles or [])}
		if "System Manager" not in role_names:
			return
		if PROFILER_USER_ROLE in role_names:
			return
		doc.append("roles", {"role": PROFILER_USER_ROLE})
	except Exception:
		try:
			frappe.log_error(title="frappe_profiler on_user_role_change")
		except Exception:
			pass


def before_uninstall():
	"""Best-effort cleanup on uninstall.

	Clears any profiler:* keys from Redis so a reinstall starts with a
	clean state. Also restores the v0.3.0 monkey-patched wraps on
	frappe.get_doc / RedisWrapper.get_value / frappe.permissions.has_permission
	so subsequent code on this worker uses the originals.

	Does NOT delete:
	- The `Profiler User` role (users may still be assigned to it; a
	  re-install would lose those assignments).
	- The `Profiler Session` MariaDB rows (frappe's uninstall flow
	  drops the DocType tables naturally).
	- The attached report files (same — frappe's File doctype cleanup
	  handles these).
	"""
	# v0.3.0: restore the three monkey-patched functions on uninstall.
	try:
		from frappe_profiler import capture

		capture.uninstall_wraps()
	except Exception:
		try:
			frappe.log_error(title="frappe_profiler before_uninstall capture")
		except Exception:
			pass

	try:
		_clear_redis_state()
	except Exception:
		# Never fail the uninstall on a Redis hiccup.
		frappe.log_error(title="frappe_profiler uninstall cleanup")


def _clear_redis_state():
	"""Scan-and-delete all profiler:* keys from the site's Redis namespace."""
	try:
		redis_conn = frappe.cache.get_redis_connection()
	except Exception:
		return

	# Frappe's cache wrapper auto-prefixes keys with the site namespace
	# via make_key(). To SCAN for our patterns we need to use the raw
	# Redis connection with the fully-qualified site key.
	try:
		site_prefix = frappe.cache.make_key("").decode() if hasattr(
			frappe.cache.make_key(""), "decode"
		) else frappe.cache.make_key("")
	except Exception:
		site_prefix = ""

	patterns = (
		f"{site_prefix}profiler:active:*",
		f"{site_prefix}profiler:session:*",
		f"{site_prefix}profiler:explain:*",
	)

	deleted_count = 0
	for pattern in patterns:
		try:
			cursor = 0
			while True:
				cursor, keys = redis_conn.scan(cursor, match=pattern, count=100)
				if keys:
					redis_conn.delete(*keys)
					deleted_count += len(keys)
				if cursor == 0:
					break
		except Exception:
			# Continue with other patterns even if one fails
			continue

	if deleted_count:
		try:
			frappe.logger().info(
				f"frappe_profiler uninstall cleared {deleted_count} Redis keys"
			)
		except Exception:
			pass
