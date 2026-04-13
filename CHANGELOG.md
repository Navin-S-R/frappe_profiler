# Changelog

All notable changes to the Frappe Profiler app.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project follows [SemVer](https://semver.org/) (pre-1.0, so minor
versions may contain breaking changes — see migration notes below).

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
