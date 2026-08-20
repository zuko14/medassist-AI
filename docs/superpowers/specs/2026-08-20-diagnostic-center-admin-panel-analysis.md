# Kriya AI — Diagnostic Center Admin Panel
## Architecture Analysis, Production Requirements & Implementation Plan

**Status:** Analysis only. No implementation has begun. This document is the deliverable requested — do not treat any section as a changelog.

**Ground truth used:** actual repository at commit `990f2e7` (main branch), actual migrations 001–036, actual `admin/index.html`, actual `app/routers/admin.py`, `app/services/tenant.py`, `app/services/permissions.py`, `connectors/`, `app/integrations/callmedex/`, `Dockerfile`, `railway.toml`. Not the screenshots. Every claim below is either a direct code citation (`file:line`) or explicitly marked as an inference/unknown in Section AA.

---

## A. Executive Assessment

The screenshots' complaint — "the Diagnostic Center admin panel looks like a generic clinic panel" — is **real but narrower than it looks**. Kriya AI already has:

- A working business-type dimension (`clinics.plan`, with `diagstream` as the Diagnostic Center tier)
- A working feature-flag registry (`PLAN_FEATURES` in `app/services/tenant.py`)
- Partial `data-feature` gating already wired into `admin/index.html`
- A real, functioning (if fragile and currently non-scheduled) browser-automation connector system for MocDoc report ingestion, with encrypted credentials, branch scoping, audit logs, and per-report failure tracking already in the database and API

What's actually missing is narrower and more fixable than "build a whole new admin system":

1. Four nav items (`Hospital Profile`, `Patients`, `Staff`, generic `Dashboard`) have **no feature gate at all** — they render for every plan including `diagstream`, which is the direct, literal cause of "why does a diagnostics tenant see hospital-shaped things."
2. The one thing a Diagnostic Center tenant actually cares about — **is my automation working, what got sent, what failed, who do I call** — has no dashboard. The data exists (`connector_audit_log`, `connector_failed_reports`, `lab_reports`) but there's no screen built around it.
3. The automation that's supposed to make this whole thing "hands-off" **is not deployed in production** — `connectors/runner.py` (the APScheduler polling loop) is never started by `Dockerfile` or `railway.toml`. Every "automation health" claim on any future dashboard would be fiction until this is fixed.
4. There is **no patient-matching/validation layer** between "what MocDoc's UI says the phone number is" and "who WhatsApp sends the PDF to." This is the single most important finding relative to the user's explicit "never send a report to the wrong patient" requirement — that safety currently rests entirely on MocDoc's own data quality, with zero MediAssist-side cross-check.

There is **no second connector system to build**. There are two *existing, unrelated* systems already using the word "connector" (Section C), and the plan must extend the first (System A) without touching or duplicating the second (System B / CallMedex).

Recommended posture: this is **not a rebuild**. It's (a) close the nav-gating gap, (b) build the operational dashboard the automation data already supports, (c) actually deploy the scheduler, (d) add the missing patient-safety layer, (e) upgrade `sent`/`failed` into a real state machine, (f) add branch/RBAC/observability pieces that are genuinely absent. Total new surface area is moderate; most of the hard infrastructure (encryption, branch scoping, idempotency, audit logging) is already built and just needs to be surfaced and hardened.

---

## B. Existing Architecture (Business-Type / Multi-Tenancy Ground Truth)

**There is no `business_type` column anywhere in the schema.** The closest real concept is `clinics.plan` (`migrations/003_multi_tenant.sql`, constraint updated in `016_fix_plan_constraint.sql`):

```sql
plan TEXT CHECK (plan IN ('soloclinic','diagstream','essential','polyclinic','enterprise'))
```

`diagstream` is the Diagnostic Center plan. There is also `clinics.features JSONB` (migration `006_alter_clinics_plan.sql`) for per-clinic overrides layered on top of the plan default.

The actual feature-gating mechanism is `app/services/tenant.py`:

```python
PLAN_FEATURES: dict[str, set[str]] = {
    "soloclinic": {...},
    "diagstream": {...},   # includes "lab_reports"
    "essential": {...},
    "polyclinic": {...},
    "enterprise": {...},
}

def has_feature(clinic: dict, feature: str) -> bool: ...
def require_feature(clinic: dict, feature: str) -> None: ...   # raises 403
ALL_FEATURES = sorted({f for s in PLAN_FEATURES.values() for f in s})
```

`GET /admin/me` (`app/routers/admin.py:~280-320`) returns `{username, role, clinic_id, permissions, branch_id, staff_role, plan, features}`. The frontend loads this into a global `myFeatures` array and calls `applyFeatureVisibility()` at boot, which walks every `[data-feature]` element and hides ones the clinic's plan doesn't grant.

**This is a real, working system already.** It is not comprehensive (Section D), but the plan does not need to invent business-type awareness — it needs to *finish applying* the mechanism that already exists, and add a `diagnostic_reports` (or equivalent) feature key to drive the new nav section.

A second, unrelated flag exists: `branches.is_diagnostic` (migration `010`) — a per-branch boolean meaning "this branch does lab-reports/pricing only, no appointment booking," consumed by the WhatsApp bot's branch-selection flow (`app/services/conversation.py`). This is orthogonal to `clinics.plan` and must not be confused with it in the new design — a `polyclinic` tenant can have one `is_diagnostic=true` branch without being a `diagstream` clinic.

