# Changelog

All notable changes to the Frappe Profiler app.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project follows [SemVer](https://semver.org/) (pre-1.0, so minor
versions may contain breaking changes — see migration notes below).

---

## [0.5.0] — 2026-04-14

The "Is it my code or my server?" release. Closes two competitive gaps
with other profilers: there's no way to tell *code-slow* from
*server-slow*, and there's no way to tell *backend-slow* from
*network-slow* or *page-paint-slow*. v0.5.0 captures the server-side
resource state at every action boundary and the browser-side transport
timing for every XHR, joins them to the matching recording, and
renders them as two new report panels alongside the existing findings.
Also bundles a scheduler-disabled safety fix that affected v0.4.0 and
earlier.

### Added

- **Server infrastructure capture** — new `infra_capture.py` module
  snapshots CPU, worker RSS, system memory, swap, load average, MariaDB
  thread counts and slow-query counter, Redis ops/sec, and RQ queue
  depths at the start and end of every recorded action. Balanced tier
  (14 metrics, ~0.8ms per snapshot). Runs in-line on the request path
  — no background sampler thread. Every source is wrapped in its own
  try/except so a broken source degrades to `None` rather than
  breaking recording.
- **`infra_pressure` analyzer** — emits four new finding types:
  - **Resource Contention** — sustained system CPU > 85% across ≥2
    actions. Severity escalates to High if any sample hits 95% or if
    >50% of actions are affected. Distinguishes "your own flow is
    CPU-bound" from "something else on the box is hogging CPU."
  - **Memory Pressure** — worker RSS grew by > 200MB during the
    session OR swap > 100MB during any action. High severity if
    delta > 500MB or swap is active.
  - **DB Pool Saturation** — `threads_running / threads_connected`
    > 0.9 across ≥2 actions. Points at gunicorn worker count vs.
    MariaDB `max_connections` mismatch.
  - **Background Queue Backlog** — any RQ queue (`default`, `short`,
    `long`) peaked above 50 during the session. Signals that the
    flow enqueued work that's waiting behind other jobs.
- **Browser-side metrics shim** — new `profiler_frontend.js` wraps
  `window.fetch` and `XMLHttpRequest.prototype.open/send` to capture
  per-XHR timings (URL, method, duration, status, response size)
  whenever the server returns an `X-Profiler-Recording-Id` response
  header. Uses `PerformanceObserver` with `buffered: true` to capture
  Web Vitals (FCP, LCP, CLS, navigation timing). Wraps WHATWG
  primitives instead of application-level APIs so instrumentation
  survives future Frappe upgrades — jQuery `$.ajax` is caught via XHR
  automatically. This is the approach every production APM library
  uses (OpenTelemetry JS, Sentry Browser, Datadog RUM).
- **`X-Profiler-Recording-Id` correlation header** — `after_request`
  injects the recording UUID as a custom response header AND appends
  it to `Access-Control-Expose-Headers` so browsers actually surface
  it to JavaScript. The expose header is load-bearing: without it,
  `xhr.getResponseHeader("X-Profiler-Recording-Id")` returns `null`
  even for same-origin requests.
- **`frappe_profiler.api.submit_frontend_metrics` endpoint** —
  receives batched XHR + Web Vitals payloads from the browser shim
  at stop time (via `frappe.call`) or at `beforeunload` (via
  `navigator.sendBeacon`). Accepts a JSON string payload because
  sendBeacon sends raw `Blob`, not form-encoded. Validates session
  ownership so a cross-user write is rejected. Soft caps (1000 XHRs,
  200 vitals) with tail-preferring truncation so end-of-flow data
  wins on overflow. Idempotent — multiple submits merge into one
  Redis blob.
- **`frontend_timings` analyzer** — joins XHR timings to Profiler
  Actions by recording UUID, dedupes multi-fire LCP per page (last
  value before next navigation, matching the Web Vitals library
  convention), and emits three finding types:
  - **Slow Frontend Render** — LCP > 2500ms → Medium, > 4000ms → High.
  - **Network Overhead** — `xhr_duration - backend_duration > 500ms`
    AND `> backend * 1.5`. The multiplier is the key insight: a 500ms
    delta is disproportionate on a 1ms backend call but proportional
    on a 5s one. Only the disproportionate case flags.
  - **Heavy Response** — single response > 500KB (Low, informational).
