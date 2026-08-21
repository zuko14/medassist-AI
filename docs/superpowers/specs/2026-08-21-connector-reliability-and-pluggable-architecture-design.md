# Connector Reliability & Pluggable Architecture — Design

**Date:** 2026-08-21
**Status:** Approved for planning
**Owner:** Diagnostic Center Admin Panel — Report Connector

## Problem

The diagnostic center admin panel's "Report Connector" page (MocDoc integration) is
unreliable in production:

- "Test Connection" frequently fails with `Connector is currently locked by another
  worker`.
- Credentials appear to need re-entry on every visit.
- Run History shows "Failed to load run history."
- Dry-run test results give no visibility into what was actually scraped (login
  success, patients/reports found).
- Only one connector type (MocDoc) exists; there's no clear path to add a second
  HMIS integration for a future client without hardcoding it into the frontend.

## Root Cause (single cascade, not five unrelated bugs)

1. `Dockerfile` runs `playwright install --with-deps chromium` as `root`, *before*
   `USER appuser` is set. Playwright's default cache path is under `$HOME`, so the
   browser lands in `/root/.cache/ms-playwright` — invisible to `appuser` at
   runtime. Every cold container boot silently re-downloads Chromium
   (`app/utils/browser_errors.py`'s runtime-install fallback), costing 1–3 minutes
   and adding non-determinism.
2. That extra boot latency raises the odds a Render redeploy kills the process
   mid-login. When that happens, the `finally:` block in
   `connectors/runner.py:run_connector()` that releases the advisory lock
   (`integration_connectors.locked_at` / `locked_by`) never executes.
3. The lock lease is 15 minutes (`runner.py:acquire_connector_lock`). A stale lock
   left by a killed run blocks every subsequent Test Connection / Run Now attempt
   for up to 15 minutes — this is the literal error message reported.
4. There is no scheduled polling in production at all: `connectors/runner.py --all`
   is never invoked (render.yaml declares a single `web` service; the Dockerfile
   `CMD` only runs uvicorn). Every connector run today is admin-triggered, so
   admins retry repeatedly while the stale lock is active, compounding the
   perceived breakage.
5. `GET /admin/connectors/{id}/audit-log` uses the `require_admin` dependency while
   every other connector endpoint uses `require_permission("CONNECTOR_MANAGE")`.
   A `DIAGNOSTIC_OPERATOR` staff account — the role this admin panel is meant for —
   holds `CONNECTOR_MANAGE` but is not admin-tier, so it gets 403'd loading run
   history.
6. Dry-run currently returns only a count (`reports_found`); nothing proves the
   login actually reached the reports page and parsed real rows.
7. `connectors/base.py` (`HospitalConnector` ABC) and `CONNECTOR_REGISTRY` in
   `runner.py` already form a working plugin architecture for multiple HMIS
   backends. The gap is entirely in the admin UI, which hardcodes "MocDoc
   Credentials" and MocDoc-specific field labels.

Credentials save/reload itself (`admin.py:upsert_connector_credentials`,
`admin/index.html:saveConnectorCredentials`/`loadConnectorCredentials`) is
functionally correct — masked placeholders, optional blank-to-keep-existing. The
"must re-enter every time" perception is a symptom of #1–#4: Test Connection
always failing makes the save look like it didn't take.

## Design

### A. Deployment fix

- `Dockerfile`: set `ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright` before the
  `pip install`/`playwright install` steps, so build-time install and the
  `appuser` runtime resolve to the same path (already covered by the later
  `chown -R appuser:appuser /app`). Removes the every-boot reinstall.
- `render.yaml`: add a second service, `type: worker`, same Dockerfile,
  `dockerCommand: python -m connectors.runner --all`, so scheduled polling
  (`run_all_connectors()`, already implemented, 10-minute interval) actually runs
  in production. This is the approved "Automatic Background Sync."

### B. Lock reliability

- Shorten `acquire_connector_lock`'s lease from 15 min to 5 min.
- Release locks held by the current process on graceful shutdown: FastAPI
  `shutdown` event handler + `SIGTERM` handler in `connectors/runner.py`'s
  scheduled mode, both calling `release_connector_lock` for any connector this
  process currently holds.
- When `acquire_connector_lock` fails because of a live lock,
  `run_connector()`'s summary includes the lock's remaining TTL
  (`error_message: "Connector is busy — retry in ~Nm"`), and the admin UI surfaces
  that instead of a bare error string.

### C. RBAC fix

- Change `GET /admin/connectors/{id}/audit-log` to use
  `require_permission("CONNECTOR_MANAGE")` instead of `require_admin`, matching
  every other `/admin/connectors/*` endpoint.

### D. Richer dry-run results

- `run_connector(dry_run=True)` summary gains a `sample` field: the first 5 parsed
  rows as `{patient_name_masked, vam_id, report_name}` — no new scraping, just
  retaining data `fetch_new_reports()` already parses in memory before it's
  discarded.
- Admin UI's Test Connection result renders a small result card (status, reports
  found, sample table) in addition to the toast, and the audit log lists this
  dry-run the same as any other run (`run_status: "dry_run"` already recorded).

### E. Pluggable connector UI (groundwork only — no second connector built)

- Add a `CONFIG_SCHEMA` class attribute to `HospitalConnector` subclasses: an
  ordered list of `{key, label, type, placeholder, required}` describing the
  credential fields a connector type needs. `MocDocConnector.CONFIG_SCHEMA`
  captures its existing fields (username, password, clinic_slug, base_url).
- New endpoint `GET /admin/connectors/types` returns
  `{type: "mocdoc", display_name: "MocDoc", schema: [...]}` for every entry in
  `CONNECTOR_REGISTRY`.
- Admin panel's Report Connector page adds a connector-type `<select>`
  (single option today: MocDoc) and renders the credentials form from the
  fetched schema instead of hardcoded MocDoc-only labels/placeholders. Adding a
  real second connector later requires one new `HospitalConnector` subclass +
  one `CONNECTOR_REGISTRY` entry — zero admin-frontend changes.

### Error handling

- All new failure paths (worker shutdown mid-lock-release, missing schema for an
  unknown connector_type, dry-run sample parse failure) degrade to the existing
  generic error surfaces (`error_message` string, toast) — no new silent
  failures introduced.

### Testing

- `tests/test_connector_runner.py` (new or extended): lock acquire/expiry/release
  timing: lock granted, lock denied while live, lock available again after TTL,
  lock released on explicit call.
- `tests/test_admin_connectors.py` (new or extended): audit-log endpoint
  reachable by a `DIAGNOSTIC_OPERATOR`-permissioned staff user (was 403 before);
  `/admin/connectors/types` returns MocDoc's schema.
- Dry-run sample payload: unit test that `fetch_new_reports()`'s parsed rows
  surface into `run_connector`'s summary `sample` list, masked correctly.
- One manual/assert-based check that `PLAYWRIGHT_BROWSERS_PATH` resolves to the
  same path for the build-time install user (root) and the runtime user
  (appuser) — smallest possible check for the Dockerfile fix, not a full
  integration test (no Docker-in-CI here).

## Explicitly out of scope

- Building a second real connector (Practo, Birlamedisoft, etc.) — no client for
  one yet; schema/registry groundwork is sufficient until there is.
- Changing the credential encryption scheme (Fernet) — not implicated by any
  reported symptom.
- Multi-branch connector UX — already implemented (migration 025), untouched here.
