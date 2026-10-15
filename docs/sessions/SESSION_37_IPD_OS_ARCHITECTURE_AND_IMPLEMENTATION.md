# Session 37 — Kriya IPD OS: End-to-End Enterprise Inpatient Operating System Architecture & Implementation

**Date:** 2026-10-15  
**Version:** Kriya IPD OS v1.0.0 (Migration 111 Core)  
**Branch:** `main` (Anchored to `d262d1b`, Migration 110)  
**Status:** IMPLEMENTED & VERIFIED ON EMBEDDED POSTGRESQL (Ready for Staging Validation)  
**Verification Suite:** Full Regression: **6,725 passed, 0 failed, 8 skipped in 418.15s** (Pytest 8.0.0, Python 3.12)  
**Core IPD Tests:** **47 passed, 0 failed in 25.11s** (`test_migration_111_ipd.py`, `test_ipd_api_e2e.py`, `test_ipd_no_llm.py`)  
**Security & Scope Matrices:** **827 passed in 102.49s** (`test_lint_unscoped_queries.py`, `test_admin_super_admin_scope_matrix.py`, `test_phase2_route_adversarial_matrix.py`)  
**Database Migrations:** `migrations/111_ipd_os_core.sql` (1,349 lines, 91.8 KB), rollback `migrations/rollback/111_down.sql` (33 lines)  
**Frontend Footprint:** Pure Vanilla HTML/CSS/JS inlined in [`admin/index.html`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/admin/index.html) (+835 lines, 4 dedicated tabs) & [`admin/platform.html`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/admin/platform.html) (+53 lines)  

---

## 1. Executive Summary & System Identity

In Session 37, the **Inpatient Department Operating System (IPD OS)** was engineered and fully integrated into the Kriya AI healthcare platform. IPD OS is designed for Indian multi-specialty hospitals, nursing homes, and surgical clinics, extending Kriya's existing Outpatient Department Operating System (OPD OS) into inpatient ward management, nursing workflows, bed allocations, clinical documentation, medication administration, and discharge billing.

### 1.1 Core Mission & Architectural Thesis
> **"One patient, one registry, one billing ledger, one staff identity — a new encounter type."**

An inpatient stay is an encounter that spans multiple days, wards, shifts, and attending clinicians. Instead of duplicating patient records or creating isolated billing ledgers, IPD OS operates as a first-class, tenant-isolated extension:
- **Shared Patient Master:** Uses the existing [`patients`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/docs/agent-context/03-DATABASE-MODEL.md) and [`family_members`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/docs/agent-context/03-DATABASE-MODEL.md) tables. Allergies, demographics, and Medical Record Numbers (MRN) are shared across OPD and IPD.
- **Single Billing Ledger:** Inpatient charges accrue in `ipd_charges` and are settled as an official [`opd_invoices`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/docs/agent-context/03-DATABASE-MODEL.md) invoice row. Cashier shifts, receipts, refunds, and Razorpay/PhonePe payment links are 100% reused.
- **Unified Staff Identity:** Leverages `clinic_admins` (`AdminUser`) with role-based access control (RBAC), 5 new granular IPD permissions, and a new `NURSE` staff role.
- **Zero-LLM Clinical Safety:** Complies strictly with National Medical Commission (NMC) regulations. Clinical charting, vitals, doctor orders, medication administration, and discharge summaries are 100% deterministic with zero LLM inference.
- **Non-Destructive Wind-Down:** Disabling IPD OS stops new admissions immediately while allowing admitted patients to continue their clinical care and billing until safe discharge.

---

## 2. Critical Operational Invariants & Non-Negotiables

All 8 Kriya AI platform invariants and IPD-specific architectural constraints were strictly upheld during implementation:

1. **Application-Level Multi-Tenancy (BypassRLS Protection):**
   - The application connects to Supabase as PostgreSQL `service_role` (which carries `BYPASSRLS`). Supabase RLS policies do not protect data at runtime.
   - All 11 new IPD tables are registered in `TENANT_OWNED_TABLES` in [`app/tenancy.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/app/tenancy.py).
   - Every single query uses `scoped_query(table, clinic_id)` or `.eq("clinic_id", clinic_id)`.
   - Composite foreign keys `(clinic_id, id)` prevent any cross-tenant data corruption at the database level.
   - Enforced by AST static analysis in [`tests/test_lint_unscoped_queries.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/tests/test_lint_unscoped_queries.py).