- **Server Resource panel in the report template** — renders the
  `infra_timeline` + `infra_summary` aggregates from `infra_pressure`
  as stat cards (CPU avg/peak, RSS delta, load peak, swap peak) and
  a per-action timeline table (CPU, RSS, load, DB pool ratio, RQ
  queue depths).
- **Frontend panel in the report template** — renders the
  `frontend_xhr_matched`, `frontend_vitals_by_page`, `frontend_orphans`,
  and `frontend_summary` aggregates from `frontend_timings`. Per-action
  XHR table with backend/browser/network-delta/status/size columns,
  Web Vitals table by page (FCP, LCP, CLS, TTFB, DCL), and a
  collapsed orphans section for diagnostic use (hidden entirely in
  Safe mode).
- **`_safe_url` helper in `renderer.py`** — strips docname segments
  from `/app/<doctype>/<name>/...` paths and redacts PII query string
  keys (`source_name`, `filters`, `name`, `doctype`, `reference_name`,
  `parent`, `customer`, `supplier`) to `?`. Method URLs
  (`/api/method/frappe.client.save`) pass through — method names are
  code identifiers, not PII. Applied to every URL rendered in the
  Frontend panel when `mode == "safe"`. Mirrors SQL normalization:
  full text stored, redacted form emitted.
- **Seven new `Profiler Finding.finding_type` Select options**:
  Resource Contention, Memory Pressure, DB Pool Saturation,
  Background Queue Backlog, Slow Frontend Render, Network Overhead,
  Heavy Response.
