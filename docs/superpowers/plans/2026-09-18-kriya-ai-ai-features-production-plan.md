# Kriya AI — Production AI Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-ml:subagent-driven-development (recommended) or superpowers-ml:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 7 production-grade AI capabilities for Kriya AI (Staff Inbox with Suggested Replies, AI Price-List Import, AI Test/Package Details, Weekly AI Insights, Multilingual & Synonym Search, Catalogue Cleanup Suggestions, AI Model Router & Cost Governance) with zero regressions, strict multi-tenant isolation, and complete automated verification.

**Architecture:** Centralized resilient AI Gateway with task-based model routing, exponential backoff, circuit breaker, and token cost accounting; tenant-scoped database persistence using Supabase `scoped_query`; multi-format parser pipeline (PDF/Excel/CSV/Image) with preview-before-apply safety; hybrid lexical-synonym-multilingual catalogue search engine; and dedicated administrative interfaces in `admin/index.html`.

**Tech Stack:** Python 3.12, FastAPI, Supabase PostgreSQL with RLS, OpenRouter AI, Groq Fallback, Meta WhatsApp Cloud API, pdfplumber, openpyxl, pytesseract, Pillow, pytest.

---

## 1. Current Architecture
- **Multi-Tenant SaaS:** Centralized PostgreSQL database scoped by `clinic_id` on all 28 business tables (`TENANT_OWNED_TABLES`).
- **Off-loop Execution:** `app.database.sb()` handles PostgREST calls off the event loop via bounded thread pool `_DB_EXECUTOR`.
- **LLM Integration:** `OpenRouterService` implementing `ILLMProvider` in `app/services/ai_engine.py`, defaulting to `deepseek/deepseek-chat` with Groq fallback.
- **Safety Gate:** Zero-LLM deterministic `clinical_firewall.py` intercepting prescription, dosage, and diagnostic queries before reaching models.
- **Catalogue & Search:** `lab_tests` table with categories (080) and details (083); in-memory substring search via `ConversationManager._match_lab_tests`.

---

## 2. Existing Workflow Dependencies & Invariants
- **WhatsApp Webhook:** Strict 20s latency requirement from Meta. Any slow AI operation must run in background or fail fast to deterministic fallback.
- **Tenant Scope:** `is_valid_clinic_scope(clinic_id)` must guard every query. Unscoped queries on tenant tables violate CI scoping linter.
- **Staff Control:** AI suggestions must never be sent to patients automatically. Explicit human staff action is mandatory.
- **Catalogue Integrity:** AI-extracted price lists and generated test details must never write directly to production without admin review and approval.

---

## 3. Existing Risks & Pre-Existing Gaps Discovered
1. `conversations` in `escalated_to_human` state currently reset to `main_menu` upon subsequent patient messages due to fallback `else:` block in `handle_message`.
2. Admin panel lacked a dedicated interface for staff to inspect escalated conversations and send replies.
3. Catalogue search did not support common synonyms ("sugar" -> glucose), Indian languages (Hindi, Telugu), or misspellings.
4. AI spend was not tracked per tenant, risking runaway API costs.

---

## 4. Proposed Architecture Overview

```
[Patient on WhatsApp] ──> [Meta Cloud API] ──> [Webhook / Queue] ──> [Conversation FSM]
                                                                             │
    ┌────────────────────────────────────────────────────────────────────────┘
    ▼
[State: escalated_to_human] ──> [Staff Inbox (Admin Panel)]
                                      │
                                      ▼
                        [AI Gateway (Task: STAFF_REPLY)]
                                      │ (Grounded in Clinic Prices, Tests, Timings)
                                      ▼
                             [Suggested Reply Preview]
                                      │ (Staff Edit / Approve)
                                      ▼
                            [Send via WhatsApp API]
```

### AI Gateway Task Routing Matrix:
| Task Type | Primary Model | Fallback Model | Timeout | Max Tokens | Response Format |
|---|---|---|---|---|---|
| `INTENT_DETECTION` | `deepseek/deepseek-chat` | `groq/llama-3.3-70b-versatile` | 5s | 100 | text |
| `SYMPTOM_MAPPING` | `deepseek/deepseek-chat` | `groq/llama-3.3-70b-versatile` | 6s | 250 | json_object |
| `STAFF_REPLY` | `deepseek/deepseek-chat` | `groq/llama-3.3-70b-versatile` | 8s | 400 | text |
| `CATALOGUE_EXTRACTION` | `deepseek/deepseek-chat` | `groq/llama-3.3-70b-versatile` | 15s | 2500 | json_object |
| `WEEKLY_SUMMARY` | `deepseek/deepseek-chat` | `groq/llama-3.3-70b-versatile` | 10s | 800 | json_object |
| `DETAIL_GENERATION` | `deepseek/deepseek-chat` | `groq/llama-3.3-70b-versatile` | 8s | 500 | json_object |
| `SEARCH_QUERY` | Deterministic Lexicon | `deepseek/deepseek-chat` | 3s | 150 | text |

