# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Tests for v0.4.0 auto-role-assignment on install."""

import pytest

from frappe_profiler import install


class FakeRole:
	def __init__(self, role):
		self.role = role


class FakeUser:
	def __init__(self, name, roles):
		self.name = name
		self.roles = [FakeRole(r) for r in roles]
		self.added_roles = []

	def append(self, table, value):
		assert table == "roles"
		self.roles.append(FakeRole(value["role"]))
		self.added_roles.append(value["role"])

	def save(self, ignore_permissions=False):
		pass


def test_auto_assign_role_adds_to_system_managers(monkeypatch):
	import frappe

	users_in_db = {
		"alice@example.com": FakeUser("alice@example.com", ["System Manager"]),
		"bob@example.com": FakeUser("bob@example.com", ["Sales User"]),
		"carol@example.com": FakeUser("carol@example.com", ["System Manager", "Profiler User"]),
	}

	def fake_get_all(doctype, filters=None, pluck=None, **kwargs):
		assert doctype == "User"
		return list(users_in_db.keys())

	def fake_get_doc(doctype, name):
		assert doctype == "User"
		return users_in_db[name]

	monkeypatch.setattr(frappe, "get_all", fake_get_all, raising=False)
	monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)

	install._assign_profiler_user_to_system_managers()

	# Alice (System Manager, no Profiler User yet) → Profiler User added
	assert "Profiler User" in users_in_db["alice@example.com"].added_roles
	# Bob (Sales User only) → not touched
	assert users_in_db["bob@example.com"].added_roles == []
	# Carol (already has both) → not touched
	assert users_in_db["carol@example.com"].added_roles == []


def test_on_user_role_change_adds_profiler_user_when_sysmanager_present(monkeypatch):
	user = FakeUser("dave@example.com", ["System Manager"])
	install.on_user_role_change(user, method=None)
	assert "Profiler User" in user.added_roles


def test_on_user_role_change_skips_non_sysmanager(monkeypatch):
	user = FakeUser("eve@example.com", ["Sales User"])
	install.on_user_role_change(user, method=None)
	assert user.added_roles == []


def test_on_user_role_change_skips_already_has_profiler_user(monkeypatch):
	user = FakeUser("frank@example.com", ["System Manager", "Profiler User"])
	install.on_user_role_change(user, method=None)
	assert user.added_roles == []
