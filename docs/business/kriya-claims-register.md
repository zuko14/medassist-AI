# KRIYA AI — PRODUCT CLAIMS & MARKETING EVIDENCE REGISTER

**Document Type:** Regulatory, Technical & Commercial Claims Verification Register  
**Audit Purpose:** Enforce strict evidence-based marketing, sales presentations, and client proposals  
**Standard:** Zero unverified claims, zero hyperbole, zero fake metrics, zero pricing inclusion  

---

## 1. Claims Governance Protocol

All executive slide decks, technical whitepapers, and client proposals generated for Kriya AI must strictly comply with this register.
* **VERIFIED (ALLOWED):** Fully implemented in active repository code, supported by automated tests, database schemas, and verified architecture.
* **QUALIFIED (ALLOWED WITH SPECIFIC WORDING):** Implemented in code or active stubs, but requires explicit qualification regarding third-party credentials or customer-specific deployment variables.
* **DISALLOWED (STRICTLY PROHIBITED):** Not present in repository, speculative, fabricated metrics, or regulatory overclaims.

---

## 2. Complete Claims Verification Register

| # | Claim / Capability Statement | Domain | Implementation & Code Evidence | Status | Allowed in Materials? | Mandatory Wording / Qualification |
| :- | :--- | :--- | :--- | :---: | :---: | :--- |
| **C-01** | "Kriya AI automates appointment booking over WhatsApp with Groq/Llama AI." | AI / Workflow | `app/services/ai_engine.py`, `app/services/conversation.py` | **VERIFIED** | **YES** | Safe to state as core product capability. |
| **C-02** | "Zero-LLM Clinical Safety Firewall blocks AI medical advice and drug prescriptions." | Clinical Safety | `app/services/clinical_firewall.py:L1-L358` (Deterministic regex interceptor) | **VERIFIED** | **YES** | Highlight as NMC compliance protection. |
| **C-03** | "PostgreSQL partial unique indexes eliminate doctor slot double-booking race conditions." | Reliability | `migrations/064_fix_slot_uniqueness_key.sql` (ACID constraint) | **VERIFIED** | **YES** | Core technical proof point for CTOs. |
| **C-04** | "UPI and Card payment pre-collection integrated via Razorpay with HMAC signature verification." | Payment | `app/services/payment.py:L1-L2403`, `migrations/008_payments.sql` | **VERIFIED** | **YES** | State that client uses their own Razorpay account. |
| **C-05** | "Diagnostream extracts lab reports from EMRs (MocDoc) and delivers PDFs on WhatsApp." | Diagnostics | `connectors/runner.py`, `app/services/lab_reports.py` | **VERIFIED** | **YES** | Explain Playwright headless connector model. |
| **C-06** | "Fuzzy patient matching safety gate prevents misrouting of sensitive lab reports." | Data Safety | `app/services/patient_match.py:L1-L340` (Honorific stripping & token sort) | **VERIFIED** | **YES** | Crucial privacy protection evidence. |
| **C-07** | "Lab reports are summarized into plain-English with PII stripped before external LLM calls." | AI / Privacy | `app/services/report_summarizer.py`, `app/utils/pii_sanitizer.py` | **VERIFIED** | **YES** | Emphasize DPDP Act data minimization. |
| **C-08** | "Automated 24-hour and 2-hour WhatsApp appointment reminders via APScheduler." | Workflow | `app/services/scheduler.py:L1-L1100` | **VERIFIED** | **YES** | Highlight no-show reduction benefit. |
| **C-09** | "Live OPD waiting room queue tokens allow patients to check queue position on WhatsApp." | Queue / OPD | `migrations/019_appointment_queue_tokens.sql`, `app/database.py` | **VERIFIED** | **YES** | Safe to demonstrate in pitch. |
| **C-10** | "Multi-tenant architecture with Supabase PostgreSQL Row-Level Security (RLS)." | Security | `migrations/003_multi_tenant.sql`, `migrations/049_force_row_level_security.sql` | **VERIFIED** | **YES** | Proof of complete tenant data isolation. |
| **C-11** | "Complies with India DPDP Act 2023 with consent capture, 30d chat purge & 7yr retention." | Compliance | `migrations/007_data_retention.sql`, `app/services/consent.py` | **VERIFIED** | **YES** | Accurate regulatory mapping. |
| **C-12** | "Full HL7 FHIR R4 API support and live ABDM ABHA integration." | Interoperability | `app/routers/fhir.py`, `app/services/abdm.py` (FHIR schemas & gateway stubs) | **QUALIFIED** | **YES (WITH QUALIFICATION)** | Must state: *"FHIR R4 schema models and ABDM gateway interfaces are built-in; live national health grid connectivity depends on client M3 sandbox credentials."* |
| **C-13** | "Kriya AI replaces all hospital HMIS, billing, and inpatient EHR systems." | Market Scope | N/A (Kriya is an outpatient operations & front-desk automation layer) | **DISALLOWED** | **STRICTLY PROHIBITED** | Must position Kriya as an operational front-door layer that connects to existing HMIS. |
| **C-14** | "Kriya AI provides certified medical diagnoses and autonomous clinical decision support." | Clinical Scope | N/A (Explicitly blocked by Clinical Firewall) | **DISALLOWED** | **STRICTLY PROHIBITED** | Must state that Kriya AI is an administrative and operational assistant, not a doctor. |
| **C-15** | "Guarantees exact 85% revenue increase or specific unverified client statistics." | Commercial | N/A (Customer results vary based on volume and baseline no-shows) | **DISALLOWED** | **STRICTLY PROHIBITED** | Must label operational returns as *"Modelled Operational Impact"* or *"Operational Estimates"*. |
| **C-16** | "Any monetary subscription pricing (e.g. ₹4,000 / month, branch license fees)." | Commercial | N/A (Strict user directive: Zero pricing in all client materials) | **DISALLOWED** | **STRICTLY PROHIBITED** | Zero currency figures or subscription pricing in any deliverable. |

---

## 3. Mandatory Compliance Directives for Presentation Generation

1. **AI vs. Deterministic Clear Delineation:** Every slide mentioning AI must clearly state that AI performs conversational interpretation and intent understanding, while all clinical safety rules, financial transactions, and database modifications are strictly governed by deterministic controls.
2. **Synthetic Data in Visuals:** All UI screenshots, sequence flow names, phone numbers (`+91 98765 43210`), patient names (`Ramesh Kumar`), and doctor names (`Dr. Sharma`) must be 100% synthetic to prevent any real-world privacy leakage.
3. **No Phantom Integrations:** Do not claim native bi-directional API support for unverified third-party proprietary systems unless implemented via our headless Playwright scraper or standard REST/FHIR endpoints.
