# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the v0.3.0 streaming _fetch_recordings + tree/sidecar load."""

import pickle

import pytest

from optimus import analyze


class FakeCache:
	def __init__(self, store=None):
		self.store = store or {}

	def hget(self, hash_name, key):
		return self.store.get((hash_name, key))

	def get_value(self, key):
		return self.store.get(key)

	def hdel(self, hash_name, key):
		self.store.pop((hash_name, key), None)

	def delete_value(self, key):
		self.store.pop(key, None)


def test_fetch_recordings_yields_tree_and_sidecar(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-1"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {
			"uuid": rec_uuid,
			"calls": [{"query": "SELECT 1", "duration": 5}],
		},
		# Phase K hardening: tree blobs are HMAC-signed by
		# ``hooks_callbacks._dump_capture_state_to_redis`` before
		# being stashed; ``analyze._fetch_recordings`` verifies the
		# signature via ``session.unsign_blob``. Round-trip the test
		# fixture through ``sign_blob`` so it survives the verify step.
		f"profiler:tree:{rec_uuid}": __import__(
			"optimus.session", fromlist=["sign_blob"]
		).sign_blob(pickle.dumps({"fake": "tree"})),
		f"profiler:sidecar:{rec_uuid}": [
			{"fn_name": "get_doc", "identifier_safe": ("User", "abc123")}
		],
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	rec = results[0]
	assert rec["uuid"] == "rec-1"
	assert rec["pyi_session"] == {"fake": "tree"}
	assert rec["sidecar"] == [
		{"fn_name": "get_doc", "identifier_safe": ("User", "abc123")}
	]


def test_fetch_recordings_loads_drifted_signed_tree(monkeypatch):
	"""Phase K v0.7 GA: when the HMAC secret drifts across processes
	(e.g., recorder + analyze workers fell back to per-process random
	keys because ``encryption_key`` wasn't in site_config), the
	stored blob is ``32-byte sig + pickle.dumps(...)`` but unsign
	fails because the sig was computed with a different secret.
	The read-side dual-attempt fallback strips the first 32 bytes
	and loads the rest as pickle.
	"""
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-drifted"
	# 32 random bytes (pretending to be an HMAC from another secret) +
	# a valid pickle. The current process's HMAC will not verify these
	# bytes, so unsign_blob returns None and we fall through to the
	# stripped-sig attempt.
	import os
	fake_sig = os.urandom(32)
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		f"profiler:tree:{rec_uuid}": fake_sig + pickle.dumps({"drifted": "tree"}),
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	assert results[0]["pyi_session"] == {"drifted": "tree"}


def test_fetch_recordings_loads_legacy_unsigned_tree(monkeypatch):
	"""Phase K transition fallback: blobs written before the HMAC
	rollout lack the 32-byte signature prefix. The analyze fetch
	should still load them (with a warning log) when
	``optimus_allow_unsigned_pickles`` defaults to True - otherwise
	every session in flight at deploy time would silently lose its
	pyi tree (and the Phase-2 picker would render no candidates).
	"""
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-legacy"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		# Raw pickle - NO HMAC prefix - simulates a pre-Sprint-1 blob.
		f"profiler:tree:{rec_uuid}": pickle.dumps({"legacy": "tree"}),
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	# Fallback succeeded - the tree loaded despite the missing signature.
	assert results[0]["pyi_session"] == {"legacy": "tree"}


def test_fetch_recordings_handles_missing_tree(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-2"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		# No tree key, no sidecar key
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	assert results[0]["pyi_session"] is None
	assert results[0]["sidecar"] == []


def test_fetch_recordings_handles_pickle_failure(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-3"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		f"profiler:tree:{rec_uuid}": b"not-a-valid-pickle",
		f"profiler:sidecar:{rec_uuid}": [],
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	# log_error is monkey-patched to a no-op so the test doesn't need a site
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)

	# Should not raise; returns recording with pyi_session=None
	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	assert results[0]["pyi_session"] is None


def test_fetch_recordings_is_a_generator():
	"""The function must be a generator (lazy), not a list-returning function."""
	import inspect

	assert inspect.isgeneratorfunction(analyze._fetch_recordings)


def test_cleanup_redis_deletes_tree_and_sidecar_keys(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH, RECORDER_REQUEST_SPARSE_HASH

	uuids = ["rec-a", "rec-b"]
	store = {
		(RECORDER_REQUEST_HASH, "rec-a"): {"uuid": "rec-a"},
		(RECORDER_REQUEST_HASH, "rec-b"): {"uuid": "rec-b"},
		(RECORDER_REQUEST_SPARSE_HASH, "rec-a"): {},
		(RECORDER_REQUEST_SPARSE_HASH, "rec-b"): {},
		"profiler:tree:rec-a": b"blob-a",
		"profiler:tree:rec-b": b"blob-b",
		"profiler:sidecar:rec-a": [],
		"profiler:sidecar:rec-b": [],
	}
	cache = FakeCache(store)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	# session.delete_session_state inside _cleanup_redis touches more keys;
	# stub it so the test stays focused on the per-recording cleanup.
	from optimus import session as ps_session

	monkeypatch.setattr(
		ps_session, "delete_session_state", lambda uuid: None, raising=True
	)
	# log_error is monkey-patched to a no-op so the test doesn't need a site
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)

	analyze._cleanup_redis("test-session", uuids)

	# All four per-recording keys are gone
	for uuid in uuids:
		assert (RECORDER_REQUEST_HASH, uuid) not in cache.store
		assert (RECORDER_REQUEST_SPARSE_HASH, uuid) not in cache.store
		assert f"profiler:tree:{uuid}" not in cache.store
		assert f"profiler:sidecar:{uuid}" not in cache.store
