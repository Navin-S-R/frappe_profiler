# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Install / uninstall hooks for frappe_profiler.

Currently the only install-time action is creating the `Profiler User`
role used by Phase 1's permission model. Customers grant this role to
non-admin users who should be able to record their own profiling sessions.
"""

import frappe

PROFILER_USER_ROLE = "Profiler User"


def after_install():
	"""Create the Profiler User role if it doesn't already exist."""
	if frappe.db.exists("Role", PROFILER_USER_ROLE):
		return

	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": PROFILER_USER_ROLE,
			"desk_access": 1,
			"is_custom": 0,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()


def before_uninstall():
	"""Best-effort cleanup on uninstall.

	Clears any profiler:* keys from Redis so a reinstall starts with a
	clean state. Does NOT delete:
	- The `Profiler User` role (users may still be assigned to it; a
	  re-install would lose those assignments).
	- The `Profiler Session` MariaDB rows (frappe's uninstall flow
	  drops the DocType tables naturally).
	- The attached report files (same — frappe's File doctype cleanup
	  handles these).

	Uses SCAN with a small batch size so large profiler:* keyspaces
	don't block Redis. Safe to run on a site that never installed or
	already uninstalled the profiler (no keys → no-op).
	"""
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
