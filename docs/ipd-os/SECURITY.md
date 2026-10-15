# Kriya IPD OS — Security and Threat Model

Builds on [docs/agent-context/10-SECURITY-MODEL.md](../agent-context/10-SECURITY-MODEL.md) and
[08-AUTH-AND-MULTI-TENANCY.md](../agent-context/08-AUTH-AND-MULTI-TENANCY.md).

## 1. Assets
Inpatient identity + location (bed), clinical records (observations, notes, orders, MAR, diagnoses, discharge
summaries), financial records (charges, bills), bed capacity (an availability error is a patient-safety issue),
audit trail (`admin_audit_logs`, `ipd_events`).

## 2. Actors and trust boundaries
| Actor | Trust | Boundary |
|---|---|---|
| Platform owner | Highest; Basic auth on `/platform` | Enables/disables IPD; purges on clinic deletion |
| Clinic admin | Full tenant authority (house convention) | Own clinic only (`enforce_clinic_access`, fail-closed) |
| Staff (desk, nurse, doctor, billing) | Delegated permissions; optional branch pin; optional doctor link | Own clinic, own branch, own permission set |
| Super admin (env) | Platform staff | Must name a clinic; unscoped requests refused (matrix) |
| Kriya Ketu | Acts **as the user** through in-process HTTP | Same guards as the user; IPD reads + hand-offs only |
| Browser | Untrusted | Every rule re-checked on the server; UI gating is cosmetic |
| Database | Trusted; `service_role` bypasses RLS | Composite FKs, triggers and functions enforce invariants anyway |

## 3. Permission matrix (server-enforced)
| Capability | ADMIN | ADMISSIONS | NURSING | CLINICAL | BILLING | Extra rule |
|---|:-:|:-:|:-:|:-:|:-:|---|
| View bed board / census / worklist / admission list | ✓ | ✓ | ✓ | ✓ | ✓ | diagnosis + clinical worklist only for NURSING/CLINICAL |
| View chart clinical sections | ✓¹ | | ✓ | ✓ | | audit-logged read |
| View charges/bills on a chart | ✓ | ✓ | | | ✓ | |
| Settings, wards, beds, maintenance/blocked | ✓ | | | | | refused in wind-down for new wards/beds |
| Mark bed clean / available | ✓ | ✓ | ✓ | | | |
| Admit, reserve, assign, cancel, release hold | | ✓ | | | | admit refused in wind-down |
| Transfer | | ✓ | ✓ | | | expected-bed check |
| Observations, nursing/handover notes, MAR | | | ✓ | ✓ | | |
| Progress/plan/procedure notes, orders, stop medication | | | | ✓ | | **doctor link** |
| Start regular/death/transfer-out discharge | | | | ✓ | | **doctor link** |
| Record LAMA/DAMA/absconded | | ✓ | | ✓ | | |
| Edit discharge summary draft | | | | ✓ | | CAS on `updated_at` |
| Sign discharge summary | | | | ✓ | | **doctor link = attending** + registration number (DB) |
| Charges, bed-day accrual, post, void, final bill | | | | | ✓ | |
| Complete discharge | | ✓ | | | ✓ | waiver needs IPD_ADMIN |
¹ clinic_admin/super_admin hold every permission (house convention, ADR-IPD-09 / owner decision O-3).

