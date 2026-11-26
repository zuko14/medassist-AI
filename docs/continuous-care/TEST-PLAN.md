# Kriya Continuous Care — Test and Verification Plan

> Harness rules are the RX/DX/IPD ones: `pytest` is hermetic (`tests/conftest.py`), **never** `KRIYA_TEST_LIVE=1`;
> real-Postgres tests use the local Postgres fixtures and `tests/ipd_pg_bridge.py` (extend only for query shapes CC
> routes use, and say so in the bridge docstring). Never skip, `xfail`, delete or edit an existing test to get green;
> never raise a linter baseline.

## 1. Standing gates — run at the end of EVERY phase; paste summary lines into status.md §9
```bash
# CG1 — tenancy linter (baseline stays 0) + CC table registration
python -m pytest tests/test_lint_unscoped_queries.py tests/test_phase4_scoped_queries.py tests/test_cc_tenancy_registry.py -q
# CG2 — security matrices incl. every /admin/cc route
python -m pytest tests/test_admin_super_admin_scope_matrix.py tests/test_phase2_route_adversarial_matrix.py tests/test_cc_route_matrix.py -q
# CG3 — zero-LLM guards (existing + CC)
python -m pytest tests/test_clinical_firewall.py tests/test_opd_no_llm.py tests/test_ipd_no_llm.py tests/test_dx_no_llm.py tests/test_rx_no_llm.py tests/test_cc_no_llm.py -q
# CG4 — live channels unchanged (files UNEDITED)
python -m pytest tests/test_webhook.py tests/test_fsm_transitions.py tests/voice/ -q
git diff --stat main -- tests/test_webhook.py tests/test_fsm_transitions.py tests/voice/   # expect empty
# CG5 — RX refills + dental reminders + health check-ins unchanged (they share the button hook / quota table)
python -m pytest tests/test_rx_*.py tests/test_dental_*.py tests/test_migration_089_dental.py \
  tests/test_scheduler_health_checkin.py tests/test_health_checkin_response.py tests/test_followup_engine.py -q
# CG6 — migrations incl. CC up/down chain on real PG
python -m pytest scripts/test_migration_runner.py "tests/test_migration_1*.py" tests/test_migration_cc_*.py -q
# CG7 — full suite, no -x; count ≥ baseline + new; 0 failed
python -m pytest -q -rs
# CG8 (P4+) — concurrency on real PG (double taps, two coordinators, job overlap)
python -m pytest tests/test_cc_concurrency.py -q
```
Before P1, confirm the exact names of the dental / voice test files with `ls tests` and pin them in CG5 (status §3).

