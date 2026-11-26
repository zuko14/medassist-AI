# Kriya Continuous Care — Security, Privacy, Clinical Safety and Compliance

## 1. Threat model summary
| Asset | Threat | Control |
|:-|:-|:-|
| Readings, adherence, PHQ-9 answers (sensitive health data) | cross-tenant read via unscoped query | every `cc_*` table in `TENANT_OWNED_TABLES`; `scoped_query` only; AST lint (A37) |
| Same | staff of another branch | branch pin in `_cc_scope`; enrollment carries `branch_id`; route matrix tests |
| Patient | wrong patient gets a card (shared phone) | cards name the person; family-member enrollments under the account holder only; identity check on voice (year of birth) |
| Patient safety | missed low-sugar / self-harm signal | ADR-CC-07 synchronous cards + urgent case; CC-L1 keyword fix; tests per trigger |
| Clinical decisions | AI or code silently changes treatment | ADR-CC-03/04: no drug/dose field anywhere in CC write paths; `test_cc_no_llm.py` |
| Consent | messaging without a lawful basis | ADR-CC-11; no `active` enrollment without a live consent (CHECK) |
| Clinic reputation | Meta blocks the number for spam | utility templates only, opt-in first, quota caps, quality monitoring (CHANNELS §7) |
| Audit | staff edits history | observations / case events append-only; corrections are new rows |

## 2. Roles and permissions
| Permission | Granted to (only while `cc_access`) | Allows |
|:-|:-|:-|
| `CC_VIEW` | clinic admin login, staff roles `DOCTOR`, `CARE_COORDINATOR`, `NURSE` (implied, like `_RX_GRANTS_IMPLIED_BY_ROLE`) | read pages, masked data |
| `CC_COORDINATE` | `CARE_COORDINATOR`, clinic admin login | cases, notes, non-clinical actions, staff-entered readings, counter-signed consent |
| `CC_APPROVE` | DOCTOR logins linked to a doctor row with a registration number | sign activations, enroll, clinical actions (W8) |
| `CC_ADMIN` | clinic admin login (grantable to `BRANCH_MANAGER` per staff account) | settings, templates, mappings, card texts, exports |
New staff role `CARE_COORDINATOR` (added to `STAFF_ROLES` and `ROLE_PRESETS` in `app/services/permissions.py`) (nurse / counsellor / health coach) with `CC_VIEW` + `CC_COORDINATE` only. Super-admin
reads as usual; super-admin **cannot** sign an activation (no registration number).

## 3. Data protection (DPDP Act 2023; DPDP Rules 2025 phasing in)
- **Purpose limitation:** `continuous_care` purpose recorded per person (ADR-CC-11); data collected only for items in
  the person's plan.
- **Notice:** itemised (what we collect, why, who sees it, how long, how to withdraw, grievance contact), in the
  patient's language, versioned with SHA-256; link to the clinic's privacy notice (`HOSPITAL_PRIVACY_POLICY_URL`).
- **Withdrawal** as easy as consent: button on every card, "STOP CARE", voice option, counter.
- **Minimisation in transit:** templates carry first name, hospital name, due item names; medicine names only if the
  clinic turns `card_show_medicine_names` on (default off); never diagnoses, values or PHQ answers in a template.
- **Logs:** phones masked (`+91XXXXXX7890`), readings never logged, payload ids only.
- **Erasure:** `cc_purge_patient` wired into the existing DPDP erasure flow; consents kept as proof (withdrawal
  recorded) — legal basis to be confirmed (O-7).
- **Children:** guardian consent via the account holder; no profiling; no PHQ instruments for minors in v1.
- **Processor note:** WhatsApp messages transit Meta under the clinic's own WABA; the clinic is the data fiduciary,
  Kriya the processor — reflected in the clinic agreement.

