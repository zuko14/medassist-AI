# KRIYA AI — AI & INTELLIGENCE CAPABILITIES

**Document Classification:** Technical & Commercial AI Feature Reference  
**Provider:** Zuko Labs  
**Platform:** Kriya AI — Multi-Tenant WhatsApp Healthcare Operations System  
**Document Version:** 1.0 | Date: September 2026  

---

## 1. Executive Summary

Kriya AI embeds **production-grade AI and machine learning** across every layer of hospital operations — from the patient's first WhatsApp message to the admin's weekly performance review. Every AI capability operates under three non-negotiable principles:

1. **Clinical Safety First** — A deterministic firewall intercepts all patient messages *before* any AI model is invoked. The platform never prescribes, diagnoses, or dispenses medical advice.
2. **Human-in-the-Loop** — AI-generated content (descriptions, summaries, cleanup suggestions) is always presented as a *preview* requiring explicit admin approval before it reaches patients or modifies data.
3. **Cost Governance** — Every AI invocation is logged to a spend ledger with per-clinic monthly budget caps. Patient-facing chat is *never* degraded by admin spend limits.

---

## 2. AI Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         KRIYA AI — INTELLIGENCE STACK                       │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────┐    ┌──────────────────────┐    ┌────────────────┐  │
│  │  CLINICAL FIREWALL  │───►│   AI ENGINE (Chat)   │───►│  PATIENT REPLY │  │
│  │  (Zero-LLM Layer)   │    │ Intent · Triage · NLG│    │  (WhatsApp)    │  │
│  └─────────────────────┘    └──────────────────────┘    └────────────────┘  │
│                                                                              │
│  ┌─────────────────────┐    ┌──────────────────────┐    ┌────────────────┐  │
│  │    AI GATEWAY        │───►│  ADMIN AI SERVICES   │───►│  ADMIN PREVIEW │  │
│  │ Multi-Model Fallback │    │ Details · Cleanup ·  │    │  (Approval UI) │  │
│  │ Spend Cap · Ledger   │    │ Import · Summary     │    └────────────────┘  │
│  └─────────────────────┘    └──────────────────────┘                         │
│                                                                              │
│  ┌─────────────────────┐    ┌──────────────────────┐    ┌────────────────┐  │
│  │  HYBRID SEARCH       │    │   LAB CLASSIFIER     │    │  OCR ENGINE    │  │
│  │  Multilingual · Fuzzy│    │   Category Detection │    │  Tesseract     │  │
│  └─────────────────────┘    └──────────────────────┘    └────────────────┘  │
│                                                                              │
│  ┌─────────────────────┐    ┌──────────────────────┐    ┌────────────────┐  │
│  │  REPORT SUMMARIZER   │    │   PATIENT MATCHING   │    │  FAQ ENGINE    │  │
│  │  PII-Safe Summaries  │    │   Fuzzy Identity     │    │  Intent Match  │  │
│  └─────────────────────┘    └──────────────────────┘    └────────────────┘  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**AI Models Used:**
| Model | Role | Invocation Path |
|:---|:---|:---|
| **DeepSeek Chat** (`deepseek/deepseek-chat`) | Primary model for all operations — patient chat (intent detection, symptom triage, response generation) and admin AI tasks | `ai_engine.py` + `ai_gateway.py` via OpenRouter |
| **Google Gemini 2.0 Flash** (`google/gemini-2.0-flash-001`) | Automatic fallback model — kicks in when primary returns 429/5xx errors | `ai_gateway.py` via OpenRouter |
| **Tesseract OCR** | Lab report text extraction from PDF scans and price list image parsing | `lab_reports.py`, `price_list_parser.py` |

---

## 3. Patient-Facing AI Capabilities

### 3.1 Clinical Safety Firewall

