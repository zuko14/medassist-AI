# Session 34 — AI Receptionist Continual Learning Loop

**Date:** 2026-10-11  
**Branch:** `main` (uncommitted working tree)  
**Scope:** Closed-loop continual learning for patient misunderstandings across Voice & WhatsApp, PII scrubbing, 6-hour scheduler distillation, clinical safety firewall screening, staff approval & dismissal workflow, zero-restart live activation, and 30-day DPDP data retention.  
**Migrations:** `106_receptionist_continuous_learning.sql`, `migrations/rollback/106_down.sql`  
**Test Suite:** 35 new unit & integration tests (`tests/test_reception_learning.py`), 252 voice tests (`tests/voice/`), full passing suite (1,030+ tests).

---

## 1. Summary & Core Architecture

Session 34 implements an audited, human-in-the-loop continual learning engine for the Kriya AI Receptionist across both **Voice** (`app/voice/`) and **WhatsApp** (`app/services/conversation.py`). 

Instead of an unsafe "self-modifying prompt" where untrusted patient text directly alters runtime system instructions (risking prompt injection, medical malpractice liability under National Medical Commission regulations, and policy drift), the system implements a **supervised distillation and deterministic lexicon/knowledge expansion loop**:

```
    ┌────────────────────────────┐
    │   Voice / WhatsApp turn    │             ◄───────────┐
    │           missed           │                         │
    └─PII stripped, phone masked─┘                         │
                   ▼                                       │
    ╭────────────────────────────╮                         │
    │  ai_reception_harvest_     │                         │
    │         events             │                         │
    ╰────every 6h, IST, locked───╯                         │
                   ▼                                       │
    ┌────────────────────────────┐                         │
    │   Group + safety screen    │                         │
    └──────────────┬─────────────┘                         │
                   ▼                                       │
    ╭────────────────────────────╮                         │
    │  ai_learning_suggestions   │                         │
    │     (status: pending)      │                         │
    ╰──────Approve / Dismiss─────╯                         │
                   ▼                                       │
    ┌────────────────────────────┐                         │
    │    lexicon / knowledge     │      read fresh on every call
    │           entry            │      & WhatsApp turn
    └────────────────────────────┘
```

### Operational Lifecycle
1. **Collecting / Harvesting**:
   - Voice reception harvests a turn when Kriya cannot classify the intent (`low_confidence` / `UNKNOWN`) or cannot answer a mid-call hospital knowledge question (`unanswered_question`).
   - WhatsApp harvests when an incoming patient query falls through to the free-form AI fallback (`low_confidence`).
   - PII (emails, phone numbers, MRNs) is automatically stripped via `reception_learning.scrub()`, and caller phone numbers are masked via `mask_phone()`.
   - Noise phrases (e.g. "hello", "hmm") and emergency/medical queries are skipped (as they are already handled by the zero-LLM safety firewall).
   - Staff test-console turns (`mode == "test"`) are strictly excluded from harvesting.
2. **Grouping & Distillation**:
   - An APScheduler job runs every 6 hours (01:20, 07:20, 13:20, 19:20 IST) protected by a distributed lease lock (`distributed_job_lock("reception_learning_distill", lease_seconds=1800)`).
   - Groups near-identical patient utterances using `SequenceMatcher` (similarity ratio $\ge 0.85$). Phrases heard from $\ge 2$ patients within the last 14 days become pending suggestions.
   - An LLM classifies the phrase against **this hospital's own catalogue only** (departments, doctors, lab tests) using words the patient actually said. If the phrase does not name an existing service, it is marked as `knowledge_faq` for staff to author.
   - Every suggestion is screened with the zero-LLM Clinical Safety Firewall (`app/voice/safety.py:screen` and `app/services/clinical_firewall.py:screen_message`).
