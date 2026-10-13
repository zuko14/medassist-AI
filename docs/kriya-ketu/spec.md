# Kriya Ketu — Canonical Product Specification (v2)

> **Read this first.** Stage: **Phase 0 — architecture & specification.** Implementation: **NOT STARTED.**
> Audit baseline: `main` @ `d002b3b`, clean tree (2026-10-13). v2 review: 2026-10-14.
> Repository rules that override anything here: `AGENTS.md` (tenancy, `sb()`, HTTP/1.1, clinical firewall, IST).

| Subject | File |
|---|---|
| Status, tasks, decisions | [status.md](status.md) |
| Architecture + diagram index | [architecture.md](architecture.md) |
| Per-role journeys (front desk, lab, specialty, cashier, doctor, admin) | [staff-journeys.md](staff-journeys.md) |
| Action catalogue (every module) | [action-registry-spec.md](action-registry-spec.md) |
| Multi-step automations | [playbooks-spec.md](playbooks-spec.md) |
| OpenRouter fallback + speech | [openrouter-and-speech-spec.md](openrouter-and-speech-spec.md) |
| UX, visual design, Ketu mark | [ux-design-spec.md](ux-design-spec.md) |
| Threat model | [security-and-threat-model.md](security-and-threat-model.md) |
| Existing bugs fixed alongside | [bug-remediation-register.md](bug-remediation-register.md) |
| Files to change / not change | [file-impact-matrix.md](file-impact-matrix.md) |
| Tests | [test-and-verification-plan.md](test-and-verification-plan.md) |
| Regression + rollback | [regression-and-rollback-plan.md](regression-and-rollback-plan.md) |
| Build plan | [implementation-plan.md](implementation-plan.md) |
| ADRs 001–006 · diagrams | [decisions/](decisions/) · [diagrams/](diagrams/) |

---

## 1. Executive summary

### 1.1 What it is
Kriya Ketu is an **internal action assistant** in the clinic admin panel. A staff member types, or speaks
and then edits, one instruction: *"call today's hot dermatology leads"*, *"register Lakshmi for
Dr Anitha"*, *"raise biochemistry prices 10 %"*, *"Dr Srinivas is on leave tomorrow"*. Kriya Ketu then
**does the job**. It resolves the request to registered actions or a playbook, checks that person's real
permissions, shows one clear preview, takes one confirmation (scaled to risk), executes through
Kriya's **existing endpoints**, **reads every result back to prove it**, and reports done / skipped /
failed.

**Understand → Validate → Authorize → Plan → Preview → Execute → Verify → Audit → Report.**

### 1.2 Cost principle (owner direction, binding)
The LLM is a **fallback only**. Navigation, commands, entity and date resolution, form completion and
**all multi-step workflows** run on rules and fixed playbooks at **zero tokens**. OpenRouter is called
only when a sentence can't be matched: one structured call that can only *name* a permitted action
(ADR-002). Target: **≥ 85 % of requests at zero LLM tokens**.

### 1.3 Autonomy (ADR-006)
| Level | What the user does | Example | v1 |
|---|---|---|---|
| L1 Ask | Asks | "Show hot leads" | ✔ |
| L2 Do | One instruction → one action → confirm if needed | "Cancel Sita's 4 pm" | ✔ |
| L3 Run a job | One instruction → a whole playbook → **one** confirmation → every step verified | "Call today's hot leads", "register + token", "bulk price change" | ✔ |
| L4a Standing (built-in) | Switches existing automations on/off; scheduled read-only briefings | "Call new leads automatically every day" (`auto_leads`), "brief me at 9" | ✔ |
| L4b Standing (custom writes) | Scheduled custom write playbooks | "Every Monday raise…" | v2 (needs ADR-006b) |

### 1.4 Why it beats what clinics use today
Based on public product pages (not hands-on testing): Indian EMRs such as Practo Ray and HealthPlix
market AI for **doctor documentation and prescriptions**, and voice-bot vendors (SquadStack, Edesy,
VaaniYantra and others) sell **patient-facing** call and WhatsApp bots as separate products. Kriya Ketu's
position:

