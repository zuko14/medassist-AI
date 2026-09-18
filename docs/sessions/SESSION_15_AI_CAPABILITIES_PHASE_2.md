# Session 15 — AI Capabilities (Phase 2: Price-List Import Pipeline & Weekly Insights Summary Engine)

**Date:** 2026-09-18  
**Branch:** main  
**Migration:** `085_ai_features_phase2.sql` (additive only, rollback in `migrations/rollback/085_down.sql`)

---

## 1. Scope & Core Architectural Constraints

Phase 2 delivers two core administrative automation and operational intelligence capabilities:
- **Feature 2:** Price-list import pipeline (`POST /admin/lab-tests/import-preview`, `GET /admin/lab-tests/import-preview/{id}`, `POST /admin/lab-tests/import-apply/{id}`).
- **Feature 4:** Weekly insights summary engine (`GET /admin/insights/summary`, `POST /admin/insights/summary/generate`).

### Non-Negotiable Invariants Enforced (9 Rules):
1. **`created_by` Integrity:** `created_by` stores `user.user_id` as a string (never a username). Apply endpoint checks and compares `user_id` strictly.
2. **Atomic Apply:** Conditional query `UPDATE catalogue_import_previews SET status='applying' WHERE id=:id AND clinic_id=:cid AND created_by=:uid AND status='pending' AND expires_at > now()`. Returns `409 Conflict` if 0 rows matched (claim failed or already applied). On success sets `status='applied'` and records counts; on any failure rolls status back to `'pending'` so the admin can safely retry.
3. **Background Worker Processing:** `POST /admin/lab-tests/import-preview` validates size (<= 10MB + 1B) and magic bytes, inserts preview row with `status='processing'`, returns `202 Accepted` immediately, and dispatches processing via `spawn_background_task`. Polling on `GET /admin/lab-tests/import-preview/{id}` (`processing` -> `pending` or `failed`). Worker concurrency is guarded by `asyncio.Semaphore(1)`. OCR limit: <= 10 pages, 300 DPI, 120s timeout.
4. **Upload Defense-in-Depth:** Rejects files exceeding 10MB + 1 byte with 413. XLSX files inspect zip structure before parsing (total uncompressed <= 50MB, <= 200 entries). Images enforce `PIL.Image.MAX_IMAGE_PIXELS = 40_000_000` and downscale to max dimension 2000px before OCR. Legacy `.xls` rejected with "Please save as .xlsx or CSV".
5. **Zero Behavior Change on Existing CSV Import:** `import_lab_tests_csv` refactored to delegate to `_execute_lab_tests_upsert`. All existing CSV import tests pass unmodified; response shape and semantics are identical.
6. **Last Completed ISO Week (IST):** Weekly summary covers the last completed ISO week (Monday–Sunday, IST) vs the prior week. Scoped clinic-wide only; existing `get_insights()` is 100% untouched.
7. **Precomputed Deterministic Number Verification:** Every metric (counts, week-on-week changes, whole-number %, formatted rupees) is precomputed in the fact sheet. The LLM receives the fact sheet and is instructed to use only those numbers; an AST/regex verifier rejects any text containing digits not present in the fact sheet and falls back to a deterministic template.
8. **Zero AI Spend on Page Load:** `GET /admin/insights/summary` returns cached summary or `{"status": "not_generated"}` with zero LLM invocations. Generation occurs strictly upon explicit admin button click, capped at 3 generations per clinic per day in IST with atomic upsert on `(clinic_id, iso_year, iso_week)` clash.
9. **Automated Data Retention Purge:** Daily 4 AM purge job (`purge_expired_catalogue_import_previews`) runs under a distributed lock (`scheduler_locks`) and executes only when `APP_ENV == "production"`.

---

## 2. Changes Made