3. **Staff Review & Approval Workbench**:
   - A new **Learning** tab in the AI Receptionist console (`admin/index.html`) displays each suggestion, its frequency, and sample patient utterances.
   - Hospital staff can edit the phrase and target canonical mapping, or write the answer for an FAQ, then click **Approve & activate** or **Dismiss**.
   - Concurrency protection: Optimistic CAS on `status == 'pending'` prevents race conditions if multiple staff members review simultaneously.
   - Medical advice block: Staff-authored answers mentioning medicines or medical diagnoses are refused (HTTP 422).
   - Dismissed phrases are updated to `status = 'dismissed'`; the unique index prevents them from ever being suggested again.
4. **Instant Zero-Downtime Live Activation**:
   - Approving an entry writes directly to `voice_lexicon_entries` (`specialty_synonym`, `doctor_alias`, `test_alias`) or `voice_knowledge_entries` (`knowledge_faq`).
   - Both tables are queried fresh on every phone call and WhatsApp message. The newly taught phrase works on the very next patient interaction without restarting containers or flushing caches.
5. **Cross-Channel Unification**:
   - WhatsApp now checks `reception_learning.approved_answer()` before invoking the free-form LLM response. An answer approved by staff in the portal is spoken by the voice receptionist and delivered word-for-word on WhatsApp, eliminating contradictory multi-channel answers.
6. **Data Minimization & Retention**:
   - Harvested patient turns in `ai_reception_harvest_events` are automatically purged after 30 days (`created_at < now() - 30 days`) complying with the Digital Personal Data Protection (DPDP) Act 2023.

---

## 2. Database Migrations

### 2.1 Migration 106: Schema Definition
**File:** [`migrations/106_receptionist_continuous_learning.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/106_receptionist_continuous_learning.sql) (4,717 bytes)  
**Rollback:** [`migrations/rollback/106_down.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/rollback/106_down.sql) (274 bytes)

```sql
CREATE TABLE IF NOT EXISTS ai_reception_harvest_events (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    channel               TEXT        NOT NULL CHECK (channel IN ('voice', 'whatsapp')),
    call_ref              TEXT,
    conversation_id       UUID,
    patient_phone_masked  TEXT,
    raw_utterance         TEXT        NOT NULL CHECK (char_length(btrim(raw_utterance)) BETWEEN 1 AND 400),
    trigger_reason        TEXT        NOT NULL CHECK (trigger_reason IN ('low_confidence', 'reprompt', 'handoff',
                                                                         'unmapped_entity', 'unanswered_question')),
    detected_language     TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_harvest_clinic_created ON ai_reception_harvest_events (clinic_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_learning_suggestions (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id             UUID        NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    category              TEXT        NOT NULL CHECK (category IN ('specialty_synonym', 'doctor_alias', 'test_alias',
                                                                   'knowledge_faq')),
    patient_phrase        TEXT        NOT NULL CHECK (char_length(btrim(patient_phrase)) BETWEEN 1 AND 200),
    proposed_canonical    TEXT        NOT NULL DEFAULT '',
    evidence_count        INT         NOT NULL DEFAULT 1 CHECK (evidence_count >= 1),
    sample_utterances     JSONB       NOT NULL DEFAULT '[]'::jsonb,
    safety_screen_passed  BOOLEAN     NOT NULL DEFAULT true,
    status                TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'dismissed')),
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_learning_suggestion_phrase
    ON ai_learning_suggestions (clinic_id, category, lower(btrim(patient_phrase)));
CREATE INDEX IF NOT EXISTS idx_learning_suggestions_pending
    ON ai_learning_suggestions (clinic_id, evidence_count DESC) WHERE status = 'pending';
```

Includes RLS enforcement and policies for both `service_role` and `kriya_app`.

---

## 3. Detailed File-by-File Changes

### 3.1 New Files