## 4. Clinical safety case
| Hazard | Cause | Mitigation | Test |
|:-|:-|:-|:-|
| H1 Hypoglycaemia not acted on | reading typed with words, mid-booking, or after the prompt expired (not claimed) | card asks for "just the number"; reading prompts stay open 12 h; unclaimed numeric replies from enrolled patients are counted on the coordinator dashboard; the next card asks again; firewall/emergency rules still apply to worded messages | `test_cc_conversation_hook.py` |
| H2 Number mis-parsed (1420 → 142) | parser too lenient | whole-message regex + plausible ranges + one re-ask | parser vectors |
| H3 Methotrexate daily prompts | "weekly" mapped to OD | weekly/custom frequencies are display-only (ADR-CC-05) | care-plan goldens |
| H4 Trigger injection time wrong (IVF) | engine computes time | time is copied verbatim from the doctor's entry; acknowledgement button; coordinator call if not acknowledged | `test_cc_ivf_cycle.py` |
| H5 Patient thinks someone watches 24×7 | wording | reply hours on consent and every card; emergency number on every safety card | card fixture checks |
| H6 Alert fatigue → coordinator ignores queue | too many watch cases | bounded parameters, cooldowns, priority score, SLA escalation, weekly noise report | pilot metric |
| H7 Self-harm statement on WhatsApp | no keyword | CC-L1 + PHQ item 9 | `test_cc_safety_cards.py`, firewall suite |
| H8 Stale plan after Rx change | missed poll | plan rebuild on every new signed version; plan shows "from Rx dated …" | W3 tests |
| H9 Doctor approves for wrong patient | UI | inbox card shows name + MRN + age + photo-free identity; action confirmation repeats the name | UI checklist |

Clinical governance: the hospital's medical director owns pack parameters (signed activation); Kriya owns pack code
quality (golden vectors, reviewed changes, version notes). A pack change log is shipped with every version.

## 5. Regulatory map
| Rule | Where it bites | How CC complies |
|:-|:-|:-|
| NMC RMP (Professional Conduct) Regulations 2023 / Telemedicine Practice Guidelines 2020 | AI must not diagnose/prescribe; prescribing needs a consultation | ADR-CC-03/04; teleconsult deferred (CC-D2) |
| DPDP Act 2023, DPDP Rules 2025 | consent, notice, withdrawal, erasure, breach | §3 |
| Mental Healthcare Act 2017 | dignity, confidentiality of mental-health data | PHQ data visible only to CC roles of the clinic; no template content; crisis routing |
| PCPNDT Act 1994 | antenatal pack must never disclose fetal sex | ANC cards are fixed texts; firewall's PCPNDT rule unchanged; Form F stays in radiology |
| ART (Regulation) Act 2021 / ART Rules 2022 | IVF records, consent, storage limits | `ivf_cryo_renewal` reminders only; record-keeping in the IVF module (docs/specialty-os/IVF.md) |
| Meta WhatsApp Business Policy | opt-in, templates, no spam | CHANNELS §7 |
| Drugs and Cosmetics Rules | refills of Schedule H/H1 | CC never dispenses; refills go through Pharmacy OS rules (H1/X excluded) |

## 6. Loophole register (abuse and mistake cases)
| ID | Case | Control |
|:-|:-|:-|
| X1 | Coordinator enrolls patients without a doctor to inflate numbers | enrollment from coordinator → `awaiting_doctor`; outcomes count only signed enrollments |
| X2 | Coordinator closes cases as "verified" by hand | `verified` only from `cc_verify` evidence; manual close reasons are separate and reported |
| X3 | Clinic loosens a threshold to cut workload | bounds in code; change needs a doctor's signature; activation history visible in the audit export |
| X4 | Patient spams numbers | rate limit 20 observations / person / day; extra ignored with one notice |
| X5 | Replayed webhook | unique `source_message_id`; existing duplicate guard |
| X6 | Staff-entered reading disguised as patient's | `source=coordinator_entered` + actor; shown with a different icon; excluded from "patient engagement" metrics |
| X7 | Outcome report gamed by enrolling only easy patients | report shows enrollment funnel (candidates → invited → active) and baseline severity mix |
| X8 | Template variable injection (patient name containing text) | names sanitised to letters/spaces/dots, ≤ 40 chars before template send |