---

## C. Existing Diagnostic Center Implementation (as it runs today)

### C.1 — Two unrelated "connector" systems (critical distinction)

**System A — the real Diagnostic Center automation** (`integration_connectors` + `connectors/` package):
- Tables: `integration_connectors`, `integration_processed_reports`, `connector_audit_log`, `connector_failed_reports` (migrations `009`, `012`, `025`)
- `HospitalConnector(ABC)` base class (`connectors/base.py`), concrete `MocDocConnector` (`connectors/mocdoc/worker.py`, 1111 lines) — Playwright-driven login, "Pending Print" table scrape, per-row parse, download, submit
- Orchestrated by `connectors/runner.py` — a **standalone CLI/scheduler script**, not part of the FastAPI app process
- Admin-facing CRUD already exists: `GET/PUT /connectors`, `POST /connectors/{id}/toggle`, `GET /connectors/{id}/audit-log`, `GET /connectors/failed-reports`, `POST /connectors/failed-reports/{id}/resolve` (`app/routers/admin.py:2367-2624`)
- Credentials encrypted at rest with Fernet (`app/utils/connector_crypto.py`, shared by both the admin write path and the runner's read path)
- Branch-scoped since migration `025`: `branch_id` nullable FK on all four tables, dual partial unique indexes for "clinic-wide connector" vs "one connector per branch"

**System B — CallMedex** (`app/integrations/callmedex/`):
- An entirely separate, adjacent B2B integration for a **different platform** ("CallMeDex" — online diagnostic booking + home sample collection), not a Kriya AI diagnostic-center tenant feature
- Own API surface, `include_in_schema=False`, Bearer/`X-Integration-Secret` + mandatory-in-prod HMAC-SHA256 signature + 5-minute replay window + IP rate limiting (`app/integrations/callmedex/api/router.py`)
- Own async queue engine, started/stopped inside `app/main.py`'s FastAPI lifecycle (unlike System A's out-of-process runner)
- Internally organized with its own `connectors/` sub-package (MocDoc/CloudLIMS/Crelio) and its own OCR/summarization pipeline — this is CallMedex servicing **its own processing centers**, not infrastructure a Kriya AI admin configures

**Why this distinction matters for the plan:** the user's spec (Sections 3, 17) explicitly warns against building a second, duplicate connector system. The risk isn't hypothetical — this repo already contains two things called "connector" for unrelated businesses. The plan in this document extends **System A only**. CallMedex is out of scope and must not be touched, merged, or exposed to ordinary diagnostic-center tenants. Where CallMedex's code demonstrates a genuinely reusable *pattern* (its `ConnectorFactory` abstraction, Section J), the plan borrows the pattern, not the code or the tables.

### C.2 — What actually happens today, end to end

1. Clinic admin (with `lab_reports` feature) opens the connector settings screen (exists in `admin/index.html` today, not yet inventoried line-by-line — see Section AA) and saves MocDoc username/password via `PUT /connectors`. Password is Fernet-encrypted before storage.
2. **Nothing polls it.** `connectors/runner.py --all` (or `--connector mocdoc`) is a CLI script. Neither `Dockerfile`'s `CMD` nor `railway.toml`'s `startCommand` invoke it — both only run `uvicorn app.main:app`. There is no second Railway service, no cron, no supervisor process declared anywhere in the repo. **The MocDoc automation is dormant in production today** unless someone is manually SSH'ing in and running the script, which is not a sustainable operational model and contradicts any "automation health" UI.
3. *If* run (manually or via a process not visible in this repo), `run_connector()` decrypts credentials, instantiates `MocDocConnector`, logs into MocDoc, opens "Pending Print," scrapes each row via regex (`_parse_patient_cell`, `_parse_test_details`), builds `ReportMetadata(patient_name, patient_phone, ...)` **directly from MocDoc's own displayed phone number**, downloads the PDF, and calls `submit_to_medassist()`.
4. `submit_to_medassist()` → `POST /internal/integrations/lab-report` (`app/routers/integrations.py`), authenticated via `X-Integration-Secret`, checked against `integration_processed_reports` for idempotency, then delegates to `LabReportService.upload_and_send()`.
5. `upload_and_send()` (`app/services/lab_reports.py`): cross-path idempotency check against `lab_reports.external_report_id` (fail-open on error) → extract PDF text → AI summary (Groq) → upload to Supabase Storage → WhatsApp: text summary, then PDF document → insert `lab_reports` row with `status: "sent" | "failed"`.
6. `run_connector()` writes one row to `connector_audit_log` per poll cycle (counts: found/uploaded/failed), and on a report-level failure writes/increments `connector_failed_reports`, escalating to an admin WhatsApp alert after a threshold (`send_admin_alert()`).

### C.3 — Report status model today

`lab_reports.status` is a **binary field, computed once, synchronously**, inside a single `upload_and_send()` call: `"sent"` or `"failed"`. There is no `queued`, `downloading`, `processing`, `pending_match`, `pending_review`, or `retrying` state. A report that fails partway (e.g., storage upload OK, WhatsApp send fails) still resolves to exactly one terminal row. This is not a state machine — it's a single-attempt outcome flag. Retries exist only via System A's `connector_failed_reports` counter + manual re-run of the connector, and via `LabReportService.resend_report()` (admin-triggered, re-downloads from Storage, re-sends).

