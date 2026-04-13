# Frappe Profiler

A flow-aware performance profiler for Frappe and ERPNext. Records a real
business workflow (Sales Invoice save → submit → Delivery Note → submit →
…), aggregates everything into a customer-friendly report, and produces
two downloadable HTML files: a **safe** one to share with a developer
and a **raw** one for internal debugging.

> **Status:** v0.1.0 — feature-complete v1. Phase 6 hardening is in
> place. Customer-facing usage and conceptual docs live in
> [`../frappe_profiler_design/README.md`](../frappe_profiler_design/README.md).
> The technical architecture lives in
> [`../frappe_profiler_design/ARCHITECTURE.md`](../frappe_profiler_design/ARCHITECTURE.md).

---

## Quick start

### Install

```bash
cd ~/frappe-bench
bench get-app https://github.com/<your-org>/frappe_profiler.git   # or local path
bench --site <your-site> install-app frappe_profiler
bench --site <your-site> migrate
```

### Use it

1. Open Desk on the site. A small **Profiler** button appears bottom-right.
2. Click it. Type a label like `"Sales Invoice flow with 50 items"`. Click Start.
3. Run your real flow — save, submit, navigate, whatever you want to profile.
4. Click the button again to Stop. Wait a few seconds for analysis.
5. Click the button (now blue: "Report ready") to open the report.
6. Download the **Safe Report** to share with your dev shop.

---

## Production safety — read this before installing on production

This app is **designed** to run on production because the whole point is
to measure with real data volumes. That said, recording is not free.

### Overhead per recorded query

Recording adds approximately **10–30 % per query** because we capture a
full Python stack trace and run `EXPLAIN` on every SELECT/UPDATE/DELETE.
A flow that normally takes 4 s will take 5–6 s while recording. We
believe this is the right trade-off — without stack traces we can't do
true N+1 detection, and N+1 detection is the highest-leverage insight.

If your customer is doing perf-sensitive timing measurements, the report
should be read as "relative" (this step took 5× longer than that step),
not "absolute" (this step took exactly 4.2 s).

### Per-user isolation

Only the user who clicked Start gets recorded. Other users on the same
site at the same time are **not** captured. The activation check is one
Redis `GET` per request — sub-millisecond on a local Redis — so users
who are not recording pay almost nothing.

### Background job inheritance

Background jobs spawned by the recording user's actions are also
captured under the same session. ERPNext's submission code path
enqueues several jobs (GL postings, stock updates) — without this,
the report would miss huge chunks of work. The mechanism is a small
monkey-patch on `frappe.enqueue` that injects a session ID into job
kwargs; the worker's `before_job` hook reads it.

### Auto-stop after 10 minutes

A session stops itself after 10 minutes even if you forget to click
Stop. The Redis active key has a TTL that matches the underlying
`frappe.recorder` auto-disable. A background janitor job (every 5
minutes) catches sessions whose state row is still `Recording` after
the TTL has expired and force-stops them.

### Hard caps

| Cap | Default | Override |
|---|---|---|
| Max recordings per session | 200 | `site_config.json: profiler_max_recordings_per_session` |
| Session duration | 10 minutes | (matches recorder, not configurable) |
| Analyze job timeout | 25 minutes | RQ long-queue default |

If a session hits the recordings cap, the analyze report shows a
warning under `analyzer_warnings`. Subsequent recordings for that session
are silently dropped until the customer restarts.

### Memory cleanup

When a session moves to `Ready`, the source recordings in Redis
(`RECORDER_REQUEST_HASH`, `RECORDER_REQUEST_SPARSE_HASH`) and the
`profiler:session:*` keys are deleted. Redis returns to baseline. The
`Profiler Session` DocType row + the two attached HTML report files
remain as the durable record.

### Sensitive data in reports

Two report artifacts are generated on stop:

- **`safe_report_file`** — Normalized SQL only (literals replaced with
  `?`), no headers, no form data, no full stack traces. Safe to email
  to a third-party dev shop.
- **`raw_report_file`** — Full data: raw SQL, headers, form data, full
  stack traces. **Internal use only.** Two protection layers:
    1. The "Download Raw Report" button is hidden in the UI for users
       without `System Manager` role or who didn't record the session.
    2. A `has_permission` hook on the `File` DocType (in
       `frappe_profiler.permissions.file_has_permission`) blocks direct
       URL access for non-admin users even if they guess the file name.

---

## How it works

The architecture in five sentences:

1. **Don't fork the recorder.** We reuse `frappe.recorder.Recorder` /
   `record(force=True)` / `dump()` for capture. Our app adds session
   tracking, per-user activation, background-job inheritance, and the
   analyze pipeline on top.
2. **Per-user activation via hook ordering.** Frappe's `before_request`
   runs `frappe.recorder.record()` first (no-op without a global flag);
   our `before_request` runs second and calls `record(force=True)` only
   if the current user has an active session in our Redis pointer.
3. **Background-job session inheritance via a `frappe.enqueue` patch.**
   We wrap the canonical `enqueue` to inject `_profiler_session_id`
   into job kwargs whenever the calling user has an active session;
   the worker's `before_job` hook pops the marker (so the user's method
   never sees it) and activates recording for the job.