## 4. Threats and mitigations
| # | Threat | Mitigation | Evidence |
|---|---|---|---|
| T1 | Clinic A user reads/mutates clinic B data by naming B's `clinic_id` | `enforce_clinic_access` (fail-closed) in `_ipd_scope` before any read | `test_phase2_route_adversarial_matrix` (all 38 routes), `test_cross_tenant_access_is_impossible` |
| T2 | Clinic B user passes clinic A's ids (IDOR) | every service query `scoped_query(table, scope)`; functions filter by `p_clinic_id`; composite FKs | E2E: 404 for A's admission, bed and observation targets from B |
| T3 | Super admin without a clinic | `enforce_clinic_access` refuses; matrix asserts no 200 | `test_admin_super_admin_scope_matrix` (IPD routes counted) |
| T4 | Direct API call to a disabled module | `ipd_access()` on every route; wind-down refuses new work | `test_module_is_off_until_the_owner_enables_it`, wind-down test |
| T5 | Enterprise wildcard grants IPD | `ipd_access` reads the explicit override only | unit logic + E2E |
| T6 | Double booking a bed (race) | row locks + 4 partial unique indexes | 20-way SQL race, 6-way HTTP race |
| T7 | Lost update on summary draft | `expected_updated_at` CAS → 409 | service code; see TEST-PLAN gaps |
| T8 | Tampering with signed clinical/financial records via SQL or a future bug | guard triggers (fire for `service_role`) | `test_migration_111_ipd.py` immutability tests |
| T9 | Retry duplicates (admission, order, MAR, charge, bill) | idempotency keys / `source_ref` / replay returns existing row | idempotency tests (SQL + E2E) |
| T10 | Nurse or desk makes a clinical decision | doctor-link rule + permission split | `test_doctor_without_link_cannot_sign_or_order`, E2E 403s |
| T11 | Billing staff sees clinical data | role-filtered read models | E2E asserts `observations` absent for billing |
| T12 | Raw DB errors leak schema/PHI in responses | `raise_for` maps `ipd_*` codes; others → fixed 500; log has error type only | service code |
| T13 | PHI in logs | IPD code logs no patient fields; audit `details` carry ids/kinds only | code review |
| T14 | Exposure via Supabase anon/authenticated keys | RLS forced + `REVOKE ALL` on tables and functions | migration §15 |
| T15 | Function search_path hijack | `search_path = public, pg_temp` on every `ipd_*` function | `test_every_ipd_function_has_pinned_search_path` |
| T16 | AI writes clinical content or bypasses authorization | no AI imports; Ketu read/hand-off only, dispatched as the user | `test_ipd_no_llm.py`, registry drift tests |
| T17 | Background job with wrong tenant | no IPD jobs (ADR-IPD-10) | — |
| T18 | XSS through patient/clinical text in the panel | every interpolation through `esc()`; ids in handlers are server UUIDs | code review (no automated DOM test yet) |
| T19 | OPD page edits IPD bill lines | trigger `ipd_guard_bill_items` | `test_discharge_end_to_end` |
| T21 | Branch-pinned staff change another branch's beds/wards (same clinic) | `_ward_in_branch()` before every bed/ward mutation; branch-filtered reads; SQL refuses cross-branch bed assignment | `test_branch_pinned_staff_cannot_touch_another_branchs_beds` |
| T20 | Owner disable interrupts care / deletes data | wind-down; deletion only through confirmed clinic purge | ADR-IPD-07 tests |

## 5. Compliance mapping (technical controls only — no certification is claimed)
| Requirement area | Control in IPD OS | Outstanding |
|---|---|---|
| DPDP Act 2023 (purpose limitation, access, security) | Consent captured at OPD registration; role-minimised reads; audit of clinical reads; tenant isolation | Erasure policy for IPD records (NMC retention vs erasure, like OPD D3) needs legal confirmation; data-principal access export not built |
| NMC record-keeping (identified prescriber, retention) | Doctor-linked authorship; registration number on signed summaries; no deletion except owner purge | Retention period configuration; DSC/e-signature not implemented (login-bound signature, same as OPD) |
| NABH (documentation, MAR, handover, audit trail) | Time-stamped attributed entries, MAR with reasons, handover notes, amendment provenance, event timeline | Formal NABH assessment; clinical audit reports; standard forms (consent, nursing assessment) |
| ABDM | Not integrated for IPD | Discharge summary as FHIR bundle / HIP linking (existing `app/services/abdm.py` covers other paths) |
| HIPAA | Not applicable to Indian deployments; controls overlap but no claim | — |