---

## D. Confirmed Problems

Each item below is a direct finding, not a generic assumption, with its evidence.

| # | Problem | Evidence | Severity |
|---|---|---|---|
| D1 | Four nav items ungated by plan/feature: `profile` ("Hospital Profile"), `patients`, `staff`, generic `dashboard` | `admin/index.html:795-839` — these entries have no `data-feature` attribute, only `data-role="admin"` at most | High — this is the literal, direct cause of the screenshot complaint |
| D2 | No Diagnostic Center-specific dashboard exists | No route/section in `admin/index.html` built around `connector_audit_log`/`connector_failed_reports`/`lab_reports` | High |
| D3 | Automation scheduler not deployed in production | `Dockerfile` and `railway.toml` both only start `uvicorn`; `connectors/runner.py` is never invoked | Critical — undermines every "hands-off automation" claim |
| D4 | No patient-matching/validation layer | `MocDocConnector.fetch_new_reports()` (`connectors/mocdoc/worker.py`) parses phone directly from MocDoc's table cell via regex and passes it straight through; `LabReportService.upload_and_send()` never cross-checks against MediAssist's own `patients` table | Critical — directly contradicts the user's "never send to wrong patient" requirement |
| D5 | Binary status, not a real lifecycle | `lab_reports.status IN ('sent','failed')` only, computed in one synchronous pass (Section C.3) | Medium-High |
| D6 | No distributed lock / overlap protection | `run_all_connectors()` (`connectors/runner.py`) loops sequentially with a fixed 2s delay, no lock table, no leader election — two runner processes (e.g. during a bad deploy overlap) could scrape the same clinic concurrently | Medium (currently masked by D3 — nothing is running at all, but becomes real the moment D3 is fixed) |
| D7 | No RBAC permissions for report/connector actions | `app/services/permissions.py` `PERMISSIONS` frozenset has 11 entries, none related to connectors, reports, or failed-report resolution — today only `role in ("super_admin","clinic_admin")` can touch connector endpoints (`require_admin`), no delegation possible | Medium |
| D8 | Ambiguous/missing-data rows are silently dropped, not queued for review | `fetch_new_reports()` skips rows with missing phone or VAM ID with only a `logger.warning` — the report is never surfaced anywhere for staff to notice and resolve | High — a report can simply vanish from a clinic's view with no alert |
| D9 | Fragile scraper with wide `try/except` swallowing and CSS-selector coupling | `connectors/mocdoc/worker.py` — multiple fallback-selector heuristics, JS force-hide of stuck modals, no schema-versioning of MocDoc's DOM | Medium — accepted cost of browser automation, but must be reflected honestly in the failure-mode matrix (Section U), not hidden |
| D10 | `connector_failed_reports` and `lab_reports` are two separate failure surfaces an operator must reconcile manually | `app/routers/admin.py:2579` returns `connector_failed_reports`; `lab_reports.status='failed'` is a different table with its own error reasons — no single "why did report X fail" view | Medium |
| D11 | 90-day PDF retention is invisible to the operator until a resend fails | `resend_report()` raises only when someone actually clicks resend; no proactive warning as reports approach the retention boundary | Low-Medium |

---

## E. Production Requirements (restated from the user's spec, as constraints on this plan)

1. Diagnostic Center tenants must never see Clinic/Hospital/Polyclinic-only navigation or terminology, and vice versa — driven entirely by `clinics.plan` + `PLAN_FEATURES`, no new dimension.
2. Clinic/Hospital/Polyclinic admin panels must be **byte-for-byte functionally unchanged** — every change here is additive (new `data-feature` gates, new nav section, new endpoints), never a modification of existing ungated behavior for those plans.
3. No new connector/automation system may be created — System A must be extended, not forked.
4. A report must never be delivered to the wrong patient — a positive, provable matching step is mandatory before every send, not just "trust the source system."
5. The automation must be observable: is it running, when did it last run, what happened, what needs a human.
6. All new tenant-facing data must remain branch-scoped and clinic-isolated using the existing `_scope_by_branch()` / `enforce_clinic_access()` / `enforce_branch_scope()` patterns already proven in `permissions.py` and `connectors/runner.py`.
7. Every new capability must be delegable to staff via the existing `permissions.py` model (extend `PERMISSIONS`, not build a parallel authz system).

---

## F. Proposed Architecture (overview)