| Kriya Ketu | Typical alternative |
|---|---|
| **One sentence runs the whole job** across OPD, WhatsApp leads, voice calls, catalogue and pricing | Separate screens per module, or a separate bot product per channel |
| **Executes and proves it** (read-back verification, verified tick only when true) | Chatbots that explain *how* to do it, or act without proof |
| **Closes the loop** between WhatsApp leads and the voice receptionist, with a conversion funnel | Lead lists and call bots in different tools |
| Telugu / Hindi / English / code-mixed, typed or spoken, **zero tokens for routine work** | English-first, LLM on every turn |
| The staff member's own permissions, branch and tenant; consent, opt-out, Meta window and call window are always enforced | Bot-level API keys with broad access |
| Kriya never invents clinical content; doctors sign | AI drafting prescriptions (regulatory risk in India) |

### 1.5 Users
Every panel role (`clinic_admin`, `super_admin` within a selected clinic, staff by `staff_role` + grants).
**No launcher** for CORPORATE_VIEWER and PHLEBOTOMIST. Per-role days: [staff-journeys.md](staff-journeys.md).

### 1.6 Boundaries
Kriya Ketu is not the patient WhatsApp bot or the voice receptionist (ADR-001). It **operates** the
receptionist (queues outbound calls, changes settings, teaches it Q&A) only through the receptionist's
existing admin endpoints and their safeguards.

### 1.7 Scope
- **In (v1):** every clinic-panel module via the catalogue in [action-registry-spec.md](action-registry-spec.md):
  appointments, bookings, refunds (R3), patients and OPD (registration, walk-ins, queue, invoices,
  payment links), doctors and schedules, leaves, holidays, lab catalogue (edit, price, bulk price,
  cleanup, import), treatments (edit, price, publish, doctor mapping), dental plans, lab reports and
  connectors, home collection, WhatsApp leads and messages, the voice receptionist (calls, outbound,
  settings, knowledge, lexicon, learning), notifications, audit, usage, clinic profile.
- **Hand-off (H), pre-filled form, human clicks Save:** staff/permissions/passwords, payment
  credentials and UPI destination, cash receipts, voids and shifts, e-prescription signing and clinical
  notes, deleting doctors/branches/tests/treatments, data import/export, corporate health, bulk calls to
  imported lists (consent attestation), live-call takeover.
- **Prohibited (P):** platform-owner and cross-tenant actions (separate phase KK-P5), AI diagnosis or
  prescribing, sex-determination requests, inferring consent, bypassing opt-out / 24 h window / call window,
  patient broadcast (does not exist, D-07), DPDP erasure on a patient's behalf.

### 1.8 Success measures (pilot exit)
| Metric | Target |
|---|---|
| Zero-LLM-token requests | ≥ 85 % |
| Writes reported success **and** verified | ≥ 99 % (the rest show "couldn't confirm", never a tick) |
| Writes executed without a required confirmation | 0 |
| Security matrix pass rate | 100 % |
| Deterministic read p95 (server) | ≤ 1.5 s |
| LLM-route p95 | ≤ 6 s |
| Median LLM cost per LLM-routed request | ≤ ₹0.10 |
| Front-desk adoption | ≥ 5 Kriya Ketu actions per front-desk user per day by week 2 |
| Lead follow-up | ≥ 80 % of hot leads called or messaged within 24 h at the pilot clinic |

"Zero bugs" cannot be guaranteed by any design. This spec commits to **measurable gates**: tests per
action, a security matrix, staged rollout, kill switches, and verified results instead of claimed ones.

---

## 2. Functional requirements

### 2.1 Access
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-001 | Launcher only when `ASSISTANT_ENABLED`, clinic `features.kriya_ketu` (owner opt-in in `OPT_IN_FEATURES`), and the user is not CORPORATE_VIEWER/PHLEBOTOMIST; exposed as `/admin/me.assistant_enabled` | Otherwise no DOM and 403 `assistant_disabled` |
| KK-FR-002 | Tenant and actor from the session only; `?clinic_id` for super_admin; `extra="forbid"` bodies | Body `clinic_id` → 422; super_admin without scope → 400 |
| KK-FR-003 | `client_request_id` idempotency per user | Duplicate POST → same run |

