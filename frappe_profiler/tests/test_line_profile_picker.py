# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for frappe_profiler.line_profile.picker — candidate generation and
free-form dotted-path resolution for the phase-2 line-profile picker UI.
"""

import pytest

from frappe_profiler.line_profile import picker


def _frame(function: str, filename: str, lineno: int, cumulative_ms: float, children=None):
	"""Helper: build a pyinstrument-shaped frame node."""
	return {
		"function": function,
		"filename": filename,
		"lineno": lineno,
		"cumulative_ms": cumulative_ms,
		"self_ms": cumulative_ms,
		"kind": "python",
		"children": children or [],
	}


def _root(*children):
	return {
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"cumulative_ms": sum(c.get("cumulative_ms", 0) for c in children),
		"self_ms": 0,
		"kind": "python",
		"children": list(children),
	}


def _action(call_tree):
	"""Action row carrying a parsed call tree."""
	return {"call_tree": call_tree, "findings": []}


class TestBuildCandidatesFromTrees:
	def test_single_frame_yields_one_candidate(self):
		tree = _root(_frame("my_app.tasks.heavy_job", "apps/my_app/tasks.py", 42, 250.0))

		candidates = picker._build_candidates_from_trees([tree], [])

		assert len(candidates) == 1
		c = candidates[0]
		assert c["dotted_path"] == "my_app.tasks.heavy_job"
		assert c["file"] == "apps/my_app/tasks.py"
		assert c["lineno"] == 42
		assert c["cumulative_ms"] == 250.0
		assert c["hit_count"] == 1
		assert c["app"] == "my_app"

	def test_same_function_in_multiple_actions_aggregates(self):
		# my_app.utils.hash_value runs in two separate actions
		tree_a = _root(_frame("my_app.utils.hash_value", "apps/my_app/utils.py", 5, 100.0))
		tree_b = _root(_frame("my_app.utils.hash_value", "apps/my_app/utils.py", 5, 60.0))

		candidates = picker._build_candidates_from_trees([tree_a, tree_b], [])

		assert len(candidates) == 1
		c = candidates[0]
		assert c["cumulative_ms"] == 160.0
		assert c["hit_count"] == 2

	def test_synthetic_frames_excluded(self):
		# <root>, <sql>, and bracketed pseudo-frames must be filtered out.
		tree = _root(
			_frame("<sql>", "", 0, 50.0),
			_frame("[finalize]", "", 0, 5.0),
			_frame("my_app.real.fn", "apps/my_app/real.py", 1, 200.0),
		)

		candidates = picker._build_candidates_from_trees([tree], [])

		paths = [c["dotted_path"] for c in candidates]
		assert paths == ["my_app.real.fn"]

	def test_sorted_by_cumulative_ms_desc(self):
		tree = _root(
			_frame("my_app.slow_one", "apps/my_app/a.py", 1, 100.0),
			_frame("my_app.slow_two", "apps/my_app/b.py", 1, 250.0),
			_frame("my_app.slow_three", "apps/my_app/c.py", 1, 50.0),
		)

		candidates = picker._build_candidates_from_trees([tree], [])

		ms_values = [c["cumulative_ms"] for c in candidates]
		assert ms_values == sorted(ms_values, reverse=True)

	def test_walks_into_children(self):
		# Nested frames must also be picked up, not just the top level.
		nested = _frame("my_app.outer", "apps/my_app/o.py", 1, 100.0, children=[
			_frame("my_app.inner", "apps/my_app/o.py", 50, 90.0),
		])
		tree = _root(nested)

		candidates = picker._build_candidates_from_trees([tree], [])

		paths = {c["dotted_path"] for c in candidates}
		assert paths == {"my_app.outer", "my_app.inner"}

	def test_caps_at_top_n(self):
		# Default cap is 30; provide 50 distinct functions, expect 30 back.
		children = [
			_frame(f"my_app.fn_{i:02d}", "apps/my_app/f.py", i, float(i))
			for i in range(50)
		]
		tree = _root(*children)

		candidates = picker._build_candidates_from_trees([tree], [])

		assert len(candidates) == 30
		# Top-N by cumulative_ms desc → entries 49..20
		top_paths = [c["dotted_path"] for c in candidates]
		assert top_paths[0] == "my_app.fn_49"
		assert top_paths[-1] == "my_app.fn_20"


class TestFrameworkSplit:
	def test_user_app_marked_primary(self):
		tree = _root(_frame("my_app.x", "apps/my_app/x.py", 1, 100.0))

		candidates = picker._build_candidates_from_trees([tree], [])

		assert candidates[0]["is_framework"] is False

	def test_erpnext_marked_framework(self):
		tree = _root(_frame("erpnext.accounts.gl_entry.make", "apps/erpnext/x.py", 1, 100.0))

		candidates = picker._build_candidates_from_trees([tree], [])

		assert candidates[0]["is_framework"] is True

	def test_frappe_marked_framework(self):
		tree = _root(_frame("frappe.client.get_value", "apps/frappe/client.py", 1, 100.0))

		candidates = picker._build_candidates_from_trees([tree], [])

		assert candidates[0]["is_framework"] is True


class TestResolveFreeform:
	def test_resolves_stdlib_function(self):
		result = picker.resolve_freeform("json.dumps")

		assert result["dotted_path"] == "json.dumps"
		assert result["eligible"] is True
		assert result["app"] == "json"

	def test_missing_module_raises(self):
		with pytest.raises(picker.PickerError) as exc:
			picker.resolve_freeform("totally_nonexistent_pkg_xyz.foo")
		assert "could not import" in str(exc.value).lower()

	def test_missing_attribute_raises(self):
		with pytest.raises(picker.PickerError) as exc:
			picker.resolve_freeform("json.does_not_exist")
		assert "attribute" in str(exc.value).lower()

	def test_builtin_c_extension_rejected(self):
		# `len` is a builtin without `__code__` → ineligible
		result = picker.resolve_freeform("builtins.len")

		assert result["eligible"] is False
		assert "c-extension" in result["ineligible_reason"].lower() or \
		       "built" in result["ineligible_reason"].lower()

	def test_lambda_rejected(self):
		# Synthesize a lambda accessible via dotted path
		import sys
		mod_name = "_lp_test_lambda_module"
		mod = type(sys)("dummy")
		mod.my_lambda = lambda x: x * 2
		sys.modules[mod_name] = mod
		try:
			result = picker.resolve_freeform(f"{mod_name}.my_lambda")
			assert result["eligible"] is False
			assert "lambda" in result["ineligible_reason"].lower()
		finally:
			del sys.modules[mod_name]

	def test_empty_path_raises(self):
		with pytest.raises(picker.PickerError):
			picker.resolve_freeform("")

	def test_top_level_module_only_raises(self):
		# "json" alone is a module, not a function — needs at least one
		# attribute access.
		with pytest.raises(picker.PickerError):
			picker.resolve_freeform("json")