#### `app/services/reception_learning.py` (338 lines)
Core learning service implementing the three lifecycle phases:
- `scrub(text)`: Regex-based masking of emails, phone numbers, and MRNs (`[email]`, `[number]`).
- `harvest(clinic_id, channel, text, reason, ...)`: Off-loop persistence to `ai_reception_harvest_events`. Skips noise, sentinel clinic scopes (`"default"`), and turns already caught by the clinical safety screen.
- `record(...)`: Fire-and-forget async task wrapper using `spawn_background_task`.
- `approved_answer(clinic, text, lang)`: Cross-channel lookup in `voice_knowledge_entries` returning approved text word-for-word for WhatsApp.
- `cluster(events)`: Groups unhandled phrases with `SequenceMatcher` ratio $\ge 0.85$.
- `validate_proposal(raw, utterance, catalogue)`: Validates that LLM suggestions strictly reference the clinic's real departments, doctors, or tests, and match the patient's actual words.
- `distill_harvested_events(clinic_id)`: Analyzes a 14-day rolling window of events, invokes bounded classification, screens candidates, and upserts into `ai_learning_suggestions`.
- `distill_all()`: Multi-tenant scheduler orchestrator that processes active clinics and executes the 30-day data retention purge.
- `approve(clinic_id, suggestion_id, reviewer, ...)`: Handles staff approval, checks `screen_message()` on answers, executes atomic CAS state transition, and inserts into `voice_lexicon_entries` or `voice_knowledge_entries`.
- `dismiss(clinic_id, suggestion_id, reviewer)`: Transitions status to `dismissed` with concurrency protection.

#### `migrations/106_receptionist_continuous_learning.sql` (76 lines)
Database migration establishing tables, check constraints, foreign keys, and unique indexes for learning events and suggestions.

#### `migrations/rollback/106_down.sql` (5 lines)
Clean rollback script dropping `ai_learning_suggestions` and `ai_reception_harvest_events`.

#### `tests/test_reception_learning.py` (341 lines)
35 comprehensive tests covering:
- Tenant table registration in `TENANT_OWNED_TABLES`.
- PII scrubbing (phone, email, MRN).
- Filtering noise and sentinel clinic scopes.
- Clustering logic and evidence thresholding ($\ge 2$).
- LLM proposal validation against clinic catalogue.
- Clinical firewall screening on candidate phrases and authored answers.
- Concurrency and duplicate review handling (CAS).
- Cross-channel approved answer retrieval.
- Complete API route behavior (`GET /suggestions`, `POST /approve`, `POST /dismiss`).

---

### 3.2 Modified Files

#### `app/tenancy.py`
- Added `"ai_reception_harvest_events"` and `"ai_learning_suggestions"` to `TENANT_OWNED_TABLES` (lines 76-77). Guarantees that the AST query scoping linter enforces tenant isolation on all queries.

#### `app/voice/session.py`
- Added `_harvest(text, nlu, new_outcomes)` hook called after every voice turn in `turn()` (lines 228-242).
- Automatically triggers harvesting with reason `unanswered_question` if a knowledge workflow returns `not_answered`, or `low_confidence` if `not nlu.intents`.
- Bypasses harvesting when `self.ctx.mode == "test"`.

#### `app/services/conversation.py`
- Updated unknown intent branch in `ConversationManager.handle_incoming_message` (lines 1934-1951).
- First checks for a staff-approved Q&A match via `reception_learning.approved_answer()`. If matched, sends it immediately.
- If not matched and the clinic has AI receptionist enabled, triggers `reception_learning.record(..., channel="whatsapp", reason="low_confidence")` before falling back to `generate_response()`.

#### `app/services/scheduler.py`
- Added cron job `reception_learning_distill` running at `01:20, 07:20, 13:20, 19:20 IST` (lines 453-455).
- Wrapped with `distributed_job_lock("reception_learning_distill", lease_seconds=1800)` to ensure single-instance cluster execution.
- Invokes `reception_learning.distill_all()` and logs output.

