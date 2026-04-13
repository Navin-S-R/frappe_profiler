app_name = "frappe_profiler"
app_title = "Frappe Profiler"
app_publisher = ""
app_description = "Flow-aware performance profiler for Frappe and ERPNext"
app_email = ""
app_license = "mit"
app_logo_url = "/assets/frappe/images/framework.png"  # placeholder; prevents sidebar 404

# Apps
# ------------------

required_apps = ["frappe"]

# Desk assets (Phase 5)
# ---------------------
# The floating start/stop widget is injected into every Desk page. The widget
# JS itself checks for the System Manager / Profiler User role before showing,
# so users without permission see nothing.

# NOTE: the ?v=... query string is a cache-buster. Frappe's dev server
# sends Cache-Control: max-age=43200 (12h) on static assets, so without
# a versioned URL the browser never re-fetches updated JS. Bump this
# whenever you change floating_widget.js so users get the new code on
# their next page load without needing "Empty Cache and Hard Reload".
app_include_js = "/assets/frappe_profiler/js/floating_widget.js?v=0.3.0-1"
app_include_css = "/assets/frappe_profiler/css/floating_widget.css?v=0.3.0-1"

# Installation
# ------------

after_install = "frappe_profiler.install.after_install"
before_uninstall = "frappe_profiler.install.before_uninstall"

# Request lifecycle (Phase 1)
# ---------------------------
# These hooks run AFTER frappe's own recorder hooks, so by the time
# `before_request` runs, frappe.recorder.record() has already been called
# (and is a no-op without the global flag). Our hook then decides per-user
# whether to force-activate the recorder for this request.
#
# `after_request` runs after frappe.recorder.dump(), so the recording is
# already in Redis by the time we register its UUID with our session.

before_request = ["frappe_profiler.hooks_callbacks.before_request"]
after_request = ["frappe_profiler.hooks_callbacks.after_request"]

# Background job lifecycle (Phase 2)
# ----------------------------------
# These mirror the request hooks. The frappe.enqueue monkey-patch in
# frappe_profiler/__init__.py injects `_profiler_session_id` into job
# kwargs at enqueue time, and `before_job` reads (and pops) it to decide
# whether to activate recording for this job.
#
# This is how a customer's "save Sales Invoice → submit" flow captures
# both the synchronous HTTP requests AND the background jobs that the
# submit triggers (GL postings, stock updates, etc.) under one session.

before_job = ["frappe_profiler.hooks_callbacks.before_job"]
after_job = ["frappe_profiler.hooks_callbacks.after_job"]

# Janitor (Phase 6)
# -----------------
# Sweep stale Recording sessions (started but never explicitly stopped)
# and stuck Analyzing sessions (worker crashed mid-analyze) every 5 minutes.

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"frappe_profiler.janitor.sweep_stale_sessions",
		],
	},
	"daily": [
		"frappe_profiler.janitor.sweep_old_sessions",
	],
}

# File permission gate (Phase 6)
# ------------------------------
# Server-side double-check that the raw profiler report can only be
# downloaded by System Manager + the recording user. The UI hides the
# download button from non-admins, but this gate also blocks direct URL
# access in case someone guesses the file name.

has_permission = {
	"File": "frappe_profiler.permissions.file_has_permission",
}
