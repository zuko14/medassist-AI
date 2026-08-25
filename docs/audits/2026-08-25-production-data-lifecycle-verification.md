# Kriya AI / MediAssist AI — Production Data Lifecycle & Retention Verification

**Date:** 2026-08-25  
**Regulatory Standards:** India Digital Personal Data Protection (DPDP) Act 2023 & National Medical Commission (NMC 2023 Reg 13)  
**Implementation Services:** `app/services/data_retention.py`, `app/database.py`  
**Test Suite:** `tests/test_phase_e_delete_my_data_lifecycle.py`  

---

## 1. Dual-Tier Data Retention & Erasure Architecture

The platform implements a legally compliant dual-tier retention architecture:

```mermaid
graph TD
    A[Patient Deletion Request 'DELETE MY DATA'] --> B{Data Tier Classification}
    B -->|Tier 1: Clinical Records| C[Anonymize PII to [REDACTED]]
    C --> D[Preserve Medical Audit Structure for 7 Years per NMC]
    C --> E[Delete Binary PDF Files from Object Storage]
    B -->|Tier 2: Transient Session Data| F[Permanently Delete from Database]
    F --> G[Purge conversations & analytics_events]
    B --> H[Record Compliance Audit Log in admin_audit_logs]
```

---

## 2. Comprehensive Data Lifecycle Matrix

| Data Entity | Database Table | Retention Mandate | Deletion Action | Post-Erasure State | Compliance Verification |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Patient Profile** | `patients` | 7 Years (NMC) | Anonymized | `name='[REDACTED]'`, `opted_in=False`, `data_consent=False` | `test_02_clinical_records_preserve_structure_and_redact_pii` |
| **Appointments** | `appointments` | 7 Years (NMC) | Anonymized | `patient_name='[REDACTED]'`, `patient_phone='[REDACTED]'`, `symptoms='[REDACTED]'`, `doctor_name` & date preserved | `test_01_dpdp_nmc_tiered_erasure_workflow` |
| **Lab Reports (DB)** | `lab_reports` | 7 Years (NMC) | Anonymized | `patient_name='[REDACTED]'`, `patient_phone='[REDACTED]'`, `file_path='[REDACTED]'`, test types preserved | `test_01_dpdp_nmc_tiered_erasure_workflow` |
| **Lab Report PDFs** | Supabase Storage (`lab-reports`) | None upon erasure | Hard Deleted | File removed from bucket storage | Storage `.remove()` invoked |
| **Prescriptions** | `prescriptions` | 7 Years (NMC) | Anonymized | `patient_name='[REDACTED]'`, `patient_phone='[REDACTED]'`, `notes='[REDACTED]'`, medicines preserved | `test_02_clinical_records_preserve_structure_and_redact_pii` |
| **Family Members** | `family_members` | Tied to Primary | Anonymized | `name='[REDACTED]'`, `relationship='[REDACTED]'` | `test_01_dpdp_nmc_tiered_erasure_workflow` |
| **Chat Sessions** | `conversations` | 30 Days (DPDP) | Hard Deleted | Row completely purged | Table `.delete().eq('phone', phone)` |
| **Analytics Events** | `analytics_events` | 12 Months | Hard Deleted | Operational events purged | Table `.delete().eq('phone', phone)` |
| **Compliance Audit** | `admin_audit_logs` | Permanent | Audit Logged | Action `DATA_ERASURE_REQUEST` recorded with redacted phone | Audit log inserted with timestamp |

---

## 3. Automated Test Evidence

All 3 lifecycle verification tests passed cleanly:
1. `test_01_dpdp_nmc_tiered_erasure_workflow`: Verified end-to-end table routing and storage removal.
2. `test_02_clinical_records_preserve_structure_and_redact_pii`: Verified redaction of PII while preserving clinical structure.
3. `test_03_retention_status_reporting`: Verified dynamic calculation of retention expiration dates.
