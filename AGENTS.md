# AGENTS.md — Persistent Operational Guide for AI Assistants

This repository contains **Kriya AI** (v2.0.0, formerly MediAssist AI), an enterprise healthcare operating system for Indian hospitals, specialty clinics, and diagnostic networks.

Before reading or modifying any code in this repository, you **MUST** review the operational rules and architecture references below.

---

## 1. CRITICAL OPERATIONAL INVARIANTS (NON-NEGOTIABLE)

1. **Database RLS is Bypassed**: The application connects to Supabase as `service_role` (which carries PostgreSQL's `BYPASSRLS`). **PostgreSQL RLS policies do NOT protect data.** Multi-tenancy is enforced 100% in application code via `TENANT_OWNED_TABLES` (30 tables in [`app/tenancy.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/tenancy.py)). Every query on a tenant table must use `scoped_query(table, clinic_id)` or include `.eq("clinic_id", clinic_id)`.
2. **Never Call PostgREST Synchronously on the Event Loop**: Supabase Python SDK v2 `.execute()` is synchronous blocking HTTP. Always wrap queries in `await sb(...)` from [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) to execute in the dedicated `_DB_EXECUTOR` thread pool.
3. **HTTP/1.1 is Mandatory**: [`app/database.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/database.py) monkey-patches PostgREST to force HTTP/1.1. Do not remove or alter this patch; HTTP/2 multiplexing resets caused cascading production outages.
4. **Meta 20-Second SLA**: Inbound webhooks (`POST /webhook`) must immediately persist the payload to `inbound_messages` and return HTTP 200 within 15ms. Never perform synchronous AI inference, OCR, or PDF downloads in the webhook handler thread.
5. **Zero-LLM Clinical Firewall**: National Medical Commission (NMC) regulations strictly prohibit AI prescription or autonomous diagnosis. All medical screening in [`app/services/clinical_firewall.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/clinical_firewall.py) must remain zero-LLM and deterministic.
6. **No Platform WhatsApp Fallback in Multi-Tenant Mode**: In [`app/services/whatsapp.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/app/services/whatsapp.py), `_get_credentials` strictly refuses to fall back to platform credentials. A clinic must have its own credentials to prevent cross-tenant reply pollution.
7. **Frontend is Inlined**: The active clinic admin dashboard is [`admin/index.html`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/admin/index.html). The file `admin/admin.js` is **dead and unreferenced**; do not edit it.
8. **Timezone Pinning**: All dates and scheduler jobs run in Indian Standard Time (`Asia/Kolkata`).

---

## 2. REPOSITORY KNOWLEDGE BASE DIRECTORY

Detailed, implementation-level intelligence documents are maintained in [`docs/agent-context/`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/):

| Document | Purpose & Key Topics |
| :--- | :--- |
| [00-REPOSITORY-OVERVIEW.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/00-REPOSITORY-OVERVIEW.md) | High-level system identity, directory layout, authoritative vs. obsolete files. |
| [01-ARCHITECTURE.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/01-ARCHITECTURE.md) | Multi-process topology, `sb()` thread pool, HTTP/1.1 patch, failure modes. |
| [02-SYSTEM-FLOWS.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/02-SYSTEM-FLOWS.md) | 15 end-to-end traced workflows (WhatsApp booking, payments, report sync, etc.). |
| [03-DATABASE-MODEL.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/03-DATABASE-MODEL.md) | Complete schema of all 50 tables, column types, unique constraints, status lifecycles. |
| [04-API-MAP.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/04-API-MAP.md) | Complete inventory of all endpoints across `/webhook`, `/admin`, `/platform`, `/fhir`. |
| [05-FRONTEND-MAP.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/05-FRONTEND-MAP.md) | Breakdown of `admin/index.html` (18 tabs, tokens, state) and `admin/platform.html`. |
| [06-AI-SYSTEM.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/06-AI-SYSTEM.md) | OpenRouter LLMs, clinical firewall, spend caps (`ai_usage_ledger`), hybrid search. |
| [07-INTEGRATIONS.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/07-INTEGRATIONS.md) | Meta WhatsApp Cloud API, Razorpay, Supabase Storage, MocDoc, CallMedex, ABDM. |
| [08-AUTH-AND-MULTI-TENANCY.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/08-AUTH-AND-MULTI-TENANCY.md) | Tenant resolution cascade, session tokens (`admin_sessions`), branch scoping, RBAC. |
| [09-BACKGROUND-JOBS.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/09-BACKGROUND-JOBS.md) | All 24 APScheduler jobs, distributed CAS locking (`scheduler_locks`), DLQ recovery. |
| [10-SECURITY-MODEL.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/10-SECURITY-MODEL.md) | HMAC signature verifications, CSP headers, prompt injection, DPDP Act 2023. |
| [11-DEPLOYMENT-AND-OPERATIONS.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/11-DEPLOYMENT-AND-OPERATIONS.md) | Render container topology, Dockerfile, lifespan pre-flights, migrations pipeline. |
| [12-TESTING-AND-VERIFICATION.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/12-TESTING-AND-VERIFICATION.md) | Test suite (2,978 tests), fixtures, AST linter (`test_lint_unscoped_queries.py`). |
| [13-KNOWN-ISSUES-AND-GAPS.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/13-KNOWN-ISSUES-AND-GAPS.md) | Capability status matrix (Implemented vs Stubbed vs Dead), fragile boundaries. |
| [14-CURRENT-STATE.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/14-CURRENT-STATE.md) | Documentation drift analysis comparing historical specs to current code. |
| [15-AGENT-HANDOFF.md](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/docs/agent-context/15-AGENT-HANDOFF.md) | Direct answers to the 16 architectural handoff questions and debugging entry points. |

---

## 3. VERIFICATION COMMANDS

> `pytest` is hermetic: `tests/conftest.py` forces test credentials before `.env` loads and fakes the
> distributed lock. Never run tests with `KRIYA_TEST_LIVE=1` unless you intend to hit production.

Before committing any code changes, execute:

```bash
# 1. Enforce AST Tenant Isolation Linter (FAILS if unscoped query is added)
pytest tests/test_lint_unscoped_queries.py

# 2. Run Super-Admin & Route Adversarial Security Matrices
pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py

# 3. Verify Clinical Safety Firewall
pytest tests/test_clinical_firewall.py

# 4. Run Core Conversation & WhatsApp Tests
pytest tests/test_webhook.py tests/test_fsm_transitions.py  # tests/test_conversation.py does not exist
```