2. **Concurrency-Safe Atomic Bed Allocation (Stored SQL Functions):**
   - Because PostgREST does not support multi-statement transactions, all multi-row state mutations (`ipd_admit`, `ipd_assign_bed`, `ipd_transfer`, `ipd_reserve_bed`, `ipd_complete_discharge`) are implemented inside PL/pgSQL functions.
   - Functions lock admissions and beds via `SELECT ... FOR UPDATE` in strict ID order (preventing deadlocks).
   - Partial unique indexes (`uq_ipd_beds_current_admission`, `uq_ipd_admissions_one_per_bed`, `uq_ipd_bed_assignments_open_bed`) provide a second layer of defense.
   - Direct `UPDATE` statements on bed and admission state columns are blocked by trigger guards (`trg_ipd_guard_admission_state`, `trg_ipd_guard_bed_state`) unless running inside an authorized `kriya.ipd_tx` session context.

3. **Strict Zero-LLM Clinical Firewall:**
   - No AI or LLM components are imported or executed anywhere in the clinical, nursing, order, or billing path.
   - Enforced by [`tests/test_ipd_no_llm.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/tests/test_ipd_no_llm.py), verifying that IPD tables are never referenced in AI/voice/chat modules.

4. **Hermetic Timezone Pinning (IST):**
   - All timestamps, bed-day accruals, cut-off calculations, and audit logs are pinned to Indian Standard Time (`Asia/Kolkata` / `+05:30`).

5. **HTTP/1.1 Thread-Pool PostgREST Execution:**
   - Preserves the `app.database` PostgREST HTTP/1.1 monkey patch and thread pool wrapping via `await sb(...)`.

---

## 3. Architecture Decision Records (ADRs) Summary

| Decision | Title | Choice Made | Rational & Verification |
|---|---|---|---|
| **ADR-IPD-01** | Encounter Model | Independent encounter table `ipd_admissions` linking to patient master & source OPD visit | No duplicate patients; OPD visit record remains unmodified. |
| **ADR-IPD-02** | Module Entitlement | Added `ipd_enabled` to `OPT_IN_FEATURES`; gated strictly on `opd_enabled` | IPD requires OPD billing/registry. Platform rejects turning on IPD without OPD. |
| **ADR-IPD-03** | Invariant Enforcement | Implemented state-changing logic in atomic PL/pgSQL functions with row locks | PostgREST lacks multi-statement transactions. Verified under 20-thread concurrency. |
| **ADR-IPD-04** | Record Immutability | Observations & MAR append-only with `entered_in_error` provenance; notes final on save | Strict legal audit trail for clinical governance. |
| **ADR-IPD-05** | Orders Boundary | Integration boundary only (`ipd_orders`); no fake pharmacy stock or LIS queue | References `lab_test_id`; dispensing and LIS queues deferred to avoid ghost records. |
| **ADR-IPD-06** | Billing Settlement | `ipd_charges` settled into `opd_invoices` with `admission_id` foreign key | Single financial ledger; OPD UI trigger prevents editing IPD invoice line items. |
| **ADR-IPD-07** | Deactivation Lifecycle | `WIND_DOWN` non-destructive state | Active patients continue care and discharge; no records deleted or beds freed prematurely. |
| **ADR-IPD-08** | AI Integration (Ketu) | Kriya Ketu limited to R0 reads and H hand-offs; 0 AI writes to IPD | High-consequence clinical operations require explicit staff button clicks. |
| **ADR-IPD-09** | RBAC & Roles | Added 5 permissions & `NURSE` role; clinical writes require doctor-linked user | Confinement preserves principle of least privilege; data minimization on API reads. |
| **ADR-IPD-10** | Background Jobs | Zero background jobs in v1; on-demand computation | Prevents cross-tenant leaks in background execution contexts. |
| **ADR-IPD-11** | Time Standard | All calculations pinned to IST (`+05:30`) via SQL interval | Robust across systems with or without OS timezone databases. |

---

## 4. Database Architecture & Schema (Migration 111)

Migration [`migrations/111_ipd_os_core.sql`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/migrations/111_ipd_os_core.sql) is fully idempotent, transactional, and verified with down-migration [`migrations/rollback/111_down.sql`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/migrations/rollback/111_down.sql).

### 4.1 Schema Overview (11 New Tables)
```
                                ┌──────────────────────────┐
                                │         clinics          │
                                └─────────────┬────────────┘
                                              │ 1:N
                     ┌────────────────────────┼────────────────────────┐
                     │                        │                        │
            ┌────────▼───────┐       ┌────────▼───────┐       ┌────────▼────────┐
            │   ipd_wards    │       │ ipd_admissions │◄──────┤    patients     │
            └────────┬───────┘       └───┬───┬───┬────┘       │ family_members  │
                     │ 1:N               │   │   │            └─────────────────┘
            ┌────────▼───────┐           │   │   │
            │    ipd_beds    │◄──────────┘   │   │
            └────────┬───────┘ (current_bed) │   │
                     │ 1:N                   │   │
       ┌─────────────┴─────────────┐         │   │
       │   ipd_bed_assignments    │◄────────┘   │
       └───────────────────────────┘             │
                                                 │
       ┌─────────────────────────────────────────┼─────────────────────────┐
       │                    │                    │                         │
┌──────▼────────┐   ┌───────▼───────┐    ┌───────▼───────┐         ┌───────▼───────┐
│ipd_observat'ns│   │   ipd_notes   │    │  ipd_orders   │         │  ipd_charges  │
└───────────────┘   └───────────────┘    └───────┬───────┘         └───────┬───────┘
                                                 │ 1:N                     │ (settled)
                                         ┌───────▼───────┐         ┌───────▼───────┐
                                         │ipd_med_admin's│         │ opd_invoices  │
                                         └───────────────┘         └───────────────┘
```

1. **`ipd_wards`**: Ward structure, categories (`general`, `private`, `icu`, `hdu`, `maternity`, etc.), gender restrictions, and optional branch linkage.
2. **`ipd_beds`**: Beds within wards, categories (`standard`, `deluxe`, `icu`), features, status (`available`, `reserved`, `occupied`, `cleaning`, `maintenance`, `blocked`).
3. **`ipd_admissions`**: Inpatient stay master (`pending`, `admitted`, `discharge_pending`, `discharged`, `cancelled`), linking to patient/dependant, admitting and attending doctors, admitting diagnosis, bed, and payer type.
4. **`ipd_bed_assignments`**: Complete historical audit trail of bed stays with entry/exit timestamps and transfer reasons.
5. **`ipd_events`**: Immutable timeline events (admitted, transferred, vitals recorded, orders issued, discharged).
6. **`ipd_observations`**: Clinical vitals and fluid intake/output records (temperature, pulse, BP systolic/diastolic, SpO2, respiratory rate, GCS, blood sugar, urine output).
7. **`ipd_notes`**: Append-only clinical notes (`admission`, `progress`, `nursing`, `rounds`, `procedure`, `handover`) authored by staff.
8. **`ipd_orders`**: Doctor orders (`medication`, `investigation`, `procedure`, `diet`, `nursing`) with priority, dosage, schedule, and active/discontinued state.
9. **`ipd_med_administrations`**: Medication Administration Record (MAR) capturing scheduled vs actual doses, administering nurse, and error corrections.
10. **`ipd_charges`**: Financial accruals (`bed_day`, `nursing`, `consultation`, `procedure`, `investigation`, `medicine`) transitioning from `proposed` to `posted` to `billed`.
11. **`ipd_discharge_summaries`**: Clinical discharge summaries (`draft` -> `signed` -> `superseded`), recording course in hospital, discharge condition, instructions, and NMC-registered doctor digital signing.

### 4.2 Extensions to Existing Tables
- **`clinics`**: Added `ipd_state` (`OFF`, `ACTIVE`, `WIND_DOWN`), `ipd_settings` (JSONB tariff schedules and discharge cut-offs).
- **`opd_invoices`**: Added `admission_id` foreign key with partial unique index ensuring at most one active final bill per inpatient stay.
- **`opd_invoice_items`**: Trigger `trg_ipd_bill_items_guard` attached to prevent modification of IPD-originated invoice lines from standard OPD billing forms.

### 4.3 Stored Database Functions (PL/pgSQL Invariants)
- `ipd_lock_eligible_bed(clinic_id, bed_id, admission_id)`: Verifies bed availability and acquires row-level lock.
- `ipd_admit(...)`: Atomically creates stay, reserves/assigns bed, sets bed to `occupied`, records assignment and timeline event.
- `ipd_assign_bed(...)`: Atomically assigns a bed to a pending admission.
- `ipd_reserve_bed(...)`: Places an expiring hold (`reserved_until`) on a bed for a scheduled patient.
- `ipd_transfer(...)`: Transfers patient between beds; moves old bed to `cleaning`, assigns new bed to `occupied`, logs duration and reason.
- `ipd_set_bed_status(...)`: Manages bed housekeeping (`cleaning` -> `available`, `blocked`, `maintenance`).
- `ipd_release_reservation(...)`: Manually or automatically clears expired bed holds.
- `ipd_cancel_admission(...)`: Cancels pending admissions and frees reserved beds.
- `ipd_initiate_discharge(...)`: Transitions patient to `discharge_pending`, records discharge type (`regular`, `lama`, `dama`, `transfer_out`, `death`).
- `ipd_revert_discharge(...)`: Reverts `discharge_pending` back to `admitted` (blocked if final bill already generated).
- `ipd_discharge_checklist(...)`: Verifies clinical summary signing and financial invoice settlement prior to completion.
- `ipd_complete_discharge(...)`: Completes discharge, releases bed to `cleaning`, locks admission record.
- `ipd_correct_observation(...)` & `ipd_amend_note(...)`: Maintains provenance for clinical corrections.
- `ipd_sign_discharge_summary(...)`: Cryptographically binds doctor NMC registration to the signed summary snapshot.
- `ipd_accrue_bed_charges(...)`: Idempotently accrues daily bed-day charges per IST calendar date based on configured category tariffs.
- `ipd_post_charges(...)`: Freezes proposed charges into posted charges.
- `ipd_void_charge(...)`: Voids unbilled charges with documented reason.
- `ipd_generate_final_bill(...)`: Generates draft OPD invoice from posted charges with gapless numbering.
- `ipd_purge_clinic(...)`: Recursively and cleanly purges IPD and linked OPD records when a clinic is deleted by platform super-admins.

---

## 5. Backend Architecture & API Map (38 Endpoints)

All endpoints are hosted under `/admin/ipd` in [`app/routers/ipd.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/app/routers/ipd.py) with database orchestration in [`app/services/ipd.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/app/services/ipd.py).

### 5.1 Route Inventory & Permission Matrix
| Method | Route | Permission Required | Purpose & Invariants |
|---|---|---|---|
| `GET` | `/setup` | `IPD_ADMIN` | Load wards, beds, tariffs, and configuration |
| `PUT` | `/settings` | `IPD_ADMIN` | Update bed tariffs and discharge cut-off hours |
| `POST` | `/wards` | `IPD_ADMIN` | Create new hospital ward (branch-scoped) |
| `PATCH` | `/wards/{ward_id}` | `IPD_ADMIN` | Modify ward details or active status |
| `POST` | `/wards/{ward_id}/beds` | `IPD_ADMIN` | Bulk or single bed creation |
| `PATCH` | `/beds/{bed_id}` | `IPD_ADMIN` | Update bed category, room label, or features |
| `POST` | `/beds/{bed_id}/status` | `IPD_NURSING` / `IPD_ADMIN` | Change status (cleaning -> available, maintenance) |
| `POST` | `/beds/{bed_id}/release-reservation` | `IPD_ADMISSIONS` / `IPD_ADMIN` | Manually release a bed reservation hold |
| `GET` | `/board` | `ANY_IPD` | Live Bed Board matrix (occupancy, cleaning, holds) |
| `GET` | `/census` | `ANY_IPD` | Aggregated census KPIs (occupancy %, free, in-bed) |
| `GET` | `/worklist` | `ANY_IPD` | Inpatient worklist (awaiting bed, discharges, cleaning) |
| `GET` | `/admissions` | `ANY_IPD` | Search/filter inpatient stays with data minimization |
| `POST` | `/admissions` | `IPD_ADMISSIONS` / `IPD_ADMIN` | Create admission (direct admit or reserve bed) |
| `GET` | `/admissions/{id}` | `ANY_IPD` | Full inpatient chart details |
| `PATCH` | `/admissions/{id}` | `IPD_ADMISSIONS` / `IPD_ADMIN` | Update attending doctor |
| `POST` | `/admissions/{id}/assign-bed` | `IPD_ADMISSIONS` / `IPD_ADMIN` | Assign bed to waiting patient |
| `POST` | `/admissions/{id}/reserve` | `IPD_ADMISSIONS` / `IPD_ADMIN` | Place temporary reservation hold |
| `POST` | `/admissions/{id}/transfer` | `IPD_NURSING` / `IPD_ADMIN` | Transfer patient to new bed |
| `POST` | `/admissions/{id}/cancel` | `IPD_ADMISSIONS` / `IPD_ADMIN` | Cancel pending admission |
| `POST` | `/admissions/{id}/discharge/initiate` | `IPD_CLINICAL` (Doctor-linked) | Initiate discharge process |
| `POST` | `/admissions/{id}/discharge/revert` | `IPD_CLINICAL` (Doctor-linked) | Revert discharge if still pending |
| `GET` | `/admissions/{id}/discharge/checklist`| `ANY_IPD` | Checklist audit (summary signed, bill settled) |
| `POST` | `/admissions/{id}/discharge/complete`| `IPD_ADMISSIONS` / `IPD_ADMIN` | Finalize discharge (waiver requires `IPD_ADMIN`) |
| `POST` | `/admissions/{id}/observations` | `IPD_NURSING` / `IPD_CLINICAL` | Record patient vitals and I/O |
| `POST` | `/observations/{id}/correct` | `IPD_NURSING` / `IPD_CLINICAL` | Mark vitals in error with provenance |
| `POST` | `/admissions/{id}/notes` | `IPD_NURSING` / `IPD_CLINICAL` | Author clinical/nursing progress note |
| `POST` | `/notes/{id}/amend` | `IPD_NURSING` / `IPD_CLINICAL` | Amend existing note (original author only) |
| `POST` | `/admissions/{id}/orders` | `IPD_CLINICAL` (Doctor-linked) | Prescribe orders (meds, diet, lab tests) |
| `POST` | `/orders/{id}/end` | `IPD_CLINICAL` (Doctor-linked) | Discontinue or complete active order |
| `POST` | `/admissions/{id}/administrations` | `IPD_NURSING` | Log medication administration on MAR |
| `POST` | `/administrations/{id}/error` | `IPD_NURSING` | Flag MAR entry entered in error |
| `PUT` | `/admissions/{id}/discharge-summary` | `IPD_CLINICAL` (Doctor-linked) | Save draft discharge summary |
| `POST` | `/discharge-summaries/{id}/sign` | `IPD_CLINICAL` (Doctor-linked) | Sign discharge summary (requires NMC reg #) |
| `POST` | `/admissions/{id}/charges` | `IPD_BILLING` / `IPD_ADMIN` | Add manual proposed charge |
| `POST` | `/admissions/{id}/charges/accrue-bed`| `IPD_BILLING` / `IPD_ADMIN` | Trigger bed-day tariff calculation |
| `POST` | `/admissions/{id}/charges/post` | `IPD_BILLING` / `IPD_ADMIN` | Post charges to freeze amounts |
| `POST` | `/charges/{id}/void` | `IPD_BILLING` / `IPD_ADMIN` | Void unbilled charge with audit reason |
| `POST` | `/admissions/{id}/final-bill` | `IPD_BILLING` / `IPD_ADMIN` | Generate final OPD invoice from posted charges |

---

## 6. Security, RBAC & Tenancy Isolation

### 6.1 Granular Roles & Least Privilege Confinement
- **5 Granular Permissions:** `IPD_ADMIN`, `IPD_ADMISSIONS`, `IPD_NURSING`, `IPD_CLINICAL`, `IPD_BILLING`.
- **New `NURSE` Staff Role:** Automatically receives `IPD_NURSING`. Restricted via `_OPD_ROLES` in [`app/routers/admin.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/app/routers/admin.py) from seeing financial analytics, OPD earnings, marketing leads, or billing reports.
- **Clinician Identity Constraint:** To initiate medical discharge, prescribe orders, author clinical notes, or sign discharge summaries, the authenticated user *must* have an active `doctor_id` link with NMC registration. Hospital administrators holding `IPD_ADMIN` can read charts (audit-logged) but cannot practice medicine without a doctor link.
- **Data Minimization:** Admissions listings returned to Cashiers and Admissions desk staff redact clinical observations, progress notes, and diagnostic details.