| Attribute | Detail |
|:---|:---|
| **Type** | Deterministic regex-based interceptor (Zero-LLM) |
| **Position** | First layer — runs *before* any AI model is invoked |
| **Purpose** | Blocks all medical advice, prescriptions, diagnoses, and treatment recommendations |
| **Compliance** | NMC (National Medical Commission) malpractice liability protection |
| **Coverage** | Medicine names, dosage patterns, "should I take", "is it safe", home remedies, pregnancy medication queries, fetal sex determination (PCPNDT Act) |
| **Response** | Fixed, curated safety messages directing patients to consult their doctor in person |
| **Plans** | All Plans |

**Key safety patterns intercepted:**
- Medication and dosage queries ("Can I take paracetamol?", "What medicine for fever?")
- Self-diagnosis attempts ("Do I have diabetes?", "Is this cancer?")
- Home remedy and alternative medicine requests
- Pregnancy medication safety queries
- Fetal sex determination requests (PCPNDT Act criminal offence)
- Obstetric and newborn emergencies ("water broke", "baby not moving", "convulsion")

---

### 3.2 Intent Detection Engine

| Attribute | Detail |
|:---|:---|
| **Model** | DeepSeek Chat via OpenRouter (fallback: Gemini 2.0 Flash) |
| **Purpose** | Classifies patient WhatsApp messages into actionable intents |
| **Latency** | < 2 seconds average response time |
| **Fallback** | Exponential backoff with jitter on 429/5xx errors |
| **Plans** | All Plans |

**Supported intents:**
| Intent | Description | Example Messages |
|:---|:---|:---|
| `book_appointment` | Patient wants to schedule a doctor visit | "I want to see a doctor", "Book appointment" |
| `book_lab_test` | Patient wants to book a lab test | "Blood test", "Full body checkup" |
| `check_status` | Patient checking existing booking | "What's my appointment time?", "Queue status" |
| `reschedule` | Change existing appointment | "Can I come later?", "Change my time" |
| `cancel` | Cancel an appointment | "Cancel my booking" |
| `emergency` | Medical emergency situation | "Chest pain", "Can't breathe", "Accident" |
| `treatment_inquiry` | Interested in a specialty treatment | "LASIK cost", "Teeth whitening", "Acne treatment" |
| `concern_based` | Describes a health concern | "I have dark circles", "Hair is falling" |
| `talk_to_staff` | Wants human assistance | "Talk to someone", "Call me" |
| `greeting` | Social greeting | "Hi", "Hello", "Good morning" |
| `feedback` | Post-visit feedback | Rating responses |
| `general` | General conversation or questions | "Where are you located?", "What are your timings?" |

---

### 3.3 Symptom-to-Department AI Triage

| Attribute | Detail |
|:---|:---|
| **Model** | DeepSeek Chat via OpenRouter |
| **Purpose** | Maps patient-described symptoms to the correct medical department |
| **Safety** | Output is strictly a department name — never a diagnosis |
| **Plans** | Essential, PolyClinic, Enterprise, MultiSpecialty |

**Examples:**
| Patient Says | AI Routes To |
|:---|:---|
| "I have chest pain and breathing difficulty" | Cardiology |
| "My child has high fever and rash" | Pediatrics |
| "Knee pain for 3 months" | Orthopedics |
| "Skin rash and itching" | Dermatology |
| "I can't see clearly at night" | Ophthalmology |
| "Toothache and swollen gum" | Dental |

---

### 3.4 Concern-Based Treatment Matching

| Attribute | Detail |
|:---|:---|
| **Model** | DeepSeek Chat via OpenRouter |
| **Purpose** | Maps patient aesthetic/clinical concerns to specific treatments from the clinic's catalog |
| **Plans** | Derma, Eye, Dental, IVF, MultiSpecialty, WomenChild |

**Examples:**
| Patient Concern | Matched Treatments |
|:---|:---|
| "I have acne scars" | Chemical Peel, Microneedling, PRP Therapy |
| "My hair is thinning" | PRP for Hair, Hair Analysis |
| "Teeth are yellowish" | Teeth Whitening, Dental Scaling |
| "Vision is blurry" | Comprehensive Eye Checkup, LASIK Screening |
| "We've been trying to conceive for 2 years" | Fertility Consultation, IUI, IVF |

