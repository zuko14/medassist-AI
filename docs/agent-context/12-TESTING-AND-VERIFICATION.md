# 12 - TESTING INFRASTRUCTURE & VERIFICATION SUITES

This document details the test framework, test suites (2,978 collected tests across 210 test files), fixtures, mocking patterns, adversarial security matrices, and automated AST linters in KriyaAI.

---

## 1. TEST SUITE HIERARCHY & VOLUME

The repository contains an exceptionally rigorous test suite executed via `pytest`:
- **Total Tests**: 2,978 collected tests across 210 test files.
- **Root Directories**:
  - `tests/` (~185 test files covering webhooks, scheduling, multi-tenancy, payments, admin endpoints, and clinical safety).
  - `app/integrations/callmedex/tests/` (25 test files covering CallMedex connector, queue engine, and sandbox workflows).

---

## 2. TEST FIXTURES & DATABASE STRATEGY

Configured in [`tests/conftest.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/conftest.py) and [`tests/conftest_db.py`](file:///c:/Users/chait/OneDrive/Desktop/SYSTEMS_ALL/KriyaAI/tests/conftest_db.py):

### Dual Database Test Modes
1. **Mock Mode (Unit Tests)**:
   - Default mode. Mocks the Supabase PostgREST client and `sb()` execution pool.
   - Fast, in-memory execution without external database dependencies.
2. **Real PostgreSQL Mode (`pgserver` / `TEST_DATABASE_URL`)**:
   - `real_postgres_uri` fixture boots a local embedded PostgreSQL server using `pgserver` or connects to an external PostgreSQL instance.
   - Automatically bootstraps the database and applies all 85 migrations from `migrations/` up to head via `scripts.migrate.apply_migrations`.
   - `clean_db` fixture truncates operational tables (`appointments`, `conversations`, `patients`, etc.) between test runs to guarantee test isolation while preserving seeded clinic metadata.

---

## 3. ADVERSARIAL SECURITY & SCOPE MATRICES

KriyaAI features specialized test suites designed specifically to prevent multi-tenant data leaks and permission escalation:

### 1. AST Unscoped Query Linter (`tests/test_lint_unscoped_queries.py`)
- Statically parses the Abstract Syntax Tree (AST) of every Python file in the repository.
- Flags any query touching a table in `TENANT_OWNED_TABLES` (defined in `app/tenancy.py`) that does not use `scoped_query(table, clinic_id)` or carry an explicit explanatory comment (`# unscoped: <justification>`).
- Fails CI/CD if an engineer introduces a bare, unscoped `.select()` or `.update()` on a tenant table.

### 2. Super-Admin Scope Adversarial Matrix (`tests/test_admin_super_admin_scope_matrix.py`)
- Iterates through all `/admin` routes.
- Simulates a `super_admin` user executing requests without specifying a `?clinic_id=<uuid>` parameter.
- Asserts that every endpoint strictly returns HTTP 400 Bad Request ("No clinic selected"), verifying that the historical 2026-09-01 cross-tenant wildcard vulnerability cannot recur.

### 3. Route Permission & Adversarial Matrix (`tests/test_phase2_route_adversarial_matrix.py`)
- Tests cross-tenant request spoofing: verifies that a user from Clinic A cannot access resources from Clinic B, even if they guess valid resource UUIDs.
- Verifies role boundaries: asserts that a user with `role="staff"` receives HTTP 403 when invoking admin-only routes (doctor fee changes, financial analytics, Razorpay credentials).

### 4. Clinical Firewall Safety Verification (`tests/test_clinical_firewall.py`)
- Tests hundreds of medical advice, drug name, and dosage queries across English, Hindi, and Telugu.
- Asserts that zero matching messages reach the LLM mock and that deterministic NMC-compliant disclaimer templates are returned.

---

## 4. INTEGRATION & CONNECTOR TEST SUITES

### CallMedex Compliance & Workflow Suites
- Located in `app/integrations/callmedex/tests/`:
  - `test_mocdoc_10step_workflow.py`: End-to-end simulation of MocDoc login, navigation, report filtering, PDF download, and forward ingestion.
  - `test_api_router.py`: Tests HMAC signature verification, timestamp replay window rejection (>300s skew), and duplicate task idempotency.
  - `test_connector_process_isolation.py`: Verifies that headless browser workers do not share state or memory with the API server.

---

## 5. RUNNING THE TEST SUITE

```bash
# Run the complete test suite
pytest

# Run fast unit tests (skipping real postgres)
pytest tests/test_conversation.py tests/test_clinical_firewall.py

# Run tenancy and security adversarial matrices
pytest tests/test_admin_super_admin_scope_matrix.py tests/test_lint_unscoped_queries.py

# Run against real local embedded PostgreSQL
TEST_DATABASE_URL=postgresql://localhost:5432/test_db pytest tests/test_database_invariants.py
```