4. **Six analyzers, all pure functions.** Per-action breakdown, top-N
   slow queries, true N+1 detection (by callsite), EXPLAIN red flags
   (type=ALL / filesort / temporary table), aggregated index suggestions
   (wraps the recorder's `_optimize_query`), and per-table breakdown.
   Each analyzer is independently testable from JSON fixtures.
5. **Two HTML reports from one Jinja template.** Safe and raw modes
   share `templates/report.html`; the `mode` context variable toggles
   redaction-sensitive sections. Both files are private File records
   attached to the Profiler Session.

For the full architecture (data flow, hook order diagrams, edge cases,
extension points), see
[`../frappe_profiler_design/ARCHITECTURE.md`](../frappe_profiler_design/ARCHITECTURE.md).

---

## Verification checklist

After install and migrate, verify in this order:

1. **DocTypes exist:**
   ```bash
   bench --site <site> mariadb -e "SHOW TABLES LIKE 'tabProfiler%';"
   ```
   Should list `tabProfiler Session`, `tabProfiler Action`, `tabProfiler Finding`.

2. **The enqueue monkey-patch is active:**
   ```bash
   bench --site <site> console
   >>> import frappe
   >>> frappe.enqueue._profiler_patched
   True
   ```

3. **Floating widget appears in Desk:** log in as a System Manager,
   open any Desk page, look bottom-right for the gray "Profiler" pill.

4. **Start a session and record some traffic:**
   ```python
   >>> from frappe_profiler import api
   >>> api.start(label="smoke")
   >>> # in another browser tab, do something — open a list, save a doc
   >>> api.stop()
   >>> # wait 5-10s for the analyze worker
   >>> doc = frappe.get_doc("Profiler Session", api.status().get("docname"))
   >>> doc.status
   'Ready'
   >>> len(doc.actions), len(doc.findings)
   ```

5. **Reports are attached:** `doc.safe_report_file` and `doc.raw_report_file`
   should both be set. Open the safe report URL in a browser — you should
   see a styled HTML page with summary cards, per-action table, findings,
   top-N queries, and per-table breakdown.

6. **Janitor runs:** the scheduler should fire `sweep_stale_sessions`
   every 5 minutes. Check `tabError Log` for any errors with title
   `frappe_profiler janitor*`.

---

## Configuration

All knobs live in `sites/<your-site>/site_config.json`. Every value is
optional — the defaults are sensible for most deployments.

| Key | Default | Purpose |
|---|---|---|
| `profiler_max_recordings_per_session` | `200` | Soft cap on how many HTTP requests + background jobs one session can capture. When hit, further recordings are silently dropped and the report shows an `analyzer_warnings` banner. |
| `profiler_n_plus_one_threshold` | `10` | Minimum repetition count before a group of identical-shape queries from the same callsite is flagged as an N+1. Bump higher on legacy ERPNext codebases that have many legitimate N-ish patterns. |
| `profiler_n_plus_one_min_total_ms` | `20` | Minimum cumulative time a group of queries must consume before it becomes an N+1 finding. Prevents noise from `10 × 0.1ms` queries that repeat but don't actually cost anything. |
| `profiler_session_retention_days` | `90` | Sessions in `Ready` or `Failed` state older than this are deleted by the daily janitor (along with their attached HTML report files). Set high for long-term audit; low for ephemeral pilots. |
| `profiler_explain_cache_ttl_seconds` | `3600` | How long EXPLAIN results are cached in Redis across analyze runs. On a stable schema, two consecutive sessions running the same queries skip the DB roundtrip entirely. Set to `0` to disable the cross-session cache (falls back to per-session dedup). |

### Runtime flags

These are set on an active session at runtime (via the widget's start
dialog or the `api.start(label, **options)` call):

- `capture_stack` — whether to capture Python stack traces for every
  query. Default: on. **Required** for N+1 detection — don't turn off.
- `explain` — whether to run `EXPLAIN` on SELECT/UPDATE/DELETE queries
  during the analyze phase. Default: on. Turn off only if your
  production MariaDB is under heavy load and you'd rather not add
  EXPLAIN overhead.

### Hooks for custom analyzers

Third-party Frappe apps can contribute analyzers without forking. In
your app's `hooks.py`:

```python
profiler_analyzers = [
    "my_app.performance.analyzers.orders.analyze",
    "my_app.performance.analyzers.payments.analyze",
]
```

Custom analyzers run AFTER the six builtins and share the same
`AnalyzeContext`. Each must be a pure function with signature
`(recordings: list[dict], context: AnalyzeContext) -> AnalyzerResult`
where `AnalyzerResult` comes from `frappe_profiler.analyzers.base`.
See `ARCHITECTURE.md` Extension Points for the full contract.

---

## Contributing

This is an MIT-licensed Frappe app. Contributions welcome via PR.

For new analyzers: see
[`frappe_profiler/analyzers/base.py`](./frappe_profiler/analyzers/base.py)
for the interface contract. Each analyzer is a pure function
`(recordings, context) → AnalyzerResult` with no Frappe DB access in the
function itself, so it's unit-testable from JSON fixtures.

---

## License

MIT — see [`license.txt`](./license.txt).