---

### 3.5 Conversational Response Generation (NLG)

| Attribute | Detail |
|:---|:---|
| **Model** | DeepSeek Chat via OpenRouter |
| **Purpose** | Generates natural, contextual WhatsApp responses in the patient's preferred language |
| **Languages** | English, Hindi, Telugu (with code-mixing support) |
| **Personality** | Professional, empathetic, hospital-branded (uses clinic name, doctor name) |
| **Safety** | Never provides medical advice; always redirects to qualified doctors |
| **Context** | Maintains conversation state via 22-state finite state machine |
| **Plans** | All Plans |

---

### 3.6 Multilingual & Synonym Search (Hybrid Search)

| Attribute | Detail |
|:---|:---|
| **Type** | Deterministic (Zero-LLM) — no AI model invoked |
| **Purpose** | Enables patients to search lab tests and treatments in Hindi, Telugu, and English with typo tolerance |
| **Architecture** | 3-tier cascade: Tier 1 (exact match) → Tier 2 (synonym dictionary) → Tier 3 (fuzzy match via `difflib`) |
| **Plans** | All Plans with lab test booking or treatment catalog |

**Search examples:**
| Patient Types | Matched Test/Treatment |
|:---|:---|
| "शुगर टेस्ट" (Hindi) | Blood Glucose / HbA1c |
| "sugar test" (English) | Blood Glucose / HbA1c |
| "thyrod" (typo) | Thyroid Profile (T3/T4/TSH) |
| "షుగర్ టెస్ట్" (Telugu) | Blood Glucose / HbA1c |
| "lipd profle" (typo) | Lipid Profile |
| "CBC" (abbreviation) | Complete Blood Count |

---

### 3.7 Lab Report AI Summary

| Attribute | Detail |
|:---|:---|
| **Model** | DeepSeek Chat via OpenRouter |
| **Purpose** | Generates plain-language patient summaries of complex lab test results |
| **Safety** | All PII (names, phone numbers, addresses) redacted before LLM invocation per DPDP Act 2023 |
| **Alerts** | Abnormal values highlighted with clinical reference ranges |
| **Delivery** | Sent to patient's WhatsApp alongside the original PDF report |
| **Plans** | Diagstream, PolyClinic, Enterprise |

---

### 3.8 Patient Identity Fuzzy Matching

| Attribute | Detail |
|:---|:---|
| **Type** | Deterministic (Zero-LLM) — fuzzy string similarity |
| **Purpose** | Matches patient names and phone numbers from lab EMR/LIMS systems to prevent report misrouting |
| **Algorithm** | Honorific stripping + normalized fuzzy ratio scoring |
| **Threshold** | Reports below 85% confidence → manual front-desk review |
| **Plans** | Diagstream, Enterprise |

---

### 3.9 FAQ & Common Questions Engine

| Attribute | Detail |
|:---|:---|
| **Type** | Hybrid (deterministic matching + LLM fallback) |
| **Purpose** | Answers common hospital questions (timings, location, parking, visiting hours) from configured FAQ data |
| **Plans** | All Plans |

---

## 4. Administrative AI Capabilities (New — Sessions 14 & 15)

### 4.1 AI Gateway — Multi-Model Routing & Cost Governance

| Attribute | Detail |
|:---|:---|
| **Primary Model** | DeepSeek Chat — `deepseek/deepseek-chat` (via OpenRouter) |
| **Fallback Model** | Google Gemini 2.0 Flash — `google/gemini-2.0-flash-001` (via OpenRouter) |
| **Routing** | OpenRouter's native `models: [primary, fallback]` — automatic failover on 429/5xx |
| **Cost Tracking** | Every invocation logged to `ai_usage_ledger` (prompt tokens, completion tokens, cost in paise) |
| **Budget Cap** | Per-clinic configurable monthly admin AI spend cap (default ₹500/month) |
| **Scope** | Admin operations only — patient WhatsApp chat is never degraded by budget limits |
| **Dashboard** | Admin panel → AI tab → real-time spend breakdown by task type |

