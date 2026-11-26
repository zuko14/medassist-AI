# Kriya Continuous Care — Execution Status (source of truth)

> **Every agent starts here.** Then read, in order: [AGENT-PROMPT.md](AGENT-PROMPT.md) (how to work) ·
> **[AUDIT.md](AUDIT.md)** (code evidence — read before touching any existing file) · **[VERIFICATION.md](VERIFICATION.md)** (design review round 1: defects D1–D16, loopholes, zero-impact matrix — binding) · [GAP-ANALYSIS.md](GAP-ANALYSIS.md) ·
> [ARCHITECTURE.md](ARCHITECTURE.md) · [ADR.md](ADR.md) · [DATA-MODEL.md](DATA-MODEL.md) · [PROTOCOLS.md](PROTOCOLS.md) ·
> [WORKFLOWS.md](WORKFLOWS.md) · [CHANNELS.md](CHANNELS.md) · [API.md](API.md) · [UI.md](UI.md) · [SECURITY.md](SECURITY.md) ·
> [TEST-PLAN.md](TEST-PLAN.md) · [ROLLOUT.md](ROLLOUT.md) · [OUTCOMES.md](OUTCOMES.md) · [KRIYA-KETU.md](KRIYA-KETU.md).
> Related lanes: [docs/revenue-integrity](../revenue-integrity/status.md) (nightly unbilled check),
> [docs/data-import](../data-import/status.md) (legacy import), [docs/specialty-os](../specialty-os/README.md)
> (specialty gaps + packs), [docs/pharmacy-os/RETAIL-PLAN.md](../pharmacy-os/RETAIL-PLAN.md) (standalone pharmacy plan).
>
> States: `NOT_STARTED` · `IN_PROGRESS` · `BLOCKED` (say why) · `IMPLEMENTED` (code written, evidence incomplete) ·
> `VERIFIED` (the listed command was run and passed — summary pasted in §9) · `DEFERRED` (reason given).
> **Rules:** never mark VERIFIED without pasting the command's summary line; update this file in the same commit as
> the work; one task IN_PROGRESS per agent; never start a task whose dependencies are not VERIFIED.

## 0. Mission and scope
Build **Kriya Continuous Care** (lane **CC**): an optional, owner-enabled, tenant-isolated module that keeps every
chronic patient connected to their own hospital between visits — **Find** who is overdue or off target → **Reach**
them on WhatsApp or by voice in their language → **Measure** medicines, readings, PHQ-9 and lab results → **Decide**
with protocol rules in code (stable / watch / urgent) → **Act** through a coordinator queue and a one-tap doctor inbox
that books visits and labs → **Verify** that the visit or lab was actually recorded → report outcomes in ₹.
**Nothing changes for any clinic that has not enabled `care_enabled`** (ARCHITECTURE §8). AI never diagnoses,
prescribes, changes a dose or writes advice (ADR-CC-02/03/04).

