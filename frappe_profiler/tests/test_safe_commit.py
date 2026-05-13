# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""v0.6.x: tests for ``frappe_profiler.safe_commit`` — the rollback-on-error
wrapper that every explicit commit in this codebase now routes through.

Addresses Lens audit finding *"frappe.db.commit() without try/except"*."""

import sys
import types

import pytest


def _install_frappe_stub(*, commit_raises=False, rollback_raises=False):
	"""Build a minimal ``frappe`` stub whose ``db.commit`` and
	``db.rollback`` are controlled by the test."""
	stub = types.ModuleType("frappe")
	stub._commits = 0
	stub._rollbacks = 0

	class _DB:
		def commit(self):
			stub._commits += 1
			if commit_raises:
				raise RuntimeError("COMMIT timed out")

		def rollback(self):
			stub._rollbacks += 1
			if rollback_raises:
				raise RuntimeError("rollback also broken")

	stub.db = _DB()
	sys.modules["frappe"] = stub
	return stub


def _import_safe_commit():
	# Force a fresh import so each test sees the fresh frappe stub.
	for mod in list(sys.modules.keys()):
		if mod == "frappe_profiler" or mod.startswith("frappe_profiler."):
			# Leave the package loaded but force safe_commit's frappe import
			# to re-resolve by removing the safe_commit module if cached.
			pass
	from frappe_profiler import safe_commit
	return safe_commit


class TestSafeCommit:
	def test_success_path_commits_once_no_rollback(self):
		stub = _install_frappe_stub(commit_raises=False)
		safe_commit = _import_safe_commit()
		safe_commit()
		assert stub._commits == 1
		assert stub._rollbacks == 0

	def test_commit_failure_triggers_rollback_then_raises(self):
		stub = _install_frappe_stub(commit_raises=True)
		safe_commit = _import_safe_commit()
		with pytest.raises(RuntimeError, match="COMMIT timed out"):
			safe_commit()
		assert stub._commits == 1
		assert stub._rollbacks == 1

	def test_rollback_failure_after_commit_failure_still_raises_commit_error(self):
		"""Rare double-fault: commit() raises, then rollback() ALSO raises.
		We swallow the rollback error (the connection's likely dead anyway
		so the rollback exception is just noise) and bubble the ORIGINAL
		commit exception — that's the one with the actionable error
		message."""
		stub = _install_frappe_stub(commit_raises=True, rollback_raises=True)
		safe_commit = _import_safe_commit()
		with pytest.raises(RuntimeError, match="COMMIT timed out"):
			safe_commit()
		assert stub._commits == 1
		assert stub._rollbacks == 1