**Task types tracked:**

| Task Type | Description | Typical Cost |
|:---|:---|:---|
| `patient_chat` | Live WhatsApp conversation (intent, triage, response) | Tracked but uncapped |
| `treatment_description` | AI-generated treatment/test descriptions | ~₹0.50 per generation |
| `catalogue_cleanup` | Duplicate detection & quality analysis | ~₹0.30 per scan |
| `price_list_import` | OCR + LLM table structuring from uploaded files | ~₹1–5 per import |
| `weekly_summary` | Operational insights generation | ~₹1 per summary |

---

### 4.2 Catalogue Quality Clean-Up Analyzer

| Attribute | Detail |
|:---|:---|
| **Purpose** | Detects duplicate lab tests, suspicious pricing, and missing preparation instructions |
| **Invocation** | Admin panel → Catalogue tab → "Quality Clean-Up" button |
| **Performance** | 1,500 rows analyzed in < 0.01 seconds |
| **Approval** | Nothing is pre-ticked; admin reviews and selects which fixes to apply |
| **Audit** | Every cleanup action is logged with test IDs, names, and admin user |

**Detection capabilities:**

| Issue Type | What It Finds | Example |
|:---|:---|:---|
| **Exact Duplicates** | Normalized name matches within the same branch | "Complete Blood Count" + "COMPLETE BLOOD COUNT" |
| **Acronym Duplicates** | Short-form equals long-form of the same test | "CBC" matches "Complete Blood Count" |
| **Suspicious Prices** | Tests priced outside ₹1–₹1,00,000 range | ₹0 or ₹5,00,000 flagged for review |
| **Missing Prep Instructions** | Fasting-required tests (Glucose, Lipid, etc.) without preparation guidance | "Fasting Blood Sugar" with no fasting instructions |

**Clinical-safe exclusions (never paired as duplicates):**
- Different numbers (Vitamin B1 ≠ Vitamin B12)
- Different antibody classes (IgG ≠ IgM ≠ IgA ≠ IgE)
- Different radiological views (AP ≠ PA ≠ Lateral)
- Different laterality (Left ≠ Right)
- Cross-branch tests (never compared across branches)

---

### 4.3 AI Test & Treatment Description Generator

| Attribute | Detail |
|:---|:---|
| **Purpose** | Generates professional clinical descriptions and preparation instructions for lab tests and specialty treatments |
| **Invocation** | Admin panel → Edit test/treatment → "✨ AI Draft" button |
| **Safety** | `sanitize_user_input` + `strip_injection_markers` + `_details_are_safe` validation |
| **Guardrails** | Output must not contain diagnostic claims, prescriptions, or treatment recommendations |
| **Fallback** | If AI fails, hallucinates, or spend cap exceeded → deterministic template generated |
| **Approval** | Preview-only; admin must explicitly save to apply |

---

### 4.4 Price List Import Pipeline (AI-Powered)

| Attribute | Detail |
|:---|:---|
| **Purpose** | Upload hospital price lists (PDF, XLSX, CSV, images) and AI extracts structured test/treatment data |
| **Invocation** | Admin panel → Catalogue tab → "Import Price List" button |
| **Supported Formats** | CSV, XLSX, PDF (text + scanned), PNG, JPEG |
| **Processing** | Asynchronous background worker (non-blocking) with 2-second polling |
| **Architecture** | File validation → Magic bytes check → Fast-path (CSV/XLSX) or OCR fallback (PDF/image) → LLM table structuring → Deduplication matching → Preview table |

**3-Step workflow:**
1. **Upload** — Drag-and-drop or file picker. Select branch and default category. Max 10 MB.
2. **Review** — AI presents extracted rows with status badges:
   - 🟢 `NEW` — New test not in current catalogue
   - 🟡 `UPDATE` — Existing test with price change (shows old → new price diff)
   - ⚪ `SKIP` — Already exists with same data
