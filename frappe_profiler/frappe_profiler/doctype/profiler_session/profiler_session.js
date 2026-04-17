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
		render_regenerate_report_button(frm);
		render_findings_summary(frm);
		render_analyzer_warnings(frm);
		render_baseline_buttons(frm);
		render_no_baseline_banner(frm);
	},
});

// v0.4.0: Pin / Unpin / Compare baseline buttons
function render_baseline_buttons(frm) {
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;

	if (frm.doc.is_baseline) {
		frm.add_custom_button(__("Unpin baseline"), function () {
			frappe.call({
				method: "frappe_profiler.api.unpin_baseline",
				args: { session_uuid: frm.doc.session_uuid },
				callback: function () {
					frappe.show_alert({
						message: __("Baseline unpinned"),
						indicator: "blue",
					});
					frm.reload_doc();
				},
			});
		});
	} else {
		frm.add_custom_button(__("Pin as baseline"), function () {
			frappe.call({
				method: "frappe_profiler.api.pin_baseline",
				args: { session_uuid: frm.doc.session_uuid },
				callback: function () {
					frappe.show_alert({
						message: __("Pinned as baseline"),
						indicator: "green",
					});
					frm.reload_doc();
				},
			});
		});
	}

	frm.add_custom_button(__("Compare with..."), function () {
		var d = new frappe.ui.Dialog({
			title: __("Compare with another Profiler Session"),
			fields: [
				{
					fieldname: "target",
					fieldtype: "Link",
					label: "Profiler Session",
					options: "Profiler Session",
					reqd: 1,
					get_query: function () {
						return { filters: { status: "Ready" } };
					},
				},
			],
			primary_action_label: __("Compare"),
			primary_action: function (values) {
				d.hide();
				frappe.call({
					method: "frappe_profiler.api.set_comparison",
					args: {
						session_uuid: frm.doc.session_uuid,
						compared_to: values.target,
					},
					callback: function () {
						frappe.show_alert({
							message: __("Comparison set; reloading report"),
							indicator: "green",
						});
						frm.reload_doc();
					},
				});
			},
		});
		d.show();
	});
}

function render_no_baseline_banner(frm) {
	// Only show if Ready, no baseline set, and not itself a baseline
	if (frm.is_new()) return;
	if (frm.doc.status !== "Ready") return;
	if (frm.doc.compared_to_session) return;
	if (frm.doc.is_baseline) return;
	// Don't override an existing analyzer_warnings intro
	if (frm.doc.analyzer_warnings) return;
	frm.set_intro(
		__("No baseline set. Pin this session to compare future runs, or click 'Compare with...' to pick one now."),
		"blue",
	);
}

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

// v0.5.3: Regenerate Reports button. Re-renders the safe + raw HTML
// from the stored session data without re-running the analyzer. Shown
// on Ready / Failed sessions. Typical use: the report template was
// upgraded (e.g. noise filters or exec summary added) and the admin
// wants existing sessions to reflect the new layout — or the original
// render crashed and a fix was deployed.
function render_regenerate_report_button(frm) {
	if (frm.is_new()) return;
	// Only makes sense once the session has content to render.
	if (!["Ready", "Failed"].includes(frm.doc.status)) return;

	frm.add_custom_button(__("Regenerate Reports"), () => {
		frappe.confirm(
			__(
				"Re-render the safe and raw HTML reports from stored "
				+ "session data? This does NOT re-run the analyzer — it "
				+ "only re-invokes the renderer, applying the current "
				+ "report template to this session. Takes a few seconds."
			),
			() => {
				frappe.call({
					method: "frappe_profiler.api.regenerate_reports",
					args: { session_uuid: frm.doc.session_uuid },
					freeze: true,
					freeze_message: __("Regenerating reports..."),
					callback: (r) => {
						const data = (r && r.message) || {};
						if (data.regenerated) {
							const rec = data.recordings_available;
							const total = data.actions_total;
							let msg = __("Reports regenerated.");
							if (total && rec < total) {
								msg += " "
									+ __(
										"Only {0} of {1} recordings were "
										+ "available (others expired from "
										+ "Redis); per-query drill-down "
										+ "may be partial.",
										[rec, total],
									);
							}
							frappe.show_alert({
								message: msg,
								indicator: "green",
							});
							setTimeout(() => frm.reload_doc(), 1500);
						} else {
							frappe.show_alert({
								message: __("Regeneration skipped"),
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

		// v0.4.0: PDF download via lazy api.download_pdf
		frm.add_custom_button(
			__("Download Safe Report (PDF)"),
			() => {
				frappe.show_alert({
					message: __("Generating PDF..."),
					indicator: "blue",
				});
				frappe.call({
					method: "frappe_profiler.api.download_pdf",
					args: { session_uuid: frm.doc.session_uuid },
					callback: (r) => {
						const data = (r && r.message) || {};
						if (data.file_url) {
							window.open(data.file_url, "_blank");
						} else {
							frappe.show_alert({
								message: __("PDF generation failed; download the HTML version instead"),
								indicator: "red",
							});
						}
					},
					error: () => {
						frappe.show_alert({
							message: __("PDF generation failed"),
							indicator: "red",
						});
					},
				});
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