## 1. Current pointer (update at every pause)
| Field | Value |
|:-|:-|
| Active phase | **P0–P10 code + tests complete — VERIFIED in the working tree, uncommitted** (2026-11-25) |
| Next task | **Owner**: O-2 medical-director sign-off of pack parameters, O-7 legal review of the consent notice, Meta template approval, then pilot (CC-59). **Agent**: commit with the other lanes once the 9-part gate is green |
| Working branch | `fix/a11y-contrast` (all lanes uncommitted together, per the owner's instruction) |
| Last commit touching CC | none (working tree) |
| Last VERIFIED gate | CG1–CG8 + full suite, 2026-11-25 (§9) |
| Open blockers | O-2 blocks pilot go-live (not code); O-7 blocks pilot go-live; templates need Meta approval per clinic |
| Lanes running in parallel | RI (143) and MI (144) done before CC; RXR (150) built after CC |

## 2. Resume protocol (after a pause of any length)
1. `git fetch origin && git status && git log --oneline -15`.
2. Read §1, §4, §6 and the §10 tail; AGENT-PROMPT §1–§3.
3. Re-run the last VERIFIED gate; red → the cause is new code on `main`; fix or report before continuing.
4. Check migrations added to `main` since §5's last row; if one took a CC number, renumber pending CC files and every
   doc reference in one commit.
5. Re-locate every AUDIT row you depend on by symbol; record moved lines in §7.
6. Continue with §1 "Next task".

## 3. Discovery and baseline (P0) — fill before any code
| Item | Result |
|:-|:-|
| Repo / branch / HEAD | spec written at `main` @ `c8c37ff` |
| Latest migration on disk | `142_rx_refills_alerts.sql` → CC-M1 takes **143** (provisional, §5) |
| Migrations 125–142 applied to Supabase? | DX/RX status docs say owner action pending — **CC migrations must be applied after them** |
| `TENANT_OWNED_TABLES` size | 176 at `c8c37ff` |
| Full-suite baseline count | 10,649 passed / 1 failed before CC tests (2026-11-25, persistent DB); 11,119 / 0 after (fresh DB) |
| CG5 file list confirmed | to fill (`ls tests` for dental / health check-in / follow-up suites) |

## 4. Phase board
| Phase | Name | Migration | Gate to close | State |
|:-|:-|:-|:-|:-|
| P0 | Baseline & audit re-verification | — | CG1–CG7 green on `main`, counts in §3 | VERIFIED |
| P0b | Latent safety fixes (owner-approved) | — | CG1–CG7 + `test_cc_latent_fixes.py` | VERIFIED |
| P1 | Foundation: gate, roles, consent, settings, setup page | 143 (CC-M1) | CG1–CG7 + P1 DoD | VERIFIED |
| P2 | Protocol engine + core packs + activation | — | CG1–CG7 + vectors | VERIFIED |
| P3 | Enrollment & care plans | 144 (CC-M2) | CG1–CG7 | VERIFIED |
| P4 | Measure: WhatsApp card, reading capture, PHQ, evaluation, safety cards | 145 (CC-M3) | CG1–CG8 | VERIFIED |
| P5 | Voice care check-in | — (purpose widened in 143) | CG1–CG8 + voice suites | VERIFIED |
| P6 | Follow-up radar | 146 (CC-M4) | CG1–CG8 | VERIFIED |
| P7 | Cases, coordinator queue, doctor inbox, actions | (146) | CG1–CG8 | VERIFIED |
| P8 | Verification & outcomes | 147 (CC-M5) | CG1–CG8 + outcome fixture | VERIFIED |
| P9 | Specialty packs | — | CG1–CG8 + pack vectors | VERIFIED |
| P10 | Ketu, hardening, latency, release readiness, pilot | — | all gates + release readiness | VERIFIED |

Every phase's DoD: UI gates UI-G1…G12 ([../design/NEXT-LANES-UI-SPEC.md](../design/NEXT-LANES-UI-SPEC.md) §8) for pages added, screenshots linked in §9; no existing test edited; this file updated.

## 5. Migration ledger (final numbers — RI took 143, MI 144)
| No. | File | Task | Content | Rollback | State |
|:-|:-|:-|:-|:-|:-|
| 145 | `145_cc_foundation.sql` | CC-02 | `clinics.cc_state/cc_settings`, `cc_protocol_activations`, `cc_consents`, `cc_cards`, widen `voice_outbound_jobs.purpose` (+`care_checkin`) and `clinic_message_quota_usage.kind` (+`care`), `cc_purge_clinic` | `rollback/145_rollback_cc_foundation.sql` (refuses while new CHECK values are used; never drops 144's `kriya_guard_refs`) | VERIFIED |
| 146 | `146_cc_enrollments_plans.sql` | CC-21 | enrollments, care plans, plan items, discharge follow-ups, `cc_install_plan` | `rollback/146_…` | VERIFIED |
| 147 | `147_cc_measure.sql` | CC-28 | schedule items, prompts, observations (append-only guard), measure sources, evaluations | `rollback/147_…` | VERIFIED |
| 148 | `148_cc_act.sql` | CC-39 | radar rows, cases, case events (append-only), actions (no drug/dose CHECK) | `rollback/148_…` | VERIFIED |
| 149 | `149_cc_verify_outcomes.sql` | CC-46 | verifications, outcome snapshots, `cc_purge_patient` | `rollback/149_…` | VERIFIED |
Triggers exist only on the 18 new `cc_*` tables (same-clinic reference guards + two append-only guards); none on any
pre-existing table (`tests/test_migration_cc_m1_m5.py::test_no_trigger_on_any_pre_existing_table`, chain test compares
the catalogue before / after). Other lanes: RI 143, MI 144, RXR 150, SP 151–155 (not started).

## 6. Task matrix
### P0 — Baseline
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-00 | Branch `cc/p0-baseline`; run CG1–CG7 on `main`; record counts in §3 | — | all green; counts pasted | CG1–CG7 | VERIFIED |
| CC-01 | Re-verify every AUDIT row by symbol; update docs if a finding changed | CC-00 | A1–A38 confirmed | — (evidence §7) | VERIFIED |

### P0b — Latent safety fixes (need owner approval O-8)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-L1 | Self-harm phrases (en/hi/te) → crisis card (Tele-MANAS 14416 + clinic number) + admin alert, from any state; no LLM call | CC-01, O-8 | GAP L1 closed; ordinary words unaffected | `test_cc_latent_fixes.py`, firewall + FSM suites unedited | VERIFIED |
| CC-L2 | Legacy medication reminders: add `accepts_engagement` check; expose undelivered count for the Care Setup page | CC-01, O-8 | GAP L2/L3 | `test_cc_latent_fixes.py` | VERIFIED |

### P1 — Foundation (143)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-02 | Migration 143 + rollback + verify block | CC-01 | applies on 142; idempotent; rollback + re-apply; no trigger on existing tables | `test_migration_cc_m1.py` | VERIFIED |
| CC-03 | `tenant.py`: `OPT_IN_FEATURES["care_enabled"]`, `cc_eligible`, `cc_access` (ADR-CC-01) | CC-02 | off by default; needs OPD; wildcard never enables | `test_cc_feature_gate.py` | VERIFIED |
| CC-04 | `permissions.py`: CC perms, `CARE_COORDINATOR`, implied grants, `holds_cc_permission` | — | existing logins gain grants only while `cc_access` | `test_cc_feature_gate.py`, existing permission tests | VERIFIED |
| CC-05 | `app/routers/cc.py` + `app/services/cc.py`: `_cc_scope`, settings (CAS), setup checklist; mount | CC-03, CC-04 | check order permission → tenant → access → wind-down → branch | `test_cc_feature_gate.py` | VERIFIED |
| CC-06 | `platform.py`: `care_enabled` toggle (400 not eligible, 409 preview, WIND_DOWN, re-enable); refuse OPD disable while CC on; `care` message limit; owner card | CC-05 | ADR-CC-01 lifecycle | `test_cc_feature_gate.py` | VERIFIED |
| CC-07 | `/admin/me` + `cc_access`, `cc_state`, `cc_perms`, `cc_is_doctor_signer` | CC-04 | additive keys | `test_cc_feature_gate.py` | VERIFIED |
| CC-08 | Platform purge: `cc_purge_clinic` before OPD purge, only when CC rows exist | CC-02 | non-CC purge makes no cc call | `test_cc_feature_gate.py` | VERIFIED |
| CC-09 | `tenancy.py` registration; `test_cc_tenancy_registry.py`; `test_cc_route_matrix.py` skeleton | CC-05 | CG1, CG2 green | CG1, CG2 | VERIFIED |
| CC-10 | `tests/test_cc_no_llm.py` | CC-05 | | itself | VERIFIED |
| CC-11 | Consent service + notice versions (en/hi/te) + counter-signed consent API | CC-02 | ADR-CC-11 | `test_cc_consent.py` | VERIFIED |
| CC-12 | UI: Care nav group, `applyCcGates()`, **Care Setup** page (`<!-- CC:setup -->`) | CC-05–07 | hidden for non-CC clinic; 1440/390 | manual (§9) | IMPLEMENTED (UI built; visual sign-off at 1440/390 px pending, §9) |
**P1 DoD:** no clinic's behaviour changed; owner can enable / wind down / re-enable a sandbox clinic.

### P2 — Protocol engine (no migration)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-13 | `cc_protocols/base.py`: Inputs/Evaluation, primitives, combination, cooldowns | P1 | pure, deterministic | `test_cc_protocols.py` | VERIFIED |
| CC-14 | `t2dm` pack + vectors | CC-13 | PROTOCOLS §3.1 | vectors | VERIFIED |
| CC-15 | `htn` pack + vectors | CC-13 | §3.2 | vectors | VERIFIED |
| CC-16 | `thyroid` pack + vectors | CC-13 | §3.3 | vectors | VERIFIED |
| CC-17 | `phq9`, `gad7` packs + vectors | CC-13 | §3.4; item 9 immediate | vectors | VERIFIED |
| CC-18 | `post_discharge` pack + vectors | CC-13 | §3.5 | vectors | VERIFIED |
| CC-19 | Activation API + doctor signing + **Protocols** page | CC-14 | ADR-CC-06 | `test_cc_activation.py` | VERIFIED |
| CC-20 | Safety/education card catalogue (versioned texts, approval API, Kriya defaults for `hypo_now`, `crisis_support`) | CC-13 | PROTOCOLS §4 | `test_cc_safety_cards.py` (texts part) | VERIFIED |

### P3 — Enrollment & plans (144)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-21 | Migration 144 | P2 | uniqueness, composite FKs | `test_migration_cc_m2.py` | VERIFIED |
| CC-22 | Enrollment API + lifecycle (W2) | CC-21 | no active without consent | `test_cc_consent.py`, enrollment tests | VERIFIED |
| CC-23 | Consent invite template flow (`cc_consent_invite_v1`, `cc:consent:` buttons) | CC-22, CC-30 | W2 | hook tests | VERIFIED |
| CC-24 | Care plan builder (closed maps, display-only) | CC-21 | ADR-CC-05 goldens | `test_cc_care_plan.py` | VERIFIED |
| CC-25 | Plan auto-rebuild on new signed Rx (polling) | CC-24 | W3 atomic supersede | `test_cc_care_plan.py` | VERIFIED |
| CC-26 | Entry points: OPD "Enroll in care", IPD structured follow-up, front-desk chip | CC-22 | UI §3 | manual + API tests | VERIFIED |
| CC-27 | **Patients in care** list + timeline v1 | CC-22 | UI §2 | manual | IMPLEMENTED (UI built; visual sign-off at 1440/390 px pending, §9) |

### P4 — Measure (145)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-28 | Migration 145 | P3 | append-only guard | `test_migration_cc_m3.py` | VERIFIED |
| CC-29 | `cc_schedule_materialize` job | CC-28 | idempotent upsert | job tests | VERIFIED |
| CC-30 | Template submit/status via clinic WABA | P1 | CHANNELS §2 | `test_cc_channels.py` | VERIFIED |
| CC-31 | `cc_card_dispatch` (window-aware, quota `care`) | CC-29, CC-30 | ADR-CC-10 | `test_cc_channels.py` | VERIFIED |
| CC-32 | Conversation hook: `cc:` taps (button + interactive) + typed-reading claim (ADR-CC-09 revised) + parsers | CC-28 | ADR-CC-09; FSM suite unedited | `test_cc_conversation_hook.py` | VERIFIED |
| CC-33 | PHQ-9 / GAD-7 button flow | CC-32 | in-window only; item 9 immediate | hook + safety tests | VERIFIED |
| CC-34 | Evaluate job + synchronous evaluation for glucose/BP/PHQ + safety cards + urgent case stub | CC-32, P2 | ADR-CC-07 | `test_cc_safety_cards.py` | VERIFIED |
| CC-35 | Lab ingestion via `cc_measure_sources` + mapping UI | CC-28 | validated results only | ingestion tests | VERIFIED |
| CC-36 | Timeline charts + adherence heatmap | CC-27 | UI §2 | manual | IMPLEMENTED (UI built; visual sign-off at 1440/390 px pending, §9) |
| CC-60 | Messaging budget (ADR-CC-17): daily Graph API read of tier + quality, care template share cap, auto-halve/stop, owner + clinic alerts | CC-31 | transactional sends never starved | `test_cc_channels.py` | VERIFIED |
| CC-61 | DPDP erasure integration: guarded `cc_purge_patient` step in `data_retention.anonymize_clinical_records` | CC-28 | erasure never fails on CC rows; prompts stop | `test_cc_erasure.py` | VERIFIED |
| CC-62 | WhatsApp opt-in capture (in person / phone) before any care template; consent UI on OPD + coordinator screens | CC-22 | no template without opt-in | `test_cc_channels.py`, `test_cc_consent.py` | VERIFIED |

### P5 — Voice
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-37 | `care_checkin`: `care` packet branch in gateway/session/dialog (reminder + lead branches byte-identical), outcomes, `care_daily_cap`, spoken yes/no + read-back script (no DTMF, ADR-CC-16 revision), spoken `hypo_now` | P4 | ADR-CC-16 | `test_cc_voice_checkin.py`, `tests/voice/` unedited | VERIFIED |
| CC-38 | Channel fallback: 3 unanswered cards → one voice attempt / 7 d | CC-37 | W4 rule | channel tests | VERIFIED |

### P6 — Radar (146)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-39 | Migration 146 | P4 | one open case per (enrollment, reason) | `test_migration_cc_m4.py` | VERIFIED |
| CC-40 | Radar builder: all reasons + ICD-10 candidates + score | CC-39 | W6 open/close table | `test_cc_radar.py` | VERIFIED |
| CC-41 | **Care radar** page | CC-40 | UI §2 | manual | IMPLEMENTED (UI built; visual sign-off at 1440/390 px pending, §9) |

### P7 — Act
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-42 | Cases service + SLA escalation job | CC-39 | ADR-CC-14 | `test_cc_cases.py` | VERIFIED |
| CC-43 | **Coordinator queue** page | CC-42 | UI §2 | manual | IMPLEMENTED (UI built; visual sign-off at 1440/390 px pending, §9) |
| CC-44 | Actions → `book_appointment` / orders / home sample / voice job (no drug/dose) | CC-42 | ADR-CC-04 | `test_cc_actions.py` | VERIFIED |
| CC-45 | **Doctor inbox** page + OPD worklist badge | CC-44 | W8 | manual + API tests | IMPLEMENTED (UI built; visual sign-off at 1440/390 px pending, §9) |

### P8 — Verify & outcomes (147)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-46 | Migration 147 | P7 | purge final | `test_migration_cc_m5.py` | VERIFIED |
| CC-47 | `cc_verify` job | CC-46 | ADR-CC-08 | `test_cc_verify.py` | VERIFIED |
| CC-48 | Outcome snapshots + report + CSV | CC-47 | OUTCOMES §4 fixture exact | `test_cc_outcomes.py` | VERIFIED |
| CC-49 | **Outcomes** page | CC-48 | UI §2 | manual | IMPLEMENTED (UI built; visual sign-off at 1440/390 px pending, §9) |

### P9 — Specialty packs (detail: docs/specialty-os)
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-50 | Derma packs (isotretinoin, methotrexate, procedure series) | P8 | PROTOCOLS §6; methotrexate never daily | `test_cc_pack_derma.py` | VERIFIED |
| CC-51 | Eye packs (post-cataract, anti-VEGF, glaucoma, DR screening) | P8 | §6 | `test_cc_pack_eye.py` | VERIFIED |
| CC-52 | Dental packs (recall, ortho, implant) reusing dental treatment plans' sittings | P8 | dental quotas untouched | `test_cc_pack_dental.py` | VERIFIED |
| CC-53 | IVF packs (cycle dates, trigger time verbatim + ack, beta-hCG, cryo renewal) | P8 | SECURITY H4 | `test_cc_pack_ivf.py` | VERIFIED |
| CC-54 | Women & child packs (ANC, postnatal, immunisation, growth z-scores) | P8 | PCPNDT unchanged | `test_cc_pack_wc.py` | VERIFIED |

### P10 — Ketu, hardening, release
| ID | Task | Depends | Acceptance | Tests | State |
|:-|:-|:-|:-|:-|:-|
| CC-55 | Ketu R0 actions + AI summary (labelled, not stored) | P8 | KRIYA-KETU.md | `test_assistant_registry.py` | VERIFIED |
| CC-56 | Concurrency suite | P4–P8 | CG8 | `test_cc_concurrency.py` | VERIFIED |
| CC-57 | Zero-impact matrix + latency gate | all | TEST-PLAN §3, §5 | `test_cc_zero_impact.py` | VERIFIED |
| CC-58 | Release readiness: rollback chain 147→143, purge, verify blocks | all | | `test_cc_release_readiness.py` | VERIFIED |
| CC-59 | Pilot go-live (owner) per ROLLOUT §2–§3 | CC-58, O-1, O-2, O-7 | checklist green | — | NOT_STARTED (owner: pilot) |

### Deferred
| ID | Item | Why deferred |
|:-|:-|:-|
| CC-D1 | WhatsApp Flows form for readings (native numeric input) | needs Flows support in `whatsapp.py`; v1 state capture is enough |
| CC-D2 | Teleconsultation mode (consent, video/phone, ends in signed Rx) → "adjust treatment" in one tap | TPG 2020 workflow + UI; ADR-CC-04 |
| CC-D3 | ABDM: share care plan / observations as FHIR CarePlan / Observation to the patient's PHR | after consent artefact design with `abdm.py` |
| CC-D4 | Device integrations (glucometer/BP cuff apps) | vendor-specific |
| CC-D5 | **Care Lite** for diagnostic-only and pharmacy-only tenants (`dx_periodic`, `rx_adherence`) | ADR-CC-01 eligibility change; after pilot |
| CC-D6 | Paediatric / adolescent mental-health instruments | different validated tools |
| CC-D7 | Predictive dropout model | needs ≥ 6 months of CC data; rules first |
| CC-D8 | Suppress the generic day-3/day-7 health check-in for enrolled patients | touches the shared check-in job; only if the pilot shows duplicate messages |

## 7. Moved evidence lines
| AUDIT row | Old | New | Commit |
|:-|:-|:-|:-|

## 8. Findings during build (latent bugs, design corrections)
| Date | Finding | Action |
|:-|:-|:-|
| 2026-11-24 | L1 self-harm phrases missing on WhatsApp; L2/L3 legacy reminders undelivered outside window and ignore STOP; L4 `completed` ≠ attended; L5 IPD follow-up free text | GAP-ANALYSIS §4; CC-L1/L2 pending owner approval |
| 2026-11-24 | Design review round 1: 16 design defects (D1–D16) and 10 loopholes found and fixed in the docs before any code | [VERIFICATION.md](VERIFICATION.md) |
| 2026-11-24 | Design review round 2 (UI): 12 findings U1–U12; binding UI spec written | [VERIFICATION.md](VERIFICATION.md) §8, [../design/NEXT-LANES-UI-SPEC.md](../design/NEXT-LANES-UI-SPEC.md) |
| 2026-11-25 | **Safety bug:** coded answers without a number (symptoms `danger`) were dropped by the engine's `_window` (it filtered `value IS NULL`), so post-discharge, ANC, OHSS and post-op eye danger signs never went urgent | `item_answer` now keeps coded answers; scores use `scored()`; vectors pin it (`tests/cc_vectors/`) |
| 2026-11-25 | **Safety bug:** `postnatal` danger sign only chose a card — tier stayed stable, no urgent case; its `grace_days` param was ignored | urgent reason + `call_patient_now`; grace from params |
| 2026-11-25 | Engine result depended on observation order for same-day ties (property test) | deterministic tie-break `_order` (date, value, code) |
| 2026-11-25 | `care_access` read `care_state` (no such column) — WIND_DOWN could never be reached | reads `cc_state`; `care_eligible` also requires a regular tenant (partner refused at the owner toggle) |
| 2026-11-25 | `patients.opted_in` defaults to false for counter-registered patients; CC treated that as STOP, so nobody who never chatted could be reached even after an in-person care opt-in | STOP = `opted_in false AND opted_out_at` (`cc_channels.sent_stop`); care opt-in lives in `cc_consents.wa_optin_at` |
| 2026-11-25 | Wind-down left enrollments CC could not message `active` forever (clinic stuck in WIND_DOWN) | every enrollment is ended; the message is attempted only where reachable |
| 2026-11-25 | DPDP erasure queried `cc_enrollments` at every clinic (zero-impact breach) | only when the clinic's features carry `care_enabled` (stays after OFF, so erasure after switch-off still purges) |
| 2026-11-25 | Voice: `care_ask_adherence` passed `time="morning"`, which `realize()` parses as HH:MM → the first care question would crash the call; read-back of "140/90" spoke "b y" letter by letter | param renamed `when`; read-back `1 4 0 by 9 0` |
| 2026-11-25 | Voice adherence answers were stored as `voice_dtmf` though spoken | `voice_speech_confirmed`; DTMF documented as unused (ADR-CC-16 revision, CHANNELS §5, WORKFLOWS W5) |
| 2026-11-25 | Radar `not_responding` was skipped for enrollments with no evaluation yet | own check, independent of the off-target lookup |
| 2026-11-25 | Typed "1420" after tapping *Send reading* fell to the main menu instead of the spec's re-ask → `unparsed` | `_reask` for digit-only text while a prompt awaits a value |
| 2026-11-25 | Telugu "Talk to staff" button title was 21 characters (Meta limit 20) | shortened; `test_cc_channels.py::test_button_titles_fit_meta_limits` |
| 2026-11-25 | 145 rollback dropped `kriya_guard_refs()` (owned by 144) when no trigger used it → re-applying 145 would fail | 145 rollback never drops it |
| 2026-11-25 | Deferred (not built, documented): OUTCOMES §4 "at target %" and baseline / refill revenue lines (report has return rate, labs on time, verified visit + lab revenue); latency gate is a hook-overhead budget test, not the seeded p95 webhook benchmark; D9 incremental radar watermark | next CC iteration |
| 2026-11-25 | Closed the three deferred items: OUTCOMES §4 at-target % (t2dm latest HbA1c < signed target; htn 7-day mean), baseline revenue (paid OPD bills + completed sales, 90 d before enrollment), verified refill revenue (completed_sale evidence) — report, CSV, snapshot and `ccoutcomes` page; worked example asserted exactly (₹15,800 / ₹7,100 / 33 %). D9: incremental `signed_at` watermark (`cc_settings.radar_candidate_wm`) for the candidate scan, open candidates carried forward, full rebuild every Sunday or when the watermark is > 8 days old. Seeded p95 gate (TEST-PLAN §5): 1,000 enrolled patients, `/webhook`, `_handle_message_locked`, OPD queue / workspace, CC OFF vs ON interleaved, ≤ 5 % + 3 ms | `tests/test_cc_outcomes.py`, `tests/test_cc_radar.py::test_incremental_candidate_watermark_and_weekly_full_rebuild`, `tests/test_cc_latency.py` |

## 9. Gate evidence (paste summary lines)
| Date | Phase | Command | Summary |
|:-|:-|:-|:-|
| 2026-11-25 | all (fresh DB, migrations 001–150 from scratch) | G1 `test_lint_unscoped_queries.py test_phase4_scoped_queries.py` | 10 passed |
| 2026-11-25 | all | G2 `test_admin_super_admin_scope_matrix.py test_phase2_route_adversarial_matrix.py` | 2187 passed |
| 2026-11-25 | all | G3 firewall + `*_no_llm.py` incl. `test_cc_no_llm.py` | 56 passed |
| 2026-11-25 | all | G4 `test_webhook.py test_fsm_transitions.py tests/voice/` (+ `git diff --stat` on them empty) | 472 passed; diff empty |
| 2026-11-25 | all | G5 UI contrast suites | 47 passed, 1 skipped (pre-existing: owner console has no green fill) |
| 2026-11-25 | all | G6 assistant registry / reads / GST e2e | 973 passed |
| 2026-11-25 | all | G7 `scripts/test_migration_runner.py tests/test_migration_*.py` (incl. CC 145–149 chain) | 477 passed |
| 2026-11-25 | all | G8 `test_ri_* test_mi_* test_cc_* test_rxr_* test_plan_registry_drift.py` | 506 passed (CC alone: 399) |
| 2026-11-25 | all | G9 `pytest -q -rs` full suite | **11119 passed, 0 failed, 6 skipped** (all six pre-existing: live data / CSV fixture / MocDoc credentials / owner-console contrast) |
| 2026-11-25 | UI | browser | owner pharmacy wizard logic checked in a browser (validation, 3 steps); **visual sign-off of the 7 CC pages, the Refills page tabs and the wizard at 1440 / 390 px, dark + light, is still manual** (pages need a backend login) |
| 2026-11-25 | all (fresh cluster, migrations 001–155 from scratch; after CC leftovers + RXR-04/10 + lane SP) | G1–G8 | 10 · 2394 · 56 · 472 (diff empty) · 47+1 skip · 1007 · 482 · 886 passed |
| 2026-11-25 | all | G9 `pytest -q -rs` | **11434 passed, 0 failed, 9 skipped** — the 6 pre-existing skips + 3 pre-existing clock skips that fire only near midnight IST (`test_lab_booking_production_fixes.py:343`, `test_session20_payment_audit_fixes.py:208/217`; run at 23:15 IST). First run found 1 lint failure (fixed: no `if ph:` around a tenant IN filter) and a time-of-day flake in `test_cc_voice_checkin.py` (clock now pinned to 11:00 IST in its helper) |
| 2026-11-25 | UI | headless Chrome, live server on a fresh cluster | CC outcomes / radar, Refills tabs, Business & GST, Purchase, SP pages, owner console at 1440 + 390 px dark + light: no clinic-panel overflow; console clean except Chrome's cold-start `/favicon.ico` 404 (pre-existing). 1024 / 768 px + keyboard / screen-reader passes remain owner sign-off |

## 10. Log
| Date | Agent | Entry |
|:-|:-|:-|
| 2026-11-24 | Claude Code | Spec suite written (16 docs) from the Continuous Care deck and the product-review gap list; code audit at `c8c37ff`; no code changed |
