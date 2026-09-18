# Session 14 — AI Capabilities (Phase 1: Model Routing, Spend Ledger, Multilingual Search, Catalogue Clean-Up, AI Details)

**Date:** 2026-09-18  
**Branch:** main  
**Migration:** `084_ai_features_infrastructure.sql` (additive only, rollback in `migrations/rollback/084_down.sql`)

---

## 1. Scope & Core Architectural Constraints

Phase 1 delivers the foundational intelligence, spend governance, catalogue quality, and multilingual search capabilities:
- **Feature 7:** Model routing with multi-model fallback, token/cost spend ledger (`ai_usage_ledger`), and configurable monthly admin spend cap.
- **Feature 5:** Multilingual & synonym catalogue search (deterministic Hindi, Telugu, and English synonym dictionary with `difflib` typo tolerance; Tier 1 exact match in `ConversationManager._match_lab_tests` is 100% untouched and runs first).
- **Feature 6:** Catalogue clean-up analyzer (detects acronym/fuzzy duplicates, suspicious prices, and missing clinical fasting instructions; deletes strictly via existing `POST /admin/lab-tests/bulk-delete`).
- **Feature 3:** AI package & test details generator (replicates `generate_treatment_description` safety pattern: `sanitize_user_input`, `strip_injection_markers`, `_details_is_safe`, template fallback, preview-only).

### Non-Negotiable Invariants Enforced:
1. **Live Patient Chat Unchanged:** `ai_engine.py` is not rewired through `ai_gateway.py`. `detect_intent`, `map_symptom_to_department`, `generate_response`, and `call_openrouter_with_backoff` maintain their exact behavior and payload. Spend recording on patient chat is strictly a background fire-and-forget task (`record_ai_usage_bg`) that cannot alter or block the reply.
2. **Admin-Only Spend Cap:** The spend cap strictly fences administrative AI features (import, details, summaries, clean-up, reply drafts). Patient WhatsApp chat is never cut off or degraded by the cap, while its usage is recorded and visible in the spend ledger.
3. **Multi-Model Fallback & Native Cost Calculation:** Leverages OpenRouter's native `models: [primary, fallback]` configuration combined with existing 429/5xx exponential backoff retries. Money is tracked as integer paise based on actual token usage and configured USD→INR conversion.
4. **Deterministic Search:** Zero LLM or vector search dependencies on the patient search path. Tier 1 exact search returns first; Tier 2/3 multilingual & fuzzy search triggers only when Tier 1 returns zero results.
5. **Human-in-the-Loop & Audit Logging:** AI output never directly updates `lab_tests` or reaches patients without explicit admin approval. Every cleanup fix, bulk delete, and budget change is recorded via `log_admin_action`.

---

## 2. Changes Made

### A. Database Infrastructure
- **`migrations/084_ai_features_infrastructure.sql`**:
  - Creates `ai_usage_ledger` table with `clinic_id UUID REFERENCES clinics(id) ON DELETE CASCADE`. Columns include `task_type`, `provider`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_paise`, `is_fallback`, `success`, `error_message`, and `created_at`.
  - Indexes: `idx_ai_ledger_clinic_month` (`clinic_id`, `created_at DESC`), `idx_ai_ledger_task` (`clinic_id`, `task_type`).
  - Added `ai_budget_paise INT DEFAULT 50000` (₹500 default) to `clinics.config`.
  - Added `staff_inbox_enabled BOOLEAN DEFAULT FALSE` to `clinics.config` (opt-in prerequisite for Phase 3).
  - All operations are additive and idempotent (`IF NOT EXISTS`).
- **`migrations/rollback/084_down.sql`**: Full clean rollback dropping table, indexes, and config keys.
- **`app/tenancy.py`**: Added `ai_usage_ledger` to `TENANT_OWNED_TABLES` (enforced by `test_lint_unscoped_queries.py`).

### B. Backend Services
- **`app/config.py`**: Added `openrouter_fallback_model` (`google/gemini-2.5-flash`), `ai_usd_to_inr_rate` (87.0), and `ai_default_monthly_budget_paise` (50,000).
- **`app/services/ai_gateway.py`**:
  - `call_ai_gateway(...)`: Core admin AI dispatch supporting multi-model fallback (`models: [primary, fallback]`), spend cap checks, token accounting, and cost tracking.
  - `check_admin_spend_cap(clinic_id)`: Verifies current month spend against clinic budget in paise.
  - `record_ai_usage(...)` & `record_ai_usage_bg(...)`: Synchronous and fire-and-forget ledger logging with `# unscoped: insert_scoped_by_payload` tenant annotation.
- **`app/services/hybrid_search.py`**:
  - Deterministic multilingual dictionary covering English, Hindi (Devanagari + Romanized), and Telugu (Telugu script + Romanized) for common tests (sugar, glucose, thyroid, lipid, liver, renal, CBC, etc.).
  - `difflib.get_close_matches` fuzzy typo tolerance (0.75 cutoff).
  - Conjunctive token matching: enforces that in multi-token queries, all tokens must match either directly, synonymously, or via fuzzy similarity.
