# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for the Sensitivity Profile presets in optimus.settings.

A ``config_profile`` Select (Strict / Recommended / Relaxed / Custom) drives
the detection-sensitivity thresholds. Storage strategy is *resolve at read
time*: only the profile name is stored; ``_resolve()`` maps profile →
thresholds for the named presets, so ``Recommended`` always tracks the current
shipped defaults. Only ``Custom`` reads the per-field stored values (preserving
the pre-profile precedence: DocType row > site_config > default).

Back-compat: an existing Single predating the field has no ``config_profile``
key, which must resolve as ``Custom`` so a previously-tuned threshold keeps
driving analysis (no silent reset to Recommended, no migration patch).
"""

import json
import os
import re
import sys
import types

import pytest

from optimus import settings

# The DocType field descriptions advertise "Reference values — Strict: X ·
# Recommended: Y · Relaxed: Z" to admins. Those numbers MUST equal _PROFILES or
# the UI lies about what a preset does. This path locates the DocType JSON
# relative to this test file (no bench needed).
_SETTINGS_JSON = os.path.join(
	os.path.dirname(os.path.dirname(__file__)),
	"optimus", "doctype", "optimus_settings", "optimus_settings.json",
)
_REFVAL_RE = re.compile(
	r"Strict:\s*([\d.]+).*?Recommended:\s*([\d.]+).*?Relaxed:\s*([\d.]+)"
)

# The nine detection-sensitivity knobs the preset governs. Kept here
# (not imported) so the test pins the contract independently of the
# implementation's own tuple.
SENSITIVITY_KEYS = (
	"redundant_doc_threshold",
	"redundant_cache_threshold",
	"redundant_perm_threshold",
	"n_plus_one_min_occurrences",
	"slow_query_threshold_ms",
	"slow_hot_path_pct_threshold",
	"slow_hot_path_min_ms",
	"hot_line_high_pct",
	"hot_line_high_min_ms",
)


@pytest.fixture(autouse=True)
def _frappe_stub(monkeypatch):
	"""Minimal frappe stub, mirroring test_settings.py — settings.py
	imports frappe lazily, but get_config's cache path touches
	frappe.cache, so give it harmless no-ops."""
	stub = types.ModuleType("frappe")
	stub.cache = types.SimpleNamespace(
		get_value=lambda k: None,
		set_value=lambda k, v: None,
		delete_value=lambda k: None,
	)
	stub.conf = {}
	stub.db = types.SimpleNamespace(exists=lambda *a, **kw: False)
	stub.get_cached_doc = lambda *a, **kw: None
	monkeypatch.setitem(sys.modules, "frappe", stub)
	yield stub


def _no_site_conf(monkeypatch):
	monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)


class TestProfileTable:
	def test_named_profiles_exist(self):
		for name in ("Strict", "Recommended", "Relaxed"):
			assert name in settings._PROFILES, f"missing profile {name}"
			# Each named profile defines all nine sensitivity knobs.
			assert set(settings._PROFILES[name]) == set(SENSITIVITY_KEYS)

	def test_custom_is_not_a_named_profile(self):
		# Custom means "use stored field values", so it must NOT be in
		# the preset table — _resolve keys off `_PROFILES.get(profile)`.
		assert "Custom" not in settings._PROFILES

	def test_recommended_equals_defaults(self):
		"""Drift guard: Recommended must mirror the shipped _DEFAULTS for
		every sensitivity key, or the 'Recommended tracks defaults'
		promise silently breaks."""
		for key in SENSITIVITY_KEYS:
			assert settings._PROFILES["Recommended"][key] == settings._DEFAULTS[key], key

	def test_strict_catches_more_than_relaxed(self):
		"""Sanity on the ordering: every Strict knob is <= its Relaxed
		counterpart (lower threshold = catch more)."""
		for key in SENSITIVITY_KEYS:
			assert settings._PROFILES["Strict"][key] <= settings._PROFILES["Relaxed"][key], key

	def test_profiles_match_doctype_reference_values(self):
		"""_PROFILES must equal the 'Reference values — Strict/Recommended/
		Relaxed' numbers advertised in each sensitivity field's DocType
		description, so the form's help text never contradicts behavior."""
		with open(_SETTINGS_JSON) as fh:
			doc = json.load(fh)
		by_name = {f["fieldname"]: f for f in doc["fields"]}
		for key in SENSITIVITY_KEYS:
			desc = by_name[key].get("description", "")
			m = _REFVAL_RE.search(desc)
			assert m, f"{key} description missing 'Reference values' triplet"
			strict, recommended, relaxed = (float(g) for g in m.groups())
			assert settings._PROFILES["Strict"][key] == strict, f"{key} Strict"
			assert settings._PROFILES["Recommended"][key] == recommended, f"{key} Recommended"
			assert settings._PROFILES["Relaxed"][key] == relaxed, f"{key} Relaxed"


class TestProfileResolution:
	@pytest.mark.parametrize("profile", ["Strict", "Recommended", "Relaxed"])
	def test_named_profile_drives_all_keys(self, monkeypatch, profile):
		"""A named profile resolves every sensitivity key to its preset
		number, IGNORING any conflicting stored field value."""
		row = {"config_profile": profile}
		# Poison every key with a value that is neither the preset nor a default.
		for key in SENSITIVITY_KEYS:
			row[key] = 9999
		monkeypatch.setattr(settings, "_read_doctype_row", lambda: row)
		_no_site_conf(monkeypatch)

		cfg = settings._resolve()
		for key in SENSITIVITY_KEYS:
			assert getattr(cfg, key) == settings._PROFILES[profile][key], key

	def test_custom_reads_stored_values(self, monkeypatch):
		"""Custom preserves the pre-profile behavior: stored DocType value wins."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"config_profile": "Custom", "redundant_doc_threshold": 7},
		)
		_no_site_conf(monkeypatch)
		assert settings._resolve().redundant_doc_threshold == 7

	def test_custom_still_honors_site_config_fallback(self, monkeypatch):
		"""Under Custom, the legacy site_config fallback still applies when
		the DocType value is unset."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"config_profile": "Custom", "redundant_doc_threshold": None},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 99 if k == "redundant_doc_threshold" else None,
		)
		assert settings._resolve().redundant_doc_threshold == 99

	def test_named_profile_bypasses_site_config(self, monkeypatch):
		"""A named profile wins over site_config — the preset is authoritative."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"config_profile": "Strict"},
		)
		monkeypatch.setattr(
			settings, "_site_conf_fallback",
			lambda k: 99 if k == "redundant_doc_threshold" else None,
		)
		assert settings._resolve().redundant_doc_threshold == \
			settings._PROFILES["Strict"]["redundant_doc_threshold"]


class TestBackCompat:
	def test_missing_profile_key_resolves_as_custom(self, monkeypatch):
		"""An existing Single predating the field has no config_profile key.
		It must resolve as Custom (preserve stored values), NOT Recommended."""
		monkeypatch.setattr(
			settings, "_read_doctype_row",
			lambda: {"redundant_doc_threshold": 7},  # no config_profile
		)
		_no_site_conf(monkeypatch)
		cfg = settings._resolve()
		assert cfg.config_profile == "Custom"
		assert cfg.redundant_doc_threshold == 7  # stored value preserved

	def test_dataclass_default_is_custom(self):
		"""The no-frappe / pre-bench path (OptimusConfig()) must default to
		Custom so threshold dataclass defaults (= Recommended numbers) are
		used as-is, not overridden by an absent profile."""
		assert settings.OptimusConfig().config_profile == "Custom"
		assert settings._DEFAULTS["config_profile"] == "Custom"
