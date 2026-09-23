# 06 - AI / LLM SYSTEM & CLINICAL ORCHESTRATION

This document details the artificial intelligence architecture, model providers, clinical safety firewalls, prompt engineering, usage tracking, and multi-tenant spend control in KriyaAI.

---

## 1. AI SUBSYSTEM OVERVIEW

KriyaAI employs a **defense-in-depth, hybrid AI architecture** designed specifically for healthcare environments:

```text
Patient WhatsApp Inbound
          ↓
[Input Sanitization] (strip_injection_markers, sanitize_user_input)
          ↓
[Clinical Firewall] ──(Medication / Advice Query)──> Deterministic Disclaimer & Book OPD
          ↓ (Safe Administrative / Scheduling Query)
[FAQ / Keyword Engine] ──(Exact Match)──> Deterministic Clinic Info (Hours, Address, UPI)
          ↓ (Natural Language / Intent / Symptom Triage)
[AI Gateway / ILLMProvider]
     ├── Primary: OpenRouter (deepseek/deepseek-chat)
     ├── Fallback: OpenRouter (google/gemini-2.0-flash-001)
     └── Tertiary: Local Rule Fallbacks
          ↓
[Structured Output Extraction] (Intent, Department, Doctor, Slot)
          ↓
[Async Usage & Cost Tracking] (ai_usage_ledger, integer paise)
```

---

## 2. LLM PROVIDERS & ADAPTERS

### Core Provider: OpenRouter
- **File**: [`app/services/ai_engine.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/ai_engine.py) (`OpenRouterService`), [`app/services/ai_gateway.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/ai_gateway.py) (`call_ai_gateway`)
- **Base URL**: `https://openrouter.ai/api/v1/chat/completions`
- **Primary Model**: `deepseek/deepseek-chat` (or configured via `OPENROUTER_MODEL`)
- **Fallback Model**: `google/gemini-2.0-flash-001` (configured via `OPENROUTER_FALLBACK_MODEL`)
- **Attribution Headers**:
  - `HTTP-Referer`: `https://kriya.health`
  - `X-Title`: `Kriya AI Healthcare OS`
- **Retry & Resilience Strategy**:
  - Maximum 2 attempts per completion.
  - Automatic 2-second sleep backoff on HTTP 429 (Rate Limit), 502, 503, 504, or `httpx.TimeoutException`.
  - Uses OpenRouter's native multi-model list: `"models": [primary, fallback]` allowing OpenRouter to automatically cascade to the fallback model within a single HTTP request if the primary is degraded.

---

## 3. CLINICAL FIREWALL (ZERO-LLM SAFETY SHIELD)

- **File**: [`app/services/clinical_firewall.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/clinical_firewall.py)
- **Rationale**: Strict compliance with Indian National Medical Commission (NMC) regulations. An LLM must **never** prescribe medications, alter dosages, or provide medical diagnoses to patients over WhatsApp.

### Screening Mechanisms
1. **Medication Keyword Blacklist (`MEDICATION_NAMES`)**:
   - Covers over 250 common Indian OTC and prescription pharmaceuticals across classes:
     - Antibiotics (Azithromycin, Amoxicillin, Augmentin, Ciprofloxacin, Cefixime)
     - Analgesics / Antipyretics (Paracetamol, Dolo, Crocin, Calpol, Ibuprofen, Combiflam, Meftal)
     - Antacids / PPIs (Pantoprazole, Omeprazole, Pan-D, Rantac, Gelusil, Digene)
     - Steroids (Prednisolone, Dexamethasone, Betamethasone)
     - Antidiabetic (Metformin, Glycomet, Glipizide, Insulin)
     - Cardiovascular / Antihypertensive (Aspirin, Ecosprin, Amlodipine, Telmisartan, Atorvastatin)
2. **Intent & Pattern Regex**:
   - Dosage requests: *"how many tablets", "kitni goli", "dosage for child"*
   - Diagnostic inquiries: *"what illness do I have", "kya bimari hai", "do I have cancer"*
   - Treatment seeking: *"what should I take for fever", "dawa batao", "mandhu cheppandi"*
3. **Multilingual Coverage**:
   - English, Hindi (Latin & Devanagari: *"dawa", "goli", "dawai"*), Telugu (Latin & Telugu script: *"mandhu", "taggadaniki"*).

### Action on Trigger
- Immediately aborts LLM invocation.
- Returns a standardized medical safety disclaimer advising the patient that an AI assistant cannot prescribe medicine, recommending immediate in-person consultation or emergency room care, and offering doctor appointment slots.

---

## 4. AI GATEWAY, SPEND CAPS & USAGE LEDGER

- **File**: [`app/services/ai_gateway.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/ai_gateway.py)