- **`app/services/conversation.py`**:
  - Updated `ConversationManager._match_lab_tests` to invoke `multilingual_synonym_search` as Tier 2/3 only when Tier 1 exact match yields no results.
- **`app/services/catalogue_cleaner.py`**:
  - Analyzes a clinic's catalogue for exact normalized duplicates (same branch) and acronym equals full name (e.g. `CBC` = `Complete Blood Count`).
  - Strict clinical exclusions: differences in numbers (e.g., Vitamin B1 vs B12), antibody classes (IgG vs IgM/IgA/IgE), radiological views (AP vs PA vs LAT), laterality, single letter, or across branches are never paired.
  - Grouping uses Union-Find with O(n) hash bucketing, running via `asyncio.to_thread`. 1,500 rows benchmarked in ~0.0087s (< 2s).
- **`app/services/test_detail_generator.py`**:
  - Generates clinical package descriptions and preparations using `call_ai_gateway` with fallback to deterministic templates.
  - Validates inputs via `sanitize_user_input` and `strip_injection_markers`.
  - Runs output safety checks (`_details_are_safe`) ensuring no diagnostic claims or prescriptions.
- **`app/routers/admin.py`**:
  - `GET /admin/lab-tests/cleanup-suggestions`: Returns identified duplicates, missing prep tests, and suspicious prices.
  - `POST /admin/lab-tests/cleanup-apply`: Applies selected fixes writing strictly to `prep_instructions` (max 500 chars), validates test_ids as UUIDs, bounds prices (₹1–₹1,00,000), runs updates before deletes (aborts deletes on update failure), and audit-logs actual IDs and names.
  - `POST /lab-tests/{test_id}/generate-details`: Generates a preview description for a test or package.
  - `GET /admin/ai/usage`: Returns current month spend breakdown (admin spend, patient spend, budget, task breakdown), paginating 1,000-row pages across the entire month.
  - `GET /admin/ai/budget`: Read-only view for clinic admins to view configured monthly budget and spend. Setting budget is moved to platform owner.
- **`app/routers/platform.py` & `admin/platform.html`**:
  - `POST /platform/clinics/{clinic_id}/ai-budget` (with `PATCH` alias): Dedicated platform-owner-only route for setting and updating a clinic's monthly administrative AI budget.
  - Clinic management modal in platform dashboard includes an AI Monthly Budget input and save handler.

### C. Admin Panel UI (`admin/index.html`)
- **AI Budget & Spend Tracking**: Read-only display of configured budget, month spend, and task breakdown for clinic admins.
- **Quality & Clean-Up Modal**: Nothing is pre-ticked. Bulk delete confirmation features danger button and requires typing `DELETE` for >= 20 tests.
- **AI Draft Details**: Reads test ID from `f-labTestId`. Disabled with "Save the test first" for unsaved tests. If template is returned, displays "AI unavailable — write details manually" and leaves the field untouched.

---

## 3. Verification & Test Outcomes

1. **Full Test Suite:**
   - Command: `pytest tests -q --ignore=tests/test_multi_worker_smoke.py`
   - Result: **2650 passed, 1 skipped, 0 failed, 0 errors** in 334.11s (5m 34s).
2. **Phase 1 Specific Suites:**
   - `tests/test_phase1_fixes.py`: 6 passed.
   - `tests/test_ai_gateway.py`: 9 passed.
   - `tests/test_hybrid_search.py`: 16 passed.
   - `tests/test_catalogue_cleaner.py`: 6 passed (1,500 rows in 0.0087s).
   - `tests/test_test_detail_generator.py`: 8 passed.
   - `tests/test_lab_tests_bulk_delete.py`: 12 passed.
3. **Frontend JavaScript Syntax Check:**
   - Executed `node --check` across inline scripts from both `admin/index.html` and `admin/platform.html`.
   - Result: **0 syntax errors, exit code 0**.
4. **Catalogue Clean-Up Scan Benchmark:**
   - 1,500 rows scanned and analyzed in **0.0087 seconds** (requirement: < 2.0 seconds).
5. **Process Table Hygiene:**
   - Verified that no orphan `python`, `uvicorn`, `pytest`, or `postgres` processes remain running.

---

## 4. Phase 1 Deployment Instructions

Before proceeding to Phase 2:
1. Apply database migration `migrations/084_ai_features_infrastructure.sql` to the production database:
   ```bash
   psql -d "$DATABASE_URL" -f migrations/084_ai_features_infrastructure.sql
   ```
2. Set optional environment variable overrides if desired (defaults apply automatically):
   - `OPENROUTER_FALLBACK_MODEL` (default: `google/gemini-2.5-flash`)
   - `AI_USD_TO_INR_RATE` (default: `87.0`)
   - `AI_DEFAULT_MONTHLY_BUDGET_PAISE` (default: `50000`)
3. Deploy application backend and updated `admin/index.html`.
4. Verify `/admin/ai/usage` and catalogue clean-up modal in the admin panel.

---
**Status:** Phase 1 complete and verified. Ready for deployment before Phase 2.