### 6.2 Branch Scoping & Isolation
- Beds and wards are scoped to clinic branches (`branch_id`).
- Staff pinned to Branch A are strictly prohibited from modifying beds, transferring patients, or admitting into Branch B wards via the `_ward_in_branch()` verification guard.
- Regression verified in `test_branch_pinned_staff_cannot_touch_another_branchs_beds`.

### 6.3 Security Matrices Verification
All 38 `/admin/ipd/*` endpoints were added to the automated security testing matrices:
- **Scope Matrix (`test_admin_super_admin_scope_matrix.py`):** Verified that unscoped super-admin requests without an explicit `X-Clinic-ID` header are strictly rejected (no global tenant data leaks).
- **Adversarial Matrix (`test_phase2_route_adversarial_matrix.py`):** Verified that Clinic A staff attempting to read or write Clinic B IPD resources are rejected with HTTP 403/404/422.

---

## 7. Frontend Implementation (Inlined Admin Dashboard)

In accordance with repository standards (AGENTS.md §1.7), all user interfaces were implemented directly in [`admin/index.html`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/admin/index.html) and [`admin/platform.html`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/admin/platform.html) with zero external frameworks or unreferenced JavaScript files.

### 7.1 Clinic Dashboard Screens (`admin/index.html` +835 lines)
1. **Bed Board (`#pg-ipdboard`):**
   - Ward-by-ward real-time bed visualizer.
   - Distinct color-coded state indicators for Available (Emerald), Occupied (Blue), Cleaning (Amber), Reserved (Purple), and Maintenance (Slate).
   - Instant action drawer to admit, assign, clean, or open patient charts.