```
+-----------------------------------------------------------------+
|  admin/index.html - Diagnostic Center nav section                |
|  (data-feature="diagnostic_reports", gated like everything       |
|   else via applyFeatureVisibility())                             |
|                                                                    |
|  New: "Report Automation" dashboard                               |
|    - connector health (last run, next run, is_enabled)            |
|    - report lifecycle board (queued/matching/sending/sent/        |
|      needs_review/failed)                                         |
|    - unified failure queue (merges connector_failed_reports       |
|      + lab_reports.status='failed' + new needs_review state)      |
+-----------------------------------------------------------------+
                          |  GET/POST via existing admin router
+-----------------------------------------------------------------+
|  app/routers/admin.py - extend existing /connectors* routes       |
|  + new /reports/queue, /reports/{id}/resolve-match endpoints      |
|  gated by new permissions (REPORTS_VIEW, REPORTS_RESOLVE,         |
|  CONNECTOR_MANAGE)                                                 |
+-----------------------------------------------------------------+
                          |
+-----------------------------------------------------------------+
|  System A (extended, not replaced)                                |
|  connectors/runner.py - now actually deployed as a Railway        |
|  worker service (Section Y)                                       |
|  connectors/mocdoc/worker.py - unchanged scrape logic, output     |
|  now flows through new patient-matching gate before submit        |
+-----------------------------------------------------------------+
                          |  POST /internal/integrations/lab-report
+-----------------------------------------------------------------+
|  NEW: PatientMatchService - cross-checks scraped identity         |
|  against clinic's `patients` table before LabReportService        |
|  ever sends a WhatsApp message                                    |
+-----------------------------------------------------------------+
                          |
+-----------------------------------------------------------------+
|  LabReportService.upload_and_send() - extended with a real        |
|  status state machine (Section I), same WhatsApp delivery         |
|  mechanics as today (kept, not rewritten)                         |
+-----------------------------------------------------------------+
```

Nothing about WhatsApp sending, encryption, Groq summarization, or Storage retention changes in kind — those are sound and stay. What's added is: deployment of the existing scheduler, a matching gate in front of delivery, a real state machine, and the dashboard/nav/RBAC surface to observe and operate it.

---

## G. Diagnostic Center Information Architecture

Current nav items and their fate:

| Nav item | Today | Diagnostic Center plan |
|---|---|---|
| Dashboard | ungated (D1) | **replace generic dashboard content** with automation-first dashboard (Section H) when `plan == 'diagstream'`; other plans see current generic dashboard unchanged |
| Hospital Profile | ungated (D1) | gate out for `diagstream` — rename concept doesn't apply (no doctors/appointments); keep a minimal "Center Profile" (name, contact, branches) instead |
| Patients | ungated (D1) | gate out for `diagstream` unless `patients` feature explicitly granted — diagnostic centers work off MocDoc's own patient records, not MediAssist's booking-patient table |
| Staff | ungated (D1) | keep, but same RBAC model applies (staff already generic, not hospital-specific) |
| **NEW: Report Automation** | doesn't exist | new top-level nav item, `data-feature="diagnostic_reports"`, visible only to `diagstream` (and any other plan explicitly granted the feature, e.g. a polyclinic with a diagnostic branch) |
| **NEW: Connectors** | exists today only as a settings sub-screen | promoted to its own nav entry under the same feature gate, since it's now the primary configuration surface for a diagstream tenant |
| Appointments/Doctors/etc. | already feature-gated per plan | unchanged |

`patients` and `profile` are corrected to use `has_feature`/`data-feature` rather than being globally visible — this is the direct fix for D1, applied surgically (two attribute additions + one new feature key), not a rewrite.

---

## H. Dashboard Specification

Primary Diagnostic Center dashboard, in priority order (operational needs first, matching what a diagnostics-center owner actually opens the app to check):

