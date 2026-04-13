// Frappe Profiler — Floating start/stop widget
//
// Injected into every Desk page via app_include_js in hooks.py.
// Renders a small floating button bottom-right that lets a user with the
// Profiler User (or System Manager) role start and stop a profiling session.
//
// State machine:
//
//   inactive  →  click → start dialog → start API → recording
//   recording →  click → stop API → stopping → analyzing → ready
//   ready     →  click → navigate to the Profiler Session detail view
//
// Polls /api/method/frappe_profiler.api.status every 5 seconds to reflect
// server-side state changes (auto-stop after 10 minutes, analyze completion).
// Also subscribes to the `profiler_session_ready` realtime event so the user
// is notified the moment their session report is ready.

(function () {
	"use strict";

	// Only run inside Desk (not on web pages or guest sessions).
	if (typeof frappe === "undefined" || typeof frappe.session === "undefined") {
		return;
	}
	if (frappe.session.user === "Guest") {
		return;
	}

	const POLL_INTERVAL_MS = 5000;
	const REQUIRED_ROLES = ["System Manager", "Profiler User"];

	let widget = null;
	let pollHandle = null;
	let elapsedHandle = null;
	let currentState = {
		active: false,
		session_uuid: null,
		docname: null,
		label: null,
		started_at: null,
		display: "inactive", // inactive | recording | stopping | analyzing | ready
	};

	function userHasRole() {
		const roles = frappe.user_roles || [];
		return REQUIRED_ROLES.some((r) => roles.includes(r));
	}

	function init() {
		if (!userHasRole()) {
			return; // user can't see the widget at all
		}
		mountWidget();
		refreshStatus();
		startPolling();
		subscribeRealtime();
		subscribeVisibility();
	}

	function startPolling() {
		if (pollHandle) return;
		pollHandle = setInterval(refreshStatus, POLL_INTERVAL_MS);
	}

	function stopPolling() {
		if (pollHandle) {
			clearInterval(pollHandle);
			pollHandle = null;
		}
	}

	function subscribeVisibility() {
		// Pause polling when the tab is hidden to avoid wasting API calls
		// in background tabs. Resume (and refresh immediately) when the
		// tab becomes visible again.
		document.addEventListener("visibilitychange", () => {
			if (document.hidden) {
				stopPolling();
			} else {
				refreshStatus();
				startPolling();
			}
		});
	}

	function mountWidget() {
		widget = document.createElement("div");
		widget.id = "frappe-profiler-widget";
		widget.className = "fp-state-inactive";
		widget.innerHTML = `
			<span class="fp-dot"></span>
			<span class="fp-label">Profiler</span>
			<span class="fp-elapsed"></span>
		`;
		widget.addEventListener("click", onClick);
		document.body.appendChild(widget);
		widget.style.display = "block";
	}

	function setDisplay(display, label, elapsed) {
		if (!widget) return;
		widget.classList.remove(
			"fp-state-inactive",
			"fp-state-recording",
			"fp-state-stopping",
			"fp-state-analyzing",
			"fp-state-ready",
		);
		widget.classList.add(`fp-state-${display}`);
		widget.querySelector(".fp-label").textContent = label;
		widget.querySelector(".fp-elapsed").textContent = elapsed || "";
	}

	function refreshStatus() {
		// If we're locally in a transient state (stopping/analyzing/ready),
		// don't override it with status() — server may not have reflected
		// our local action yet.
		if (currentState.display === "stopping" || currentState.display === "analyzing") {
			return;
		}
		frappe.call({
			method: "frappe_profiler.api.status",
			callback: (r) => {
				const data = r.message || {};
				if (data.active) {
					currentState.active = true;
					currentState.session_uuid = data.session_uuid;
					currentState.docname = data.docname;
					currentState.label = data.label;
					currentState.started_at = data.started_at;
					currentState.display = "recording";
					startElapsedTimer();
					setDisplay("recording", "Recording", computeElapsed());
				} else {
					if (currentState.display === "recording") {
						// We thought we were recording but server says no — auto-stop
						// fired or someone called stop from elsewhere. Reset.
						currentState.display = "inactive";
						currentState.active = false;
						stopElapsedTimer();
					}
					if (currentState.display !== "ready") {
						setDisplay("inactive", "Profiler", "");
					}
				}
			},
		});
	}

	function onClick() {
		if (currentState.display === "inactive") {
			openStartDialog();
		} else if (currentState.display === "recording") {
			confirmAndStop();
		} else if (currentState.display === "ready") {
			// Navigate to the session detail view
			if (currentState.docname) {
				frappe.set_route("Form", "Profiler Session", currentState.docname);
				currentState.display = "inactive";
				setDisplay("inactive", "Profiler", "");
			}
		}
		// stopping/analyzing: clicks are no-op until the state resolves
	}

	function openStartDialog() {
		const d = new frappe.ui.Dialog({
			title: "Start profiling session",
			fields: [
				{
					fieldname: "label",
					fieldtype: "Data",
					label: "Session label",
					reqd: 1,
					description:
						"Give this session a name you'll recognize later — e.g. 'Sales Invoice flow with 50 items'.",
				},
				{
					fieldname: "warning_html",
					fieldtype: "HTML",
					options: `
						<div style="background: #fffbeb; border: 1px solid #fbbf24; border-radius: 4px; padding: 10px 12px; margin-top: 10px; font-size: 0.85rem; color: #92400e;">
							<strong>Note:</strong> Recording adds 10–30% overhead per database query.
							Only your traffic will be captured — other users on this site are not affected.
							The session auto-stops after 10 minutes.
						</div>
					`,
				},
			],
			primary_action_label: "Start",
			primary_action: (values) => {
				d.hide();
				frappe.call({
					method: "frappe_profiler.api.start",
					args: { label: values.label || "" },
					callback: (r) => {
						const data = r.message || {};
						if (data.session_uuid) {
							currentState.active = true;
							currentState.session_uuid = data.session_uuid;
							currentState.docname = data.docname;
							currentState.label = data.title;
							currentState.started_at = data.started_at;
							currentState.display = "recording";
							startElapsedTimer();
							setDisplay("recording", "Recording", "0:00");
							frappe.show_alert({
								message: __("Profiler started: {0}", [data.title]),
								indicator: "green",
							});
						}
					},
				});
			},
		});
		d.show();
	}

	function confirmAndStop() {
		// No confirmation modal — keep it one-click. Just fire stop().
		setDisplay("stopping", "Stopping…", "");
		stopElapsedTimer();
		frappe.call({
			method: "frappe_profiler.api.stop",
			callback: () => {
				setDisplay("analyzing", "Analyzing…", "");
				currentState.display = "analyzing";
				frappe.show_alert({
					message: __("Profiler stopped — analyzing session…"),
					indicator: "orange",
				});
			},
		});
	}

	function subscribeRealtime() {
		if (!frappe.realtime || !frappe.realtime.on) {
			return;
		}
		frappe.realtime.on("profiler_session_ready", (data) => {
			if (!data || !data.session_uuid) return;
			// Match against our current session if we have one in flight
			if (currentState.session_uuid && data.session_uuid !== currentState.session_uuid) {
				return;
			}
			currentState.display = "ready";
			currentState.docname = data.docname;
			setDisplay("ready", "Report ready", "click to view");
			frappe.show_alert({
				message: __("Profiler report ready"),
				indicator: "blue",
			});
		});

		// Round 2 fix #17: show live progress during analyze
		frappe.realtime.on("profiler_progress", (data) => {
			if (!data || !data.session_uuid) return;
			if (currentState.session_uuid && data.session_uuid !== currentState.session_uuid) {
				return;
			}
			if (currentState.display !== "analyzing") return;
			const pct = typeof data.percent === "number" ? Math.round(data.percent) : 0;
			setDisplay("analyzing", `Analyzing ${pct}%`, data.description || "");
		});
	}

	function computeElapsed() {
		if (!currentState.started_at) return "";
		const start = new Date(currentState.started_at).getTime();
		if (isNaN(start)) return "";
		const seconds = Math.floor((Date.now() - start) / 1000);
		const m = Math.floor(seconds / 60);
		const s = seconds % 60;
		return `${m}:${s.toString().padStart(2, "0")}`;
	}

	function startElapsedTimer() {
		stopElapsedTimer();
		elapsedHandle = setInterval(() => {
			if (currentState.display === "recording") {
				const el = widget && widget.querySelector(".fp-elapsed");
				if (el) el.textContent = computeElapsed();
			}
		}, 1000);
	}

	function stopElapsedTimer() {
		if (elapsedHandle) {
			clearInterval(elapsedHandle);
			elapsedHandle = null;
		}
	}

	// Wait until Frappe Desk has finished bootstrapping, then mount.
	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", () => {
			frappe.after_ajax(() => init());
		});
	} else {
		frappe.after_ajax(() => init());
	}
})();