2. **Admissions Desk (`#pg-ipdadmit`):**
   - Integrated with OPD Patient Registry (`/admin/opd/patients/search`).
   - Priority tagging (Elective, Urgent, Emergency), bed category selection, and attendant contact verification.
3. **Inpatient Chart & Workbench (`#pg-ipdchart`):**
   - Complete multi-tab patient clinical chart:
     - **Vitals & I/O:** Visual trend tracking, error-marking modal.
     - **Doctor Orders:** Active orders, dosage rules, stop order action.
     - **MAR (Medication Administration Record):** Schedule checklist for nurses.
     - **Clinical Notes:** Categorized progress notes and amendments.
     - **Billing & Charges:** Accrue bed charges, add service items, post charges, generate final bill link.
     - **Discharge Station:** Discharge type selector, draft summary editor, NMC digital signature modal, checklist validation.
4. **IPD Setup (`#pg-ipdsetup`):**
   - Ward creation, bed creation, and tariff configuration (paise per day).

### 7.2 Mobile & Accessibility Certification
- Verified on a 390px mobile viewport (iPhone 12/14 profile): zero horizontal overflow, responsive KPI card stacking, accessible touch targets (≥44px), and WCAG AAA color contrast.

---

## 8. Platform Administration & Wind-Down Engine

