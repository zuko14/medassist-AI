# Kriya IPD OS — Test and Acceptance Plan

Run with the repo venv (`.venv/Scripts/python.exe -m pytest …`). Real-Postgres tests use the embedded
`pgserver` (PG 16) from `tests/conftest_db.py`, or `TEST_DATABASE_URL` (staging). Never point them at production.

## 1. Suites
| File | Kind | What it proves |
|---|---|---|
| `tests/test_migration_111_ipd.py` | real Postgres, SQL level | every invariant, trigger, function, race; idempotent re-apply; rollback + re-apply |
| `tests/test_ipd_api_e2e.py` | real HTTP routes + real Postgres (`tests/ipd_pg_bridge.py`) | owner entitlement, full journey, role denials, cross-tenant, wind-down, HTTP-level race |
| `tests/test_ipd_no_llm.py` | AST/text | IPD never imports AI/voice/assistant; AI/voice/chat/assistant never name IPD tables |
| `tests/test_phase2_route_adversarial_matrix.py`, `tests/test_admin_super_admin_scope_matrix.py` | route matrices | all 38 `/admin/ipd/*` routes refuse another tenant and an unscoped super admin |
| `tests/test_assistant_registry.py`, `test_assistant_reads.py`, `test_assistant_handoff.py`, `test_assistant_rules.py` | Ketu | IPD pages = sidebar; reads validated, no route drift, handler permission really checked, data shown; corpus of en/te/hi phrases routes correctly; change requests never answered by a read |
| `tests/test_lint_unscoped_queries.py`, `test_phase4_scoped_queries.py` | AST linter | no unscoped query on a tenant table (IPD tables registered) |
| full suite | regression | OPD, WhatsApp, voice, payments, Ketu unaffected |

## 2. Requirement → evidence
| Requirement (brief §11.2 / §11.3) | Test(s) |
|---|---|
| Admission with valid / invalid patient | `test_admit_into_bed_occupies_it_and_records_history`, `test_cross_tenant_ids_are_not_found` |
| Bed unavailable / ineligible | `test_occupied_bed_is_refused_and_leaves_no_admission`, `test_gender_restricted_ward`, `test_bed_status_rules` |
| Two concurrent requests for one bed | `test_twenty_concurrent_admissions_to_one_bed_exactly_one_wins` (SQL, 20), `test_concurrent_api_admissions_to_one_bed` (HTTP, 6) |
| One active stay per patient under concurrency | `test_one_active_stay_per_patient_even_concurrently` |
| Reservation expiry, cancellation, reassignment | `test_reservation_hold_expiry_and_cancel`, `test_own_reservation_is_released_when_admitted_elsewhere` |
| Transfer, failed transfer leaves state consistent, races, no deadlock | `test_transfer_moves_patient_and_keeps_history`, `test_failed_transfer_changes_nothing`, `test_stale_transfer_is_refused`, `test_concurrent_transfers_into_one_bed`, `test_concurrent_swap_does_not_deadlock` |
| Observation creation, correction, audit history | `test_observation_immutable_and_corrected_with_provenance`, `test_observation_typo_bounds_and_shape`, E2E journey |
| Duplicate delivery / retry | idempotency: `test_idempotency_key_replays_the_same_admission`, E2E (admission, order, charge, bill replays) |
| Discharge authorization + incomplete prerequisites | `test_discharge_end_to_end`, `test_active_order_blocks_discharge`, `test_summary_signing_rules`, `test_unregistered_attending_cannot_sign`, E2E (desk 403, waiver 403 for non-admin) |
| Billing posting, no duplicate charges, reconciliation | `test_bed_charges_are_idempotent_and_priced_from_tariff`, `test_posted_charge_is_immutable`, `test_voided_bill_releases_charges_for_a_new_bill`, E2E `invoice.total_paise == Σ posted` via the real OPD issue route |
| Module activation / deactivation / reactivation; active admissions during deactivation | `test_module_is_off_until_the_owner_enables_it`, `test_ipd_requires_opd`, `test_owner_disable_with_patients_winds_down_and_reenable_loses_nothing`, `test_disable_without_patients_turns_off_and_blocks_routes` |
| Unauthorized role access | E2E (nurse→progress note/order/stop med 403, desk→regular discharge 403, billing sees no clinical data, unlinked clinician 403, maintenance by nurse 403) |
| Cross-tenant requests and identifier manipulation | `test_cross_tenant_access_is_impossible`, adversarial + super-admin matrices |
| Background jobs with wrong tenant | N/A — no IPD jobs (ADR-IPD-10) |
| Records immutable, purge, rollback | `test_bed_state_cannot_be_written_directly`, `test_notes_and_amendment_by_author_only`, `test_mar_rules`, `test_purge_removes_everything_and_clinic_can_be_deleted`, `test_rollback_refuses_while_admitted_then_reapplies`, `test_reapply_is_idempotent`, `test_every_ipd_function_has_pinned_search_path` |
| E2E journey (§11.3 steps 1–12) | `test_full_inpatient_journey` (+ cross-tenant, wind-down tests); step 12 OPD = full-suite regression |

## 3. Known gaps (not yet automated — manual procedure or future work)
| Gap | Manual verification until automated |
|---|---|
| Browser UI (admin/index.html IPD pages) has no automated DOM test | Drive each page in Chrome desktop + 390 px (as done for OPD on 2026-10-09): enable IPD on sandbox; configure; admit; nurse/doctor/billing tabs; transfer; discharge; check no console errors |
| Summary-draft concurrent edit (CAS 409) | Two browsers edit the same draft; second save must get 409 |
| Staging run of migration 111 + E2E against Supabase PostgREST (the bridge emulates PostgREST) | ROLLOUT.md §1 step 2 |
| Load: 1,000-bed board latency | Seed 1,000 beds on staging; `GET /admin/ipd/board` p95 < 1 s target |
| Ketu IPD phrases against the live LLM fallback | `scripts/assistant_llm_smoke.py` with IPD phrases |
| Accessibility audit of new pages (keyboard, contrast, screen reader) | Lighthouse + manual keyboard pass |
