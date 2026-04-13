__version__ = "0.2.0"


# ---------------------------------------------------------------------------
# frappe.enqueue monkey-patch (Phase 2)
# ---------------------------------------------------------------------------
# We wrap frappe.utils.background_jobs.enqueue so that when a user with an
# active profiler session enqueues a background job, the session UUID rides
# along inside the job's kwargs as `_profiler_session_id`. The before_job
# hook in hooks_callbacks.py reads this key (and pops it before the method
# runs, so the method's signature isn't disturbed) and activates recording
# for the job.
#
# This is the only way to make background-job profiling work without forking
# frappe — the worker process is a fresh interpreter that has no idea who
# enqueued the job, but RQ preserves kwargs verbatim across the queue boundary.
#
# Patched at app-import time. Idempotent via the `_profiler_patched` marker
# so re-imports during dev (e.g. `bench update`) don't double-wrap.
# ---------------------------------------------------------------------------


def _patch_enqueue():
	"""Install the enqueue wrapper. Safe to call in environments without
	frappe installed — will silently no-op (useful for running analyzer
	unit tests from a plain Python interpreter)."""
	try:
		import frappe
		import frappe.utils.background_jobs as _bg
	except ImportError:
		# Frappe isn't available — we're probably running unit tests
		# or a standalone script. Nothing to patch.
		return

	if getattr(_bg.enqueue, "_profiler_patched", False):
		return

	_original_enqueue = _bg.enqueue

	def _profiler_enqueue(method, *args, **kwargs):
		try:
			# Lazy import to avoid circular dependency on app init.
			from frappe_profiler import session as _profiler_session

			user = getattr(frappe.session, "user", None) if hasattr(frappe, "session") else None
			if user and user != "Guest":
				active = _profiler_session.get_active_session_for(user)
				if active:
					# Inject our marker into the job kwargs. The before_job
					# hook will pop it before the method runs.
					kwargs["_profiler_session_id"] = active
		except Exception:
			# Never break enqueue. The profiler is best-effort by design.
			pass
		return _original_enqueue(method, *args, **kwargs)

	_profiler_enqueue._profiler_patched = True
	_profiler_enqueue.__wrapped__ = _original_enqueue

	# Patch BOTH locations: the canonical module attribute AND the
	# frappe.enqueue re-export at frappe/__init__.py:1590. They reference
	# the same function but Python module imports create separate bindings.
	_bg.enqueue = _profiler_enqueue
	frappe.enqueue = _profiler_enqueue


_patch_enqueue()
