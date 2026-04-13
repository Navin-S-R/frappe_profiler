# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for the sidecar wrap factory in capture.py.

These tests exercise the wrap mechanics in isolation by passing in a
fake `frappe_local_proxy` object — they don't require a real Frappe
runtime. The integration with frappe.local happens in test_capture_pipeline.py.
"""

import pytest

from frappe_profiler import capture


class FakeLocal:
	"""Stand-in for frappe.local — supports getattr/setattr."""
	pass


@pytest.fixture
def fake_local():
	return FakeLocal()


def test_wrap_passthrough_when_no_active_session(fake_local):
	calls = []

	def orig(*args, **kwargs):
		calls.append(("orig", args, kwargs))
		return "result"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	result = wrapped("User", "x@y.com")

	assert result == "result"
	assert calls == [("orig", ("User", "x@y.com"), {})]
	assert not hasattr(fake_local, "profiler_sidecar")


def test_wrap_records_entry_when_active(fake_local):
	fake_local._profiler_active_session_id = "test-session"
	fake_local.profiler_sidecar = []

	def orig(doctype, name):
		return "result"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	wrapped("User", "x@y.com")

	assert len(fake_local.profiler_sidecar) == 1
	entry = fake_local.profiler_sidecar[0]
	assert entry["fn_name"] == "get_doc"
	assert entry["identifier_raw"] == ("User", "x@y.com")
	assert entry["identifier_safe"][0] == "User"
	assert len(entry["identifier_safe"][1]) == 12


def test_wrap_records_even_on_exception(fake_local):
	fake_local._profiler_active_session_id = "test-session"
	fake_local.profiler_sidecar = []

	def orig(doctype, name):
		raise ValueError("boom")

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)

	with pytest.raises(ValueError, match="boom"):
		wrapped("User", "x@y.com")

	# Entry must be recorded even though the call raised
	assert len(fake_local.profiler_sidecar) == 1


def test_wrap_reentrancy_guard(fake_local):
	"""A wrapped call from inside another wrapped call must not double-record."""
	fake_local._profiler_active_session_id = "test-session"
	fake_local.profiler_sidecar = []

	inner_calls = []

	def inner_orig(key):
		inner_calls.append(key)
		return "inner"

	wrapped_inner = capture._make_wrap(inner_orig, "cache_get", local_proxy=fake_local)

	def outer_orig(doctype, name):
		# Outer wrap calls inner wrap from inside its execution
		return wrapped_inner("user_lang:" + name)

	wrapped_outer = capture._make_wrap(outer_orig, "get_doc", local_proxy=fake_local)
	wrapped_outer("User", "x@y.com")

	# Outer recorded one entry. Inner ran (call went through) but recorded
	# no entry because the re-entrancy flag was set.
	assert len(fake_local.profiler_sidecar) == 1
	assert fake_local.profiler_sidecar[0]["fn_name"] == "get_doc"
	assert inner_calls == ["user_lang:x@y.com"]


def test_wrap_caps_sidecar_at_50000_entries(fake_local):
	fake_local._profiler_active_session_id = "test-session"
	fake_local.profiler_sidecar = [{"placeholder": True}] * 50_000

	def orig(doctype, name):
		return "result"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	wrapped("User", "x@y.com")  # this should be dropped

	assert len(fake_local.profiler_sidecar) == 50_000
	assert getattr(fake_local, "profiler_sidecar_truncated", False) is True


def test_wrap_preserves_original_via_attribute(fake_local):
	def orig(doctype, name):
		return "ok"

	wrapped = capture._make_wrap(orig, "get_doc", local_proxy=fake_local)
	assert wrapped._profiler_original is orig


def test_wrap_chains_preexisting_wrap(fake_local):
	"""If orig has _profiler_original, our wrap chains via it (no double-wrap)."""
	def true_orig(doctype, name):
		return "true"

	def existing_wrap(*args, **kwargs):
		return true_orig(*args, **kwargs)

	existing_wrap._profiler_original = true_orig

	wrapped = capture._make_wrap(existing_wrap, "get_doc", local_proxy=fake_local)
	# Our _profiler_original points to the existing wrap (not double-deep)
	assert wrapped._profiler_original is existing_wrap
