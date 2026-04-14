# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Source-inspection regression guards for analyze.py's v0.5.0 wiring.

These protect against silent removal/renaming of the v0.5.0 integration
points. Since analyze.run is a large orchestrator that's hard to
exercise end-to-end without a running Frappe site, the cheapest
regression guard is to check that the wiring symbols literally appear
in the source.
"""

import inspect

from frappe_profiler import analyze


def test_analyze_imports_v5_analyzers():
	src = inspect.getsource(analyze)
	assert "infra_pressure" in src
	assert "frontend_timings" in src


def test_builtin_analyzers_list_includes_v5():
	# _BUILTIN_ANALYZERS is the list consumed by _get_analyzers which
	# drives the run loop. If this list loses the v0.5.0 analyzers,
	# they never fire.
	assert any(
		a.__module__.endswith("infra_pressure")
		for a in analyze._BUILTIN_ANALYZERS
	), "infra_pressure.analyze missing from _BUILTIN_ANALYZERS"
	assert any(
		a.__module__.endswith("frontend_timings")
		for a in analyze._BUILTIN_ANALYZERS
	), "frontend_timings.analyze missing from _BUILTIN_ANALYZERS"


def test_run_loads_frontend_data_into_context():
	src = inspect.getsource(analyze.run)
	# The load line must reference both the Redis key family and the
	# context attribute.
	assert "profiler:frontend:" in src
	assert "context.frontend_data" in src


def test_run_attaches_infra_to_recordings():
	src = inspect.getsource(analyze.run)
	# Per-recording infra dicts must be read from profiler:infra: keys
	# and attached as rec["infra"] before the analyzer loop runs.
	assert "profiler:infra:" in src
	# The assignment can be spelled rec["infra"] or rec['infra']; accept either.
	assert 'rec["infra"]' in src or "rec['infra']" in src


def test_persist_writes_v5_aggregate_json():
	src = inspect.getsource(analyze._persist)
	# _persist must serialize the v0.5.0 aggregate into v5_aggregate_json
	# on the session doc, or the renderer gets nothing to work with.
	assert "v5_aggregate_json" in src
	# And it must read at least one of the v0.5.0 aggregate keys from context.
	assert (
		"infra_timeline" in src
		and "frontend_xhr_matched" in src
	)
