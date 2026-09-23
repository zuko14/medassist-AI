# 00 — Repository Overview: Kriya AI

## 1. Identity & System Purpose
**Kriya AI** (originally structured as *MediAssist AI*) is an enterprise multi-tenant healthcare conversational operations and patient engagement platform engineered for Indian clinics, multi-specialty hospitals, and diagnostic centres.

The platform provides:
1. **Automated WhatsApp Front Desk**: AI-driven appointment booking, doctor directory browsing, slot availability checks, multi-branch scheduling, cancellation, and rescheduling via Meta WhatsApp Cloud API (`v22.0`).
2. **Diagnostic & Pathology Automation**: Lab test catalogue browsing, home/centre sample collection booking, automated retrieval of lab reports from Electronic Health Record (EHR) and Laboratory Information Management Systems (LIMS) like MocDoc, and WhatsApp delivery of PDF reports with AI-generated clinical summaries.
3. **Specialty Clinical Modules**: Specialty-tailored care pathways and treatment catalogues for Dermatology, Ophthalmology, Dental, IVF/Fertility, Multi-Specialty Hospitals, and Women & Child hospitals (including PCPNDT compliance).
4. **Clinical Safety Firewall**: Deterministic zero-LLM screening blocking medical advice, prescription requests, dosage queries, and diagnostic claims before LLM evaluation, protecting against National Medical Commission (NMC) liability.
5. **Multi-Tenant Administration**:
   - **Clinic Admin Portal** (`admin/index.html`): High-density clinical console for hospital staff and clinic administrators (appointments, doctor rosters, holidays, leaves, staff delegation, patients, lab catalogues, payments, broadcasts, and automated reports).
   - **Platform Super-Admin Console** (`admin/platform.html`): Platform-level governance, multi-hospital analytics, clinic tenant provisioning, subscription lifecycle management, and financial reconciliation.
6. **Regulatory Compliance**: Built for India's **Digital Personal Data Protection (DPDP) Act 2023** (explicit consent tracking, 30-day session purge, right-to-be-forgotten / "DELETE MY DATA" workflow) and **NMC Medical Records Regulations** (7-year clinical record retention with identifier redaction).

---

## 2. Repository Metadata & Environment
- **Root Directory**: `C:\Users\chait\OneDrive\Desktop\SYSTEMS_ALL\KriyaAI`
- **Application Version**: `2.0.0`
- **Python Runtime**: Python 3.11 / 3.12 (`python:3.11-slim` in Dockerfile)
- **Timezone**: `Asia/Kolkata` (IST, UTC+05:30) explicitly pinned across Docker container, APScheduler, datetime helpers, and business logic.
- **Process Supervision**: `tini` as PID 1, Uvicorn ASGI server with configurable multi-worker concurrency (`WEB_CONCURRENCY=2` default).
- **Test Suite**: 2,978 automated test cases collected across 210 test files in `tests/` and `app/integrations/callmedex/tests/`.

---

## 3. High-Level Repository Directory Map