---

## 5. Database Changes (Migration 084)

Additive SQL migration `migrations/084_ai_features_infrastructure.sql`:
1. `staff_suggested_replies`: Tracks AI drafts, staff edits, sending actions, and token costs.
2. `catalogue_import_previews`: Staging table for multi-modal uploads (PDF/Excel/CSV/Image) before admin apply.
3. `ai_usage_ledger`: Per-tenant, per-task token accounting and monetary spend tracking in paise.
4. Updates `app/tenancy.py` to register all 3 tables in `TENANT_OWNED_TABLES`.
5. Rollback migration in `migrations/rollback/084_rollback_ai_features_infrastructure.sql`.

---

## 6. File-by-File Implementation Plan

### Task 1: Migration 084 & Tenancy Registration
**Files:**
- Create: `migrations/084_ai_features_infrastructure.sql`
- Create: `migrations/rollback/084_rollback_ai_features_infrastructure.sql`
- Modify: `app/tenancy.py`
- Test: `tests/test_migration_084.py`

- [ ] **Step 1: Write failing test for Migration 084 and Tenancy Registration**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Create SQL migration files and update `TENANT_OWNED_TABLES` in `app/tenancy.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Run CI query scoping linter to verify no regressions**

---

### Task 2: Centralized AI Gateway & Cost Governance (Feature 7)
**Files:**
- Create: `app/services/ai_gateway.py`
- Modify: `app/services/ai_engine.py`
- Create: `tests/test_ai_gateway.py`

- [ ] **Step 1: Write failing unit test for `AIGateway`**
  - Test task-aware model routing
  - Test primary to fallback failover
  - Test 429 exponential backoff
  - Test token accounting & ledger logging
  - Test monthly clinic budget enforcement (50%, 80%, 90%, 100% cap)
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `AIGateway` in `app/services/ai_gateway.py` and bridge `ai_engine.py`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Verify existing OpenRouter tests continue to pass**

---

### Task 3: Multilingual, Synonym & Hybrid Search (Feature 5)
**Files:**
- Create: `app/services/hybrid_search.py`
- Modify: `app/services/vector_search.py`
- Modify: `app/services/conversation.py:4795-4830`
- Create: `tests/test_hybrid_search.py`

- [ ] **Step 1: Write failing unit tests for Hybrid Search**
  - Test exact token containment (100% compatibility with existing `_match_lab_tests`)
  - Test synonym expansion ("sugar" -> glucose / HbA1c, "lipid" -> cholesterol)
  - Test Hindi search queries ("शुगर", "थायराइड", "खून की जांच")
  - Test Telugu search queries ("షుగర్", "రక్త పరీక్ష")
  - Test typo tolerance ("tyroid", "suger")
  - Test tenant isolation (results strictly scoped to clinic catalogue)
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `HybridSearchService` and integrate with `ConversationManager._match_lab_tests`**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Verify all 27 tests in `tests/test_lab_test_search.py` pass without regression**

---

### Task 4: Staff Inbox with AI-Suggested Replies (Feature 1)
**Files:**
- Create: `app/services/staff_inbox.py`
- Modify: `app/routers/admin.py`
- Modify: `app/services/conversation.py:1247-1260` (Hold conversation in `escalated_to_human`)
- Modify: `admin/index.html` (Add Staff Inbox tab and UI)
- Create: `tests/test_staff_inbox.py`

- [ ] **Step 1: Write failing unit and integration tests for Staff Inbox**
  - Test conversation parking in `escalated_to_human` without state drop
  - Test suggestion generation strictly grounded in clinic tests/prices/doctors
  - Test anti-hallucination boundary (refuses to invent missing data)
  - Test staff reply sending via WhatsApp
  - Test audit logging and reply status update (`sent`, `edited_and_sent`, `rejected`)
  - Test tenant isolation (cross-clinic conversation access rejected with 403/404)
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `staff_inbox.py`, admin endpoints, and admin UI tab**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Verify admin inline JS syntax via `node`**

---

### Task 5: Multi-Modal Price-List Import Pipeline (Feature 2)
**Files:**
- Create: `app/services/catalogue_importer.py`
- Modify: `app/routers/admin.py`
- Modify: `admin/index.html` (Add Multi-format Upload & Preview modal in Test Catalog)
- Create: `tests/test_catalogue_importer.py`

- [ ] **Step 1: Write failing tests for Multi-Modal Importer**
  - Test PDF parsing via `pdfplumber`
  - Test Excel (.xlsx) parsing via `openpyxl`
  - Test CSV parsing with formula protection
  - Test Image OCR parsing via `pytesseract`
  - Test structured AI normalization & preview generation
  - Test flagging of duplicates, suspicious prices, and invalid fields
  - Test transactional preview apply (safe from corruption or duplicate creation)
  - Test tenant isolation
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `catalogue_importer.py`, endpoints in `admin.py`, and UI in `admin/index.html`**
- [ ] **Step 4: Run test to verify it passes**

---

### Task 6: AI-Generated Package & Test Details (Feature 3)
**Files:**
- Create: `app/services/test_detail_generator.py`
- Modify: `app/routers/admin.py`
- Modify: `admin/index.html` (Add "AI Details" button in Lab Test edit modal)
- Create: `tests/test_test_detail_generator.py`

- [ ] **Step 1: Write failing tests for Test Detail Generation**
  - Test prompt grounding and clinical safety (no treatment/diagnosis claims)
  - Test field constraints (description <= 500 chars, category <= 60 chars)
  - Test admin preview and explicit approval workflow
  - Test tenant isolation
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `test_detail_generator.py`, admin endpoints, and UI modal integration**
- [ ] **Step 4: Run test to verify it passes**

---

### Task 7: Catalogue Clean-Up Suggestions (Feature 6)
**Files:**
- Create: `app/services/catalogue_cleaner.py`
- Modify: `app/routers/admin.py`
- Modify: `admin/index.html` (Add "Quality & Cleanup" tab/button in Test Catalog)
- Create: `tests/test_catalogue_cleaner.py`

- [ ] **Step 1: Write failing tests for Catalogue Cleaner**
  - Test duplicate detection (Levenshtein + token sort, e.g. "CBC" vs "Complete Blood Count")
  - Test suspicious price detection (outlier high/low, zero price)
  - Test missing clinical prep detection for fasting tests
  - Test category anomaly detection
  - Test preview generation and safe admin application
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `catalogue_cleaner.py`, admin endpoints, and UI integration**
- [ ] **Step 4: Run test to verify it passes**

---

### Task 8: Weekly AI Insights Summary (Feature 4)
**Files:**
- Create: `app/services/insights_summarizer.py`
- Modify: `app/routers/admin.py`
- Modify: `admin/index.html` (Add AI Summary card to Insights page)
- Create: `tests/test_insights_summarizer.py`

- [ ] **Step 1: Write failing tests for Insights Summarizer**
  - Test intake of real `get_insights()` data payload
  - Test separation of facts, interpretations, and operational actions
  - Test anti-hallucination constraint (no fabricated statistics)
  - Test caching per tenant/week
  - Test tenant isolation
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `insights_summarizer.py`, admin endpoints, and UI card**
- [ ] **Step 4: Run test to verify it passes**

---

### Task 9: Full Regression, Security & Production Verification (Part 20)
**Files:**
- Create: `tests/test_ai_features_e2e_matrix.py`
- Create: `docs/audits/2026-09-18-ai-features-production-verification.md`

- [ ] **Step 1: Run complete test suite (all unit, integration, tenant-isolation, and scoping tests)**
- [ ] **Step 2: Run adversary cross-tenant security matrix**
- [ ] **Step 3: Validate admin panel frontend JS syntax and responsive layout**
- [ ] **Step 4: Document final production verification report**

---

## 7. Rollback Strategy
- **Code Rollback:** Revert git commit cleanly. All new endpoints are additive.
- **Database Rollback:** Execute `migrations/rollback/084_rollback_ai_features_infrastructure.sql` which safely drops `staff_suggested_replies`, `catalogue_import_previews`, and `ai_usage_ledger`. Existing tables are unmodified.
- **Feature Disable:** Toggle `ENABLE_AI_FEATURES=false` in environment or clinic config if rapid runtime disabling is required.
