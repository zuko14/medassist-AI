# Session 33 — Kriya OPD OS Phase 1

**Dates:** 2026-10-09 to 2026-10-10  
**Branch:** `feat/opd-os-phase-1` → merged to `main`  
**Scope:** Full OPD Operating System — patient registry, queue management, consultation workspace, e-prescriptions, billing, payments, analytics, TV display, and multi-channel integration  
**Migrations:** `103_opd_os_core.sql`, `104_opd_payment_exceptions.sql`, `105_opd_function_search_path.sql`  
**Design Spec:** [`docs/opd-os/SPEC.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/opd-os/SPEC.md)  
**Execution Tracker:** [`docs/opd-os/status.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/opd-os/status.md)

---

## 1. Summary

Kriya AI's OPD Operating System transforms the platform from an appointment-and-messaging system into a full clinic operating system. A hospital owner enables OPD from the platform console, runs the setup wizard, and the entire walk-in-to-receipt lifecycle is managed: patient registration with DPDP consent, walk-in token issuance, live queue board, doctor consultation workspace with vitals and SOAP notes, zero-LLM allergy warnings, signed e-prescriptions with multilingual A5 PDF generation, GST-ready invoicing, cashier shifts with drawer reconciliation, UPI QR payments, online payment link settlement, thermal receipt printing, hallway TV display, and operational analytics.

The system was built across 5 sub-phases in a single sprint:

| Phase | Scope | New Tests |
|---|---|---|
| 1.1 | Migration 103, tenancy isolation, owner entitlement, platform toggles | 27 |
| 1.2 | Patient registry, walk-ins, live queue, TV display | 68 |
| 1.3 | Consultation workspace, signed e-prescriptions, PDF generation | 55 |
| 1.4 | Invoicing, cashier shifts, payments, receipts, webhook settlement | 70 |
| 1.5 | Channel stamping, analytics, security matrices, hardening | 9 |

**Total:** 31 files modified, 34 new files created. 229 new OPD-specific tests. Full suite rose from 2,897 to 4,188+ passing tests.

---

## 2. Migrations

### 2.1 Migration 103 — OPD Core Schema

**File:** [`migrations/103_opd_os_core.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/103_opd_os_core.sql) (59,619 bytes)  
**Rollback:** [`migrations/rollback/103_down.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/rollback/103_down.sql)

Creates 7 tenant-owned tables with composite tenant FK enforcement:

| Table | Purpose |
|---|---|
| `opd_encounters` | Walk-in/booked visit lifecycle (draft→signed→superseded) |
| `opd_prescriptions` | Prescription headers with version chain |
| `opd_prescription_items` | Line items locked after parent signed |
| `opd_invoices` | Gapless `INV-YYYY-XXXXX` numbering, derived `paid_paise` |
| `opd_receipts` | Append-only payment records |
| `opd_shifts` | Cashier drawer sessions with computed variance |
| `opd_settings` | Per-clinic OPD configuration (templates, thresholds, state) |

Plus 8 PostgreSQL RPCs (`opd_sign_encounter`, `opd_close_shift`, `opd_format_number`, `opd_issue_invoice`, `opd_purge_clinic`, etc.), 5 triggers (`opd_record_locked`, `opd_paid_is_derived`, `opd_shift_not_open`), and rebuilt slot indexes excluding walk-ins.

### 2.2 Migration 104 — Payment Exceptions

**File:** [`migrations/104_opd_payment_exceptions.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/104_opd_payment_exceptions.sql) (6,472 bytes)  
**Rollback:** [`migrations/rollback/104_down.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/rollback/104_down.sql)

Adds `opd_payment_exceptions` table for parking over-payments, amount mismatches, and void-invoice payments. One-click gateway refund or manual settlement tracking.

### 2.3 Migration 105 — Function Search Path