### 2.2 Input
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-004 | Multiline composer ≤ 1000 chars; Enter sends (not during IME), Shift+Enter newline; Send disabled in flight | Double-click = 1 request |
| KK-FR-005 | Draft kept until the server accepts it | Survives a network failure and a reload |
| KK-FR-006 | Voice: record ≤ 30 s → server transcription → **editable** text in the composer → explicit Send. **Never auto-submits.** | Unit test: no submit path from transcription |
| KK-FR-007 | `transcript_original` stored when the text was edited | Both values stored |
| KK-FR-008 | Each voice failure state has its own message; typing always works | UI matrix |
| KK-FR-009 | Mic only with `ASSISTANT_STT_ENABLED` and `getUserMedia` + `MediaRecorder` | Hidden otherwise |
| KK-FR-010a | **Attachment** (one file, ≤ 10 MB) accepted **only** for catalogue import (PB-09), forwarded unchanged to the existing `import-preview` endpoint, which validates size and magic bytes | Other actions reject attachments |

### 2.3 Understanding
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-010 | L1 navigation from the page table, with sidebar visibility rules | Hidden page → `not_available` |
| KK-FR-011 | L2 rules: triggers + slot extractors (`dates.parse_date`, `lexicon.match_doctors`, catalogue fuzzy match, money/percent parser), negation, hypothetical, question detection; a unique match is required | ≥ 95 % corpus accuracy, 0 wrong writes, ≥ 300 phrases incl. playbooks |
| KK-FR-012 | L3 form completion for missing or ambiguous slots, no LLM | `llm_called=false` |
| KK-FR-013 | L4 OpenRouter fallback only on a miss or conflict; one `json_schema` call; the enum = the user's permitted actions and playbooks; output re-resolved | Out-of-enum or ids → rejected |
| KK-FR-014 | LLM disabled, down or over budget → deterministic suggestions | Rules still work |
| KK-FR-015 | "How do I…" → help card + link | No write |
| KK-FR-016 | Negated → rejected; "what if" → impact read only | No write |
| KK-FR-017 | Clinical text → `clinical_firewall.screen_message` → rejected; sex-determination → rejected | SEC-17 |

### 2.4 Authorization and confirmation
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-020 | Policy pre-check (permission **with its real source**, role, feature, branch) gives a readable refusal naming the missing permission | e.g. "You need *Leads* permission" |
| KK-FR-021 | The authoritative check is the real endpoint, dispatched in-process with the user's own session. D-09 limits permission-*edit* revocation until KK-P0-04. | Deactivated user → 401 at execution |
| KK-FR-022 | Tiers R0–R3, H, P (registry §2) | Matrix test per action |
| KK-FR-023 | Confirmation bound to `run_id` + `plan_hash` (covers **every** planned write + snapshots); 5 min expiry; recomputed at confirm | Changed data → re-preview |
| KK-FR-024 | Server CAS on run state | Parallel confirms → one execution |
| KK-FR-025 | **H tier**: Kriya navigates to the page and pre-fills the existing form fields; it never submits; passwords are never pre-filled or stored | No network write from the assistant for H actions |

### 2.5 Execution and verification
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-030 | Writes only via existing endpoints (or NEW-BE additive endpoints once approved); no table writes except `assistant_runs` | Static test |
| KK-FR-031 | Every write is verified by read-back; a tick only on `COMPLETED_VERIFIED` | Mismatch → "couldn't confirm" |
| KK-FR-032 | Timeout/5xx → `UNKNOWN_OUTCOME` → re-verify; never auto-retry writes | One write max |
| KK-FR-033 | Playbooks: per-step status; the first failed write stops later writes; **divergence from the preview stops and re-confirms the remainder** | EXE-07, EXE-14 |
| KK-FR-034 | Structured results with "Open in <page>"; raw `detail` never shown | Generic message on 5xx |
| KK-FR-035 | `assistant_runs` + the endpoint's own audit rows | Both present |