```text
KriyaAI/
├── admin/                               # Frontend Single-Page Applications (Vanilla JS + CSS)
│   ├── index.html                       # Clinic Admin & Staff Console (~550 KB)
│   ├── platform.html                    # Platform Owner / Super-Admin Console (~223 KB)
│   └── vendor/
│       └── chart.umd.min.js             # Self-hosted Chart.js for CSP compliance
├── app/                                 # Main Backend Application Package (FastAPI)
│   ├── config.py                        # Pydantic Settings & Environment Variables
│   ├── database.py                      # Supabase client, sb() off-loop query executor, scoped_query
│   ├── main.py                          # FastAPI factory, lifespan, security middlewares, static mounts
│   ├── tenancy.py                       # Single source of truth for TENANT_OWNED_TABLES & scope rules
│   ├── integrations/                    # Integration modules
│   │   └── callmedex/                   # CallMedex LIMS/EHR browser automation & OCR pipeline
│   ├── models/                          # Pydantic data transfer schemas (Message, Appointment, Patient)
│   ├── routers/                         # FastAPI HTTP Endpoints
│   │   ├── admin.py                     # Clinic administration routes (~326 KB)
│   │   ├── clinics.py                   # Tenant clinic provisioning & onboarding
│   │   ├── fhir.py                      # HL7 FHIR R4 interoperability API
│   │   ├── health.py                    # Health, readiness, and liveness probes
│   │   ├── integrations.py              # Connector ingest callback router
│   │   ├── platform.py                  # Platform owner / super-admin endpoints (~117 KB)
│   │   ├── razorpay_webhook.py          # Razorpay payment event webhook
│   │   └── webhook.py                   # Meta WhatsApp Cloud API webhook handler
│   ├── services/                        # Business Logic & Core Domain Services
│   │   ├── abdm.py                      # Ayushman Bharat Digital Mission (ABHA verification)
│   │   ├── ai_engine.py                 # OpenRouter/Groq LLM engine, prompts, intent classification
│   │   ├── ai_gateway.py                # Centralized AI routing, spend caps & usage ledger
│   │   ├── analytics.py                 # Analytics metrics & revenue aggregation
│   │   ├── broadcast.py                 # Outbound bulk messaging & notification service
│   │   ├── catalogue_cleaner.py         # AI-assisted lab catalogue hygiene
│   │   ├── clinical_firewall.py         # Deterministic NMC compliance & drug screening firewall
│   │   ├── consent.py                   # DPDP patient consent capture & logging
│   │   ├── conversation.py              # 27-state Finite State Machine for WhatsApp (~297 KB)
│   │   ├── data_retention.py            # DPDP purge & NMC 7-year anonymization worker
│   │   ├── distributed_lock.py          # Distributed database advisory locks on scheduler_locks
│   │   ├── faq_engine.py                # Deterministic clinic FAQ & information engine
│   │   ├── feedback.py                  # Post-consultation patient feedback collection
│   │   ├── fhir_schemas.py              # FHIR R4 resource converters
│   │   ├── hmis_bridge.py               # Generic HMIS appointment synchronization
│   │   ├── hybrid_search.py             # Deterministic multilingual catalogue search (En/Hi/Te)
│   │   ├── lab_classifier.py            # Diagnostic test category classifier
│   │   ├── lab_reports.py               # Lab report upload, storage, and WhatsApp dispatch
│   │   ├── message_accounting.py        # Meta WABA billing & outbound message ledger
│   │   ├── message_queue.py             # Durable inbound queue & per-phone lock manager
│   │   ├── metrics.py                   # Prometheus text metric exporter
│   │   ├── patient_match.py             # Lab report to patient probabilistic matching
│   │   ├── payment.py                   # Razorpay payment links, holds, reconciliation (~124 KB)
│   │   ├── permissions.py               # Granular RBAC and branch scope enforcement
│   │   ├── platform_finance.py          # Platform fee calculations and billing rates
│   │   ├── prescriptions.py             # Prescription management and dosage reminders
│   │   ├── price_list_parser.py         # Diagnostic price list CSV/Excel import parser
│   │   ├── report_routing.py            # Provider/TPA lab report desk routing
│   │   ├── report_summarizer.py         # Clinical LLM report summarizer
│   │   ├── scheduler.py                 # APScheduler service for reminders & sweeps (~67 KB)
│   │   ├── specialty_catalog.py         # Treatment definitions across 6 medical specialties
│   │   ├── specialty_flow.py            # WhatsApp dialog trees for specialty treatments
│   │   ├── subscription.py              # Tenant subscription tier & daily limit enforcement
│   │   ├── tenant.py                    # Tenant resolution (phone -> clinic), caching & plans
│   │   ├── tenant_scoped_client.py      # Scoped client wrapper
│   │   ├── test_detail_generator.py     # AI preparation guide generator for lab tests
│   │   ├── vector_search.py             # Tenant-scoped vector similarity search wrapper
│   │   ├── weekly_summary.py            # AI weekly clinic operational briefing generator
│   │   └── whatsapp.py                  # Meta WhatsApp Cloud API client (Graph API v22.0)
│   ├── templates/                       # Message formatting
│   │   └── whatsapp_templates.py        # Static text & interactive button/list templates
│   └── utils/                           # Shared Utilities
│       ├── async_tasks.py               # Safe background task spawner with drain tracking
│       ├── browser_errors.py            # Playwright browser error taxonomy
│       ├── connector_crypto.py          # Fernet symmetric encryption for HMIS credentials
│       ├── correlation.py               # X-Correlation-ID tracing middleware & contextvars
│       ├── helpers.py                   # Formatting, date calculations, slot helpers
│       ├── logger.py                    # Structured logging setup with PII masking
│       ├── pdf_reader.py                # PyMuPDF / pdfplumber extraction
│       ├── pii_sanitizer.py             # Phone number and name scrubbing for logs
│       ├── security.py                  # Webhook signature verification, security headers, rate limiters
│       └── validators.py                # Phone, date, and input validation
├── connectors/                          # Standalone HMIS & EMR Background Scraper Worker
│   ├── base.py                          # HospitalConnector abstract base class & ReportMetadata
│   ├── runner.py                        # Standalone polling worker entry point (~66 KB)
│   └── mocdoc/                          # MocDoc EHR scraper (Playwright web automation)
│       ├── selectors.py                 # DOM selectors for MocDoc web interface
│       └── worker.py                    # MocDoc authentication and report downloader (~56 KB)
├── docs/                                # Documentation, architecture, audit logs, runbooks
├── migrations/                          # PostgreSQL Migrations (001 through 085 + rollbacks)
├── loadtest/                            # Locust load testing scripts
├── scripts/                             # Operational & maintenance CLI scripts
│   ├── migrate.py                       # PostgreSQL migration runner
│   ├── preflight_tenant_check.py        # Pre-deploy tenant isolation validator
│   ├── recover_deleted_doctors.py       # Data recovery utility
│   └── whatsapp_doctor.py               # WhatsApp doctor notification CLI
├── tests/                               # Comprehensive Automated Test Suite (210 files)
├── Dockerfile                           # Production container specification
├── render.yaml                          # Render.com multi-service blueprint
└── requirements.txt                     # Production Python package dependencies
```