3. **Apply** — Admin selects rows to import. For ≥ 20 items, must type `IMPORT` to confirm.

**Security protections:**
| Protection | Detail |
|:---|:---|
| File size limit | 10 MB + 1 byte rejection with HTTP 413 |
| XLSX zip bomb guard | Max 50 MB uncompressed, max 200 entries |
| Image pixel limit | 40 million pixels max, auto-downscale to 2000px |
| OCR limit | Max 10 pages, 300 DPI, 120-second timeout |
| Legacy `.xls` | Rejected with "Please save as .xlsx or CSV" |
| Magic bytes verification | Real file type detected from binary signatures, not file extension |

---

### 4.5 Weekly Operational Insights Summary Engine

| Attribute | Detail |
|:---|:---|
| **Purpose** | Generates executive weekly performance summaries comparing current week vs. prior week |
| **Invocation** | Admin panel → Insights tab → "Generate Weekly Summary" button |
| **Time Period** | Last completed ISO week (Monday 00:00 IST – Sunday 23:59 IST) |
| **Cost** | Zero AI spend on page load; generation only on explicit button click |
| **Rate Limit** | Max 3 generations per clinic per day (IST) |

**Precomputed fact sheet metrics:**
| Metric | Description |
|:---|:---|
| **Total Bookings** | Appointments booked this week vs. last week (count + % change) |
| **Completions** | Appointments completed (check-ins) |
| **Cancellations** | Cancellation count and rate (%) |
| **Revenue Collected** | Total pre-collected payments (₹) |
| **Average Ticket Size** | Revenue ÷ completed bookings (₹) |
| **Service Breakdown** | Top services/departments by booking volume |
| **Week-on-Week Change** | Absolute and percentage differences for all metrics |

**Hallucination prevention:**
- Every number in the summary is precomputed in a deterministic fact sheet
- An AST/regex verifier scans the AI output for any number not present in the fact sheet
- If hallucinated numbers detected → entire AI output rejected → deterministic template used instead
- Template fallback guarantees a summary is always delivered, even if AI fails

---

### 4.6 Tesseract OCR Engine

| Attribute | Detail |
|:---|:---|
| **Purpose** | Extracts text from scanned lab reports (PDF) and uploaded price list images |
| **Technology** | Tesseract OCR + pdfplumber for native PDF text |
| **Resolution** | 300 DPI rendering for optimal character recognition |
| **Plans** | Diagstream (lab reports), All Plans (price list import) |

---

### 4.7 Lab Test Category Classifier

| Attribute | Detail |
|:---|:---|
| **Type** | Deterministic (Zero-LLM) — keyword-based classification |
| **Purpose** | Automatically categorizes lab tests into clinical categories (Hematology, Biochemistry, Radiology, etc.) |
| **Plans** | All Plans with lab test booking |

---

## 5. AI Spend Governance & Transparency

### 5.1 Spend Ledger (`ai_usage_ledger`)

Every AI invocation across the platform is recorded:
- **Clinic ID** — which clinic triggered the invocation
- **Task Type** — `patient_chat`, `treatment_description`, `catalogue_cleanup`, `price_list_import`, `weekly_summary`
- **Model Used** — primary or fallback model
- **Token Count** — prompt tokens + completion tokens
- **Cost (Paise)** — actual cost calculated from token usage × USD→INR rate
- **Success/Failure** — whether the invocation succeeded
- **Timestamp** — for monthly aggregation and billing

### 5.2 Budget Controls

| Control | Detail |
|:---|:---|
| **Default Budget** | ₹500/month per clinic (configurable) |
| **Budget Owner** | Platform superadmin sets per-clinic budgets via Platform Panel |
| **Scope** | Admin-only operations. Patient chat is tracked but never capped. |
| **Enforcement** | AI gateway checks remaining budget before every admin invocation |
| **Visibility** | Admin panel → AI tab → real-time spend breakdown |

### 5.3 Admin AI Spend Dashboard

