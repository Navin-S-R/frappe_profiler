# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Lazy PDF generation for the safe report.

Generated on first request via frappe.utils.pdf.get_pdf (wkhtmltopdf),
cached to a private File attachment on the Profiler Session. Subsequent
requests serve from cache. Analyze pipeline is never touched — keeping
PDF generation outside the analyze budget.
"""

import frappe


def get_or_generate_pdf(session_uuid: str) -> str:
	"""Return the file_url of the cached PDF, generating on first call.

	Permission check is the caller's responsibility (api.download_pdf).
	Raises on generation failure; does NOT cache failed generations.
	"""
	doc = _load_session(session_uuid)
	if doc.safe_report_pdf_file:
		return doc.safe_report_pdf_file
	html = _load_safe_html(doc)
	pdf_bytes = _html_to_pdf(html)
	url = _save_pdf_attachment(doc.name, session_uuid, pdf_bytes)
	frappe.db.set_value("Profiler Session", doc.name, "safe_report_pdf_file", url)
	frappe.db.commit()
	return url


def clear_cached_pdf(session_uuid: str) -> None:
	"""Clear the cached PDF for a session. Called from retry_analyze.

	Best-effort: if the File row is gone or the field isn't set, no-op.
	"""
	docname = frappe.db.get_value(
		"Profiler Session", {"session_uuid": session_uuid}, "name",
	)
	if not docname:
		return
	current_url = frappe.db.get_value(
		"Profiler Session", docname, "safe_report_pdf_file",
	)
	if not current_url:
		return
	try:
		file_row = frappe.get_doc("File", {"file_url": current_url})
		frappe.delete_doc("File", file_row.name, force=True, ignore_permissions=True)
	except Exception:
		pass
	frappe.db.set_value("Profiler Session", docname, "safe_report_pdf_file", None)
	frappe.db.commit()


def _load_session(session_uuid: str):
	docname = frappe.db.get_value(
		"Profiler Session", {"session_uuid": session_uuid}, "name",
	)
	if not docname:
		frappe.throw(f"No Profiler Session found for uuid {session_uuid}")
	return frappe.get_doc("Profiler Session", docname)


def _load_safe_html(doc) -> str:
	if not doc.safe_report_file:
		frappe.throw("This session has no safe report to convert.")
	file_row = frappe.get_doc("File", {"file_url": doc.safe_report_file})
	return file_row.get_content().decode("utf-8")


def _html_to_pdf(html: str) -> bytes:
	"""Run HTML through wkhtmltopdf via frappe.utils.pdf.get_pdf.

	Options tuned for the safe report's CSS — A4, conservative margins,
	UTF-8, print-media-type so any @media print rules in the report are
	honored (e.g. the SVG donut fallback).
	"""
	import frappe.utils.pdf

	options = {
		"page-size": "A4",
		"margin-top": "15mm",
		"margin-bottom": "15mm",
		"margin-left": "12mm",
		"margin-right": "12mm",
		"encoding": "UTF-8",
		"print-media-type": "",
		"enable-local-file-access": "",
	}
	return frappe.utils.pdf.get_pdf(html, options)


def _save_pdf_attachment(docname: str, session_uuid: str, pdf_bytes: bytes) -> str:
	"""Insert a private File attached to the Profiler Session.

	Returns the file_url. Raises on failure (caller decides not to cache).
	"""
	filename = f"profiler_safe_report_{session_uuid}.pdf"
	file_doc = frappe.get_doc({
		"doctype": "File",
		"file_name": filename,
		"attached_to_doctype": "Profiler Session",
		"attached_to_name": docname,
		"attached_to_field": "safe_report_pdf_file",
		"is_private": 1,
		"content": pdf_bytes,
	})
	file_doc.insert(ignore_permissions=True)
	return file_doc.file_url