---

## 4. Authoritative vs. Obsolete Files

### Authoritative Files (Source of Truth)
- **Tenant Isolation**: [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py) is the sole authoritative definition of `TENANT_OWNED_TABLES` (30 tables) and `is_valid_clinic_scope()`.
- **Database Access**: [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) using `sb()` off-loop runner and `scoped_query()`.
- **Tenant Resolution**: [`app/services/tenant.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/tenant.py) for WhatsApp incoming display number resolution.
- **WhatsApp State Machine**: [`app/services/conversation.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/conversation.py) is the authoritative conversation flow.
- **Background Jobs**: [`app/services/scheduler.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/scheduler.py) drives all APScheduler cron and interval tasks.
- **Deployment**: [`render.yaml`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/render.yaml) and [`Dockerfile`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/Dockerfile).
- **Admin Frontend**: [`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html) and [`admin/platform.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/platform.html).

### Obsolete / Deprecated / Misleading Artifacts
1. **`claude.md`**: Outdated build guide for original "MediAssist" v1 before multi-tenancy, multi-branch, specialty plans, CallMedex, and Kriya AI rebranding were introduced.
2. **`claude2.md`**: Intermediate working notes from earlier development phases; superceded by repository code and current test suites.
3. **`admin/admin.js` (Removed)**: Note in `app/main.py:446` documents that `admin/admin.js` was removed because it was a stale, desynchronized copy of `admin/index.html` inline JS that contained cross-tenant query vulnerabilities.
4. **Shell Redirection Artifacts at Root**: `main`, `tuple[bool`, `type`, `bool`, `Expected`, `str`, `CUserschaittmp_nlm_login_output.txt` are accidental shell output files from past terminal redirections. They are not referenced by any Python code or tests.
5. **`railway.toml`**: Minimal config; production deployment runs on Render.com (`render.yaml`).
