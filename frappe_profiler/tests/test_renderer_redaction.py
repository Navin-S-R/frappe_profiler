# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Unit tests for the sensitive-field redactor (Round 2 fix #1).

The redactor is defense-in-depth for the raw report — even though raw
reports are permission-gated, once downloaded to disk they can leak
via backups, email, screen shares. Redacting known-sensitive fields
means those leaks don't expose credentials.
"""

from frappe_profiler.renderer import _REDACTED, redact_sensitive


def test_redacts_password_field():
	d = {"username": "alice", "password": "hunter2"}
	result = redact_sensitive(d)
	assert result["username"] == "alice"
	assert result["password"] == _REDACTED


def test_redacts_common_auth_fields():
	cases = {
		"Authorization": "Bearer abc123",
		"cookie": "sid=xyz",
		"Set-Cookie": "session=abc",
		"X-Frappe-CSRF-Token": "tok_123",
		"api_key": "secret",
		"api-key": "secret",
		"access_token": "token",
		"refresh_token": "refresh",
		"pwd": "password123",
	}
	for key, val in cases.items():
		result = redact_sensitive({key: val})
		assert result[key] == _REDACTED, f"Failed to redact {key}"


def test_redacts_nested_dicts():
	d = {
		"outer": "value",
		"auth": {
			"password": "secret",
			"username": "alice",
		},
	}
	result = redact_sensitive(d)
	assert result["outer"] == "value"
	assert result["auth"]["username"] == "alice"
	assert result["auth"]["password"] == _REDACTED


def test_redacts_case_insensitively():
	d = {"PASSWORD": "x", "Password": "y", "pAsSwOrD": "z"}
	result = redact_sensitive(d)
	for k in d:
		assert result[k] == _REDACTED


def test_preserves_non_sensitive_fields():
	d = {"doctype": "Sales Invoice", "customer": "Acme", "grand_total": 1000}
	result = redact_sensitive(d)
	assert result == d


def test_handles_none():
	assert redact_sensitive(None) is None


def test_handles_non_dict():
	# If the thing isn't a dict, return as-is
	assert redact_sensitive("not a dict") == "not a dict"
	assert redact_sensitive(123) == 123
	assert redact_sensitive([1, 2, 3]) == [1, 2, 3]


def test_redacts_otp_and_verification_codes():
	d = {
		"otp": "123456",
		"verification_code": "abcdef",
		"verification-code": "xyzzyx",
	}
	result = redact_sensitive(d)
	assert result["otp"] == _REDACTED
	assert result["verification_code"] == _REDACTED
	assert result["verification-code"] == _REDACTED


def test_redacts_payment_card_fields():
	d = {"card_number": "4111111111111111", "cvv": "123", "card-number": "4111"}
	result = redact_sensitive(d)
	assert result["card_number"] == _REDACTED
	assert result["cvv"] == _REDACTED
	assert result["card-number"] == _REDACTED


def test_redacts_personal_identifiers():
	d = {"ssn": "123-45-6789", "aadhar": "1234567890", "pan_number": "ABCDE1234F"}
	result = redact_sensitive(d)
	for k in d:
		assert result[k] == _REDACTED


def test_recursion_depth_limit():
	"""Deeply nested structures should not cause infinite recursion."""
	# Build a 10-level deep dict
	inner = {"password": "leak"}
	for _ in range(10):
		inner = {"nested": inner}
	result = redact_sensitive(inner)
	# After depth 4, nested values are returned as-is.
	# We don't strictly care what the leaf looks like, just that the
	# function returns without blowing the stack.
	assert result is not None


def test_doesnt_mutate_original():
	d = {"password": "original"}
	result = redact_sensitive(d)
	assert d["password"] == "original"  # original unchanged
	assert result["password"] == _REDACTED  # copy has redacted value
