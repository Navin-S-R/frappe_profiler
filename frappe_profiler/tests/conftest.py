# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Pytest configuration for frappe_profiler analyzer tests.

These tests are deliberately decoupled from Frappe — each analyzer is
a pure function over a list of recording dicts, so we can exercise them
with JSON fixtures and no running site. Run with:

    cd apps/frappe_profiler
    python -m pytest frappe_profiler/tests/ -v

(or just `pytest frappe_profiler/tests/ -v` if pytest is installed globally).
"""

import json
import os

import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str) -> dict:
	"""Load a JSON fixture from tests/fixtures/<name>.json."""
	path = os.path.join(FIXTURES_DIR, f"{name}.json")
	with open(path, encoding="utf-8") as f:
		return json.load(f)


@pytest.fixture
def n_plus_one_recording():
	return load_fixture("n_plus_one_recording")


@pytest.fixture
def full_scan_recording():
	return load_fixture("full_scan_recording")


@pytest.fixture
def clean_recording():
	return load_fixture("clean_recording")


@pytest.fixture
def empty_context():
	"""Minimal AnalyzeContext — just enough to satisfy analyzer signatures."""
	from frappe_profiler.analyzers.base import AnalyzeContext

	return AnalyzeContext(session_uuid="test-uuid", docname="test-docname")
