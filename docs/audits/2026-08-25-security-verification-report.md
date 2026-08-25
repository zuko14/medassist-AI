# KRIYA AI — SECURITY & TENANT ISOLATION VERIFICATION REPORT

**Audit Date:** 2026-08-25  
**Evaluation Scope:** Complete API Surface, Authentication, RBAC, Multi-Tenant Boundaries, and Data Privacy  
**Overall Security Status:** **VERIFIED & PRODUCTION HARDENED**

---

## 1. Multi-Tenant Boundary Verification

| Component / Endpoint | Attack Scenario Tested | Security Control Implemented | Verification Test Evidence |
|---|---|---|---|
| `POST /admin/lab-reports/{id}/resend` | Clinic A attempts to resend Clinic B's patient lab report | Server verifies report belongs to `admin_user.clinic_id` (returns 404) | `tests/test_phase2_tenant_isolation.py` |
| `POST /admin/bookings/{id}/refund` | Clinic A attempts to refund Clinic B's booking with platform keys | Server verifies clinic ownership and resolves tenant Razorpay keys | `tests/test_phase2_tenant_isolation.py` |
| `POST /admin/branches/{branch_id}/doctors` | Clinic A attempts to assign Clinic B's doctor to Clinic A's branch | Server queries doctor by `doctor_id` + `clinic_id` (returns 404) | `tests/test_audit_regressions_and_invariants.py` |
| `GET /internal/integrations/lab-report` | External client attempts to supply forged `match_confidence` | Server ignores client values and executes `patient_match_service.match()` | `tests/test_phase5_connector_hardening.py` |
| `GET /admin/profile` & `PUT /admin/profile` | Clinic staff attempts to view/modify other tenant profiles | Query strictly bound to `admin_user.clinic_id` | `tests/test_phase6_frontend_backend_wiring.py` |

---

## 2. Authentication, Authorization & RBAC

1. **Connector Management RBAC:** All connector CRUD and run endpoints (`/admin/connectors/*`) strictly enforce `require_permission(AdminPermission.CONNECTOR_MANAGE)` and reject unauthorized roles with HTTP 403.
2. **Integration Webhook Authentication:** `/internal/integrations/*` endpoints validate `X-Integration-Secret` against `INTEGRATIONS_SHARED_SECRET` using constant-time comparison.
3. **Session Token Expiry:** JWT tokens for clinic admins and staff enforce expiration and signature verification.

---

## 3. Data Protection & PHI Privacy

1. **Aadhaar & PII Redaction:** Automated redactor purges Aadhaar numbers and PII from log payloads (`tests/test_aadhaar_redaction.py`, `tests/test_pii_sanitizer.py`).
2. **Patient Match Fail-Closed:** If match confidence is ambiguous or database errors occur, the dispatch fails closed to `needs_review` to prevent wrong-patient delivery (`tests/test_patient_match.py`).
3. **Right to Deletion & NMC Retention:** Anonymizes conversation logs while preserving financial audit records according to NMC regulations (`app/services/data_retention.py`).