**File:** [`migrations/105_opd_function_search_path.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/105_opd_function_search_path.sql) (3,409 bytes)

Sets explicit `search_path` on all OPD RPCs (`SET search_path = public, pg_temp`), a Supabase security requirement for functions exposed via PostgREST.

### Checksum registration (run in Supabase SQL Editor)

```sql
INSERT INTO schema_migrations (name, checksum) VALUES
    ('103_opd_os_core.sql', '245febd7d0918ddad6468260c4c5248034851bf4b2a6ca60c93506f28092bf17'),
    ('104_opd_payment_exceptions.sql', '7f6fc045124ed64c24e214c5e133824b971bdd3c358493943a0a927e61fc1b9d'),
    ('105_opd_function_search_path.sql', '6292ecb04d0c5574755a701527d385906d1b896b830953f142982570a5623dcc')
ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;
```

---

## 3. Files Changed

31 modified, 34 new — ~14,835 insertions, ~9,359 deletions.

### 3.1 Backend — New Files

| File | What It Does |
|---|---|
| [`app/routers/opd.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/opd.py) | All OPD API routes: setup wizard, registry, queue, clinical workspace, prescriptions, billing, analytics, public display |
| [`app/services/opd.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/opd.py) | Core OPD service: patient search, MRN generation, walk-in creation, queue board with ETag caching, CAS stage transitions, setup checklist, go-live gating, display token |
| [`app/services/opd_billing.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/opd_billing.py) | Invoice lifecycle, receipt recording, shift management, payment link creation, refunds, drawer variance computation |
| [`app/services/opd_clinical.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/opd_clinical.py) | Vitals validation, SOAP notes with CAS autosave, amendment versioning, deterministic allergy warnings, signing invariants |
| [`app/services/opd_pdf.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/opd_pdf.py) | A5 portrait PDF generation with `fpdf2`, multilingual Indic script support (Devanagari, Telugu), clinic letterhead snapshots |
| [`admin/queue-display.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/queue-display.html) | Zero-PII hallway TV display with WebAudio chime, 5-second polling, display token authentication |
| [`admin/vendor/qrcode.min.js`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/vendor/qrcode.min.js) | Pinned QR code library for UPI QR generation on billing page |
| [`app/assets/fonts/`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/assets/) | NotoSans (Regular/Bold), NotoSansDevanagari, NotoSansTelugu + OFL.txt license |
| [`docs/opd-os/SPEC.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/opd-os/SPEC.md) | 142 KB design specification with 14 sections covering every OPD capability |
| [`docs/opd-os/status.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/opd-os/status.md) | Phase-by-phase execution tracker with test outputs, failure injection results, rollback procedures |

### 3.2 Backend — Modified Files

| File | What Changed |
|---|---|
| [`app/main.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/main.py) | Include OPD routers (admin + public), serve `queue-display.html` at `/public/queue-display`, serve QR library at `/panel-assets/qrcode.min.js` |
| [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) | `check_in_appointment` sets `initial_queue_status="waiting"`, `checked_in_at`, `queue_timeline`; walk-in exclusion in `get_available_slots` and `book_appointment` pre-check |
| [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py) | 7 tables added to `TENANT_OWNED_TABLES`: `opd_encounters`, `opd_prescriptions`, `opd_prescription_items`, `opd_invoices`, `opd_receipts`, `opd_shifts`, `opd_settings` |
| [`app/services/tenant.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/tenant.py) | `OPT_IN_FEATURES["opd_enabled"]`, `opd_enabled()`, `opd_eligible()` helpers |
| [`app/services/permissions.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/permissions.py) | `OPD_FRONT_DESK`, `OPD_CLINICAL`, `OPD_BILLING`, `OPD_ADMIN` permissions; `DOCTOR` and `CASHIER` staff role presets |
| [`app/routers/admin.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/admin.py) | `AdminUser.doctor_id`; `/admin/me` returns `opd_enabled`, `opd_state`, `opd_doctor_id`; doctor `registration_number`/`registration_council` on create/update; doctor FK guard on delete |
| [`app/routers/platform.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/platform.py) | OPD eligibility guard, `provision_defaults`, state transitions, `confirm_disable` with 409 preview, deactivation preview counts, `opd_purge_clinic` before clinic delete |
| [`app/services/payment.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/payment.py) | Walk-in exclusion in `create_booking_with_payment`; `create_opd_payment_link`; OPD invoice settlement in `process_payment_webhook` and `process_phonepe_webhook` |
| [`app/services/conversation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/conversation.py) | `booking_channel="whatsapp"` stamped on all WhatsApp bookings |
| [`app/services/dental_plans.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/dental_plans.py) | `booking_channel="front_desk"` stamped on dental plan sittings |
| [`app/services/scheduler.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/scheduler.py) | Walk-ins excluded from `send_24h_reminders`, `send_2h_reminders`, `check_doctor_leaves` |
| [`app/services/data_retention.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/data_retention.py) | Family members with OPD references redacted (not deleted); patient contact columns NULLed on erasure |
| [`app/utils/helpers.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/utils/helpers.py) | IST timestamp helpers for OPD receipt/invoice audit trails |
| [`app/voice/tools.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/voice/tools.py) | `booking_channel="voice"` stamped; type annotation fix for `_kb_task` (resolves pyright error on line 262); null-narrowing in `_create` |
| [`requirements.txt`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/requirements.txt) | Added `fpdf2` (PDF generation), `uharfbuzz` (Indic script shaping) |
| [`migrations/verify_supabase_schema.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/verify_supabase_schema.sql) | OPD tables, indexes, functions, and triggers verification queries |