The admin panel provides real-time AI spend visibility:
- **Monthly Budget Bar** — visual progress bar showing spend vs. budget
- **Task Breakdown** — cost distribution across treatment descriptions, cleanup, import, summaries
- **Patient Chat Cost** — separately displayed (tracked but not budget-constrained)
- **Cost Per Invocation** — granular per-task cost tracking

---

## 6. Data Retention & Privacy

| Policy | Detail |
|:---|:---|
| **PII Redaction** | All patient identifiers stripped before LLM invocation for lab report summaries |
| **Catalogue Import Previews** | Auto-expire after 24 hours; daily 4 AM purge job removes expired previews |
| **Conversation Transcripts** | 30-day automatic purge per DPDP Act 2023 |
| **Medical Records** | 7-year retention per Indian medical audit requirements |
| **AI Spend Logs** | Retained for billing and audit purposes |

---

## 7. AI Feature Availability by Plan

| AI Feature | Solo | Essential | Poly | Diag-stream | Diag-booking | Enterprise | Derma | Eye | Dental | IVF | Multi-Spec | WomenChild |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Clinical Safety Firewall | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Intent Detection | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Conversational NLG | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Multilingual Search | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Symptom → Dept Triage | — | ✅ | ✅ | — | — | ✅ | — | — | — | — | ✅ | ✅ |
| Concern → Treatment Match | — | — | — | — | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lab Report AI Summary | — | — | ✅ | ✅ | — | ✅ | — | — | — | — | — | — |
| Patient Fuzzy Matching | — | — | — | ✅ | — | ✅ | — | — | — | — | — | — |
| FAQ Engine | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| AI Gateway (Admin) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Catalogue Clean-Up | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| AI Description Generator | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Price List Import (AI) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Weekly Insights Summary | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| AI Spend Dashboard | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Tesseract OCR | — | — | — | ✅ | — | ✅ | — | — | — | — | — | — |

---

## 8. Technical Specifications

### 8.1 Model Configuration

| Parameter | Default Value | Environment Variable |
|:---|:---|:---|
| Primary Model | `deepseek/deepseek-chat` | `OPENROUTER_MODEL` |
| Fallback Model | `google/gemini-2.0-flash-001` | `OPENROUTER_FALLBACK_MODEL` |
| API Provider | OpenRouter | `OPENROUTER_API_KEY` |
| USD → INR Rate | 87.0 | `AI_USD_TO_INR_RATE` |
| Default Monthly Budget | ₹500 (50,000 paise) | `AI_DEFAULT_MONTHLY_BUDGET_PAISE` |

### 8.2 Performance Benchmarks

| Operation | Benchmark |
|:---|:---|
| Intent detection latency | < 2 seconds |
| Symptom triage latency | < 3 seconds |
| Catalogue cleanup scan (1,500 rows) | < 0.01 seconds |
| Multilingual search (Tier 1 exact) | < 5 milliseconds |
| Multilingual search (Tier 2/3 fuzzy) | < 50 milliseconds |
| Weekly summary generation | < 10 seconds |
| Price list import (CSV/XLSX fast-path) | < 5 seconds |
| Price list import (OCR + LLM) | < 60 seconds |

### 8.3 Safety & Reliability

| Mechanism | Description |
|:---|:---|
| **Exponential Backoff** | 429/5xx errors trigger retry with jitter (3 attempts) |
| **Multi-Model Failover** | Primary model failure → automatic fallback to secondary model |
| **Template Fallback** | AI failure/hallucination → deterministic template always delivers result |
| **Input Sanitization** | `sanitize_user_input` + `strip_injection_markers` on all admin AI inputs |
| **Output Validation** | `_details_are_safe` checks block diagnostic claims and prescriptions |
| **Number Verification** | Weekly summary verifier rejects any number not in precomputed fact sheet |
| **Atomic Operations** | Concurrent claim protection via conditional SQL updates |
| **Background Processing** | Non-blocking async workers with semaphore concurrency guards |

---

*This document covers all production-active AI and intelligence capabilities in Kriya AI as of September 2026. Every feature described herein is deployed, tested, and operational.*
