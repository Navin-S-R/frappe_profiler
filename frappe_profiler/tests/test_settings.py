# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for the frappe_profiler.settings cached reader.

The reader is the single place analyzers and hooks call to resolve
configuration — threshold values, the enabled toggle, the tracked-
apps allowlist. Tests here pin the precedence (DocType > site_config
> default), the soft-fail behavior (never crash a request), and the
dataclass immutability that makes caching safe.
"""

from dataclasses import FrozenInstanceError
import sys
import types

import pytest

# Stub frappe so the lazy import inside settings.py can be monkeypatched
# in tests without a real bench. We unconditionally REPLACE any existing
# stub other tests may have registered — other pure-logic test modules
# (test_boot_session, test_pdf_expand_collapsibles) install bare
# types.ModuleType("frappe") stubs that lack cache/conf/db, which would
# break monkeypatching here.
_stub = types.ModuleType("frappe")
_cache = types.SimpleNamespace(
	get_value=lambda k: None,
	set_value=lambda k, v: None,
	delete_value=lambda k: None,
)
_stub.cache = _cache
_stub.conf = {}
_stub.db = types.SimpleNamespace(exists=lambda *a, **kw: False)
_stub.get_cached_doc = lambda *a, **kw: None
sys.modules["frappe"] = _stub

from frappe_profiler import settings  # noqa: E402


class TestDefaults:
	def test_config_has_defaults_when_doctype_missing(self, monkeypatch):
		"""No DocType available → hardcoded defaults. This is the
		fresh-install / pre-migrate path, must not raise."""
		monkeypatch.setattr(settings, "_read_doctype_row", lambda: None)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)

		cfg = settings._resolve()
		assert cfg.enabled is True
		assert cfg.session_retention_days == 30
		assert cfg.tracked_apps == ()
		assert cfg.redundant_doc_threshold == 5
		# v0.5.2 round 4: bumped from 10 → 50 to cut 0ms "cache
		# looked up 20× from same callsite" noise that we can't
		# time (cache lookups are free individually). 50 matches
		# the High-severity boundary; anything below is too
		# ambiguous to emit at Medium.
		assert cfg.redundant_cache_threshold == 50
		assert cfg.redundant_perm_threshold == 10
		assert cfg.n_plus_one_min_occurrences == 10

	def test_config_is_frozen(self):
		cfg = settings.ProfilerConfig()
		with pytest.raises(FrozenInstanceError):
			cfg.enabled = False


class TestPrecedence:
	"""Precedence: DocType row > site_config.json > hardcoded default."""

	def test_doctype_wins_over_site_config(self, monkeypatch):
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": 7},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 3 if k == "redundant_doc_threshold" else None,
		)
		cfg = settings._resolve()
		assert cfg.redundant_doc_threshold == 7

	def test_site_config_wins_when_doctype_unset(self, monkeypatch):
		"""DocType returns 0 / None for the field → fall through to
		site_config. Tests the pattern of 'admin hasn't populated
		Settings but has legacy site_config overrides'."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": None},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 99 if k == "redundant_doc_threshold" else None,
		)
		cfg = settings._resolve()
		assert cfg.redundant_doc_threshold == 99

	def test_default_fallback_when_both_unset(self, monkeypatch):
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": None},
		)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		cfg = settings._resolve()
		assert cfg.redundant_doc_threshold == 5  # default


class TestSoftFail:
	def test_get_config_never_raises(self, monkeypatch):
		"""No matter what _resolve raises, get_config returns defaults."""
		def boom():
			raise RuntimeError("db gone")
		monkeypatch.setattr(settings, "_resolve", boom)
		# Defeat the cache path too.
		import frappe
		monkeypatch.setattr(
			frappe.cache, "get_value",
			lambda k: None, raising=False,
		)
		cfg = settings.get_config()
		assert cfg.enabled is True  # fail-open default

	def test_is_enabled_defaults_true_on_error(self, monkeypatch):
		"""If the settings read crashes, is_enabled must return True.
		Returning False would silently disable the profiler — a very
		confusing support issue ('why isn't it recording anything?')."""
		def boom():
			raise RuntimeError("cache down")
		monkeypatch.setattr(settings, "get_config", boom)
		assert settings.is_enabled() is True

	def test_get_tracked_apps_returns_empty_tuple_on_error(self, monkeypatch):
		def boom():
			raise RuntimeError("cache down")
		monkeypatch.setattr(settings, "get_config", boom)
		assert settings.get_tracked_apps() == ()


class TestTrackedApps:
	def test_tracked_apps_normalized_to_tuple(self, monkeypatch):
		"""tracked_apps must be a tuple in the dataclass so it's
		hashable / immutable. Input from the DocType is a list — the
		reader must convert."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"tracked_apps": ("myapp", "another")},
		)
		monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
		cfg = settings._resolve()
		assert isinstance(cfg.tracked_apps, tuple)
		assert cfg.tracked_apps == ("myapp", "another")
