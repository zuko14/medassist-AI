# SESSION 40 — Multi-Client Production Hardening Audit

**Date**: 2026-10-27
**Branch**: `hardening/production-readiness-audit` (not merged; `main` auto-deploys to production)
**Migrations**: none — every fix is application code, so nothing changes in the live database.
**Baseline**: 6,946 passed / 5 skipped before any change. **After**: 6,950 passed / 5 skipped (4 new tests).

---

## Scope

Audit of the whole platform for onboarding more clients (clinics, multi-specialty hospitals)
beyond the live diagnostic-centre client: auth and multi-tenancy, staff accounts and branch
pinning, payments and refunds, OPD OS, IPD OS, scheduler, MocDoc connector, admin panel.

Method: route inventory of all 398 endpoints with their auth dependencies, AST scan of every
write handler for an in-body permission check, then line-by-line reading of the money paths
(Razorpay/PhonePe webhooks, OPD receipts/refunds/voids and their DB triggers), IPD service +
RPCs, connector runner locking, tenant routing, and every query that aggregates rows in Python.

## What held up (no change needed)

| Area | Evidence |
|:---|:---|
| Auth surface | Every `/admin`, `/platform`, `/fhir`, OPD, IPD route has an auth dependency; the only unauthenticated routes are health, Meta/payment webhooks (signature-verified), voice (token-verified) and the queue display (hashed token). |
| Write permissions | Every OPD/IPD/voice/home-collection/assistant write handler calls its scope guard (`_opd_scope`, `_ipd_scope`, `_scope`, …) with a named permission. |
| Tenant routing | `clinics.whatsapp_number` and `phone_number_id` are unique; `resolve_tenant` fails closed; `enforce_clinic_access` never returns a wildcard. |
| Staff accounts | Grants capped to the granter; branch-pinned staff cannot escape their branch; sessions revoked on any authority change, deactivation or delete. |
| Payment webhooks | HMAC verified before parsing; no global secret fallback; clinic-scoped lookups; CAS on every status write; late payments auto-refunded. |
| OPD money integrity | DB triggers forbid over-payment, over-refund, voiding a paid bill, editing an issued bill; receipts are append-only and idempotent (mig 113). |
| IPD | Every state change is one locked PL/pgSQL transaction (admit, assign, transfer, discharge, billing). |
| MocDoc connector | CAS lease lock with owner check and renewal; held reports still stored for review; per-error alert de-duplication. |

## Defects fixed

| # | Severity | Defect | Fix |
|:-|:-|:-|:-|
| 1 | High (money) | OPD refunds had no idempotency: a retry after a timeout recorded the refund twice (the DB only blocks refunds beyond the paid total). | `RefundIn.idempotency_key` (optional, back-compatible) → `create_refund` replays the first refund; the panel keeps one key per refund attempt. Uses the existing mig-113 unique index. |
| 2 | High (silent, live client) | Patients page and dashboard patient totals used `.limit(2000)`, but PostgREST returns at most 1,000 rows — clinics past 1,000 patients/reports saw a truncated list and wrong totals with no error. | New `database.fetch_all()` pages any query; `get_genuine_patients` pages all four sources. |
| 3 | High (silent, multi-client) | 24h reminders (daily), follow-ups, health check-ins, 2h reminders and auto-complete each read one ≤1,000-row page across **all clinics**. Rows of clinics with the feature off are skipped but never flagged, so they keep the slots. Past ~1,000 platform bookings/day, the remaining patients silently get no reminder/follow-up, and past bookings stay "confirmed". | All five sweeps page with `fetch_all`. |
| 4 | Medium (silent) | Dashboard appointment tiles fetched a 30-day window in one request (wrong past ~33 bookings/day). OPD day-collections summary capped receipts/invoices at 1,000. Voice analytics capped at 1,000 calls. | Paged (`_fetch_window` / `fetch_all`). |
| 5 | Medium (scale, hospitals) | IPD bed board, census and worklist sent every occupied bed's admission/patient UUID in one `in.(…)` URL. Around 200+ occupied beds the request line exceeds the API gateway limit and the bed board fails. | `ipd._rows_in` batches IN lookups by 100 (the pattern OPD collections already used). |
| 6 | Medium (reliability) | 11 patient WhatsApp sends (OPD receipts, payment links, queue calls) and the message-accounting ledger write used bare `asyncio.create_task`, which the event loop only weakly references — a task can be garbage-collected mid-send. | Use the codebase's own `spawn_background_task`. |
| 7 | Low (info leak) | Encounter/prescription sign returned raw database exception text in HTTP 500 bodies. | Logged server-side; generic message to the client. |

## Tests added

- `tests/test_opd_billing_real_pg.py::test_a_retried_refund_pays_out_once` (real Postgres)
- `tests/test_patients_branch_scope.py::test_a_clinic_past_1000_patients_is_listed_in_full`
- `tests/test_phase_g_scheduler.py::test_24h_reminders_reach_every_patient_past_1000_bookings`
- `tests/test_ipd_in_batching.py`

Three existing tests mocked the old single-query shape and were updated to the paged shape.

## Live-client safety

- No migration, no schema change, no config change.
- The MocDoc connector code is untouched.
- Refund idempotency key is optional on the API, so an older cached panel keeps working.
- Paging returns exactly what one request returned whenever a result is under 1,000 rows,
  so small clinics see identical behaviour; larger ones now see complete data.

## Recommended next (not done here — need an owner decision)

1. **Connector sweep is sequential.** `run_all_connectors` polls every MocDoc connector one
   after another in one worker (~6 min per busy connector). With several diagnostic clients,
   report delivery delay grows linearly. Options: a bounded-concurrency sweep (memory cost:
   ~150–400 MB Chromium per run) or more worker instances (the per-connector lock already
   makes that safe).
2. **Tenant cache staleness** (KA-19, 30 s across workers) and **shared platform
   integration secret** for clinics without `config.integration_secret` — still open from
   earlier sessions; set per-clinic secrets as part of each new client's onboarding.
3. **`get_genuine_patients` cost** now grows with data (it pages instead of truncating). For a
   clinic with tens of thousands of patients, move the engagement filter into a SQL view/RPC.
4. **IPD bed board** reads all beds in one request (documented shortcut, fine below 1,000 beds).