## 2. New test files by phase
| Phase | File | Must cover |
|:-|:-|:-|
| P0b | `tests/test_cc_latent_fixes.py` | CC-L1: each self-harm phrase (en/hi/te, variants, inside longer text) → crisis card from main menu, mid-booking and symptom states; ordinary words ("diet", "die-cast") do not trigger; CC-L2: legacy reminders skip opted-out patients |
| P1 | `tests/test_migration_cc_m1.py` (real PG) | 145 on 144; idempotent; `cc_state` default/CHECK; voice purpose and quota kind widened (old values still valid); rollback refuses while new values are used; re-apply; **no trigger on pre-existing tables** (catalog query) |
| P1 | `tests/test_cc_feature_gate.py` | off by default; needs `opd_enabled` (400); enterprise wildcard alone does not enable; partner account 400; `/admin/me.cc_access`; 409 preview → WIND_DOWN → OFF; OPD disable refused while CC on; every `/admin/cc` route 403 before any `cc_*` query (spy on `sb`); route-count pin |
| P1 | `tests/test_cc_no_llm.py` | AST import scan of `app/services/cc*`, `app/services/cc_protocols/*`, `app/routers/cc.py`; AI/voice NLU modules never name `cc_` tables |
| P1 | `tests/test_cc_tenancy_registry.py` | `cc_*` names in `TENANT_OWNED_TABLES` == tables created by CC migrations (parsed) |
| P1 | `tests/test_cc_consent.py` | notice hash/version stored; no `active` enrollment without live consent (DB CHECK + API 422); withdrawal stops prompts; guardian consent for family member |
| P2 | `tests/test_cc_protocols.py` + `tests/cc_vectors/*.json` | every rule fires / does not fire / boundary; params outside bounds 422; same inputs hash → same output (property test with Hypothesis if installed, else 200 seeded cases) |
| P2 | `tests/test_cc_activation.py` | sign requires doctor with registration number; super-admin cannot sign; new version does not auto-apply |
| P3 | `tests/test_migration_cc_m2.py` | enrollment uniqueness; one active plan; composite FKs reject cross-tenant patient/doctor |
| P3 | `tests/test_cc_care_plan.py` (goldens) | OD/BD/TDS/QID/HS; before/after food text; SOS, STAT, custom ("1-0-1", "weekly"), taper, insulin text → display-only; H1/X/narcotic → no adherence prompt; new signed version supersedes atomically; unsigned draft ignored |
| P4 | `tests/test_migration_cc_m3.py` | observations append-only (UPDATE/DELETE refused); unique `source_message_id` |
| P4 | `tests/test_cc_conversation_hook.py` | `cc:` ids consumed as **both** `button` (template) and `interactive` (in-window) taps; other payloads (`dentrev:`, `rx_refill:`, `checkin_*`) untouched; "142" / "140/90" stored only with state idle/main_menu **and** an open prompt; "142" with no open prompt untouched; "12" in a `selecting_*` / family state untouched; "1420" re-asked then `unparsed`; "sugar 142 dizzy" untouched; two family prompts → "whose reading"; Devanagari/Telugu digits; non-CC clinic: zero `cc_*` queries (spy) |
| P4 | `tests/test_cc_channels.py` | window open → interactive; closed → template; unapproved template → release + failure code; quota −1 → skip; Meta error → release; opted-out → skip; no WhatsApp opt-in → never a template; card window hours; ADR-CC-17 tier share cap, quality MEDIUM halves, LOW stops, safety reply never capped |
| P4 | `tests/test_cc_erasure.py` | DPDP erasure of an enrolled patient and of an enrolled family member: prompts stop, CC rows redacted, erasure succeeds (no FK error), non-CC clinic erasure makes no `cc_*` call |
| P4 | `tests/test_cc_safety_cards.py` | hypo, very high, BP crisis, PHQ item 9, ANC danger, OHSS, post-op eye: card + urgent case in the same call, even with quota exhausted and template unapproved |
| P5 | `tests/test_cc_voice_checkin.py` | enqueue eligibility (opt-out never called); the `care` packet gets the care greeting, never the lead greeting; reminder and lead packets produce byte-identical greetings to `main`; `care_daily_cap` independent of `daily_cap`; script asks only due items; spoken yes/no and read-back-confirmed numbers stored (`voice_speech_confirmed`); unconfirmed speech not stored; identity "no" ends the call; low reading → `hypo_now` spoken; safety phrase path |
| P6 | `tests/test_cc_radar.py` (real PG fixture) | each reason opens and closes on the documented events; auto-completed no-show still `visit_missed`; candidate from ICD E11 encounter; dismissed candidate not re-raised for 90 d |
| P7 | `tests/test_cc_cases.py`, `tests/test_cc_actions.py` | one open case per (enrollment, reason); SLA escalation; slots only from `get_available_slots` (a slot on a doctor's leave day or a holiday is never offered or booked); booked row is `confirmed` pay-at-counter and survives `expire_stale_bookings`; `cc_visit_booked_v1` sent once; **no action accepts drug/dose keys**; coordinator cannot take clinical actions |
| P8 | `tests/test_cc_verify.py`, `tests/test_cc_outcomes.py` | evidence kinds; `status='completed'` alone not verified; revenue from paid lines only; outcome fixture worked by hand (OUTCOMES.md §4) |
| P9 | `tests/test_cc_pack_<pack>.py` per specialty pack | pack vectors + plan-specific gates (ivf trigger time verbatim + acknowledgement; methotrexate never daily) |
| P10 | `tests/test_cc_release_readiness.py`, `tests/test_cc_zero_impact.py` | rollback chain 149→145; purge; a non-CC clinic's webhook, booking, voice, OPD and RX paths make zero `cc_*` queries (spy) |
| P4+ | `tests/test_cc_concurrency.py` | two taps of the same button → one observation; two coordinators acknowledge → one wins (CAS 409); evaluate job overlap → one evaluation |

## 3. Zero-impact matrix (grows each phase)
| Clinic configuration | Expectation |
|:-|:-|
| any plan, CC OFF | no `cc_*` query on webhook, booking, voice, OPD, IPD, RX, DX paths; menu identical; jobs do nothing |
| CC ON, patient not enrolled | conversation identical to CC OFF except `cc:` payloads (never sent to them) |
| CC ON, enrolled patient mid-booking | booking identical; card does not change the state |
| Dental clinic with CC | dental quotas unchanged; `care` kind separate |
| RX clinic with CC | `rx_refill:` buttons unchanged; refill plans unchanged |

## 4. UI gates per UI phase
UI-G1…G12 from [docs/design/NEXT-LANES-UI-SPEC.md](../design/NEXT-LANES-UI-SPEC.md) §8: tokens only, screenshots at
1440 / 1024 / 768 / 390 px in dark and light, no horizontal scroll, keyboard and screen-reader passes, loading / empty /
error / wind-down states, contrast, icons, theme repaint of charts, long Indic names, hidden when the module is off.

## 5. Latency gate (P10)
Seeded perf fixture: `/webhook` p95, a text message through `_handle_message_locked` p95, `/admin/opd/consultations/*`
p95 — before vs after CC migrations and code with CC OFF and with CC ON (1,000 enrolled patients): ≤ 5 % regression.

## 6. Pilot measurement (not a code gate — reported in ROLLOUT §6)
Answered-card rate, reading submission rate, unparsed rate, cases per coordinator-hour, SLA breaches, verified actions,
outcome deltas.