### 3.3 Frontend

| File | What Changed |
|---|---|
| [`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html) | 6 new OPD pages inlined: **Setup Wizard** (`#pg-opdsetup`), **Front Desk** (`#pg-opdfrontdesk`), **Live Queue** (`#pg-opdqueue`), **Consultation Workspace** (`#pg-opdworkspace`), **Billing** (`#pg-opdbilling`), **Analytics** (`#pg-opdanalytics`). Nav entries gated by `opd_enabled` + staff permissions. OPD premium styles: glass dialogs, scoped badges, money formatting, shift KPI bar, reconciliation dialog, thermal receipt layout. 37 native `alert/confirm/prompt` replaced with panel glass dialogs. |
| [`admin/platform.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/platform.html) | OPD card with enable/disable toggle, deactivation preview dialog, directory pill, plan eligibility check |

### 3.4 Tests — New Files (20 files)

| File | Tests | Scope |
|---|---|---|
| [`tests/test_migration_103_opd.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_migration_103_opd.py) | 14 | Real PG: idempotency, cross-tenant FK, slot guards, record locking, gapless numbering, concurrent issues, receipt over-pay, shift guards, MRN uniqueness, RPCs, purge, rollback |
| [`tests/test_migration_104_opd_exceptions.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_migration_104_opd_exceptions.py) | — | Migration 104 verification |
| [`tests/test_opd_feature_gate.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_feature_gate.py) | 13 | Enterprise plan opt-in, partner lab rejection, `/admin/me` flags, deactivation preview, confirm disable, re-enable, staff/doctor links |
| [`tests/test_opd_registry.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_registry.py) | 11 | Patient search (patients + family + legacy), duplicate checks, DPDP consent, age calculation, demographics patch |
| [`tests/test_opd_queue.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_queue.py) | 8 | Transition matrix, 422 on forbidden, 409 CAS conflict, call-next auth, WhatsApp notification, ETag 304, recall |
| [`tests/test_opd_walkin_slots.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_walkin_slots.py) | 9 | Walk-ins don't consume slots, holiday/leave/inactive/nonexistent doctor rejection, cross-branch, scheduler exclusion |
| [`tests/test_opd_setup_wizard.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_setup_wizard.py) | 10 | All 14 checklist items, effective state machine, CAS settings, dry run, go-live gating, display token rotation |
| [`tests/test_opd_public_display.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_public_display.py) | 8 | Display token auth (header + query param), 401/404, zero-PII recursive audit, branch isolation, static HTML |
| [`tests/test_opd_data_retention.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_data_retention.py) | 2 | Patient erasure nulls all OPD fields, family member redact vs delete |
| [`tests/test_opd_tenant_isolation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_tenant_isolation.py) | 49 | Cross-clinic 403 on every OPD endpoint (setup/registry/queue/clinical/billing/analytics), super-admin scoping, branch guards |
| [`tests/test_opd_clinical.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_clinical.py) | 8 | Vitals bounds, BMI compute, notes autosave CAS 409, sign requires linked doctor, registration missing 422, amend versioning |
| [`tests/test_opd_prescriptions.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_prescriptions.py) | 8 | Draft replace, items locked after sign, allergy warnings, unknown allergy warning, sign refused until acknowledged, Rx sign requires signed encounter, send limit |
| [`tests/test_opd_pdf.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_pdf.py) | 5 | PDF `%PDF` header, registration number present, Devanagari/Telugu render, reprint shows original letterhead, draft refusal |
| [`tests/test_opd_no_llm.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_no_llm.py) | 2 | AST: OPD modules import nothing from AI/voice; no AI module imports OPD |
| [`tests/test_opd_billing.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_billing.py) | 15 | Consultation pricing, catalog pricing, draft CAS, sequential numbering, zero-total auto-paid, split payments, UPI ref required, idempotency, refund requires ADMIN, void blocked, one live invoice, prepaid receipt, shifts |
| [`tests/test_opd_payment_webhooks.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_payment_webhooks.py) | 6 | Razorpay/PhonePe webhook settlement, replay idempotency, amount mismatch, cross-tenant mismatch, void invoice, non-regression |
| [`tests/test_opd_payment_exceptions.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_payment_exceptions.py) | — | Payment exception handling |
| [`tests/test_opd_channels.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_channels.py) | 5 | Channel stamping (whatsapp/voice/front_desk), WhatsApp queue reply, voice queue_status, non-OPD unchanged |
| [`tests/test_opd_analytics.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_opd_analytics.py) | 4 | Range 422, row cap 422, reconciliation, RBAC gating |
| [`tests/test_checkin_token_nulls_last.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_checkin_token_nulls_last.py) | — | Token ordering fix: NULLS LAST instead of NULLS FIRST |

### 3.5 Tests — Modified Files

| File | What Changed |
|---|---|
| `tests/test_admin_super_admin_scope_matrix.py` | Added all `/admin/opd/*` routes to the scope matrix |
| `tests/test_phase2_route_adversarial_matrix.py` | Added all `/admin/opd/*` routes to the adversarial matrix |
| `tests/test_audit_offloop_and_api_contract.py` | OPD route count update |
| `tests/test_conversation_payment_mode.py` | Module isolation fix: fake `app.database` installed by fixture, not at import time |
| `tests/test_payment.py` | Module isolation fix + `booking_channel` field validation |
| `tests/test_corporate_partner_accounts.py` | Partner accounts cannot enable OPD |
| `tests/test_plan_features.py` | `opd_enabled` ∉ `ALL_FEATURES`, ∈ `OPT_IN_FEATURES` |
| `tests/test_platform_roster_and_plan_features.py` | OPD feature gate integration |

### 3.6 Documentation — Modified Files

| File | What Changed |
|---|---|
| [`docs/agent-context/03-DATABASE-MODEL.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/03-DATABASE-MODEL.md) | 7 OPD tables documented |
| [`docs/agent-context/04-API-MAP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/04-API-MAP.md) | All OPD endpoints documented |
| [`docs/agent-context/05-FRONTEND-MAP.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/05-FRONTEND-MAP.md) | 6 OPD nav pages + queue-display.html documented |
| [`docs/agent-context/08-AUTH-AND-MULTI-TENANCY.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/08-AUTH-AND-MULTI-TENANCY.md) | OPD permissions documented |
| [`docs/agent-context/13-KNOWN-ISSUES-AND-GAPS.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/13-KNOWN-ISSUES-AND-GAPS.md) | OPD gaps noted |

---

## 4. Architectural Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Booked-patient priority | `arrival` (FIFO after check-in) | Simpler for Indian OPD culture; patients expect order |
| Invoice year format | `calendar` (INV-2026-00001) | Matches financial year in most states |
| Data erasure strategy | `redact_in_place` | DPDP Act 2023 compliance; signed medical records kept with PII stripped |
| Gateway refunds | `manual_only` | OPD refunds are small cash amounts; gateway refund automation deferred |
| Names on TV display | `hidden` | Zero-PII hallway display; token numbers only |
| Abnormal vitals flags | Removed at owner request | Values displayed without clinical interpretation |

---

## 5. Verification

### Standing Gates (all passed)

| Gate | Command | Result |
|---|---|---|
| G1 — AST tenancy linter | `pytest tests/test_lint_unscoped_queries.py` | 4 passed in 66.80s (0 bare queries) |
| G2 — Security matrices | `pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py` | 674 passed in 11.24s |
| G3 — Clinical firewall | `pytest tests/test_clinical_firewall.py tests/test_opd_no_llm.py` | 34 passed |
| G4 — WhatsApp + Voice | `pytest tests/test_webhook.py tests/test_fsm_transitions.py tests/voice/` | 263 passed |
| G5 — Real Postgres | `pytest tests/test_real_postgres_invariants.py scripts/test_migration_runner.py` | 22 passed |
| G6 — Full suite | `pytest -q` | 4,188 passed, 5 skipped |

### Failure Injection (20/20 passed)

All 20 failure injection scenarios (FI-1 through FI-20) passed. See [`docs/opd-os/status.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/opd-os/status.md) §Failure injection for the full matrix.

### Static Typing

```bash
npx pyright app/voice/tools.py
# 0 errors, 0 warnings, 0 informations
```

### Legacy Non-Regression

```bash
git diff main -- tests/test_admin_queue.py tests/test_queue_database.py
# (empty — zero changes to legacy queue tests)
```

---

## 6. Rollback Procedures

Three escalation levels documented in [`docs/opd-os/status.md`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/opd-os/status.md) §Rollback:

- **R1 — Per-clinic switch-off** (seconds, no deploy): Owner console → untick OPD → 403 within 30s
- **R2 — Code revert** (minutes): Revert merge commit and redeploy; migration 103 is additive
- **R3 — Schema rollback** (destructive, owner approval required): `103_down.sql` with pre-condition checks

---

## 7. Bug Fixes Applied During Development

1. **`app/voice/tools.py` type error** (line 262): `_kb_task` lacked type annotation → pyright reported `"None" is not awaitable`. Fixed with `Optional[asyncio.Future[Any]]` annotation, `preload_knowledge()` now returns the future, and null-narrowing in `_create`.
2. **Walk-in `appointment_time` UTC bug**: Half-day leave check and walk-in time were UTC; pinned to IST.
3. **Token ordering**: `check_in_appointment` used `DESC NULLS FIRST` for max token → 6th check-in per doctor per day failed. Fixed to `nullsfirst=False`.
4. **Test module isolation**: `test_payment.py` and `test_conversation_payment_mode.py` installed fake `app.database` at import time, poisoning other tests in suite runs. Fixed to fixture-scoped installation with proper teardown.
5. **37 native dialogs**: All `alert()`, `confirm()`, `prompt()` calls in `admin/index.html` replaced with the panel's glass dialog system.
6. **OPD billing/analytics DOM placement**: Sections were outside `<main>` due to stray `</div>`; fixed to render inside layout.
7. **Queue board data shape mismatch**: UI read `columns`/`summary`, API returns `doctors[].rows`; aligned.
8. **10 billing writes as GET**: `api()` calls for shifts, invoices, receipts used GET → 405; fixed to proper POST/PUT/DELETE.
9. **Payment exception handling**: Over-payments, amount mismatches, and void-invoice webhook events now create `opd_payment_exceptions` rows instead of silently dropping money.

---

## 8. Post-Merge Deployment Steps

1. Apply migrations 103, 104, 105 on Supabase (already done)
2. Record checksums in `schema_migrations` (SQL in §2 above)
3. Verify: `SELECT name, checksum FROM schema_migrations WHERE name >= '103_' ORDER BY name;`
4. Deploy the new code
5. Enable OPD on the sandbox clinic from the platform console
6. Run the setup wizard and complete all 14 checklist items
7. Verify the TV display at `/public/queue-display?token=<display_token>`
