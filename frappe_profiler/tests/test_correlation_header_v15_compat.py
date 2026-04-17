# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for v0.5.3 X-Profiler-Recording-Id header injection across
Frappe v15 and v16.

Why this exists: the Per-XHR timings section of the Frontend panel
was empty on v15 deployments. Root cause — ``frappe.local.response_
headers`` (the staging dict our injector wrote to) does not exist
on v15. Only v16's ``frappe/app.py`` has
``response.headers.update(frappe.local.response_headers)`` at
response-build time. On v15 that line is absent, so the staged
value silently vanished.

Both versions do pass the ``response`` object into
``after_request`` hooks via ``run_after_request_hooks(request,
response)`` → ``frappe.call(after_request_task, response=response,
request=request)``. So the portable fix is to accept the response
object and write to ``response.headers`` directly when available,
keeping the staging-dict path as a belt-and-braces fallback.

These tests exercise both code paths and pin the invariant that
both the recording-id header AND Access-Control-Expose-Headers
land on whichever object is the authoritative sink.
"""

import sys
import types


def _install_frappe_stub_with_local():
	"""Install a minimal frappe stub with frappe.local configurable
	per test. Tests can then monkey-patch
	``sys.modules['frappe'].local.response_headers`` to simulate v16
	presence or v15 absence.

	Also stubs ``frappe.recorder`` and the ``frappe_profiler.session``
	/ ``.capture`` submodules because ``hooks_callbacks.py`` imports
	them at module top — those imports would fail when
	``_fresh_module`` re-imports ``hooks_callbacks``.
	"""
	frappe = types.ModuleType("frappe")
	frappe.local = types.SimpleNamespace()
	frappe.log_error = lambda **kw: None
	frappe.session = types.SimpleNamespace(user=None)
	frappe.cache = types.SimpleNamespace(
		set_value=lambda *a, **kw: None,
		get_value=lambda *a, **kw: None,
	)
	frappe.logger = lambda: types.SimpleNamespace(
		warning=lambda *a, **kw: None,
		info=lambda *a, **kw: None,
	)

	recorder = types.ModuleType("frappe.recorder")
	sys.modules["frappe"] = frappe
	sys.modules["frappe.recorder"] = recorder

	# Submodules hooks_callbacks imports at top.
	session_mod = types.ModuleType("frappe_profiler.session")
	session_mod.register_recording = lambda *a, **kw: True
	session_mod.get_active_session_for = lambda user: None
	session_mod.SESSION_TTL_SECONDS = 600
	sys.modules["frappe_profiler.session"] = session_mod

	capture_mod = types.ModuleType("frappe_profiler.capture")
	capture_mod._force_stop_inflight_capture = lambda **kw: None
	sys.modules["frappe_profiler.capture"] = capture_mod

	return frappe


def _fresh_module():
	"""Re-import the hooks_callbacks module so it picks up the fresh
	frappe stub. Clears any cached frappe_profiler.* modules too so
	they rebind to the current stubs (otherwise test pollution from
	an earlier test's stub leaks into the re-import).
	"""
	for mod in list(sys.modules.keys()):
		if mod.startswith("frappe_profiler.hooks") or mod == "frappe_profiler":
			del sys.modules[mod]
		elif mod == "frappe_profiler.settings":
			del sys.modules[mod]
	from frappe_profiler import hooks_callbacks
	return hooks_callbacks


class _FakeHeaders(dict):
	"""Werkzeug-style Headers analog. The real class supports a
	case-insensitive get with a default + iteration; for the
	injection logic our basic dict with .get() is enough."""
	pass


class _FakeResponse:
	def __init__(self):
		self.headers = _FakeHeaders()


class TestV15Path:
	"""On v15, ``frappe.local.response_headers`` does not exist.
	The injector must fall back to writing on ``response.headers``
	directly."""

	def test_writes_header_to_response_when_local_dict_missing(self):
		frappe = _install_frappe_stub_with_local()
		# Explicitly simulate v15: no response_headers attribute.
		assert not hasattr(frappe.local, "response_headers")

		hc = _fresh_module()
		resp = _FakeResponse()
		hc._inject_correlation_header("rec-abc", response=resp)

		assert resp.headers["X-Profiler-Recording-Id"] == "rec-abc"
		# Browsers require this to expose the custom header to JS.
		assert (
			resp.headers["Access-Control-Expose-Headers"]
			== "X-Profiler-Recording-Id"
		)

	def test_no_response_and_no_local_dict_is_silent_noop(self):
		"""Defensive: called in a non-HTTP context with no response
		and no staging dict. Must not raise."""
		_install_frappe_stub_with_local()
		hc = _fresh_module()
		# Must not raise.
		hc._inject_correlation_header("rec-abc", response=None)


class TestV16Path:
	"""On v16, ``frappe.local.response_headers`` is a dict/Headers
	instance that gets ``update()``'d onto the real response at
	build time. Writing there still needs to work."""

	def test_writes_header_to_local_dict_when_present(self):
		frappe = _install_frappe_stub_with_local()
		frappe.local.response_headers = {}
		hc = _fresh_module()

		hc._inject_correlation_header("rec-xyz")

		assert frappe.local.response_headers["X-Profiler-Recording-Id"] == "rec-xyz"
		assert (
			frappe.local.response_headers["Access-Control-Expose-Headers"]
			== "X-Profiler-Recording-Id"
		)

	def test_also_writes_to_response_when_both_available(self):
		"""Belt-and-braces: on v16, passing ``response`` too should
		write the header on BOTH the staging dict and the response.
		Double-write is harmless (same key+value) and makes the
		injection survive any framework change that stops copying
		the staging dict."""
		frappe = _install_frappe_stub_with_local()
		frappe.local.response_headers = {}
		hc = _fresh_module()

		resp = _FakeResponse()
		hc._inject_correlation_header("rec-dual", response=resp)

		assert frappe.local.response_headers["X-Profiler-Recording-Id"] == "rec-dual"
		assert resp.headers["X-Profiler-Recording-Id"] == "rec-dual"


class TestExposeHeadersMerging:
	"""The Access-Control-Expose-Headers must MERGE with any
	existing value set by upstream middleware (e.g. another app's
	CORS config), not overwrite. Token-by-token check, not
	substring."""

	def test_merges_into_existing_expose_header_on_v15_response(self):
		frappe = _install_frappe_stub_with_local()
		hc = _fresh_module()

		resp = _FakeResponse()
		resp.headers["Access-Control-Expose-Headers"] = "X-Custom-App-Header"
		hc._inject_correlation_header("rec-merge", response=resp)

		val = resp.headers["Access-Control-Expose-Headers"]
		# Both tokens present.
		assert "X-Custom-App-Header" in val
		assert "X-Profiler-Recording-Id" in val

	def test_does_not_duplicate_on_second_call(self):
		"""Idempotent: calling the injector twice must not append
		the token a second time."""
		frappe = _install_frappe_stub_with_local()
		hc = _fresh_module()

		resp = _FakeResponse()
		hc._inject_correlation_header("rec-1", response=resp)
		hc._inject_correlation_header("rec-1", response=resp)

		val = resp.headers["Access-Control-Expose-Headers"]
		assert val.count("X-Profiler-Recording-Id") == 1

	def test_case_insensitive_token_check_v15(self):
		"""Prevents a substring-match false positive: an upstream
		header "X-Profiler-Recording-Id-Legacy" should NOT prevent
		us from appending our real token."""
		frappe = _install_frappe_stub_with_local()
		hc = _fresh_module()

		resp = _FakeResponse()
		resp.headers["Access-Control-Expose-Headers"] = "X-Profiler-Recording-Id-Legacy"
		hc._inject_correlation_header("rec-distinct", response=resp)

		val = resp.headers["Access-Control-Expose-Headers"]
		# The legacy token is preserved…
		assert "X-Profiler-Recording-Id-Legacy" in val
		# …AND our real one is appended.
		tokens = {t.strip() for t in val.split(",")}
		assert "X-Profiler-Recording-Id" in tokens


class TestFailSoft:
	"""Injection must never break a request. If headers container
	misbehaves (raises on get/set), the injector swallows it."""

	def test_raising_response_headers_does_not_propagate(self):
		class _BrokenHeaders:
			def __setitem__(self, k, v):
				raise RuntimeError("cursed headers object")
			def get(self, k, default=None):
				return default

		class _BrokenResponse:
			headers = _BrokenHeaders()

		_install_frappe_stub_with_local()
		hc = _fresh_module()
		# Must not raise.
		hc._inject_correlation_header("rec-x", response=_BrokenResponse())
