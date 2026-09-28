# Session 27 — Corporate Health Insights (Annual Employee Screening Camps)

**Date:** 2026-09-28 · **Migration:** 095 · **Plans affected:** `diagnostic_stream` and `diagnostic_test_booking` (Owner opt-in only)  
**Origin:** Commercial partnership with diagnostic centres (Taiyo Labs). Diagnostic labs execute annual health check-up packages for corporate clients. The corporate HR/management demands aggregate health analytics across their workforce without violating employee medical confidentiality. Labs need an automated, zero-marginal-cost pipeline to ingest hundreds of PDF reports, extract clinical parameters, and deliver an interactive dashboard to client companies.

---

## 1. Architectural Decisions

1. **Owner-Controlled Feature Gating (`corporate_health_enabled`)**:
   - The feature is gated at the platform owner layer: **Owner Platform → Clinic → Feature Overrides → "Corporate Health Insights"**.
   - Enforced in `app/services/tenant.py` (`corporate_health_enabled`): Strictly restricted to `diagnostic_stream` and `diagnostic_test_booking` plans. The server refuses activation on any hospital or clinic specialty plan.
   - Disabled by default across all tenants until explicitly toggled by the platform owner.

2. **Zero-LLM Deterministic Text Parser (`pypdf` + regex)**:
   - Reports produced by the lab's LIS contain a native digital text layer.
   - Uses a deterministic line-oriented regex parser directly over extracted text streams.
   - **Zero AI API costs**, zero latency variance, and 100% reproducible extractions across runs.
   - Strictly refuses scanned image-only PDFs or multi-patient combined files with explicit error reasons rather than hallucinating or guessing.
   - Verified on real diagnostic PDFs: 100% match across all 54 clinical parameters and reference ranges.

3. **Dynamic, Sex-Specific Reference Range Extraction**:
   - Clinical reference ranges are read **directly from each report page**, capturing the lab's exact sex-specific ranges (e.g., Hemoglobin 13–17 g/dL for males vs. 11–15 g/dL for females; ESR 0–10 mm/hr for males vs. 0–15 mm/hr for females).
   - Multi-tier clinical band parsing: Complex panels like Vitamin D (where the first line is "Deficiency: < 20") are parsed down to the true "Sufficiency: 30–100 ng/mL" normal band.
   - Unclassifiable ranges are marked `"unclassified"`: counted as tested, but safely excluded from normal/abnormal percentage denominators.

4. **Data Minimization & DPDP Compliance**:
   - **Zero PDF or PII Storage**: Report PDFs, employee names, and uploaded filenames are **never stored on disk or in the database**.
   - Storage footprint: ~0.6 KB per employee record (`patient_sex`, `patient_age`, `collection_date`, `bill_id` for deduplication, and the 18 test results with normal/low/high classification).
   - 1,000 employees consume ~0.6 MB of structured database storage.
   - Preserving individual anonymized records (rather than pre-aggregated totals) allows deleting specific invalid batches, preventing duplicate uploads by bill ID, and recalculating analytics if clinical definitions evolve.

5. **Server-Enforced Confinement for Company Viewers (`CORPORATE_VIEWER`)**:
   - Corporate client access is provided via a dedicated role: `staff_role = 'CORPORATE_VIEWER'` in `clinic_admins`.
   - **Enforced at the HTTP layer** in `verify_credentials` ([app/routers/admin.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/admin.py)): A viewer login is restricted strictly to `_CORPORATE_VIEWER_ROUTES` (`/admin/me`, username/password changes, `/admin/corporate-health/companies`, and `/admin/corporate-health/companies/{id}/insights`).
   - Every other `/admin/*` route (patients, appointments, doctors, billing, settings) immediately rejects company viewers with `HTTP 403 Forbidden`. Hiding menu items is cosmetic; the API layer enforces absolute confinement.
   - The user's pinned `corporate_client_id` is re-validated against the database on every single request.

6. **Small-Cohort Privacy Guard**:
   - While company logins only see aggregate statistics, extreme filter combinations (e.g. Female, Age 60+) in small organizations could inadvertently isolate individual health data.
   - The dashboard dynamically flags any filtered cohort with fewer than 10 employees with an explicit privacy warning banner.

---

## 2. What Shipped