1. **Automation status strip**: per configured connector — `is_enabled`, last successful poll timestamp (from `connector_audit_log`), next scheduled poll (derived from APScheduler's actual next-run time once D3 is fixed), a red/amber/green state derived from "no successful poll in > 2x poll interval."
2. **Today's report activity**: counts of `sent`, `needs_review`, `failed` (new state machine, Section I) for the current day, per branch if multi-branch.
3. **Needs-review queue** (new — replaces the silent-drop behavior in D8): every report the matcher (Section L) couldn't confidently resolve, with the scraped name/phone/VAM-ID and a one-click "assign to patient" or "send anyway" (permission-gated) action.
4. **Failed deliveries**: merges `lab_reports.status='failed'` + `connector_failed_reports`, single list, single "why," single resolve action — closes D10.
5. **Retention warning banner**: reports approaching the 90-day PDF purge with `status != 'sent'` — closes D11.

This is intentionally not a generic "reports list" — it's a triage/operations board, because that is what the underlying data model (audit log + failure tracking + idempotency) was already built to support and what an unattended-automation product needs a human to check.

---

## I. Report Lifecycle / State Machine

Replace the binary `sent`/`failed` with:

```
DETECTED -> MATCHING -> MATCHED -> SENDING -> SENT
               |            |                  |
               v            v                  v
          NEEDS_REVIEW  NEEDS_REVIEW      FAILED (retryable)
```

- `DETECTED`: connector found the row in MocDoc, before download
- `MATCHING`: PDF downloaded, `PatientMatchService` running
- `MATCHED`: confident match found (or admin manually resolved) → proceeds automatically
- `NEEDS_REVIEW`: match confidence below threshold, or missing phone/VAM-ID (replaces D8's silent skip) — surfaces in the dashboard queue (Section H.3), never auto-sent
- `SENDING`: WhatsApp API calls in flight
- `SENT`: terminal success (same meaning as today's `sent`)
- `FAILED`: terminal-but-retryable (WhatsApp rejected, storage failed, etc.) — same meaning as today's `failed`, retry via existing `resend_report()` path

`lab_reports.status` column is extended (not replaced) with these values via a `CHECK` constraint migration; existing rows (`sent`/`failed`) remain valid without backfill. `DETECTED`/`MATCHING`/`NEEDS_REVIEW` are new rows created *before* `upload_and_send()` is called (today no row exists until the send attempt completes) — this is the concrete schema change needed to make D8 visible instead of silently dropped.

---

## J. Connector Architecture

No new connector system. Two concrete, additive changes to System A:

1. **Factory-pattern extraction** (borrowing the *shape* of CallMedex's `ConnectorFactory`, not its code or tables): today `connectors/runner.py` hardcodes `MocDocConnector` construction. Extract a small `CONNECTOR_REGISTRY: dict[str, type[HospitalConnector]]` keyed by `connector_type`, so adding CloudLIMS/Crelio support later (already proven possible — CallMedex did it for its own use case) means registering a new `HospitalConnector` subclass, not touching the runner's control flow. `HospitalConnector(ABC)` already has the right interface (`authenticate`, `fetch_new_reports`, `download_report`, `cleanup`) — no interface change needed.
2. **Deploy the runner** (Section Y) so `run_all_connectors()` actually executes on schedule in production.

No change to `integration_connectors`'s config JSONB shape, no change to encryption, no change to branch scoping — all already correct.

---

## K. Browser Automation Architecture

`MocDocConnector` stays as-is functionally (regex parsing, modal-dismissal loop, session persistence) — rewriting a working, already-hardened (encrypted sessions, sanitized debug dumps, prod-safe screenshot gating) scraper is not justified by this task. Two additive hardening changes:

1. **Version/selector drift detection**: wrap the "Pending Print" table locate step with a canary check — if the expected column headers aren't found, raise a distinct `ConnectorLayoutChangedError` rather than falling through to "0 reports found," so a MocDoc UI change produces a loud alert instead of silent zero-output (a specific instance of D9 worth calling out because "0 new reports" today is indistinguishable between "genuinely nothing new" and "the scraper is broken").
2. **Emit `DETECTED` rows immediately on scrape** (feeds Section I) instead of only producing output after a full download+submit cycle succeeds — this is what makes `NEEDS_REVIEW` for missing-phone/VAM-ID rows possible instead of the current silent-skip (D8).

---

## L. Patient Matching and Safety

This is the most safety-critical new component and the direct answer to D4.

**`PatientMatchService`** — inserted between `download_report()` and `submit_to_medassist()` (still inside System A's flow, called from `connectors/runner.py`, not a new service layer duplicated elsewhere):

1. Normalize the scraped phone to E.164 (MocDoc worker already does this in `_parse_patient_cell` — reuse, don't reimplement).
2. Look up the clinic's own `patients` table by normalized phone.
   - **Exact one match, name similarity above threshold (e.g. token-sort ratio ≥ 0.8)** → `MATCHED`, proceed automatically.
   - **No match** → this is normal for a diagnostic center walk-in never previously in MediAssist's `patients` table (most diagnostic-center patients are one-off, not returning appointment patients) — do **not** treat "no match" as an error; the scraped identity from MocDoc is still the source of truth for delivery, since MocDoc is the diagnostic center's own patient-of-record system. Proceed to `MATCHED` but flag `match_source: "moc_doc_only"` in the audit trail.
   - **Multiple `patients` rows share the same phone with materially different names**, or **name similarity is below threshold against an existing record with the same phone** → `NEEDS_REVIEW`. This is the actual "might send to the wrong person" signal — e.g. a shared family phone number where MocDoc's row says "Patient B" but MediAssist's own records under that number say "Patient A."
   - **Phone missing or malformed** (already caught today, but silently — Section D8/K) → `NEEDS_REVIEW`, never auto-sent.
3. Every decision (`MATCHED`/`NEEDS_REVIEW` + reason) is written to `connector_audit_log` per-report (extends the existing per-poll summary row to also support a per-report detail line — additive column, not schema replacement) so "why did/didn't this get auto-sent" is always answerable without guessing.

**Explicit design decision**: the matcher is a *conflict detector*, not a *requirement that every patient pre-exist in MediAssist*. Diagnostic centers routinely serve patients who never book anything through MediAssist. Requiring a pre-existing match would break the common case; the real safety property the user asked for ("never send to wrong patient") is satisfied by catching *conflicting* signals, not by mandating full pre-registration.

---

## M. WhatsApp Delivery Architecture

Unchanged mechanics (`whatsapp_service.upload_media` → `send_text` → `send_document`, same AI-summary-with-fallback pattern). Two additive changes tied to the new state machine:

1. `upload_and_send()` now receives an already-`MATCHED` (or manually-resolved) report — it no longer needs to make its own trust decision about the phone number, since Section L already gated that. This is a pure integration point, not a rewrite of the send logic.
2. On `FAILED`, instead of only logging, write the specific failure reason into the new unified failure queue (Section H.4) so `connector_failed_reports` and `lab_reports.status='failed'` are presented as one thing to an operator (closes D10) even though they remain two tables internally (no join-breaking schema merge needed — a dashboard-layer union is sufficient and lower risk).

---

## N. Database Changes

All additive, no destructive migrations, no changes to existing columns' meanings:

1. `lab_reports.status` CHECK constraint extended to include `detected`, `matching`, `needs_review` alongside existing `sent`, `failed` (migration, e.g. `037_lab_reports_status_lifecycle.sql`). Existing rows unaffected.
2. `lab_reports` — add nullable `match_confidence NUMERIC`, `match_source TEXT` (`moc_doc_only` | `patients_table` | `manual`), `matched_patient_id UUID REFERENCES patients(id) NULL` — captures Section L's decision without requiring a match to exist.
3. New table `connector_report_events` (or extend `connector_audit_log` with an optional `lab_report_id` FK + `event_detail JSONB`) — the per-report decision trail referenced in Section L.3. Decision: extend existing table (fewer moving parts) unless per-report event volume proves too high for the existing poll-level audit table's access patterns (flag as a build-time decision, not pre-decided here).
4. New permissions rows are code-level (`PERMISSIONS` frozenset in `permissions.py`), not schema — no migration needed for RBAC (Section Q).
5. No changes to `integration_connectors`, `integration_processed_reports`, `connector_failed_reports`, `clinics`, `branches`, `patients` schemas.

---

## O. Backend Changes

1. `app/services/tenant.py` — add `"diagnostic_reports"` to `PLAN_FEATURES["diagstream"]` (and any other plan that should see it); no structural change to the registry.
2. `app/services/permissions.py` — extend `PERMISSIONS` with `REPORTS_VIEW`, `REPORTS_RESOLVE`, `CONNECTOR_MANAGE`; add a `DIAGNOSTIC_OPERATOR` entry to `STAFF_ROLES`/`ROLE_PRESETS` mirroring the existing `DOCTOR_SCHEDULE_MANAGER` pattern.
3. `connectors/runner.py` — introduce `PatientMatchService` call between `download_report()` and `submit_to_medassist()`; write `DETECTED` rows immediately after scrape (Section K.2); extract `CONNECTOR_REGISTRY` (Section J.1).
4. New service `app/services/patient_match.py` — `PatientMatchService.match(clinic_id, branch_id, scraped_name, scraped_phone) -> MatchResult`.
5. `app/routers/admin.py` — extend existing connector endpoints' response payloads with lifecycle-aware fields; add `GET /reports/queue` (needs-review + failed union, Section H), `POST /reports/{id}/resolve-match` (manual match resolution, `REPORTS_RESOLVE`-gated).
6. `app/services/lab_reports.py` — `upload_and_send()` gains an optional `pre_matched: MatchResult` parameter; when present, skip re-deriving trust in the phone number (Section M.1). No change to the WhatsApp call sequence itself.
7. `app/main.py` — no change (runner remains a separate process, not folded into FastAPI's lifecycle, unlike CallMedex's queue engine — see Section Y for why).

---

## P. Frontend Changes

All within `admin/index.html`, additive:

1. Add `data-feature="diagnostic_reports"` gate to the new nav items (Section G); add `data-feature` to the currently-ungated `profile`/`patients` items (fix for D1).
2. New dashboard section (Section H) — status strip, needs-review queue, failure queue, retention banner — built as a new `<section>` block following the existing per-nav-item section pattern already used throughout the file.
3. Connector settings screen (already exists) gets a "last run" / "next run" display sourced from the audit log, and the enable/disable toggle gets a visible consequence (today it's a bare switch with no feedback on what "enabled" currently means operationally).
4. Manual resolve action on needs-review rows — a small modal (reuse existing modal patterns in the file) posting to `POST /reports/{id}/resolve-match`, gated client-side by `myFeatures`/`permissions` the same way staff-table actions are already gated (established pattern from the just-completed RBAC work).

---

## Q. RBAC / Security

- Extends `app/services/permissions.py` exactly as that module's own docstring intends ("extends the existing super_admin/clinic_admin/staff tier... with granular, server-enforced grants") — no parallel authz system.
- New permissions (`REPORTS_VIEW`, `REPORTS_RESOLVE`, `CONNECTOR_MANAGE`) follow the same `require_permission()` dependency pattern already proven for doctors/leaves/holidays/staff.
- Connector credential write (`PUT /connectors`) remains `require_admin`-only (clinic_admin/super_admin) — deliberately **not** delegable to staff, since it holds a third-party password; only *operational* actions (viewing the queue, resolving a match, marking a failure handled) become delegable via the new permissions.
- `enforce_branch_scope()` applied to the new `/reports/queue` and `/reports/{id}/resolve-match` endpoints exactly as it's applied to doctor/leave endpoints today — a branch-scoped staff account only sees/resolves their branch's reports.
- No new secrets, no new auth mechanism — reuses Fernet + `connector_encryption_key` (Section C.1) and existing HTTP Basic Auth (`AdminUser`/`verify_credentials`/`require_admin`).

---

## R. Reliability, Idempotency & Retry Strategy

- Idempotency: unchanged and already sound — `integration_processed_reports` unique index + `lab_reports.external_report_id` cross-path check (fail-open, Section C.2 step 5) are kept exactly as-is.
- **New**: distributed-lock gap (D6) fix — before `run_all_connectors()` starts a clinic/branch's run, acquire a short-lived advisory lock row (e.g. `UPDATE integration_connectors SET locked_at = now() WHERE id = ? AND (locked_at IS NULL OR locked_at < now() - interval '15 minutes') RETURNING id` — a Postgres-native compare-and-swap, no new infra). Ponytail-appropriate: this is a single UPDATE-with-WHERE pattern, not a distributed lock service.
- Retry: `NEEDS_REVIEW` and `FAILED` both remain retryable via existing `resolve_connector_failed_report()`/`resend_report()` flows; no new retry engine needed, since the volume (diagnostic center report throughput) doesn't justify a queue/backoff system beyond what APScheduler's 10-minute poll already provides as a natural retry cadence for `NEEDS_REVIEW` re-evaluation.
- Storage retention (D11): dashboard banner (Section H.5) is the fix — proactive, not reactive-only-on-resend-failure.

---

## S. Observability & Alerts

- `send_admin_alert()` (already exists, `connectors/runner.py`) — extend its trigger conditions to include: scheduler heartbeat missed (no successful poll in > 2x interval — this is what makes D3's fix *provable* operationally, not just "the process is running"), and `NEEDS_REVIEW` queue depth crossing a threshold (e.g. >10 unresolved for >1 hour).
- Dashboard status strip (Section H.1) is the human-facing half of the same signal `send_admin_alert()` uses — both read from `connector_audit_log`'s timestamps, single source of truth, no duplicate health-check logic.
- No new external monitoring dependency (no Datadog/Sentry addition implied here) — reuses the existing WhatsApp-based admin alert channel already built.

---

## T. Multi-Tenant Isolation Strategy

No new isolation primitive needed — every new table/column/endpoint reuses the exact patterns already proven correct in this codebase:

- `clinic_id` scoping via `enforce_clinic_access()` (existing) on every new endpoint
- `branch_id` scoping via `enforce_branch_scope()` (existing) and the `_scope_by_branch()` query helper (existing, `connectors/runner.py`) for the runner's own queries
- `PatientMatchService.match()` takes `clinic_id`/`branch_id` explicitly and queries `patients` scoped the same way every other patient lookup in the codebase already does — no new cross-tenant query surface is introduced

---

## U. Failure-Mode Matrix

| Failure | Current behavior | Behavior after this plan |
|---|---|---|
| MocDoc UI selector changes | Falls through to "0 new reports," silent | `ConnectorLayoutChangedError` → alert (Section K.1) |
| Row has missing phone/VAM-ID | Skipped, `logger.warning` only | `NEEDS_REVIEW` row created, visible in dashboard queue |
| Two `patients` rows conflict on phone/name | Not detected — sent directly to MocDoc's phone | `NEEDS_REVIEW`, blocks auto-send (Section L) |
| WhatsApp API rejects message (24h window / not opted in) | `lab_reports.status='failed'`, buried unless someone checks the reports table | Surfaced in unified failure queue (Section H.4) |
| Storage upload fails but WhatsApp send succeeds | Row saved with `error_message` noting resend unavailable, easy to miss | Same storage behavior kept, but now visible via retention/failure dashboard, not just a DB field |
| Runner process not deployed at all | Currently true in production, invisible | Deployment fix (Section Y) + heartbeat alert (Section S) makes absence loud, not silent |
| Two runner instances overlap (post-deploy fix) | No lock — possible double-send risk once D3 is fixed | Advisory-lock CAS (Section R) prevents concurrent runs per connector |
| Credential expires / MocDoc login fails repeatedly | `_login()` retries once, then `authenticate()` fails, `run_connector()` catches and records failure | Unchanged — already reasonable; surfaced same as any other failure in the unified queue |
| 90-day PDF retention passed, resend requested | Raises `ValueError` only at resend time | Proactive dashboard banner before it happens (Section H.5) |

---

## V. Phased Implementation Plan

**Phase 0 — Nav gating fix (smallest, ships independently)**
Add `data-feature` to `profile`/`patients`; add `diagnostic_reports` feature key. Fixes D1 immediately, zero backend risk.

**Phase 1 — Deploy the scheduler (unblocks everything else being "real")**
Get `connectors/runner.py --all` actually running in production (Section Y). No code change to the runner itself required for this phase — pure deployment config.

**Phase 2 — State machine + dashboard skeleton**
Migration for extended `status` values + new nullable columns (Section N). Dashboard UI reading existing + new fields, read-only first (no matcher yet) — makes today's already-collected data visible, which alone resolves D2/D10/D11.

**Phase 3 — Patient matching gate**
`PatientMatchService`, wired into `connectors/runner.py` between download and submit. This is the safety-critical phase — ship behind a per-clinic feature flag, dry-run/shadow mode first (log decisions without blocking sends) before enforcing.

**Phase 4 — RBAC delegation + resolve actions**
New permissions, `resolve-match`/`resolve-failure` endpoints, frontend actions.

**Phase 5 — Hardening**
Distributed lock (R), layout-drift detection (K.1), alert threshold tuning (S).

Each phase is independently shippable and independently testable — consistent with this codebase's existing migration-per-concern convention (36 migrations to date, each scoped).

---

## W. Migration Strategy

- All migrations additive (`ALTER TABLE ADD COLUMN`, `ALTER TABLE ... DROP CONSTRAINT / ADD CONSTRAINT` for the status CHECK widen) — matches the established pattern in `016_fix_plan_constraint.sql` (introspect `pg_constraint`, drop, re-add) and `025_connector_branch_scope.sql` (add nullable column + new partial indexes alongside old).
- No backfill required — existing `sent`/`failed` rows remain valid under the widened CHECK constraint.
- New permissions are pure Python (`PERMISSIONS` frozenset) — no migration.

---

## X. Regression Strategy

- Every gate added in Phase 0 is additive (`data-feature` attributes) — verify via the same technique already used in this session's prior RBAC work: grep for the attribute presence, then a manual pass with each plan's `myFeatures` set to confirm non-`diagstream` plans see no change (their items simply gain a feature check that always evaluates true for their existing granted features).
- Existing System A tests (`tests/test_admin_connectors.py`, `tests/test_connector_failed_reports.py`, `tests/test_connector_security.py` — located, not yet read in this session, see Section AA) must be re-run unmodified after Phase 1/3 changes; any test needing modification because of the new matcher gate is a signal the gate changed existing contract behavior and needs review before proceeding.
- `LabReportService.upload_and_send()`'s existing callers (admin manual upload, presumably a `POST /admin/lab-reports` endpoint — not yet located, see Section AA) must continue to work with `pre_matched=None` (default), preserving today's behavior for non-connector-sourced uploads exactly as-is.

---

## Y. Production Deployment Strategy

Section D3's fix requires an actual deployment decision, presented as options rather than a foregone conclusion since it's infrastructure, not application code:

- **Option 1 (recommended): separate Railway service** running `python -m connectors.runner --all --scheduled` (or equivalent), sharing the same Supabase DB and `connector_encryption_key` env var as the main service. Matches System A's existing design (`connectors/runner.py` already has its own `start_scheduled_mode()` with APScheduler `IntervalTrigger`/`CronTrigger`) — this phase is *deployment config only*, no code change.
- **Option 2**: fold into the main FastAPI process's startup/shutdown lifecycle like CallMedex's queue engine — rejected as primary recommendation because Playwright/Chromium in the same process as the request-serving web server risks resource contention and blast radius (a stuck browser automation shouldn't be able to degrade webhook/API latency); System A was already architected as a separate process for this reason (`connectors/runner.py`'s docstring/design, Section C.1) and that separation should be preserved, just actually deployed.
- Either option needs a Dockerfile/railway.toml change (or a second service definition) — this is the one piece of this plan that touches deployment config rather than only app code, and should be confirmed with the user before Phase 1 execution (Section AA).

---

## Z. Definition of Done

- [ ] `diagstream` tenants see no Clinic/Hospital/Polyclinic-only nav items (Phase 0), verified by manual pass with each plan value
- [ ] Clinic/Hospital/Polyclinic admin panels show zero behavioral diff (regression pass, Section X)
- [ ] `connectors/runner.py --all --scheduled` is running as a deployed, monitored process in production, confirmed via a real `connector_audit_log` row appearing on the expected interval
- [ ] Every report that reaches `NEEDS_REVIEW` is visible in the dashboard queue and resolvable by a permitted user — zero silent-drop paths remain (D8 closed)
- [ ] `PatientMatchService` is live (at minimum in shadow/dry-run mode) for at least one pilot `diagstream` clinic, with decision logs reviewed before enforcement mode
- [ ] Unified failure queue merges `connector_failed_reports` + `lab_reports.status='failed'` in one dashboard view
- [ ] New permissions (`REPORTS_VIEW`, `REPORTS_RESOLVE`, `CONNECTOR_MANAGE`) are delegable and enforced server-side, with branch scoping verified
- [ ] All existing System A/CallMedex tests pass unmodified; any modified test has an explicit reviewed reason
- [ ] No changes to CallMedex code, tables, or API surface

---

## AA. Risks / Unknowns Requiring Confirmation

1. **Deployment mechanism for Phase 1** (Section Y) — needs an explicit user decision on Railway service topology before any deployment-config change is made; this plan recommends Option 1 but does not assume authorization to modify `railway.toml`/add a service without confirmation.
2. **`app/routers/platform.py`** (owner/platform-level dashboard) was identified but not read in this session — if it already surfaces any connector/report data at the platform-owner level, the new clinic-facing dashboard (Section H) should reuse those queries rather than duplicate them. Needs a follow-up read before Phase 2 implementation.
3. **Existing connector settings screen in `admin/index.html`** — confirmed to exist (referenced by the working `PUT /connectors` integration) but its exact current markup/line range was not re-inventoried in this session; Phase 0/2 implementation should re-read it fresh immediately before editing rather than trusting this document's characterization.
4. **`tests/test_admin_connectors.py`, `test_connector_failed_reports.py`, `test_connector_security.py`** — located via file existence only, not read; their actual coverage should be confirmed before Phase 1/3 to know what regression safety net already exists versus what new tests are needed.
5. **Admin-manual lab-report upload endpoint** (the non-connector caller of `LabReportService.upload_and_send()` referenced in Section X) — its exact route was not located in this session; must be confirmed so Phase 3's `pre_matched` parameter addition is verified not to break it.
6. **`connector_report_events` vs. extending `connector_audit_log`** (Section N.3) — flagged as an open implementation-time decision, not pre-resolved, pending a look at expected per-report event volume for the largest current `diagstream` tenant.
7. **CloudLIMS/Crelio support** (Section J.1's registry) is presented as an extensibility point, not a committed deliverable — no evidence in this session that any current Kriya AI diagnostic-center tenant uses anything other than MocDoc; building registry support ahead of an actual second-connector need would be scope creep beyond what's requested.
8. **Name-similarity threshold value** (Section L, "≥ 0.8 token-sort ratio") is a placeholder default requiring real-world tuning against actual MocDoc data before enforcement mode — explicitly why Phase 3 ships in shadow mode first.

---

*No implementation has begun. Awaiting user direction on which phase (if any) to execute first, and confirmation on the Section Y deployment decision before Phase 1 touches any deployment configuration.*
