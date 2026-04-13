// Copyright (c) 2026, Frappe Profiler contributors
// For license information, please see license.txt
//
// Profiler Session form script (Phase 5).
//
// Customizes the detail view to feel like a "report" rather than a raw
// data form. The customer-facing summary HTML is rendered prominently at
// the top, the analyzer findings are listed in a friendly format, and the
// two report files get prominent download buttons (raw is gated to admins).

frappe.ui.form.on("Profiler Session", {
	refresh(frm) {
		render_status_indicator(frm);
		render_download_buttons(frm);
		render_retry_button(frm);
		render_findings_summary(frm);
		render_analyzer_warnings(frm);
	},
});

function render_retry_button(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Failed") return;

	frm.add_custom_button(__("Retry Analyze"), () => {
		frappe.confirm(
			__("Re-run the analyze pipeline for this session?"),
			() => {
				frappe.call({
					method: "frappe_profiler.api.retry_analyze",
					args: { session_uuid: frm.doc.session_uuid },
					callback: (r) => {
						const data = r.message || {};
						if (data.retried) {
							frappe.show_alert({
								message: __("Analyze retry enqueued"),
								indicator: "orange",
							});
							setTimeout(() => frm.reload_doc(), 2000);
						} else {
							frappe.show_alert({
								message: data.reason || __("Retry skipped"),
								indicator: "gray",
							});
						}
					},
				});
			},
		);
	});
}

function render_analyzer_warnings(frm) {
	if (frm.is_new()) return;
	if (!frm.doc.analyzer_warnings) return;
	frm.set_intro(frm.doc.analyzer_warnings, "orange");
}

function render_status_indicator(frm) {
	if (frm.is_new()) return;
	const status = frm.doc.status || "Recording";
	const colors = {
		Recording: "green",
		Stopping: "orange",
		Analyzing: "orange",
		Ready: "blue",
		Failed: "red",
	};
	frm.page.set_indicator(status, colors[status] || "gray");
}

function render_download_buttons(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;

	if (frm.doc.safe_report_file) {
		frm.add_custom_button(
			__("Download Safe Report"),
			() => {
				window.open(frm.doc.safe_report_file, "_blank");
			},
			__("Reports"),
		);
	}

	// Raw report is gated: only System Manager and the recording user can
	// see the button. The actual file is also gated server-side via
	// Frappe's private file permission system.
	if (frm.doc.raw_report_file && user_can_see_raw(frm.doc)) {
		frm.add_custom_button(
			__("Download Raw Report (admin)"),
			() => {
				frappe.confirm(
					__(
						"The raw report contains literal SQL values, request headers, and stack traces. Do not share it externally. Continue?",
					),
					() => {
						window.open(frm.doc.raw_report_file, "_blank");
					},
				);
			},
			__("Reports"),
		);
	}
}

function user_can_see_raw(doc) {
	const roles = frappe.user_roles || [];
	if (roles.includes("System Manager")) return true;
	if (doc.user === frappe.session.user) return true;
	return false;
}

function render_findings_summary(frm) {
	if (frm.is_new()) return;
	if (!frm.doc.findings || frm.doc.findings.length === 0) return;

	// Add a small color-coded badge dashboard above the form fields.
	const high = frm.doc.findings.filter((f) => f.severity === "High").length;
	const medium = frm.doc.findings.filter((f) => f.severity === "Medium").length;
	const low = frm.doc.findings.filter((f) => f.severity === "Low").length;

	const badges = [];
	if (high) badges.push(`<span class="indicator-pill red">${high} High</span>`);
	if (medium) badges.push(`<span class="indicator-pill orange">${medium} Medium</span>`);
	if (low) badges.push(`<span class="indicator-pill blue">${low} Low</span>`);

	if (badges.length === 0) return;

	frm.dashboard.add_section(
		`<div style="padding: 8px 0; font-size: 0.9rem;">
			<strong>${__("Findings")}:</strong> ${badges.join(" ")}
		</div>`,
		__("Performance issues"),
	);
}