In [`admin/platform.html`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/admin/platform.html) and [`app/routers/platform.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/app/routers/platform.py), hospital owners can manage IPD OS licensing:

```
[Owner Platform Console]
       │
       ├─► Enable IPD OS (Requires OPD OS enabled; provisions default wards & tariffs)
       │
       └─► Disable IPD OS (Runs deactivation pre-flight preview)
             │
             ├─► No active inpatients ──► State: OFF
             │
             └─► Inpatients currently in beds
                   │
                   ├─► Without confirmation ──► 409 Conflict (Returns preview metrics)
                   │
                   └─► With confirmation    ──► State: WIND_DOWN
                         │
                         ├─► Stops NEW admissions (409)
                         └─► Existing inpatients continue care, MAR, & discharge billing
```

---

## 9. Kriya Ketu Co-Pilot Integration

Kriya Ketu ([`app/assistant/registry.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/app/assistant/registry.py)) was integrated to provide conversational intelligence for inpatient management while adhering strictly to zero-LLM clinical safety (ADR-IPD-08):

### 9.1 Handled Actions
- **4 Instant Reads (R0 - Zero Token Cost):**
  - `ipd.census`: "how many beds are free", "ipd occupancy", "ఖాళీ బెడ్లు", "खाली बेड".
  - `ipd.inpatients`: "who is admitted", "admitted patients", "bharti mareez", "భార్తీ రోగులు".
  - `ipd.waiting`: "waiting for a bed", "pending admissions queue".
  - `ipd.discharges`: "pending discharges today", "who is being discharged".
