# Diagnostic Center Product Refinement & Enhancement Plan
## Post-Implementation Audit — Kriya AI Diagnostic Center Admin Panel

> Baseline: `docs/superpowers/specs/2026-08-20-diagnostic-center-admin-panel-analysis.md`
> Implementation commit: `118a9fa` — "feat: implement staff RBAC doctor delegation and diagnostic center automation panel"
> This document does **not** replace the baseline analysis. It audits what was actually built against it and against real-world diagnostic-center operations, then proposes a focused amendment.

---

## 1. Current State Assessment

The baseline analysis proposed a design and explicitly did not implement it. Commit `118a9fa` (2026-08-20, same day) then implemented most of it. This audit re-verified the *actual code*, not the analysis document's proposal, file by file:

| Baseline proposal | Built? | Evidence |
|---|---|---|
| Nav gating fix (profile/patients/staff/dashboard) | **Partially** | `staff`/`dashboard` correctly ungated; `profile`/`patients` now gated on `booking` — but this over-corrects (see §3) |
| Report lifecycle states (`needs_review`, etc.) | **Yes** | `migrations/037_lab_reports_status_lifecycle.sql`, `lab_reports.status` now used with `"needs_review"` |
| `PatientMatchService` | **Yes, exceeds spec** | `app/services/patient_match.py` — handles multi-record conflicts, honorific-stripped name similarity, not just the single-record case the spec described |
| Operational dashboard | **Yes** | `admin/index.html:933-1003` (`diagDashboardContent`) — status strip, today's stats, needs-review queue, failed queue, retention banner |
| Unified triage queue + resolve/resend endpoints | **Yes** | `GET /reports/queue`, `POST /reports/{id}/resolve-match`, `POST /reports/{id}/resend`, `GET /diagnostic/stats` (`app/routers/admin.py:2641-2886`) |
| RBAC permissions `REPORTS_VIEW`/`REPORTS_RESOLVE`/`CONNECTOR_MANAGE` | **Partially** | Defined and enforced on report endpoints; **`CONNECTOR_MANAGE` is defined but never checked anywhere** (see §7 gap) |
| Distributed connector lock | **Yes** | `migrations/037` adds `locked_at`/`locked_by`; `connectors/runner.py:72-116` implements 15-min advisory lease |
| Deploy the scheduler (Section Y decision) | **No — still not decided or done** | See §3, this is the most important remaining gap |

**Net assessment:** the team did substantially more than the baseline asked for on the safety/data-model side (patient matching, lifecycle states, locking) and correctly resisted scope creep (no LIMS features, no export tooling, no sample-tracking). The gaps that remain are narrower and more concrete than the original 11-finding list — three of them, detailed below.

---

## 2. Current Navigation Audit