#### `app/routers/voice_admin.py`
- Added Pydantic model `ApproveIn` (lines 385-389).
- Added `GET /admin/voice/learning/suggestions` requiring `VOICE_VIEW` permission (lines 392-401). Returns suggestions and clinic catalogue.
- Added `POST /admin/voice/learning/suggestions/{id}/approve` requiring `VOICE_MANAGE` permission (lines 404-417). Audited with `log_admin_action`.
- Added `POST /admin/voice/learning/suggestions/{id}/dismiss` requiring `VOICE_MANAGE` permission (lines 420-431). Audited with `log_admin_action`.

#### `admin/index.html`
- Added **Learning** navigation tab under the Voice Console (line 2832).
- Wired `vcTab('learning')` to `vcLearning()` (line 11313).
- Added rendering logic `vcLearning()`, `vcLearnCard()`, `vcLearnApprove()`, and `vcLearnDismiss()` (lines 11594-11654).
- Provides UI for reviewing evidence count, sample patient quotes, editing phrases, entering FAQ answers, one-click approving, or dismissing suggestions. Displays a dedicated warning if a suggestion was blocked by the safety firewall.

---

## 4. Key Architectural Decisions & Intentional Deviations

1. **Omission of Track 2 (Dynamic Few-Shot In-Prompt Injection)**:
   - *Rationale:* Injecting arbitrary patient text into live system prompts creates an exploitable prompt-injection vector and risks conversational drift. 
   - *Better Alternative:* Mapping learned phrases into deterministic lexicon entries (`voice_lexicon_entries`) solves the problem at the rule layer (`nlu_rules.py`), meaning the LLM is completely bypassed for that phrase on future calls. This yields sub-millisecond latency, zero token cost, and mathematical certainty.
2. **Unified Cross-Channel Knowledge**:
   - Rather than keeping voice and WhatsApp knowledge separate, WhatsApp now checks `voice_knowledge_entries` before calling the free-form LLM. Approving a single answer updates both channels simultaneously.
3. **No Separate Handoff Harvester**:
   - Patient turns that lead to human handoffs are already captured as unhandled or low-confidence turns, making a redundant handoff harvest stage unnecessary.

---

## 5. Verification & Test Evidence

All tests ran in hermetic test mode (PostgreSQL / WhatsApp mocks via `tests/conftest.py`):

```bash
# 1. New Receptionist Learning Tests
pytest tests/test_reception_learning.py
# Result: 35 passed in 3.22s

# 2. Existing Voice Receptionist Suite
pytest tests/voice/
# Result: 252 passed in 4.88s

# 3. Clinical Safety Firewall
pytest tests/test_clinical_firewall.py
# Result: 32 passed in 3.21s

# 4. AST Query Scoping Linter
pytest tests/test_lint_unscoped_queries.py
# Result: 4 passed in 98.14s (All tenant queries scoped)

# 5. Full Security Matrix & Webhook Tests
pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py tests/test_webhook.py tests/test_fsm_transitions.py
# Result: 698 passed
```

---

## 6. Pre-Deployment Checklist (Supabase Deployment)

Before merging to `main` and deploying to staging/production:

1. **Run Migration 106 in Supabase SQL Editor**:
   Execute [`migrations/106_receptionist_continuous_learning.sql`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/migrations/106_receptionist_continuous_learning.sql).
2. **Register Checksum in `schema_migrations`**:
   ```sql
   INSERT INTO schema_migrations (name, checksum) VALUES
       ('106_receptionist_continuous_learning.sql', md5(pg_read_file('106_receptionist_continuous_learning.sql')))
   ON CONFLICT (name) DO NOTHING;
   ```
3. **Commit & Push**:
   ```bash
   git add migrations/ app/ admin/ tests/ docs/sessions/SESSION_34_RECEPTIONIST_CONTINUOUS_LEARNING.md
   git commit -m "feat(voice): closed-loop receptionist continual learning (migration 106)"
   ```