### 2.6 Doctor leave (PB-01)
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-040 | The preview lists confirmed, non-walk-in bookings for that doctor and range; half-day = only slots of the blocked session (`doctor_session_slots`), i.e. exactly what `check_doctor_leaves` cancels | Preview count = scheduler selection on fixtures |
| KK-FR-041 | It states the consequence: the 08:00 IST job cancels N bookings, refunds paid ones, sends `appointment_cancelled_doctor_leave` | Text present, N correct |
| KK-FR-042 | **D-01 warning** when the date is today, or tomorrow after 08:00 IST, with an optional cancel-and-notify (`appointment.cancel_bulk`, R3, typed count) | 09:00 fixture → warning; 07:00 → none |
| KK-FR-043 | Exact doctor via `lexicon.match_doctors`; ambiguity → picker, never auto-picked; send that row's exact `name` | "Srinivas" with two matches → picker |
| KK-FR-044 | Existing leave dates in the range are skipped and reported (no unique constraint, D-11) | Double request → no duplicate rows |

### 2.7 WhatsApp leads → voice receptionist (PB-03/04/05)
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-050 | "Show leads / leads details" → `GET /admin/leads` with segment, interest, days, search | Rows match the Leads page |
| KK-FR-051 | "Call these/hot leads" → PB-03: dry run (exact skip reasons once KK-P4-06a ships; labelled "estimate" before that), window, cap, ETA → one confirmation (typed count > 10) → `outbound/bulk` with `consent_attested=false` → read back the queued jobs | `booked`/`dnc` never queued; opted-out never queued; ≤ 100 per run |
| KK-FR-052 | "How did the calls go?" → PB-04 funnel (queued → dialled → connected → booked) | Numbers equal the source rows |
| KK-FR-053 | "Call leads automatically" → PB-05 toggles `outbound.auto_leads` with cap and window via the whole-object settings PUT; snapshot in the plan hash | Concurrent edit → re-preview |
| KK-FR-054 | `lead.message` only within the 24 h window; outside it, suggest a voice call | Endpoint rules unchanged |
| KK-FR-055 | Report-delivery failures + resend (PB-02) via `/admin/reports/{id}/resend` only | Duplicate-risk tick |
| KK-FR-056 | Broadcast to all patients → unsupported | 0 sends |

### 2.8 Front desk and OPD (PB-06, PB-12, PB-13)
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-060 | Register + token: duplicate check → pick/confirm → **explicit consent card** (`data_consent`, `whatsapp_opt_in` from staff answers only) → register → walk-in → queue read-back | Consent never defaulted to true |
| KK-FR-061 | Arrive / check-in / call-next / recall / stage via the OPD endpoints when `opd_enabled`, otherwise the legacy check-in | CAS conflicts → "already handled" |
| KK-FR-062 | Morning briefing and end-of-day cards (R0), each section shown only if the role can read it | Section filtering test |

### 2.9 Catalogue, pricing, treatments (PB-07..PB-10, PB-15)
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-070 | Single price/field edit with an old→new diff | Read-back equals new |
| KK-FR-071 | Bulk price change (absolute/percent, stated rounding, ≤ 0 refused) with typed count; old prices kept so a reversal plan can be offered | ≤ 200 lab / ≤ 100 treatments |
| KK-FR-072 | Lab price edits rely on the existing update validator (price > 0); D-10 was withdrawn | 422 on ≤ 0 |
| KK-FR-073 | Cleanup uses the deterministic suggestions; deletions never pre-ticked | — |
| KK-FR-074 | Import uses the existing preview diff; apply only after confirmation | Counts verified |
| KK-FR-075 | Treatment publish/hide, doctor mapping, AI description as a draft (saved only on confirm) | Draft not persisted |
| KK-FR-076 | Dental plan + sitting booking through existing endpoints (leave and holiday rules apply) | Read-back |