- **Upgraded `notes` field on Profiler Session** from plain `Text` to
  `Text Editor` (rich HTML), relabeled as **"Steps to Reproduce /
  Notes"**. Rendered at the top of the report above findings so any
  reviewer reads the reproduction context before the technical
  detail. Also added to the floating widget's Start dialog as an
  optional Text Editor field so users can document "what I'm about
  to do" at the moment they start. The existing `notes` field already
  covered this use case (its description literally said "reproduction
  steps"); v0.5.0 upgrades it in place rather than adding a duplicate
  `steps_to_reproduce` field, avoiding DB schema bloat and data
  migration.
- **`v5_aggregate_json` field on Profiler Session** — hidden Long
  Text field that serializes the v0.5.0 `infra_pressure` and
  `frontend_timings` aggregates as a single JSON dict. Persisted by
  `_persist` alongside the existing `top_queries_json` and
  `table_breakdown_json`, read by `renderer.render()`.
- **`data-session-uuid` attribute on the floating widget DOM element**
  — set when a session is active, cleared when it ends. Read by
  `profiler_frontend.js` to tag its flush payloads, keeping the two
  modules loosely coupled without a shared global.
- **Test coverage:** 65+ new tests across:
  - `test_scheduler_inline_fallback.py` — 5 tests
  - `test_infra_capture.py` — 6 tests (snapshot, diff, force_stop,
    psutil defensive behavior, getloadavg fallback, idempotency)
  - `test_correlation_header.py` — 7 tests
  - `test_submit_frontend_metrics.py` — 7 tests
  - `test_infra_pressure_analyzer.py` — 10 tests
  - `test_frontend_timings_analyzer.py` — 11 tests
  - `test_safe_url.py` — 9 tests
  - `test_steps_to_reproduce.py` — 5 tests
  - `test_v5_panels_render.py` — 5 end-to-end panel render tests
  - `test_end_to_end_metrics.py` — 2 full-chain integration tests
  - Two new fixture files (`infra_pressure_session.json`,
    `frontend_metrics_session.json`)
  - Full suite: **277 tests passing**, zero regressions against v0.4.0.

### Changed

- **Scheduler-aware `_enqueue_analyze` fallback (also fixes a latent
  v0.4.x bug).** When `bench disable-scheduler` is in effect —
  common on dev, demo, and Frappe Cloud trial instances — no
  `bench worker` process consumes the RQ queue on many deployments,
  so an enqueued analyze job would sit forever and the session would
  hang in the **"Stopping"** state. v0.5.0 detects
  `is_scheduler_disabled()` and passes `now=True` to `frappe.enqueue`
  so analyze runs synchronously inside the stop request. A new
  `profiler_inline_analyze_limit` site config (default 50) hard-caps
  the recording count for inline analyze — sessions above the cap
  are marked Failed with an actionable error directing the user to
  `bench enable-scheduler` and the **Retry Analyze** button. Prevents
  gunicorn's 120s worker timeout from killing a 200-recording inline
  analyze mid-flight.
- **`api.stop()` response now includes `ran_inline: bool`** — the
  floating widget reads this to decide whether to transition through
  the "Analyzing…" state or jump straight to "Ready" (when analyze
  already completed inline, the report is attached by the time stop
  returns).
- **`api.start()` accepts an optional `notes` kwarg** (default `""`)
  and persists it into the new Profiler Session row. Backward
  compatible with callers that don't pass notes.
- **`floating_widget.js:confirmAndStop`** now calls
  `window.frappe_profiler_frontend.flush()` before firing the stop
  API so buffered browser metrics land in Redis before analyze runs.
  Best-effort — a failed flush never blocks stop.
- **`_stop_session` signature changed** from `(user, session_uuid) -> str | None`
  to `(user, session_uuid) -> tuple[str | None, bool]`. Callers
  that discarded the return value still work; the only other
  internal caller (`start()`'s idempotent restart path) also
  discards it.
- **`before_request` / `before_job` / `after_request` / `after_job`
  hooks** now take an infra snapshot into
  `frappe.local.profiler_infra_start` at the start of the action and
  diff it against an end snapshot in the `finally` block, writing
  the result under `profiler:infra:<recording_uuid>` with the same
  TTL as other session keys. All work happens inside the existing
  try/except blocks — a broken snapshot logs and falls through but
  never breaks the customer's request.
- **`capture._force_stop_inflight_capture`** is now accompanied by
  `infra_capture._force_stop_inflight` in both `api.start()` and
  `api._stop_session()` so leaked state from a previous session on
  the same worker can't poison the next one.
- **`session.delete_session_state`** now also removes
  `profiler:frontend:<session_uuid>`. Per-recording
  `profiler:infra:<recording_uuid>` keys are cleaned up alongside
  `RECORDER_REQUEST_HASH` entries when analyze walks the recording
  list.
- **`hooks.py:app_include_js`** converted from a string to a list
  and now includes `profiler_frontend.js` alongside `floating_widget.js`.
  Both entries carry the version cache-buster.
- **`analyze.run`** now loads `profiler:frontend:<session_uuid>`
  into `context.frontend_data` and attaches per-recording infra
  dicts as `rec["infra"]` before the analyzer loop runs, so
  `infra_pressure` and `frontend_timings` can read them inline
  without a Redis hop inside each analyzer. Also appends the two
  new analyzers to `_BUILTIN_ANALYZERS`. Order is irrelevant — both
  are independent of every existing analyzer.

### Fixed

- **Widget stop-button race condition** (backported to v0.4.0
  `handoff-ux` branch as `e620a57`). `confirmAndStop()` set the DOM
  display to "Stopping…" but left `currentState.display` as
  `"recording"`, so the 5-second polling guard in `refreshStatus()`
  — which checks `currentState.display` — never tripped. If polling
  raced the stop API and the status call returned `active=true`
  (because the server hadn't processed stop yet), the widget would
  flip back to "Recording" mid-stop. Also added an `error` callback
  on the stop `frappe.call` so a failed stop reverts to "Recording"
  with a red toast instead of stranding the user on "Stopping…"
  forever.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply `frappe_profiler.patches.v0_5_0.add_metrics_finding_types`
   which reloads the Profiler Finding DocType so the seven new
   `finding_type` Select options become available. Idempotent.
2. Apply `frappe_profiler.patches.v0_5_0.upgrade_notes_to_text_editor`
   which reloads the Profiler Session DocType to pick up the
   upgraded `notes` field (now Text Editor) and the new
   `v5_aggregate_json` Long Text field. Existing `notes` values
   carry over unchanged because plain-text content is valid Text
   Editor input — no data migration needed, only the metadata
   changes.

No breaking API changes:

- `api.start` accepts a new optional `notes` kwarg with a
  backward-compatible default (`""`).
- `api.stop` adds a new `ran_inline` key to its return dict;
  existing consumers that ignore unknown keys work unchanged.
- `_stop_session` signature changed internally but is not part of
  the public API surface.

Existing v0.4.0 sessions render unchanged in v0.5.0 because
`v5_aggregate_json` is NULL for pre-v0.5.0 rows and the renderer
skips the Server Resource and Frontend panels when the aggregates
are empty.

**Known v0.5.0 operational notes:**

- On sites where the scheduler is disabled, the stop API will block
  for the full analyze duration (typically 2–20s). Widget transitions
  through "Stopping…" → "Ready" directly (no intermediate "Analyzing…"
  state) because the report is already attached when stop returns.
- If `navigator.sendBeacon` is rejected by Frappe v16's CSRF
  middleware (unverified), the `beforeunload` flush path will fail
  silently and the user loses their frontend metrics for that
  session. The server-side recording is unaffected. Mitigation: the
  stop-time flush path uses `frappe.call` (standard cookie auth)
  and is the primary delivery path. The beacon is a best-effort
  hedge for tab-close scenarios.
- `navigator.sendBeacon` calls made from *other* apps are not
  captured (beacons can't return response headers, so our shim
  can't see the `X-Profiler-Recording-Id`).

To disable the new pyinstrument + infra capture for a specific
session, uncheck **"Capture Python call tree"** in the start
dialog as before — the v0.3.0 flag continues to gate the heaviest
capture paths. Infra capture is unconditional because it costs
~0.8ms per action and runs only while the user's session is active.

---

## [0.4.0] — 2026-04-14

The "Make it usable" release. Sands down the rough edges between
"customer installs the app" and "customer hands a useful report to
their software company". The product thesis is unchanged; the
handoff workflow is faster and the report is more actionable.

### Added

- **Session comparison / baseline pinning** — pin any Ready session as
  the baseline for its label. Subsequent recordings with the same label
  auto-render three comparison sections in the safe + raw reports:
  session-level delta, per-action diff, and finding-level diff
  (fixed / new / unchanged buckets). Lets the dev shop prove "the fix
  worked" by recording a before/after.
- **`Pin as baseline` and `Compare with...` buttons** on the Profiler
  Session form view. Pinning is per-session-label and persists in
  Redis under `profiler:baseline:<label>`.
- **Auto-inheritance of baseline** at recording start — `api.start`
  checks the baseline cache for the label and pre-populates
  `compared_to_session` on the new session.
- **`comparison.py` module** — pure-function action and finding
  matchers, fixture-testable, no Frappe DB access.
- **PDF export of the safe report** — lazy-generated on first
  download click via `frappe.utils.pdf.get_pdf` (wkhtmltopdf), cached
  to a private File attachment on the Profiler Session. Subsequent
  downloads serve from cache. Generation cost is kept out of the
  analyze pipeline.
- **`pdf_export.py` module** — `get_or_generate_pdf` and
  `clear_cached_pdf` helpers.
- **PDF download button** on the Profiler Session form (lazy generation
  with progress alert).
- **Auto-assign `Profiler User` role to System Managers** on install
  via `after_install`. Also wires a `User.validate` doc_event so new
  System Managers automatically get the Profiler User role.
- **One-time onboarding toast** on first Desk visit after install,
  pointing the user at the floating Profiler pill. Suppressed for
  experienced users (anyone with a Ready Profiler Session row).
  Tracked via `profiler:onboarding_seen:<user>` in Redis.
- **Version-driven asset cache buster** — `app_include_js` and
  `app_include_css` now read `?v={__version__}` so every release
  automatically invalidates browser caches.
- **6 new whitelisted API endpoints** —
  `check_onboarding_seen`, `mark_onboarding_seen`, `pin_baseline`,
  `unpin_baseline`, `set_comparison`, `download_pdf`.
- **3 new fields on Profiler Session** — `compared_to_session`
  (Link), `is_baseline` (Check), `safe_report_pdf_file` (Attach).
- **SVG donut fallback for PDF mode** — wkhtmltopdf doesn't handle
  `conic-gradient` reliably; the renderer now produces an inline SVG
  pie chart that's hidden in HTML mode (via `@media print` CSS) and
  shown in PDF rendering.
- **Janitor cascade** — `sweep_old_sessions` clears the baseline
  cache key before deleting a baseline session and cascades the
  v0.4.0 `safe_report_pdf_file` attachment.
- **`retry_analyze` clears the cached PDF** so the next download
  regenerates from the freshly-analyzed report.
- **Self-contained safe report regression gate** — new test
  `test_safe_report_self_contained.py` asserts the rendered HTML
  contains no external URL fetches. Catches accidental introductions
  of CDN references at CI time.

### Changed

- **No changes to the v0.3.0 capture or analyze pipelines.**
  `capture.py`, `hooks_callbacks.py`, `analyze.py`, and the analyzer
  modules are frozen for this release.
- **`api.start(label, ...)` accepts the same kwargs as v0.3.0** —
  `capture_python_tree` is unchanged. The new auto-inheritance of
  `compared_to_session` is transparent to callers.
- **Renderer adds a comparison computation block** when
  `compared_to_session` is set on the session being rendered.
  Backward-compat: sessions with the field unset render exactly as
  in v0.3.0.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply patch `frappe_profiler.patches.v0_4_0.add_comparison_and_pdf_fields`
   which reloads the Profiler Session DocType.
2. Add 3 new fields to `tabProfiler Session`: `compared_to_session`,
   `is_baseline`, `safe_report_pdf_file`. All nullable / default 0.

No breaking API changes. v0.3.0 sessions render unchanged in v0.4.0
because all new fields default to NULL/0 and the renderer skips the
comparison and PDF paths when fields are unset.

To pin a session as a baseline, open it in the Profiler Session form
view (Status: Ready) and click **Pin as baseline**. Subsequent
recordings of the same flow (matching session title) will auto-include
comparison sections in their safe + raw reports.

---

## [0.3.0] — 2026-04-13

Adds a Python call tree capture and analysis layer on top of the existing
SQL-only profiler. Customers reading a safe report now see where their
flow spent time across SQL **and** Python, with hot-path detection,
hook bottleneck findings, and redundant-call detection. See
`apps/frappe_profiler_design/specs/2026-04-13-call-tree-and-redundant-calls-design.md`
for the full design spec.

### Added

- **pyinstrument-based Python call tree capture** per recording. Sampled
  at 1ms intervals (configurable via
  `site_config.json: profiler_sampler_interval_ms`). Scoped per request
  so non-recording users are unaffected.
- **Reconciled unified call tree** — each captured SQL call is grafted
  onto the deepest user-code frame in the pyinstrument tree, so the
  customer sees "your `discounts.calculate` function spent 320ms — 280ms
  of that in 14 SQL queries (children below)".
- **Four new finding types:**
  - `Slow Hot Path` — a Python subtree consumes >25% of an action AND >200ms.
  - `Hook Bottleneck` — same shape, but the subtree is a doc-event hook
    (called via `Document.run_method`); the finding names the hook function.
  - `Repeated Hot Frame` — the same frame appears in ≥3 actions and
    consumes ≥500ms total across the session.
  - `Redundant Call` — the same `frappe.get_doc(doctype, name)` /
    `frappe.cache.get_value(key)` / `frappe.permissions.has_permission(...)`
    fired N times from the same callsite (thresholds: 5/10/10 by default,
    all configurable).
- **Session-wide time-attribution donut** in the safe report — at-a-glance
  "this session was 38% SQL, 22% erpnext, 18% your custom code, …".
- **Hot frames leaderboard** in the safe report — top 20 hottest function
  paths across the whole session, sortable.
- **`api.start(label, capture_python_tree=True)`** — new kwarg lets
  customers opt out per session (falls back to v0.2.0 SQL-only capture).
  Surfaced in the floating widget's start dialog as a checkbox.
- **Auto-promote of large per-action call trees** to private File
  attachments when the inline JSON exceeds 200KB. Hard-truncation
  fallback if the file write fails. 16MB hard guard against pathological
  trees.
- **Sidecar wraps** for redundant-call detection on `frappe.get_doc`,
  `RedisWrapper.get_value` (the underlying class behind `frappe.cache`),
  and `frappe.permissions.has_permission`. Idempotent install at app
  load; restored on uninstall.
- **PII safety on sidecar arguments:** values that may contain user data
  (doc names, cache keys) are sha256-hashed (`identifier_safe`) for
  safe-mode display. Raw values stored only in raw-mode-visible
  technical details. Doctype names and ptypes are NOT hashed (schema,
  not data).
- **`pyinstrument >= 4.6, < 6` dependency** added to `pyproject.toml`.
  Pure-Python, MIT, no compiled extensions.
- **Streaming `_fetch_recordings`** — converted from list-returning to
  generator so the analyze pipeline holds bounded memory across large
  sessions.
- **Per-analyzer wall-clock budget tracker** — analyzers exceeding 60s
  are flagged; total analyze budgeted at 20 min (5-min headroom under
  RQ long-queue timeout). Past the cap, remaining analyzers are skipped
  with a partial-completion warning.
- **`api.export_session()` v0.3.0 fields** — JSON output now includes
  `call_tree`, `hot_frames`, `session_time_breakdown`, `total_python_ms`,
  `total_sql_ms`.
- **New site config keys:**
  - `profiler_sampler_interval_ms` — pyinstrument sample interval (default 1).
  - `profiler_tree_prune_threshold_pct` — drop frames below N% of action time (default 0.005).
  - `profiler_tree_node_cap` — max nodes per persisted tree (default 500, hot path always preserved).
  - `profiler_redundant_doc_threshold` (default 5).
  - `profiler_redundant_cache_threshold` (default 10).
  - `profiler_redundant_perm_threshold` (default 10).
  - `profiler_redundant_high_multiplier` (default 5).
  - `profiler_safe_extra_allowed_apps` — extra app prefixes whose function names are kept un-redacted in safe mode.

### Changed

- **Per-flow recording overhead** climbs from "10–30% per query" to
  roughly "1.5–2× wall clock during recording" when `capture_python_tree=True`.
  Non-recording users on the same site are still unaffected — the
  activation gate is per-user, and the wraps' hot-path check is a single
  attribute lookup with **<100ns overhead** measured against an unwrapped
  baseline.
- **`health()` `last_24h.analyze_avg_ms`** will rise after this ships.
  Customers monitoring it will see a step change at upgrade time.
- **Renderer adds donut + hot frames sections** to both safe and raw
  reports. Old v0.2.0 sessions render with the old layout (no v0.3.0
  fields → sections skipped).
- **R2 redaction policy** — function names in safe-mode reports collapse
  custom-app frames to `<app>:<top-level-module>` (e.g.
  `my_acme_app.discounts.pricing.calc_secret` → `my_acme_app:discounts`).
  Frappe / ERPNext / payments / hrms keep full names.

### Fixed (caught during v0.3.0 development)

- **`pyproject.toml` empty `authors = [{ name = "", email = ""}]`**
  broke `flit_core` on Python 3.14 with `email.errors.HeaderParseError`.
  Removed the empty entry.
- **`__init__.py` `frappe.log_error` fallback** in the
  `capture.install_wraps` except handler crashed when test code stubs
  `frappe` with a minimal fake module that lacks `log_error`. Now
  bulletproofed with a nested try/except.
- **Best-effort sidecar entry build** — a failure inside `_identify_args`
  (e.g. an arg with a broken `__str__`) used to propagate out and break
  the user's `frappe.get_doc` call. Now caught locally; the wrap skips
  the entry but always calls `orig`.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply patch `frappe_profiler.patches.v0_3_0.add_call_tree_fields`,
   which reloads `Profiler Action` and `Profiler Session` to pick up
   the new nullable columns.
2. Add 3 new fields to `tabProfiler Action`: `call_tree_json`,
   `call_tree_size_bytes`, `call_tree_overflow_file`.
3. Add 4 new fields to `tabProfiler Session`: `total_python_ms`,
   `total_sql_ms`, `hot_frames_json`, `session_time_breakdown_json`.

No breaking API changes — `start`, `stop`, `status`, `get_active_session`,
`health`, `export_session`, `retry_analyze` all keep their existing
signatures (`start` accepts a new optional kwarg with backward-compatible
default).

Existing v0.2.0 sessions continue to render with the old layout
(NULL v0.3.0 fields → donut/hot frames sections skipped via the
backward-compat fallbacks).

To opt out of the new pyinstrument capture per-session, uncheck
**"Capture Python call tree"** in the start dialog or pass
`capture_python_tree=False` to `api.start`.

---

## [0.2.0] — 2026-04-09

Round 2 improvements. 28 items across correctness, operations, UX,
extensibility, and housekeeping. See
`apps/frappe_profiler_design/ARCHITECTURE.md` for the full design rationale.

### Added

- **JSON export endpoint** — `frappe_profiler.api.export_session(uuid)`
  returns a structured blob (session + actions + findings + top queries +
  table breakdown) for programmatic consumption by dev-shop tools.
- **Health / metrics endpoint** — `frappe_profiler.api.health()` returns
  counts by status and analyze-pipeline performance over the last 24 hours.
  Intended for Prometheus/Grafana/Datadog scrapers.
- **Custom analyzer hook** — third-party Frappe apps can contribute
  analyzers via `hooks.py: profiler_analyzers = ["my_app.analyzers.custom.analyze"]`.
  Hooks run after the builtins and share the same `AnalyzeContext`.
- **Cross-session EXPLAIN cache** — EXPLAIN results are now cached in
  Redis with a 1-hour TTL (configurable via
  `site_config.json: profiler_explain_cache_ttl_seconds`). Two consecutive
  analyze runs on a stable schema skip the DB roundtrip entirely.
- **Notes field on Profiler Session** — customers can annotate sessions
  with reproduction steps, ticket refs, context. Editable even on Ready
  sessions. Rendered in the HTML report header.
- **Progress updates during analyze** — the analyze pipeline emits
  `frappe.publish_realtime("profiler_progress", ...)` events at each
  phase (5% fetching, 20% EXPLAIN, 50% analyzers, 80% persist, 90%
  render, 100% done). The floating widget subscribes and displays a live
  percentage instead of a bare "Analyzing…".
- **Retention-policy cleanup** — daily janitor deletes Ready/Failed
  sessions older than 90 days (configurable via
  `site_config.json: profiler_session_retention_days`).
- **Orphan Redis cleanup** — the daily janitor also sweeps
  `profiler:session:*` Redis keys whose parent Profiler Session row no
  longer exists (e.g. failed analyzes that never retried).
- **Sensitive-field redactor** — raw report now redacts known-sensitive
  fields from headers and form_dict before rendering. Redacts: password,
  secret, token, api_key, authorization, cookie, csrf, otp, card_number,
  cvv, ssn, aadhar, pan_number, and similar. Defense-in-depth against
  download-and-share leaks.
- **Session TTL refresh on activity** — long flows (45+ minutes) no
  longer silently stop at the 10-minute TTL. Every
  `register_recording` call refreshes the user's active-session key so
  an actively-used session stays alive as long as there's traffic.
- **Server timezone in report header** — the report now labels times
  with an explicit server timezone so distributed teams don't get
  confused about UTC vs. local.
- **Retry Analyze button** — Failed sessions now have a "Retry Analyze"
  custom button in the form view that re-enqueues the analyze job. New
  `frappe_profiler.api.retry_analyze(session_uuid)` whitelisted endpoint.
- **Fixture builder helpers** — `frappe_profiler.tests.fixture_builders`
  provides `build_call`, `build_recording`, `build_explain_row` to
  reduce boilerplate in analyzer tests.

### Fixed

- **N+1 attribution blamed frappe framework code** — `_callsite()` now
  walks the stack skipping `frappe/` and `frappe_profiler/` prefixes so
  N+1 findings point at customer business logic (e.g.
  `erpnext/accounts/sales_invoice.py:212`) instead of framework helpers
  (`frappe/database/database.py:742`). Single most impactful fix in
  the round-1 review.
- **`explain_flags` documented a `filtered < 10` check that wasn't
  implemented** — the new check fires on queries where MariaDB's
  `filtered` column < 10 AND rows_examined > 100, emitting a new
  `Low Filter Ratio` finding type.
- **`before_request`/`before_job` could clobber an existing recorder** —
  if the standalone Recorder UI is active globally, frappe's own hook
  creates a Recorder first; our hook now checks
  `frappe.local._recorder` and piggybacks instead of overwriting it.
- **`api.start()` had no role check** — any authenticated user could
  POST to the endpoint and start a session on themselves. Now requires
  `Profiler User` or `System Manager` role (enforced at the HTTP level,
  not just the UI).
- **N+1 threshold of 5 was too low** — raised default to 10 with a
  `profiler_n_plus_one_threshold` site config override. Also requires
  minimum total time (default 20ms) so 10×0.1ms queries no longer
  trigger false positives.
- **`_enrich_recordings` had no EXPLAIN cap** — now caps at 2000
  queries per recording and dedupes EXPLAIN by query shape. Prevents
  the analyze job from running millions of EXPLAINs on pathological
  sessions.
- **DB indexes missing on `status` and `started_at`** — the janitor
  query was a table scan at scale. Added `search_index: 1` on both.
- **`index_suggestions` silently swallowed errors** — now logs the
  first 3 per-query failures and surfaces a `"Could not analyze X queries"`
  warning in the report.
- **Multi-line SQL rendered as single line in top-N table** — switched
  from `<code>` to `<pre class="sql-inline">` with bounded height.
- **`before_job` left `_profiler_session_id` in kwargs on malformed
  kwargs** — defensive type check + error log.
- **Widget polled forever in hidden tabs** — now pauses polling on
  `visibilitychange` and resumes when the tab becomes visible again.
- **Cap warning not surfaced in UI** — `analyzer_warnings` now renders
  as an orange `frm.set_intro` banner at the top of the form.
- **"Top contributor" summary missed session-wide findings** — the
  two-step fallback now picks the highest-impact finding overall when
  there's no action-specific match.
- **Session list view had no severity indicator** — new `top_severity`
  field populated by analyze, color-coded in the list view via a custom
  `listview_settings.get_indicator`.
- **`track_changes=1` on Profiler Session caused storage bloat** —
  every analyze created 10+ tabVersion rows. Disabled track_changes;
  patch `v0_2_0.remove_version_tracking` cleans up existing rows on
  `bench migrate`.
- **Potential recursive analyze** — `analyze.run()` now sets
  `frappe.local.profiler_analyzing = True` so hooks skip activation on
  the analyze pipeline's own DocType writes.
- **`_optimize_query` errors could leak query literals in the error
  log** — added a paranoia scrub (`'foo'` → `'?'`, long numbers → `?`)
  before logging.
- **Uninstall didn't clean Redis state** — `before_uninstall` now
  SCAN+DELETEs all `profiler:*` keys for the site.

### Changed

- **Analyzer unit tests** — 50+ new tests covering per_action, top_queries,
  n_plus_one (with callsite attribution assertions), explain_flags (all
  4 red flags including the new filtered check), index_suggestions (with
  `monkeypatch` for `_optimize_query`), table_breakdown, the enqueue
  patch (idempotency + session id injection), and frontend asset smoke
  tests (JS syntax + content assertions). All 67 tests pass in < 1s.
- **Shared `SEVERITY_ORDER` and `walk_callsite`** — moved from
  per-module copies to `analyzers/base.py`.
- **Refactored `_stop_session`** — split into `_clear_active`,
  `_mark_stopping`, `_enqueue_analyze` for clarity.
- **README overhauled** — operational caveats, hard-cap table, config
  knobs, verification checklist.
- **Version bumped** from 0.0.1 to 0.2.0.

### Migration notes

Running `bench --site <site> migrate` will:

1. Apply the `profiler_session.status` and `profiler_session.started_at`
   database indexes.
2. Run `patches.v0_2_0.remove_version_tracking` to delete existing
   `tabVersion` rows for Profiler Session (freeing storage; no data loss
   because these versions weren't useful anyway).
3. Add the new `notes`, `top_severity`, `analyze_duration_ms` fields to
   `tabProfiler Session`.
4. Add the new `Low Filter Ratio` value to the `Profiler Finding.finding_type`
   select.

No breaking API changes — existing calls to `start`, `stop`, `status`,
`get_active_session`, `retry_analyze` are unchanged.

---

## [0.1.0] — 2026-04-08

Initial feature-complete v1. All 8 phases from the design doc plus 21
fixes from the first-review pass. See `ARCHITECTURE.md` for the design
rationale.

### Added

- Scaffold (Phase 0): installable Frappe app with three DocTypes
  (`Profiler Session`, `Profiler Action`, `Profiler Finding`).
- Session lifecycle (Phase 1): whitelisted `start`/`stop`/`status`/
  `get_active_session` API, Redis-backed per-user session tracking,
  before/after request hooks that activate the recorder only for users
  with an active session.
- Background job inheritance (Phase 2): `frappe.enqueue` monkey-patch
  injects `_profiler_session_id` into job kwargs; before/after job
  hooks pop the marker and activate recording.
- Six analyzers (Phase 3): per-action breakdown, top-N slowest queries,
  true N+1 detection, EXPLAIN red flags (full scan, filesort,
  temporary table), aggregated index suggestions, per-table breakdown.
- HTML report renderer (Phase 4): safe and raw modes from a single
  Jinja template. Self-contained HTML with inline CSS.
- UI (Phase 5): floating start/stop widget, Profiler Session form
  customization with status indicator, download buttons, findings
  dashboard.
- Production hardening (Phase 6): 200-recording cap per session, stale
  session janitor every 5 minutes, raw report permission gate,
  comprehensive README.

---

## [0.0.1] — 2026-04-08

Initial scaffold. Empty app with no logic — just the DocType structure.