### A. Database Infrastructure
- **`migrations/085_ai_features_phase2.sql`**:
  - Creates `catalogue_import_previews` table with `id UUID PRIMARY KEY`, `clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE`, `branch_id UUID REFERENCES branches(id) ON DELETE SET NULL`, `status VARCHAR(20)` (`processing`, `pending`, `applying`, `applied`, `failed`, `expired`), `rows JSONB`, `created_by VARCHAR(255)`, `failure_reason TEXT`, `applied_counts JSONB`, `created_at TIMESTAMPTZ`, `expires_at TIMESTAMPTZ`, `applied_at TIMESTAMPTZ`.
  - Creates `weekly_insights_summaries` table with `id UUID PRIMARY KEY`, `clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE`, `iso_year INT`, `iso_week INT`, `fact_sheet JSONB`, `summary_text TEXT`, `source VARCHAR(20)` (`ai`, `template`), `regenerate_date VARCHAR(10)`, `regenerate_count INT`, `created_at TIMESTAMPTZ`, `updated_at TIMESTAMPTZ`, with unique constraint on `(clinic_id, iso_year, iso_week)`.
  - Indexes: `idx_catalogue_previews_clinic_status`, `idx_catalogue_previews_expires`, `idx_weekly_insights_lookup`.
- **`migrations/rollback/085_down.sql`**: Drops created tables and indexes cleanly.
- **`app/tenancy.py`**: Added `catalogue_import_previews` and `weekly_insights_summaries` to `TENANT_OWNED_TABLES`.
- **`requirements.txt`**: Added `openpyxl>=3.1.0,<4.0`.

### B. Backend Services
- **`app/services/price_list_parser.py`**:
  - `detect_magic_bytes(data, filename)`: Identifies real file type using file signatures (PDF `%PDF`, XLSX `PK\x03\x04`, PNG, JPEG, CSV plain text). Rejects legacy `.xls` (`\xD0\xCF\x11\xE0`).
  - `parse_catalogue_file(...)`: Main parsing pipeline with fast-path CSV & XLSX extraction, OCR fallback, LLM table structuring via `call_ai_gateway`, and deduplication matching against existing catalogue tests.
  - Generates row actions (`new` vs `update`) and price diff indicators for review.
- **`app/services/weekly_summary.py`**:
  - `get_last_completed_iso_week(now)`: Computes boundaries in IST for Monday 00:00:00 to Sunday 23:59:59.
  - `build_weekly_fact_sheet(clinic_id, now)`: Computes bookings, completions, cancellations, cancellation rates, collected revenue (paise/rupees), average ticket size, and service breakdown for both weeks with absolute and percentage diffs.
  - `verify_deterministic_numbers(summary_text, fact_sheet)`: Extracts all number sequences from summary and verifies every number exists in the precomputed fact sheet.
  - `build_template_weekly_summary(fact_sheet)`: Guaranteed fallback when AI fails, hallucinates numbers, or spend cap is exceeded.
  - `generate_weekly_summary(...)`: Orchestrates generation, rate limit checks (max 3/day IST), and upserts summary.
- **`app/services/data_retention.py` & `app/services/scheduler.py`**:
  - Added `purge_expired_catalogue_import_previews` method with `# unscoped: platform_sweep` annotation, guarded by `APP_ENV == "production"`.
  - Registered 4 AM daily purge cron with distributed lock in `scheduler.py`.
- **`app/routers/admin.py`**:
  - Extracted `_execute_lab_tests_upsert` from `import_lab_tests_csv` to share between single CSV imports and catalogue preview applications.
  - `POST /admin/lab-tests/import-preview`: Validates payload, stores preview, triggers async background job, returns 202.
  - `GET /admin/lab-tests/import-preview/{preview_id}`: Retrieves preview status and staged rows, checking expiration.
  - `POST /admin/lab-tests/import-apply/{preview_id}`: Atomic claim (`applying`), applies selected rows, logs admin action, returns counts.
  - `GET /admin/insights/summary`: Zero AI spend read-only endpoint returning cached summary or `not_generated`.
  - `POST /admin/insights/summary/generate`: Triggers summary generation with daily limit enforcement and upsert.

