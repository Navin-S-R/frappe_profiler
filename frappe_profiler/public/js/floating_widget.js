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

	// LOUD diagnostic so we can SEE this script execute in the browser console.
	console.log("[frappe_profiler] floating_widget.js LOADED at", new Date().toISOString());

	// Only run inside Desk (not on web pages or guest sessions).
	if (typeof frappe === "undefined" || typeof frappe.session === "undefined") {
		console.warn("[frappe_profiler] no frappe global, script exiting");
		return;
	}
	if (frappe.session.user === "Guest") {
		console.warn("[frappe_profiler] user is Guest, script exiting");
		return;
	}
	console.log("[frappe_profiler] proceeding for user:", frappe.session.user);

	const POLL_INTERVAL_MS = 5000;
	const REQUIRED_ROLES = ["System Manager", "Profiler User", "Administrator"];

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
		// frappe.user_roles is set by Desk.set_globals(). On initial page
		// load our app_include_js script may run before set_globals, so
		// fall back to frappe.boot.user.roles which is populated inline
		// in the Desk HTML before any script executes.
		// Administrator is always allowed (matches api._require_profiler_user).
		if (frappe.session && frappe.session.user === "Administrator") {
			return true;
		}
		const roles =
			frappe.user_roles
			|| (frappe.boot && frappe.boot.user && frappe.boot.user.roles)
			|| [];
		return REQUIRED_ROLES.some((r) => roles.includes(r));
	}

	function init() {
		// Mount the widget UNCONDITIONALLY for any logged-in user. The
		// server-side `_require_profiler_user()` check in api.py is the
		// real permission gate; this client-side check is just a hint
		// and was causing race-condition bugs because `frappe.user_roles`
		// may not be populated when this script runs (see desk.js:329 —
		// set_globals runs after Desk class construction). We err on the
		// side of always mounting; users without the role will see a
		// permission error if they actually click Start.
		mountWidget();
		try { refreshStatus(); } catch (e) { /* noop */ }
		try { startPolling(); } catch (e) { /* noop */ }
		try { subscribeRealtime(); } catch (e) { /* noop */ }
		try { subscribeVisibility(); } catch (e) { /* noop */ }
		try { maybeShowOnboardingToast(); } catch (e) { /* noop */ }
	}

	function maybeShowOnboardingToast() {
		// v0.4.0: one-time dismissible toast on first Desk visit after install.
		// Server decides whether to show (via api.check_onboarding_seen),
		// which suppresses for experienced users automatically.
		frappe.call({
			method: "frappe_profiler.api.check_onboarding_seen",
			callback: function (r) {
				var data = (r && r.message) || {};
				if (data.seen) return;
				renderOnboardingToast();
			},
		});
	}

	function renderOnboardingToast() {
		// Avoid double-rendering if init runs twice
		if (document.getElementById("frappe-profiler-onboarding-toast")) return;
		var toast = document.createElement("div");
		toast.id = "frappe-profiler-onboarding-toast";
		toast.style.cssText = [
			"position: fixed",
			"top: 20px",
			"right: 20px",
			"z-index: 2147483647",
			"max-width: 360px",
			"padding: 14px 18px",
			"border-radius: 10px",
			"border: 1px solid #16a34a",
			"background: #f0fdf4",
			"color: #166534",
			"font-family: -apple-system, BlinkMacSystemFont, sans-serif",
			"font-size: 0.9rem",
			"line-height: 1.4",
			"box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18)",
			"cursor: default",
		].join("; ");
		toast.innerHTML = [
			'<div style="margin-bottom: 8px; font-weight: 700;">Frappe Profiler is installed</div>',
			'<div style="margin-bottom: 12px;">',
			"Click the <strong>Profiler</strong> pill in the bottom-right corner ",
			"to record your first slow flow.",
			"</div>",
			'<button id="frappe-profiler-onboarding-dismiss" ',
			'style="background: #16a34a; color: #fff; border: none; padding: 6px 14px; ',
			'border-radius: 6px; cursor: pointer; font-weight: 600;">Got it</button>',
		].join("");
		document.body.appendChild(toast);

		var dismissed = false;
		function dismiss() {
			if (dismissed) return;
			dismissed = true;
			try {
				toast.remove();
			} catch (e) { /* noop */ }
			try {
				frappe.call({ method: "frappe_profiler.api.mark_onboarding_seen" });
			} catch (e) { /* noop */ }
		}
		var btn = document.getElementById("frappe-profiler-onboarding-dismiss");
		if (btn) btn.addEventListener("click", dismiss);
		// Auto-dismiss after 15 seconds
		setTimeout(dismiss, 15000);
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
		console.log("[frappe_profiler] mountWidget() called");
		// Idempotent: don't double-mount on accidental re-init.
		const existing = document.getElementById("frappe-profiler-widget");
		if (existing) {
			console.log("[frappe_profiler] widget already in DOM, skipping mount");
			widget = existing;
			return;
		}
		widget = document.createElement("div");
		widget.id = "frappe-profiler-widget";
		widget.className = "fp-state-inactive";
		widget.innerHTML = `
			<span class="fp-dot"></span>
			<span class="fp-label">Profiler</span>
			<span class="fp-elapsed"></span>
		`;
		widget.addEventListener("click", onClick);
		// Inline styles with MAXIMUM visibility — bright red, large, top-right,
		// max z-index. This is intentionally loud so the user can SEE it.
		// Once we confirm it's visible we'll dial it back to the styled pill.
		widget.style.cssText = [
			"position: fixed",
			"right: 20px",
			"bottom: 20px",
			"z-index: 2147483647",  /* max int32 — above everything */
			"display: block !important",
			"visibility: visible !important",
			"opacity: 1 !important",
			"min-width: 180px",
			"padding: 16px 24px",
			"border-radius: 32px",
			"border: 3px solid #b91c1c",
			"background: #fef2f2",
			"box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3)",
			"font-family: -apple-system, BlinkMacSystemFont, sans-serif",
			"font-size: 1rem",
			"font-weight: 700",
			"color: #b91c1c",
			"cursor: pointer",
			"user-select: none",
			"pointer-events: auto",
		].join("; ");
		document.body.appendChild(widget);
		console.log("[frappe_profiler] widget appended to body, id=#frappe-profiler-widget");
		console.log("[frappe_profiler] widget element:", widget);
		console.log("[frappe_profiler] body has child count:", document.body.children.length);
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
					// v0.5.0: expose session UUID on the widget element so
					// profiler_frontend.js can tag its flush payloads without
					// a shared global.
					if (widget) {
						widget.setAttribute("data-session-uuid", data.session_uuid || "");
					}
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
					if (widget) {
						widget.removeAttribute("data-session-uuid");
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
					fieldname: "capture_python_tree",
					fieldtype: "Check",
					label: "Capture Python call tree (recommended)",
					default: 1,
					description:
						"Adds ~5–15% overhead but enables hot path detection, hook bottleneck findings, and redundant call detection. Disable for SQL-only capture (v0.2.0 behavior).",
				},
				{
					fieldname: "warning_html",
					fieldtype: "HTML",
					options: `
						<div style="background: #fffbeb; border: 1px solid #fbbf24; border-radius: 4px; padding: 10px 12px; margin-top: 10px; font-size: 0.85rem; color: #92400e;">
							<strong>Note:</strong> Recording adds 10–30% overhead per database query (1.5–2× wall clock with Python tree capture).
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
					args: {
						label: values.label || "",
						capture_python_tree: values.capture_python_tree ? 1 : 0,
					},
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
							// v0.5.0: expose session UUID for profiler_frontend.js.
							if (widget) {
								widget.setAttribute("data-session-uuid", data.session_uuid || "");
							}
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
		// Update currentState.display BEFORE the API call so refreshStatus()'s
		// transient-state guard (line ~241) kicks in — otherwise the 5s poll
		// can race the stop API and flip the widget back to "Recording".
		//
		// v0.5.0: also flush any buffered frontend metrics before the stop
		// API fires, so analyze can join them to recordings. Best-effort —
		// a failed flush never blocks stop.
		setDisplay("stopping", "Stopping…", "");
		currentState.display = "stopping";
		stopElapsedTimer();

		try {
			if (window.frappe_profiler_frontend && window.frappe_profiler_frontend.flush) {
				window.frappe_profiler_frontend.flush({ sync: false });
			}
		} catch (e) { /* noop — frontend module missing or flush failed */ }

		frappe.call({
			method: "frappe_profiler.api.stop",
			callback: (r) => {
				const data = (r && r.message) || {};
				if (data.ran_inline) {
					// v0.5.0: scheduler was disabled and analyze ran
					// synchronously inside the stop request. Skip the
					// "Analyzing…" state because the report is already
					// attached to the session.
					currentState.display = "ready";
					currentState.docname = data.docname;
					setDisplay("ready", "Report ready", "click to view");
					frappe.show_alert({
						message: __("Profiler report ready"),
						indicator: "blue",
					});
				} else {
					setDisplay("analyzing", "Analyzing…", "");
					currentState.display = "analyzing";
					frappe.show_alert({
						message: __("Profiler stopped — analyzing session…"),
						indicator: "orange",
					});
				}
			},
			error: () => {
				// Stop failed — recording is still active server-side.
				// Revert to recording so the user can retry instead of
				// being stranded on "Stopping…" forever.
				currentState.display = "recording";
				setDisplay("recording", "Recording", computeElapsed());
				startElapsedTimer();
				frappe.show_alert({
					message: __("Failed to stop profiler — try again"),
					indicator: "red",
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

	// Bootstrap: wait for document.body to exist, then mount the widget
	// UNCONDITIONALLY. We don't gate on frappe.user_roles or frappe.boot
	// because those may not be populated when our app_include_js script
	// runs (Desk.set_globals at desk.js:329 runs later, asynchronously).
	// The server-side _require_profiler_user check is the real gate.
	function bootstrap() {
		console.log("[frappe_profiler] bootstrap() called, document.body exists:", !!document.body);
		if (!document.body) {
			setTimeout(bootstrap, 50);
			return;
		}
		try {
			init();
		} catch (e) {
			console.error("[frappe_profiler] init failed:", e);
		}
	}

	console.log("[frappe_profiler] document.readyState:", document.readyState);
	if (document.readyState === "loading") {
		document.addEventListener("DOMContentLoaded", bootstrap);
	} else {
		bootstrap();
	}
})();
