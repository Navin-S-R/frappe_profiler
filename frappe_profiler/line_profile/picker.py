# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Phase-2 picker: turn phase-1 results into a candidate function list and
resolve free-form dotted paths typed by the customer.

The picker has two responsibilities:

1. **Curated list** — walk the pyinstrument call trees attached to phase-1
   actions, aggregate Python frames by dotted path, and return the top-N
   candidates with cumulative_ms + hit_count + app + framework-membership
   metadata. The form UI uses this for its multi-select.

2. **Free-form resolution** — given a dotted path the customer typed
   (``my_app.tasks.heavy_job``), import the module, walk attribute access,
   and validate eligibility for ``line_profiler``. C-extensions / builtins
   lack ``__code__`` and cannot be line-profiled; lambdas and Server
   Scripts (filename starts with ``<``) are similarly out-of-scope.

This module is **pure** — no Frappe DB / Redis access. The API endpoint
(``api.py``) loads the parsed call trees from the Profiler Session and
passes them to ``_build_candidates_from_trees``.
"""

import importlib

from frappe_profiler.analyzers.base import FRAMEWORK_APPS

CANDIDATE_CAP = 30


class PickerError(Exception):
	"""Raised when free-form resolution fails (bad import, unknown attribute,
	empty path, or top-level-module-only). The form surfaces the message to
	the customer inline."""


def _is_synthetic_frame(function: str) -> bool:
	"""pyinstrument synthesizes nodes for ``<root>``, ``<sql>``, and
	bracketed pseudo-frames like ``[finalize]``. None of these are real
	Python functions and they cannot be line-profiled."""
	if not function:
		return True
	return function.startswith("<") or function.startswith("[")


def _walk_tree(node: dict, hits: dict) -> None:
	"""Recursively walk a pyinstrument tree, accumulating per-function
	cumulative_ms and hit count into ``hits``.

	hits[function] = {
	    "dotted_path": str,
	    "file": str,
	    "lineno": int,
	    "cumulative_ms": float,
	    "hit_count": int,
	}
	"""
	if not isinstance(node, dict):
		return

	function = node.get("function") or ""
	kind = node.get("kind", "python")

	if kind == "python" and not _is_synthetic_frame(function):
		entry = hits.get(function)
		if entry is None:
			hits[function] = {
				"dotted_path": function,
				"file": node.get("filename") or "",
				"lineno": int(node.get("lineno") or 0),
				"cumulative_ms": float(node.get("cumulative_ms") or 0),
				"hit_count": 1,
			}
		else:
			entry["cumulative_ms"] += float(node.get("cumulative_ms") or 0)
			entry["hit_count"] += 1

	for child in node.get("children") or []:
		_walk_tree(child, hits)


def _build_candidates_from_trees(trees: list[dict], findings: list[dict]) -> list[dict]:
	"""Aggregate candidates from per-action pyinstrument trees.

	``findings`` is currently a placeholder for future enrichment (pulling
	additional callsites from N+1/Slow-Query findings). The v1 candidate
	list is purely tree-derived.
	"""
	hits: dict = {}
	for tree in trees:
		_walk_tree(tree, hits)

	candidates = []
	for entry in hits.values():
		dotted = entry["dotted_path"]
		app = dotted.split(".", 1)[0] if "." in dotted else dotted
		candidates.append({
			"dotted_path": dotted,
			"qualname": dotted.rsplit(".", 1)[-1] if "." in dotted else dotted,
			"file": entry["file"],
			"lineno": entry["lineno"],
			"app": app,
			"cumulative_ms": round(entry["cumulative_ms"], 2),
			"hit_count": entry["hit_count"],
			"is_framework": app in FRAMEWORK_APPS,
		})

	candidates.sort(key=lambda c: c["cumulative_ms"], reverse=True)
	return candidates[:CANDIDATE_CAP]


def _check_eligibility(obj) -> tuple[bool, str | None]:
	"""Return (eligible, reason). line_profiler can attach to functions
	with a real ``__code__`` object that aren't lambdas or runtime-eval'd
	code."""
	code = getattr(obj, "__code__", None)
	if code is None:
		return False, "C-extension or builtin (no __code__ attribute)"

	name = getattr(obj, "__name__", "")
	if name == "<lambda>":
		return False, "lambda functions cannot be line-profiled"

	filename = getattr(code, "co_filename", "") or ""
	if filename.startswith("<"):
		# Server Scripts compile via compile(source, '<server_script_...>', ...).
		return False, "Server Scripts cannot be line-profiled (no stable file)"

	return True, None


def resolve_freeform(dotted_path: str) -> dict:
	"""Resolve a free-form dotted path to a function metadata dict.

	Returns a dict with the same shape as ``_build_candidates_from_trees``
	entries, plus ``eligible`` and (when ineligible) ``ineligible_reason``.

	Raises ``PickerError`` if the path is empty, points only at a module,
	or any prefix of it can't be imported / walked.
	"""
	if not dotted_path:
		raise PickerError("dotted path is empty")

	parts = dotted_path.split(".")
	if len(parts) < 2:
		raise PickerError(
			f"'{dotted_path}' looks like a top-level module — need at least "
			"module.function (e.g. 'my_app.tasks.heavy_job')"
		)

	# Find the longest leading prefix that imports as a module.
	module = None
	module_parts = 0
	last_import_error: str | None = None
	for i in range(len(parts), 0, -1):
		candidate = ".".join(parts[:i])
		try:
			module = importlib.import_module(candidate)
			module_parts = i
			break
		except ImportError as exc:
			last_import_error = str(exc)
			continue

	if module is None:
		raise PickerError(
			f"could not import any prefix of '{dotted_path}' "
			f"(last error: {last_import_error})"
		)

	if module_parts == len(parts):
		raise PickerError(
			f"'{dotted_path}' resolved to a module, not a function — "
			"include the function name (e.g. 'json.dumps' rather than 'json')"
		)

	# Walk the remaining parts as attribute access on the resolved module.
	obj = module
	for attr in parts[module_parts:]:
		try:
			obj = getattr(obj, attr)
		except AttributeError:
			raise PickerError(
				f"attribute '{attr}' not found while resolving '{dotted_path}'"
			)

	qualname = ".".join(parts[module_parts:])
	eligible, reason = _check_eligibility(obj)

	code = getattr(obj, "__code__", None)
	file = code.co_filename if code is not None else "<unknown>"
	lineno = code.co_firstlineno if code is not None else 0

	return {
		"dotted_path": dotted_path,
		"qualname": qualname,
		"file": file,
		"lineno": lineno,
		"app": parts[0],
		"cumulative_ms": 0.0,
		"hit_count": 0,
		"is_framework": parts[0] in FRAMEWORK_APPS,
		"eligible": eligible,
		"ineligible_reason": reason,
	}