### C. Frontend UI (`admin/index.html`)
- **Price List Import Modal (`#priceListImportModal`)**:
  - Added "Import Price List" button in catalogue management toolbar.
  - 3-step modal flow: File Upload (drag-and-drop or picker, branch selection, default category) -> Processing state with 2s polling -> Review table with row selection, status badges (`NEW`, `UPDATE`, `PRICE CHANGE`), and price difference indicators.
  - Confirmation gate: For imports with >= 20 items, requires typing `IMPORT` to unlock the apply button.
- **Operational Summary Card in Insights**:
  - Clean executive summary card in Insights tab with date badge (Week N, YYYY).
  - Fact sheet comparison badges showing bookings, revenue, and cancellation changes.
  - Formatted markdown display for executive summary sections.
  - "Generate Weekly Summary" button with daily quota indicator ("Generations remaining today: X/3") and refresh capability.

---

## 3. Verification & Test Outcomes

1. **Full Test Suite:**
   - Command: `pytest tests -q --ignore=tests/test_multi_worker_smoke.py`
   - Result: **2686 passed, 1 skipped, 0 failed, 0 errors** in 252.99s (4m 12s).
2. **Existing CSV Import Test Integrity:**
   - `pytest tests/test_lab_tests_admin.py tests/test_lab_tests_branch_scoping.py tests/test_lab_test_categories.py -q`
   - Result: **91 passed, 1 skipped, 0 failed**.
   - `git diff tests/test_lab_tests_admin.py tests/test_lab_tests_branch_scoping.py tests/test_lab_test_categories.py` is **completely empty**.
3. **Phase 2 Test Suites:**
   - `tests/test_price_list_import.py`: **11 passed**.
     - Magic bytes validation, binary disguise rejection, zip bomb protection, upload limits, fast-path parsing, 202 preview status, tenant isolation, atomic claim conflict 409, apply failure rollback to pending, retention purge production guard.
   - `tests/test_weekly_insights_summary.py`: **10 passed**.
     - ISO week boundaries (Mon–Sun IST), fact sheet precomputations, number extraction, hallucination rejection, template fallback guarantee, zero spend on page load, max 3/day IST limit, successful AI generation, spend cap fallback.
   - `tests/test_lint_unscoped_queries.py` & `tests/test_phase2_unscoped_query_linter.py`: **5 passed**.
4. **Frontend JavaScript Syntax:**
   - Executed `node --check` on scripts extracted from `admin/index.html`.
   - Result: **All scripts passed node --check with 0 syntax errors**.

---

## 4. New Routes and Required Permissions

| Method | Endpoint | Permission | Status Code | Description |
|---|---|---|---|---|
| `POST` | `/admin/lab-tests/import-preview` | `LAB_TESTS_MANAGE` | `202 Accepted` | Upload file and queue background parsing |
| `GET` | `/admin/lab-tests/import-preview/{preview_id}` | `LAB_TESTS_MANAGE` | `200 OK` | Poll status and fetch staged review rows |
| `POST` | `/admin/lab-tests/import-apply/{preview_id}` | `LAB_TESTS_MANAGE` | `200 OK` | Atomically claim and apply selected rows |
| `GET` | `/admin/insights/summary` | `INSIGHTS_READ` | `200 OK` | Read cached summary (zero AI spend) |
| `POST` | `/admin/insights/summary/generate` | `INSIGHTS_READ` | `200 OK` | Generate weekly summary (max 3/day IST) |

---

## 5. Phase 2 Deployment Instructions

1. Apply database migration `migrations/085_ai_features_phase2.sql` to the production database:
   ```bash
   psql "$SUPABASE_DB_URL" -f migrations/085_ai_features_phase2.sql
   ```
2. Install new dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Restart application workers to activate scheduler purge cron and new router endpoints.