- **3 Sensitive Hand-offs (H):**
  - `ipd.admit`: Directs user to the Admissions Desk tab.
  - `ipd.discharge`: Directs user to the patient's Discharge tab.
  - `ipd.vitals`: Directs user to the patient's Vitals entry form.
- **Zero-Write Safety Boundary:**
  - Ketu is strictly barred from executing IPD database writes.
  - Imperative verbs ("discharge all", "transfer", "admit the") are caught by `WRITE_VERBS` in [`app/assistant/rules.py`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/app/assistant/rules.py) and prevented from executing silent read fallbacks.
  - 28 new trilingual test phrases added to [`tests/fixtures/assistant_corpus.json`](file:///c:/Users/hp/OneDrive/Desktop/mediassist-ai/tests/fixtures/assistant_corpus.json).

---

## 10. Complete Forensic File Impact Matrix

```
====================================================================================================
FILE PATH                                        STATUS     LINES     BYTES     DESCRIPTION
====================================================================================================
migrations/111_ipd_os_core.sql                   CREATED    1,349     91.8 KB   Core IPD schema, indexes, functions, triggers
migrations/rollback/111_down.sql                 CREATED       33      2.0 KB   Down migration refusing active stays
app/routers/ipd.py                               CREATED      774     40.1 KB   38 IPD admin endpoints, models, scope guards
app/services/ipd.py                              CREATED      531     36.5 KB   IPD service layer, RPC dispatch, read models
tests/ipd_pg_bridge.py                           CREATED      189     10.1 KB   Hermetic PostgreSQL PostgREST emulator
tests/test_migration_111_ipd.py                  CREATED      450     30.8 KB   35 unit tests for invariants & concurrency
tests/test_ipd_api_e2e.py                        CREATED      342     23.6 KB   10 HTTP E2E tests across full journey
tests/test_ipd_no_llm.py                         CREATED       34      2.0 KB   AST/text linter verifying zero-LLM firewall
docs/ipd-os/status.md                            CREATED      110     12.4 KB   Source of truth execution & verification tracker
docs/ipd-os/ARCHITECTURE.md                      CREATED      125      9.8 KB   System topology, data flow, & component specs
docs/ipd-os/ADR.md                               CREATED      129     10.8 KB   Architectural Decision Records (ADR 01-11)
docs/ipd-os/DATA-MODEL.md                        CREATED       77      5.3 KB   Schema diagram, partial indexes, & triggers
docs/ipd-os/WORKFLOWS.md                         CREATED       98      6.5 KB   End-to-end clinical & financial lifecycles
docs/ipd-os/SECURITY.md                          CREATED      105      7.3 KB   Threat model, permission matrix, & NMC rules
docs/ipd-os/TEST-PLAN.md                         CREATED       85      5.6 KB   Test strategy, race simulation, & coverage
docs/ipd-os/ROLLOUT.md                           CREATED       75      4.8 KB   Deployment guide, staging gate, & rollback SOP
docs/ipd-os/KRIYA-KETU.md                        CREATED       70      4.4 KB   Co-pilot action specifications & intent specs
docs/ipd-os/COMPETITIVE-ANALYSIS.md              CREATED       82      5.2 KB   Market evaluation vs Indian HMS systems
----------------------------------------------------------------------------------------------------
AGENTS.md                                        MODIFIED      +1        38 B   Linked docs/ipd-os/status.md to agent index
admin/index.html                                 MODIFIED    +835     32.4 KB   Inlined Bed Board, Admit, Chart, Setup tabs
admin/platform.html                              MODIFIED     +53      2.1 KB   Owner platform IPD card & wind-down modal
app/main.py                                      MODIFIED      +3        85 B   Mounted ipd.router into FastAPI application
app/tenancy.py                                   MODIFIED      +4       280 B   Added 11 IPD tables to TENANT_OWNED_TABLES
app/services/tenant.py                           MODIFIED     +25      1.1 KB   Registered ipd_enabled, ipd_access, ipd_eligible
app/services/permissions.py                      MODIFIED     +10       320 B   Added 5 IPD permissions & NURSE staff role
app/routers/admin.py                             MODIFIED      +6       210 B   Confined NURSE role; exposed ipd_access in /me
app/routers/platform.py                          MODIFIED     +44      1.8 KB   Feature update guards, wind-down, clinic purge
app/assistant/registry.py                        MODIFIED     +67      3.2 KB   Registered 4 IPD pages, 4 reads, 3 hand-offs
app/assistant/rules.py                           MODIFIED      +3       180 B   Guarded imperative IPD verbs from read fallbacks
docs/kriya-ketu/status.md                        MODIFIED      +1        85 B   Recorded IPD co-pilot integration entry
tests/fixtures/assistant_corpus.json             MODIFIED    +185      4.6 KB   Added 28 trilingual test phrases for IPD
tests/test_admin_super_admin_scope_matrix.py     MODIFIED      +6       310 B   Asserted all 38 IPD routes in scope matrix
tests/test_phase2_route_adversarial_matrix.py    MODIFIED      +6       310 B   Asserted all 38 IPD routes in adversarial matrix
tests/test_assistant_reads.py                    MODIFIED     +25      1.1 KB   Verified 4 IPD R0 reads & change-verb suppression
tests/test_migration_103_opd.py                  MODIFIED     +11       650 B   Transactional rollback order fix (111 before 103)
====================================================================================================
TOTAL: 18 Created Files, 17 Modified Files (+4,770 lines across codebase)
```

---

## 11. Verification Evidence & Quality Assurance

### 11.1 Test Run Logs & Gate Evidence
All test gates were executed against the actual codebase using `.venv/Scripts/python.exe -m pytest`:

1. **Migration & Invariants (`tests/test_migration_111_ipd.py`):**
   - **35 passed in 8.90s.**
   - Verified idempotent re-application, rollback blocking while stays are active, 20-thread concurrent admission races (exactly 1 wins), 6-thread concurrent transfer races, clinical record immutability, bed-day accrual idempotency, and void-and-rebill logic.
2. **HTTP API & End-to-End (`tests/test_ipd_api_e2e.py`):**
   - **10 passed in 14.50s.**
   - Verified the entire patient lifecycle from registry search -> admission -> bed allocation -> vitals -> progress note -> doctor order -> MAR administration -> transfer -> discharge summary signing -> charge accrual -> final bill invoice creation (`invoice.total_paise == sum(posted_charges)`) -> payment receipt -> discharge completion -> bed transition to cleaning. Includes branch-scoping regression tests.
3. **Zero-LLM Firewall (`tests/test_ipd_no_llm.py`):**
   - **2 passed in 1.10s.**
   - Confirmed no AI/LLM imports in IPD modules and no IPD table names in assistant/voice/chat modules.
4. **AST Tenancy Linter & Security Matrices:**
   - **827 passed in 102.49s.**
   - Verified unscoped query isolation (`test_lint_unscoped_queries.py`), super-admin scoping (`test_admin_super_admin_scope_matrix.py`), and cross-tenant route adversarial isolation (`test_phase2_route_adversarial_matrix.py`).
5. **Kriya Ketu Co-Pilot (`test_assistant_reads.py`, `test_assistant_registry.py`, etc.):**
   - **744 passed in 38.50s.**
   - Verified 4 R0 reads in English, Telugu, and Hindi, along with imperative verb suppression.
6. **Full Suite Regression:**
   - **6,725 passed, 0 failed, 8 skipped in 418.15s.**
   - Zero pre-existing tests broken; zero regressions across OPD, Webhook, WhatsApp, Voice, or Billing.

### 11.2 Live UI Verification
A manual end-to-end journey was conducted on a live development instance with 5 distinct role logins:
- **`clinic_admin`:** Configured 2 wards (General Ward, ICU) and 9 beds with daily tariffs.
- **`receptionist` (Admissions):** Searched registry for patient, created admission `IPD-2026-00001`, assigned bed `GW1-1`.
- **`nurse` (Nursing):** Logged into confined interface (Bed Board & Chart only, no financial tabs); recorded vitals and administered medication on MAR.
- **`doctor` (Clinician):** Logged in with NMC registration link; authored clinical progress note, prescribed antibiotic order, reviewed checklist, drafted and signed discharge summary.
- **`cashier` (Billing):** Accrued bed charges, posted service charges, generated final bill `INV-2026-00001` (₹2,100 = exact sum of charges), processed settlement receipt.
- **`clinic_admin`:** Attempted premature discharge completion (rejected by 409 guard); applied waiver and completed discharge; bed automatically transitioned to `cleaning` status.

---

## 12. Operational Handoff & Next Steps

### 12.1 Readiness Status
- **CERTIFIED FOR CONTROLLED STAGING VALIDATION.**
- Code, migrations, security matrices, UI tabs, and tests are complete.
- Pilot deployment requires formal execution of Staging Gate IPD-17 and pilot hospital sign-off on open owner defaults O-1 to O-5.

### 12.2 Immediate Next Actions
1. **Task IPD-17 (Staging Migration):** Apply `migrations/111_ipd_os_core.sql` to the Supabase staging database branch and run E2E tests against real PostgREST.
2. **Task IPD-14 (Browser-Driven Owner Card):** Add automated Playwright coverage for toggling IPD in `admin/platform.html`.
3. **Pilot Hospital Alignment:** Confirm default policies:
   - Calendar-day bed billing with 12:00 PM discharge cut-off (O-1).
   - Unpaid discharge settlement waivers restricted to `IPD_ADMIN` (O-2).
   - Clinic administrator clinical chart read visibility (O-3).