| Feature | Current gate | Diagnostic Center relevance | Classification | Recommendation | Reason |
|---|---|---|---|---|---|
| Dashboard (generic) | none (always visible) | N/A — replaced by `diagDashboardContent` at runtime for `diagstream` | F — Platform | Keep as-is | `loadDiagnosticDashboard()` swaps content client-side (`admin/index.html:2264-2269`); correct mechanism |
| **Hospital Profile** | `data-feature="booking"` | **Core — organization identity** | **A → misclassified as E** | **Remove the `booking` gate; show for every plan** | Diagnostic centers need to set their own bot name, address, Maps link, emergency phone — the exact same fields a lab report WhatsApp message renders (`lab_reports.py:114`, `f"🏥 *{clinic['name']}..."`). Gating this on `booking` hides the *only* UI to configure it. Confirmed bug, not a design choice — see §3 |
| Appointments | `data-feature="booking"` | Irrelevant (no booking in diagstream) | E — Irrelevant | Keep gated | Correct as-is |
| Doctors | `data-feature="booking"` | Irrelevant | E — Irrelevant | Keep gated | Correct as-is |
| Leaves | `data-feature="roster_management"` | Irrelevant (no doctor roster to manage) | E — Irrelevant | Keep gated | Correct as-is |
| Holidays | `data-feature="roster_management"` | Conditional — closure days affect connector polling only indirectly (MocDoc itself won't have new reports on a closed day; no separate config needed) | E — Irrelevant for now | Keep gated | Confirmed no operational hook exists or is needed; do not build one speculatively |
| **Patients** | `data-feature="booking"` | Conditional | C — Conditional | Keep gated as-is | Correct: diagnostic centers work from MocDoc-sourced identity, not a pre-registered patient list. `PatientMatchService` already queries `patients` when it exists — this table isn't the diagnostic center's primary data source, so hiding the CRUD page is right |
| **Report Automation** (`diagreports`) | `data-feature="diagnostic_reports"` | Core | A — Must Have | Keep | Working as designed |
| **Lab Reports** (`labreports`) | `data-feature="lab_reports"` | Core | A — Must Have | Keep | Manual-upload path, coexists with automation queue correctly |
| Prescriptions | `data-feature="booking"` | Irrelevant | E — Irrelevant | Keep gated | Correct: `test_diagstream_plan_features` explicitly asserts `"prescriptions" not in diag_features` |
| Payments | `data-feature="booking"` | See §17 | E — Irrelevant (today) | Keep gated | See Payment Decision below |
| Payment Settings | `data-feature="payments_razorpay"` | See §17 | E — Irrelevant (today) | Keep gated | See Payment Decision below |
| **Branches** | `data-feature="multi_branch"` | Core for multi-site labs | B — Operational | Keep | `diagstream` plan includes `multi_branch` — correct; connector config already branch-scoped (`connectorBranchCard`) |
| Staff Accounts | none (role-gated only) | Core | A — Must Have | Keep | Correct — every plan needs staff management |
| **Report Connector** | `data-feature="diagnostic_reports"` | Core | A — Must Have | Keep, extend | Credential form + run history + failed reports exist; missing Test Connection / Run Now — see §11 |

---

## 3. Features to Remove from Diagnostic Center UI

**None.** Every visible module for `diagstream` today is either operationally relevant or already correctly hidden. There is no leftover clutter to strip — the screenshot complaint that motivated the original analysis has already been resolved for every item except one (Hospital Profile, which was *over*-hidden, not under-hidden). This audit found no case of "hospital feature bleeding into diagnostic center UI."

---

## 4. Features to Keep

Dashboard, Report Automation Queue, Lab Reports, Branches, Staff Accounts, Report Connector — all as enumerated in §2. No changes.

---

## 5. Features to Make Conditional

No new conditional gating needed. The one change required (§6) is a gate *removal*, not a new condition.

---

## 6. Missing Real-World Diagnostic Center Features — Confirmed Gaps

### Gap 1 (P0 — breaks the product today): Hospital Profile is unreachable for diagnostic centers

`admin/index.html:800` — `data-feature="booking"` on the Profile nav link. `diagstream`'s `PLAN_FEATURES` set (`tenant.py:212-223`) does not include `booking`. Result: a diagnostic center admin has **no UI path** to set their organization's WhatsApp-facing name, address, Google Maps link, or emergency phone — the exact fields `LabReportService` interpolates into every patient-facing message. This isn't a missing nice-to-have; it's a configuration dead end for a brand-new diagnostic center tenant. **Already exists partially**: the backend profile-save endpoint has no plan gate of its own — only the frontend nav item is gated. Frontend-only fix.

### Gap 2 (P0 — carried over from baseline, still unresolved): Connector runner is not deployed

`Dockerfile` CMD is still only `exec uvicorn app.main:app ...`; `railway.toml` startCommand is the same; `render.yaml` is present but empty. `connectors/runner.py --all` (the APScheduler polling loop) has no process to run it in production. Everything downstream of it — patient matching, lifecycle states, locking, the whole dashboard — is fully built and correctly wired, but **currently receives zero real automated runs** in production. This was flagged Critical in the baseline (Section Y) and remains the single highest-leverage fix: without it, "Report Automation" is a UI shell over a database that only ever gets rows from manual admin uploads.

### Gap 3 (P1): `CONNECTOR_MANAGE` permission is defined but not enforced

`app/services/permissions.py` defines `CONNECTOR_MANAGE` and grants it to the `DIAGNOSTIC_OPERATOR` role preset (`_DIAGNOSTIC_OPERATOR_GRANTS`). But `GET /connectors`, `PUT /connectors`, and `POST /connectors/{id}/toggle` (`admin.py:2368-2497`) all still depend on `require_admin`, which — per `permissions.py`'s own comment — "unconditionally 403s every role='staff' account." A staff member assigned the `DIAGNOSTIC_OPERATOR` role, specifically built for this purpose, still cannot manage the connector. The delegation model works for reports (`REPORTS_VIEW`/`REPORTS_RESOLVE`) but silently doesn't for connectors.

### Gap 4 (P2): No on-demand connector trigger or credential test

Once Gap 2 is fixed, the only way to know new credentials work is to wait for the next 10-minute poll, or run `python -m connectors.runner --once --dry-run` from a shell — not available to admin users. A "Test Connection" button (dry-run login, no downloads) and a "Run Now" button (trigger one poll cycle immediately) are both **backend-only additions** — `run_connector(..., dry_run=True)` already exists and does exactly this; it just isn't exposed via an admin API endpoint.

### Explicitly not gaps — confirmed correct as-is

- No LIMS features (sample tracking, test catalog, analyzer integration) — correctly absent, and should stay absent per the product's stated scope.
- No CSV/export tooling — correctly absent; nothing in current diagnostic-center operations requires it yet.
- No sample-collection/specimen workflow — out of scope, correctly absent.

---

## 7. Existing Backend Capabilities Not Exposed

| Capability | Where it lives | Exposed via UI/API? |
|---|---|---|
| Dry-run connector test (login + parse only, no downloads) | `connectors/runner.py:run_connector(dry_run=True)` | No admin endpoint calls this |
| `CONNECTOR_MANAGE` permission | `permissions.py` | Defined, granted to a role, never checked by any endpoint |
| Branch-scoped connector failure resolution | `admin.py:2605` `POST /connectors/failed-reports/{id}/resolve` | Yes, exposed and used (`resolveFailedReport()`) |
| Connector audit/run history | `admin.py:2540` `GET /connectors/{id}/audit-log` | Yes, exposed (`pg-connectors` "Run History" card) |

---

## 8. Dashboard Refinement

Current `diagDashboardContent` (`admin/index.html:933-1003`) already covers: automation status strip (health dot + last run + error), today's discovered/delivered/needs-review/failed counts, needs-review triage table, failed-deliveries table, 90-day retention banner. This matches the "answer in a few seconds" bar from the request almost exactly. **No structural change recommended.** Two small additions once Gap 2/4 land:
- Status strip: add "Next run in Xm" (trivial — `last_run_at + poll_interval_minutes`, both already stored).
- Status strip: replace the static "Connector Settings" button-only recovery path with a "Run Now" button when `CONNECTOR_MANAGE` is held (ties into Gap 4).

Do not add a trend chart or delivery-time-average widget — nothing in current operations calls for it, and it isn't part of the "few seconds" answer.

---

## 9. Navigation Refinement

No structural changes. The one edit is removing `data-feature="booking"` from the Profile nav link (`admin/index.html:800`) — see §6 Gap 1 and §21.

---

## 10. Report Operations

Already complete: unified queue (`GET /reports/queue`) with `needs_review`/`failed`/`connector_failures` sections, filter toggle (All/Needs Review/Failed), search, resolve-and-send modal, retry action. No changes needed.

---

## 11. Automation Operations

Existing: credential form, enable/disable checkbox, run history, failed-reports list. **Add** (P1/P2, backend-only, reuses `run_connector`):
- `POST /admin/connectors/{id}/test` → calls `run_connector(..., dry_run=True)`, returns auth success/failure + reports-found count without writing anything.
- `POST /admin/connectors/{id}/run-now` → calls `run_connector(...)` directly (bypassing the scheduler wait), same lock semantics apply.

Both gated on `require_permission("CONNECTOR_MANAGE")` — which also fixes Gap 3 by finally giving that permission somewhere to matter.

---

## 12. Exception/Review Operations

Already complete — see §10. The needs-review and failed-delivery tables, resolve modal, and retry button cover connector-download failures, WhatsApp-delivery failures, and patient-match conflicts in one place (`renderDiagQueuePageTable`, `admin.py:2641`).

---

## 13. Patient Matching Operations

`PatientMatchService` (`app/services/patient_match.py`) is complete and exceeds the original design: honorific-stripped normalized name comparison, three-way similarity scoring (sequence ratio / token-sort ratio / Jaccard), correct handling of zero/one/multiple existing-patient records for a phone number, and a `manual` match source recorded when an admin resolves a conflict by hand (`admin.py:2723`). No changes needed. The `0.75` similarity threshold (vs. `0.8` in the original spec) is still a placeholder pending real-world tuning — carry this forward as an open item, not a defect.

---

## 14. Alerts and Monitoring

`send_admin_alert()` already fires on: authentication failure, per-report consecutive-failure threshold (default 3), and connector crash (`connectors/runner.py:118-505`). This is adequate for P0/P1 scope. No new alert channel needed — WhatsApp-to-admin-phone is consistent with how the rest of the product communicates operational state.

---

## 15. Staff/RBAC

`DIAGNOSTIC_OPERATOR` (`REPORTS_VIEW`, `REPORTS_RESOLVE`, `CONNECTOR_MANAGE`) and `LAB_OPERATOR` (`REPORTS_VIEW`, `REPORTS_RESOLVE`) role presets exist and are tested (`test_diagnostic_feature_gating.py`). The only defect is Gap 3 — `CONNECTOR_MANAGE` has no enforcement point yet. No new roles needed; two presets are appropriately scoped and match real job functions (an operator who triages reports vs. one who also manages the MocDoc connection).

---

## 16. Branch Requirements

Connector config is branch-scoped (`branch_id` nullable FK, `_scope_by_branch()` in both `admin.py` and `runner.py`), and the `pg-connectors` page has a branch selector. Reports queue and stats endpoints enforce `enforce_branch_scope()` for branch-restricted staff (`admin.py:2654-2656`, `2824-2826`). This is correctly built for multi-branch diagnostic centers. No changes needed.

---

## 17. Payment Decision (evidence-based)

- Diagnostic centers do not book appointments through Kriya (`diagstream` excludes `booking` — confirmed by `test_diagstream_plan_features`).
- No diagnostic-test booking/payment flow exists anywhere in the codebase — `payments`/`paysettings` are gated on `booking`/`payments_razorpay`, neither of which `diagstream` has.
- Razorpay is wired for `soloclinic`/`essential`/`polyclinic` (appointment-fee collection), not for diagnostic report delivery.
- **Conclusion: capability exists, plan does not enable it, correctly not displayed. No code change.** If a future diagnostic-center payment use case emerges (e.g., paid report delivery, prepaid test packages), it becomes a new named feature flag added to `PLAN_FEATURES["diagstream"]` — not a re-purposing of `booking`.

---

## 18. Prescription Decision (evidence-based)

Same reasoning as Payments: `prescriptions` is gated on `booking`; `diagstream` doesn't have it; a diagnostic center issues no prescriptions. `test_diagstream_plan_features` explicitly asserts this. **No change.**

---

## 19. Holidays/Leaves Decision (evidence-based)

Both gated on `roster_management`, which `diagstream` does not have — correct, since there's no doctor roster to manage at a pure diagnostic center. No operational hook (e.g., "pause polling on holiday") exists or was requested by any evidence in the codebase; do not build one speculatively per the anti-LIMS/anti-scope-creep directive. **No change.**

---

## 20. Multi-Tenant Implications

All three proposed changes are additive to existing tenant-scoped surfaces:
- Profile gate removal: no new surface, just visibility — the profile save endpoint is already `clinic_id`-scoped.
- `test`/`run-now` connector endpoints: must call `enforce_clinic_access()` and `enforce_branch_scope()` exactly as `resolve_report_match`/`get_diagnostic_stats` already do (copy the existing pattern, `admin.py:2653-2656`).
- No schema changes required for either fix.

---

## 21. Frontend Changes

1. `admin/index.html:800` — remove `data-feature="booking"` from the Profile nav `<div>`. Keep `data-role="admin"` (profile editing should stay admin-only, consistent with today).
2. `admin/index.html` `pg-connectors` (~line 1509-1528): add "Test Connection" and "Run Now" buttons next to "Save Credentials", wired to the two new endpoints in §11.
3. `admin/index.html:940-947` (`diagStatusStrip`): add a "Next run" line once the backend exposes `poll_interval_minutes` + `last_run_at` together (already both present in `integration_connectors` — just needs surfacing in `get_diagnostic_stats`' `connector_info` dict, `admin.py:2864-2872`).

---

## 22. Backend Changes

1. `app/routers/admin.py`: add `POST /connectors/{id}/test` and `POST /connectors/{id}/run-now`, both `Depends(require_permission("CONNECTOR_MANAGE"))`, both `await`-calling `connectors.runner.run_connector(...)` (import at call time to avoid a circular import with the FastAPI app — `runner.py` currently imports `app.config`/`app.database`, not the reverse).
2. `app/routers/admin.py:2368,2394,2497`: change `Depends(require_admin)` → `Depends(require_permission("CONNECTOR_MANAGE"))` on `get_connectors`, `upsert_connector_credentials`, `toggle_connector`. `require_permission()` already passes through `clinic_admin`/`super_admin` unconditionally (`permissions.py`'s `_dep`), so this is a pure widening for staff — no regression for existing admin users.
3. `app/routers/admin.py:2864-2872`: add `poll_interval_minutes` and a computed `next_run_at` to the `connector_info` dict in `get_diagnostic_stats`.
4. Deployment config (Dockerfile/railway.toml/render.yaml): **requires an explicit decision before touching**, per the baseline's Section Y. See Implementation Plan below — this is Phase 0 of that plan and needs your confirmation on topology (separate worker service vs. in-process) before any config file changes.

---

## 23. Database Changes

**None required.** `poll_interval_minutes` already lives in `integration_connectors.config` (JSONB); `last_run_at` already exists as a column. Every change in this plan is additive at the code layer only.

---

## 24. Security Implications

- Widening `get_connectors`/`upsert_connector_credentials`/`toggle_connector` to `CONNECTOR_MANAGE` staff: credentials remain masked in every response via `_mask_connector()` regardless of caller (`admin.py`'s existing helper) — a staff member with `CONNECTOR_MANAGE` can rotate credentials but never read the existing password back. Consistent with how `REPORTS_RESOLVE` already works for patient data.
- New `test`/`run-now` endpoints must not accept a raw clinic_id from an unscoped caller — reuse `enforce_clinic_access()` exactly as every other diagnostic endpoint does.
- Profile gate removal has no security implication — it only affects who *sees the nav link*; the underlying save endpoint's authorization is unchanged.

---

## 25. Regression Risks

- **Clinic/Hospital/Polyclinic**: `data-feature="booking"` removal from Profile only affects `diagstream` clinics (the only plan lacking `booking`) — every other plan already has `booking` and thus already sees the tab; behavior for them is unchanged.
- **`require_admin` → `require_permission("CONNECTOR_MANAGE")` widening**: `require_permission`'s own logic passes `clinic_admin`/`super_admin` straight through, so no existing admin loses access. Only a newly-created `DIAGNOSTIC_OPERATOR`/staff account with the grant gains access — a strictly additive change. Existing tests (`test_permissions.py`, `test_diagnostic_feature_gating.py`) already assert the permission grant; add one test asserting the endpoint now honors it.
- No shared API touched by any other business type in this plan.

---

## 26. Prioritized Implementation Phases

- **P0** — Required for safe/working production:
  1. Remove `booking` gate from Hospital Profile nav item (§6 Gap 1).
  2. Deploy `connectors/runner.py --all` as a scheduled process (§6 Gap 2) — **blocked on your confirmation of deployment topology.**
- **P1** — Required for the delegation model to actually work:
  3. Point `CONNECTOR_MANAGE` at the connector CRUD endpoints (§6 Gap 3).
- **P2** — Valuable, not urgent:
  4. `test`/`run-now` connector endpoints + UI buttons (§6 Gap 4).
  5. "Next run" display on the dashboard status strip.
- **P3** — Explicitly not in scope, no action:
  - LIMS-style features, CSV export, holiday-driven polling pause — all confirmed unnecessary by current evidence; revisit only if new product evidence emerges.

---

## 27. Definition of Done

- [ ] A `diagstream`-plan admin can open "Hospital Profile" and save name/address/Maps link/emergency phone.
- [ ] `connectors/runner.py --all` runs continuously in production (topology per your Phase 0 decision) and `integration_connectors.last_run_at` advances every ~10 minutes for any enabled connector.
- [ ] A staff account with role `DIAGNOSTIC_OPERATOR` can view, edit, and toggle a connector without being 403'd.
- [ ] `test_diagnostic_feature_gating.py` and `test_diagnostic_admin_queue.py` still pass unmodified; a new test confirms `CONNECTOR_MANAGE` staff can call `GET /connectors`.
- [ ] No change to any Clinic/Hospital/Polyclinic-visible nav item or endpoint behavior.

---

## Risks/Unknowns Requiring Confirmation

1. **Deployment topology (Section Y, still open)** — separate Railway/Render worker service running `connectors.runner --all`, vs. running it as a background task inside the existing FastAPI process (`app/main.py` lifespan, the way CallMedex's queue is wired). The baseline recommended the separate-service option to avoid Playwright/Chromium resource contention with the web server; that reasoning still holds and nothing in this audit changes it. **This needs your explicit go-ahead before any deployment config file is touched** — deployment changes are exactly the kind of action that should be confirmed, not inferred.
2. Exact route name for the existing hospital-profile save endpoint wasn't re-verified in this pass (assumed unchanged since original analysis) — confirm at implementation time before editing.
3. `0.75` name-similarity threshold in `PatientMatchService` remains a placeholder — no real-world MocDoc data has been used to tune it yet.

*No implementation has begun beyond what commit `118a9fa` already shipped. This document identifies exactly three code changes (§6) as the P0/P1 remaining work, plus two P2 conveniences — everything else in the current implementation is confirmed correct and should not be touched.*