### Monthly Administrative Spend Cap
- **Function**: `check_admin_spend_cap(clinic_id)`
- **Behavior**:
  - Sums current UTC calendar month non-chat token costs from `ai_usage_ledger` for that `clinic_id`.
  - Compares against `clinic.config.ai_budget_paise` (defaults to 50,000 paise = ₹500.00).
  - If exceeded, raises `SpendCapExceededError`, blocking administrative AI operations (test detail generation, catalog auto-classification, insight summaries).
  - **CRITICAL INVARIANT**: Patient WhatsApp chat (`task_type == "patient_chat"`) is **exempt** from the spend cap and is **never blocked or degraded**.

### Token & Cost Accounting (`ai_usage_ledger`)
- Stores: `clinic_id`, `task_type`, `provider`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_paise`, `is_fallback`, `success`, `error_message`.
- Executed out-of-band via `spawn_background_task(record_ai_usage(...))` to ensure database writes do not add latency to user interactions.

---

## 5. DETERMINISTIC HYBRID SEARCH & KNOWLEDGE BASE

### Multilingual Synonym Search (`app/services/hybrid_search.py`)
- Provides zero-LLM, sub-millisecond catalogue discovery for lab tests and clinic services.
- Translates vernacular queries (Hindi & Telugu) into canonical medical terms:
  - Hindi: *शुगर / सुगर* -> *Sugar / Glucose / HbA1c*; *खून* -> *Blood / CBC*; *गुर्दा* -> *Kidney / KFT*
  - Telugu: *షుగర్* -> *Sugar / Glucose*; *రక్తం* -> *Blood / CBC*; *మూత్రపిండం* -> *Kidney / KFT*
- Uses Python standard library `difflib.get_close_matches()` with boundary checks to handle spelling variations without calling vector databases or LLMs.

### Vector Search Architecture (`app/services/vector_search.py`)
- **Status**: Stubbed / Architectural Guard.
- The active FAQ search uses deterministic keyword routing.
- `VectorSearchService` defines the mandatory security pattern for future pgvector deployment, enforcing that `clinic_id` is applied as an exact equality pre-filter (`WHERE clinic_id = $1`) before computing vector distance metrics (`<->`).

---

## 6. ADMINISTRATIVE AI CAPABILITIES

Beyond patient chat, the LLM is leveraged for several administrative tasks via `call_ai_gateway`:

| Administrative Feature | Endpoint | Model | Purpose |
| :--- | :--- | :--- | :--- |
| **Diagnostic Test Detail Generator** | `POST /admin/lab-tests/{id}/generate-details` | `deepseek/deepseek-chat` | Generates patient preparation instructions, fasting requirements, and specimen specifications. |
| **Catalog Auto-Classifier** | `POST /admin/lab-tests/auto-classify` | `deepseek/deepseek-chat` | Categorizes raw test lists into clinical departments (Biochemistry, Hematology, Pathology). |
| **Treatment Copywriter** | `POST /admin/treatments/ai-description` | `deepseek/deepseek-chat` | Drafts procedure overviews, patient expectations, and FAQ bullet points for aesthetic and specialty clinics. |
| **Executive Insights Summary** | `POST /admin/insights/summary/generate` | `deepseek/deepseek-chat` | Analyzes 30-day booking volume, cancellation patterns, and revenue to produce a 3-bullet management briefing. |