### 2.10 Voice receptionist administration
| ID | Requirement | Acceptance |
|---|---|---|
| KK-FR-080 | "Teach the receptionist <Q/A>" → `voice.knowledge_add` with preview | Entry visible in the Voice page |
| KK-FR-081 | Doctor aliases / pronunciations → `voice.lexicon_add` (also improves Kriya Ketu's doctor matching) | — |
| KK-FR-082 | Review learning suggestions → approve/dismiss | — |
| KK-FR-083 | Call list, live, analytics, usage reads | VOICE_VIEW |

---

## 3. Non-functional requirements
| ID | Area | Requirement | How measured |
|---|---|---|---|
| KK-NFR-PERF-01 | Performance | Deterministic routing ≤ 300 ms p95 server-side, excluding the dispatched endpoint | `assistant_runs.route_ms` |
| KK-NFR-PERF-02 | Performance | Widget adds ≤ 35 KB to `admin/index.html`; DOM built only on first open | Diff size; Lighthouse before/after |
| KK-NFR-PERF-03 | Performance | All DB access via `await sb(...)`; no blocking calls on the loop | Tenant lint + grep test |
| KK-NFR-SCALE-01 | Throughput | Bulk playbooks write sequentially, ≤ 5 writes/s; a 200-row price change in ≤ 2 min with live progress | Playbook timing test |
| KK-NFR-REL-01 | Reliability | Patient WhatsApp and voice latency unchanged (no shared locks or queues) | `/webhook` p95 before/after pilot |
| KK-NFR-REL-02 | Provider outage | OpenRouter or Sarvam down → typed rules and playbooks still work | Fault injection |
| KK-NFR-REL-03 | Patient pacing | Calls and WhatsApp keep their existing pacing, caps and windows; Kriya Ketu never bypasses `dispatch_due` | Code review + PB-03 tests |
| KK-NFR-A11Y-01 | Accessibility | WCAG 2.2 AA: contrast ≥ 4.5:1 (solid fallback), full keyboard path, non-modal labelled dialog, `aria-live` log, focus returns to launcher | axe + NVDA pass |
| KK-NFR-A11Y-02 | Motion | Reduced motion removes transforms and pulsing; reduced transparency → solid | `emulateMedia` |
| KK-NFR-SEC-01 | Tenant | `assistant_runs` in `TENANT_OWNED_TABLES`; every query scoped | AST lint + adversarial matrix |
| KK-NFR-SEC-02 | Secrets | No provider key reaches the browser; CSP `connect-src 'self'` unchanged | Response scan |
| KK-NFR-OBS-01 | Observability | Logs carry run id, layer, action, states, durations; never input text or unmasked phones | Log-format test |
| KK-NFR-MAINT-01 | Maintainability | A new read action = registry entry + phrases + one test, with no executor change | Review checklist |
| KK-NFR-MOB-01 | Mobile | ≤ 640 px full-height sheet; composer above the keyboard; no horizontal scroll | 375×812 test |
| KK-NFR-BROWSER-01 | Browsers | Text: current Chrome/Edge/Safari/Firefox. Voice: Chrome/Edge/Firefox (webm/opus), Safari 14.1+ (mp4) | Device matrix |
| KK-NFR-COST-01 | Cost | ≥ 85 % zero-token; LLM ≤ 20/user/h; admin AI budget applies | `route_layer`, `ai_usage_ledger` |
| KK-NFR-RET-01 | Retention | `assistant_runs` purged after 90 days by a new `DataRetentionService.purge_assistant_runs()`; raw audio never stored | Retention test |
| KK-NFR-REC-01 | Recovery | `EXECUTING` > 2 min → `UNKNOWN_OUTCOME` → re-verify | Fault injection |

## 4. Roles
See [staff-journeys.md](staff-journeys.md). Kriya Ketu adds **no** role or permission. Missing grants are
named to the user. R3 = clinic_admin/super_admin unless the registry states a permission. There is no
maker-checker in v1 (DP-04).

## 5. Lifecycles
Widget, transcription and run state machines as in [architecture.md](architecture.md) §5 and
[diagrams/](diagrams/). Plan-level confirmation adds one guard: **EXECUTING → NEEDS_CONFIRMATION** when a
step's actual input diverges from the previewed one (remaining steps only).

## 6. Existing defects
All found defects (D-01…D-16, K-01…K-03) with fixes and tests are in
[bug-remediation-register.md](bug-remediation-register.md). Those that gate Kriya Ketu features:
D-01 (PB-01 warning, fix KK-P0-01), D-05 (voice), D-09 (revocation), D-13
(settings snapshot), D-16 (auto lead calls accuracy).

## 7. Phase 0 acceptance
[status.md](status.md) planning tasks VERIFIED with evidence; consistency review passed; implementation
tasks NOT_STARTED.
