# Testing Strategy — MediAssist AI & CallMeDex Subsystem

## Overview
This document outlines the testing strategy, test suite organization, CI verification rules, and credential-gated integration testing practices across MediAssist AI and CallMeDex.

---

## Dual-Tier Testing Framework

To ensure 100% deterministic test execution in CI environments while supporting live portal verification, tests are partitioned into two distinct tiers:

### Tier 1: Fast CI Unit & Mocked Test Suite (Default)
- **Scope**: Executed automatically on every pull request, commit, and build.
- **Environment**: Runs without external network access, using isolated mock contexts (`_is_live_page=False`) and synthetic PDF fixtures.
- **Execution Command**:
  ```bash
  python -m pytest app/integrations/callmedex/tests -q
  ```
- **Coverage**: 52 unit tests covering all 10 workflow steps, OCR engine parsing, worker recovery checkpoints, HMAC signature validation, path sanitization, and API router endpoints.

### Tier 2: Credential-Gated Live Sandbox Test Suite
- **Scope**: Executed prior to production deployments against staging/sandbox EMR portals (e.g. MocDoc staging).
- **Gate Requirement**: Requires environment variable `MOCDOC_SANDBOX_ENABLED=1` and credentials (`MOCDOC_SANDBOX_USER`, `MOCDOC_SANDBOX_PASS`).
- **File**: `app/integrations/callmedex/tests/test_mocdoc_live_sandbox.py`
- **Execution Command**:
  ```bash
  MOCDOC_SANDBOX_ENABLED=1 python -m pytest app/integrations/callmedex/tests/test_mocdoc_live_sandbox.py -v
  ```
- **Behavior**: Skipped by default in CI when `MOCDOC_SANDBOX_ENABLED` is omitted.

---

## Key Test Suites & Coverage Breakdown

| Test File | Phase Covered | Key Assertions & Scenarios |
|---|---|---|
| `test_phase_r1_bugs.py` | Phase R1 | Real HTTP callback POST in prod mode, HTTP 500 delivery failure handling, storage path traversal sanitization |
| `test_phase3_connector.py` | Phase 3 | MocDoc 10-step connector workflow, recovery checkpoints, fail-fast container validation |
| `test_phase4_5_browser_validation.py` | Phase 4 | Playwright browser context lifecycle, v1.0.0 DOM selector resolution, PDF signature/SHA256 checksum integrity |
| `test_phase5_canonical_ocr.py` | Phase 5 / R3 | `pdfplumber` native text/table extraction against PDF fixtures, dynamic confidence scoring, rejection of 0-byte/corrupted PDFs |
| `test_phase6_ai_summary.py` | Phase 6 | Layer 1 clinical reasoning, Layer 2 multi-audience summary generation, zero-medical-advice compliance |
| `test_phase7_whatsapp_delivery.py` | Phase 7 | WhatsApp media template assembly, phone masking, HMAC signed callback dispatch |
| `test_phase8_e2e_acceptance.py` | Phase 8 | Full end-to-end production execution chain |
| `test_api_router.py` | Phase R4 | Bearer auth, `X-Integration-Secret` validation, HMAC signature checks, 5-minute timestamp replay protection, `/process-report`, `/health`, `/jobs/{task_id}` |
| `test_mocdoc_live_sandbox.py` | Phase R2 / R6 | Live Playwright browser navigation, login, and search against live sandbox portal |

---

## Verification Rules & CI Expectations

1. **Zero Failing Tests**: No PR or release build is approved with failing, skipped (except credential-gated integration tests), or xfail unit tests.
2. **Regression Suite Integrity**: Running `python -m pytest tests/ -q` must keep all 204 core hospital bot tests green.
3. **No Mocking in Production Code**: Mocking is permitted strictly inside test files, never inside core production modules (`engine.py`, `connector.py`, `runner.py`, `router.py`).