| Area | Component & Details |
| :--- | :--- |
| **Database (095)** | `migrations/095_corporate_health.sql`: Created `corporate_companies` and `corporate_health_records` with RLS, foreign keys, unique constraint on `(clinic_id, company_id, bill_id)`, and cascade deletes. Added `corporate_client_id` foreign key on `clinic_admins`. Added rollback script `migrations/rollback/095_down.sql`. |
| **Tenancy** | [app/tenancy.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py): Added `corporate_companies` and `corporate_health_records` to `TENANT_OWNED_TABLES` (AST isolation linter enforced). |
| **Service Layer** | [app/services/corporate_health.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/corporate_health.py): Deterministic 18-parameter parser, reference range resolution, deduplication by bill ID, cohort aggregation across gender and age groups, CSV export. |
| **Router** | [app/routers/corporate_health.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/corporate_health.py): Mounted at `/admin/corporate-health`. Endpoints for companies CRUD, batch PDF uploads, report deletion, company-viewer credentials management, insights analytics, and CSV exports. |
| **Auth & Security** | [app/routers/admin.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/routers/admin.py): Added `CORPORATE_VIEWER` role lockdown in `verify_credentials`, prevented role tampering in `update_staff`, and exposed `corporate_health_enabled` flag on `/admin/me`. |
| **Permissions** | [app/services/permissions.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/permissions.py): Defined `CORPORATE_VIEWER` role and `CORPORATE_HEALTH_MANAGE` granular staff permission. |
| **Lab Admin UI** | [admin/index.html](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html): "Corporate Health" navigation tab and full dashboard with 4 tabs: Upload reports (drag-and-drop with per-file status), Uploaded reports (paged, delete one / delete all), Company access (credentials generator), and Insights. |
| **Company Viewer UI** | [admin/index.html](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html): Dedicated single-view portal for corporate HR logins displaying exclusively their own company's Insights dashboard, print/PDF layout, dark/light theme support, and mobile responsiveness. |
| **Owner Platform UI** | [admin/platform.html](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/platform.html): Added "Corporate Health Insights" checkbox under Feature Overrides for diagnostic clinics. |
| **Tests** | [tests/test_corporate_health.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_corporate_health.py) (96 tests) and [tests/test_migration_095_corporate_health.py](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/test_migration_095_corporate_health.py) (7 tests). |

---

## 3. Supported Clinical Parameters (18 Tests Across 6 Panels)

1. **Blood Count**: Hemoglobin (Hb%), ESR
2. **Diabetes**: Fasting Blood Sugar (FBS), HbA1c
3. **Lipid Profile**: Total Cholesterol, Triglycerides
4. **Liver Function**: Total Bilirubin, SGOT (AST), SGPT (ALT), Total Protein
5. **Kidney Function**: Serum Creatinine, Blood Urea
6. **Vitamins & Minerals**: Vitamin D (25-OH), Vitamin B12, Serum Calcium
7. **Thyroid**: Total T3, Total T4, TSH

---

## 4. Verification Evidence

| Verification Phase | Target / Scope | Result |
| :--- | :--- | :--- |
| **Parser & Calculations** | 18 clinical parameters, units, reference range extraction, male/female splits, age brackets | 96 passed |
| **Real Postgres Migration** | Schema constraints, indexes, cascade deletions, re-runnability | 7 passed |
| **AST Tenant Isolation Linter** | `tests/test_lint_unscoped_queries.py` across all new routes and service functions | 4 passed |
| **Security & Regressions** | `tests/test_audit_p0_p1_regressions.py` (RT-16 schema_migrations ownership, route matrices) | 6 passed |
| **Full Regression Suite** | Entire repository hermetic test suite | 3,638 passed |
| **End-to-End Browser Check** | Verified lab admin upload workflow, duplicate detection, and company viewer lockdown | Clean |

---

## 5. Deployment & Go-Live Runbook

1. **Supabase SQL Editor**:
   Execute `migrations/095_corporate_health.sql` on the live database, then record the migration:
   ```sql
   INSERT INTO schema_migrations (name, checksum) VALUES
       ('095_corporate_health.sql', '7d4b766294155158d669f6077e208647807971d5c1fe7e61be52f7b3c4d36014')
   ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum;
   ```
2. **Git Commit & Push**:
   Deploy the codebase to Render. Render preflight will verify that `highest_applied = 095` matches `disk = 095`.
3. **Owner Platform Activation**:
   Navigate to Platform Admin → Select Target Diagnostic Lab (e.g. Taiyo Labs) → Clinic Config → Feature Overrides → Tick **"Corporate Health Insights"** → Save.
4. **Lab Admin Configuration**:
   - Open Clinic Admin → Navigate to **Corporate Health** tab.
   - Create a Company (e.g. "Acme Corp – 2026 Annual Camp").
   - Drag & drop PDF reports into the Upload tab.
   - Navigate to **Company Access** tab to generate login credentials for the client company HR.

---

## 6. Operational Considerations & Best Practices

- **Report Layout Tuning**: The regex parser is optimized for Taiyo Labs' LIS report formats. Non-standard layouts from third-party labs will fail gracefully into "unclassified" or raise explicit parse warnings without producing incorrect numbers.
- **Upload Throughput**: PDF parsing takes ~1 second per report and processes sequentially to ensure the event loop remains responsive and Meta webhook 20-second SLAs are unaffected. A batch of 300 reports takes ~5 minutes.
- **Camp Segmentation**: Annual screening camps should be provisioned as separate companies (e.g. "Acme Corp – 2025", "Acme Corp – 2026") so health metrics are evaluated per camp rather than merged into a single multi-year bucket.
