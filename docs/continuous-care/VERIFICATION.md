# Next lanes (CC · RI · MI · RXR · SP) — Forensic Design Verification, review round 1

> Binding for every implementing agent, like `docs/pharmacy-os/VERIFICATION.md`. Method: each integration point the
> designs touch was traced in code on `main` @ `c8c37ff` (2026-11-24) and attacked with "what breaks for a clinic that
> never turns this on" and "what breaks for one that does". Every defect below was **found in the design, before any
> code**, and fixed in the named doc in the same change. Nothing in production changed.

## 1. Verdict
- **Design level:** after the fixes, no designed change alters behaviour for a clinic that has not opted in, and every
  touch on a shared path is listed, minimal and covered by an unedited-suite gate. That is as far as a document can go.
- **Not proven yet:** "no bugs" is only proven by the gates in each TEST-PLAN passing on real code (CG1–CG8, RG1–RG8),
  the zero-impact matrix (§5) and the latency gate. Treat this file as the checklist those gates must satisfy.

## 2. Verified safe (traced, no change needed)
| Concern | Evidence | Result |
|:-|:-|:-|
| Adding quota kind `care` to `clinic_message_quota_usage` breaks dental boards | dental readers iterate the fixed `QUOTA_KINDS` tuple (`app/services/dental_plans.py:170`, `:373`; `app/routers/platform.py:1483`) | `care` rows are invisible to dental code; CC needs its own usage board |
| CC foreign keys block doctor delete | `delete_doctor` already answers 409 "deactivate instead" on any FK (`app/routers/admin.py:2994`) | acceptable, RESTRICT |
| CC foreign keys block DPDP erasure of a family member | erasure falls back to redaction on FK violation (`app/services/data_retention.py` family-member step, after `:388`) | safe; CC still must purge its own rows (D6) |
| Booking from CC expires as unpaid | `book_appointment` writes `confirmed` unless a payment flow sets otherwise; `expire_stale_bookings` acts on pending payments | safe when CC books pay-at-counter (D5) |
| Generic 24 h reminder skips CC-booked visits | reminder job selects `status='confirmed'` consultations (`app/services/scheduler.py` ~`:897-902`) | CC visits get the normal reminder |

## 3. Defects found in the designs (all fixed)
| ID | Sev | Defect | Evidence | Fix (where) |
|:-|:-|:-|:-|:-|
| D1 | High | CC taps from an **in-window** card arrive as `message_type="interactive"`; a hook inside the existing `if message_type == "button"` block would never see them | `app/services/conversation.py:683` | one CC block outside that branch for both types (ARCHITECTURE §7, API §6) |
| D2 | High | A new `cc_reading` FSM state would be reset by the unknown-state branch **with a main-menu reply**; a scheduler writing state races the per-phone lock; `update_conversation` has no compare-and-set and swallows errors | `conversation.py:1509`; `app/database.py:528` | no new state, no job-side writes; claim only inside the locked handler under four conditions (ADR-CC-09 revised, CHANNELS §4, WORKFLOWS W4, TEST-PLAN) |
| D3 | High | A `care_checkin` call would greet the patient with the **lead follow-up** script ("you enquired about…"): the script is chosen by the packet (`reminder` or lead); care calls would also eat the shared lead/callback `daily_cap` | `app/voice/gateway.py:357`; `app/voice/session.py:177-192`; `app/voice/dialog.py:104`; `app/voice/outbound.py:271` | `care` packet branch mirroring `reminder`, new outcomes, `care_daily_cap` (ADR-CC-16, ARCHITECTURE §7, status CC-37) |
| D4 | High | **Meta messaging limits and quality rating are not read anywhere**; care templates could exhaust the clinic number's daily business-initiated limit or drop its quality, blocking appointment reminders, lab reports and payment links for the whole clinic | no `messaging_limit`/`quality_rating` handling in `app/services/whatsapp.py` | ADR-CC-17 messaging budget (tier share cap, halve at MEDIUM, stop at LOW, lowest-priority lane); CC-60 |
| D5 | Med | W8 said the patient gets "the existing confirmation message" — `book_appointment` sends nothing | `app/database.py:1223` | CC sends `cc_visit_booked_v1` itself (WORKFLOWS W8, CHANNELS §2) |
| D6 | Med | DPDP erasure did not know CC rows; the needed edit to `data_retention.py` was missing from the list of touched files | `app/services/data_retention.py:239` | guarded `cc_purge_patient` step (ARCHITECTURE §7, CC-61, `test_cc_erasure.py`) |
| D7 | Med | The consent **invite template** would be the first message to patients who never opted in on WhatsApp — Meta requires opt-in before business-initiated messages | W2 v1 | opt-in captured in person / by phone first; recorded in `cc_consents` (ADR-CC-11, W2, DATA-MODEL, CC-62) |
| D8 | Med | Delete behaviour of FKs into existing tables unspecified | DATA-MODEL v1 | explicit table (DATA-MODEL "Delete behaviour") |
| D9 | Low | Candidate scan over all signed encounters' JSON diagnoses twice daily scales badly for large hospitals | ARCHITECTURE §6 v1 | incremental `signed_at` watermark + weekly rebuild |
| D10 | Low | Low reading captured by voice only got a WhatsApp card | CHANNELS §5 v1 | `hypo_now` spoken in the call |
| D11 | Med | Care card showed medicine names by default — on shared family phones a medicine name discloses a condition | CHANNELS §2 v1 | default off (DPDP minimisation) |
| D12 | High | Revenue Guard `opd_seen_not_billed` would flag **every WhatsApp/voice prepaid consultation** (prepayments are applied only when staff issue the invoice) and every clinic that bills outside Kriya | `app/services/opd_billing.py:450` (`_apply_prepaid`) | `prepaid_not_invoiced` review rule + billing-adoption gate (revenue-integrity SPEC §3) |
| D13 | High | Standalone pharmacies could not register customers (no RX patient route; OPD route needs OPD), so refill plans (which need `patient_id`) could not work | `app/routers/opd.py:729` | `/admin/pharmacy/customers` over the same `register_patient` service (RETAIL-PLAN RXR-ADR-10) |
| D14 | High | Creating a `pharmacy`-plan clinic before its WhatsApp menu / voice scope exist would offer **doctor bookings** to shop customers | `_send_main_menu` (`conversation.py:1790`) has no pharmacy branch | one-release rule (RETAIL-PLAN RXR-ADR-11) |
| D15 | Med | Specialty packages planned as zero-value invoice lines — unverified against invoice issue rules and would add empty HSN rows to GSTR-1 | GAP-ANALYSIS S3 v1 | package consumption ledger; only the sale is invoiced (specialty GAP-ANALYSIS S3, DERMA §4) |
| D16 | High | `book_appointment` checks only slot conflicts, **not holidays or doctor leave** — any new booking path that skips `get_available_slots` would book a doctor on leave (CLAUDE.md rule: holiday + leave check runs before slot availability) | `app/database.py:1223-1300`; `get_available_slots` `:1028` | CC actions and data-import future appointments validate through `get_available_slots` first (WORKFLOWS W8, data-import SPEC §3) |

## 4. Loophole register (adds to SECURITY.md X1–X8)
| ID | Case | Control |
|:-|:-|:-|
| X9 | Clinic loosens protocol thresholds to make "at target" look better | outcome report prints the activation version and parameters used per period; parameter changes visible to the owner |
| X10 | Coordinator dismisses radar rows to hide workload | dismiss needs a reason; dismissed counts per coordinator on the outcomes page |
| X11 | Care cards become marketing ("offer: full body check-up") | card texts are fixed keys; no free text reaches a template; owner can disable CC per clinic |
| X12 | Fake / imported patients enrolled to inflate outcomes | outcomes count only doctor-signed enrollments with live consent and verified events |
| X13 | Shop customer requests an H1/X drug on WhatsApp without a prescription | order request is a reservation; sale still needs the POS prescription checks (RX); H1/X never shown as "available to order" |
| X14 | Revenue Guard dismissals hide real leakage | dismissed ₹ shown to the clinic owner login and in the owner console value view |
| X15 | Staff marks a no-show as checked in to make CC "verified" | check-in carries the actor; outcome report shows verified-by-check-in vs by-signed-encounter split; signed encounter is the stronger evidence |
| X16 | Patient answers for someone else on a shared phone | cards name the person; "whose reading" when two prompts are open; voice identity check |
| X17 | Owner turns on CC before templates are approved | Care Setup checklist blocks go-live; dispatch refuses without approved templates |
| X18 | A CC bug throws inside the conversation hook and swallows the patient's message | hook returns `False` on any exception and the message continues normally (API §6) |

## 5. Zero-impact matrix (each row must be a passing test before release)
| Existing workflow | With all new lanes deployed, module OFF | With module ON |
|:-|:-|:-|
| WhatsApp booking, payments, reports, menus (every plan) | identical; zero `cc_*`/`ri_*`/`mi_*` queries (spy) | identical except `cc:` taps and claimed numeric readings |
| Dental sitting reminders, reviews, quotas, owner dental board | identical | identical; `care` kind separate |
| RX refill buttons, refill job, POS, registers | identical | identical; CC reads refill plans only |
| Health check-ins day 3/7, generic follow-ups | identical | identical (enrolled patients may get both; suppressing the generic check-in for them is deferred item CC-D8 — not built, so no change) |
| Voice inbound, reminders, lead calls | identical (voice suites unedited) | care calls on their own cap; lead/reminder branches byte-identical |
| OPD billing, GST, GSTR-1, credit notes | identical | identical (RI is read-only; packages invoice normally) |
| IPD, OT, LIS, DX, insurance | identical | identical (read-only consumers) |
| DPDP erasure | identical for clinics without CC rows | CC rows purged; erasure never fails |
| Doctor / branch delete, clinic purge | identical | CC/RI/MI purges run first; FK fallbacks unchanged |
| Owner console create/toggles/plan tiers | identical (new options appended) | new toggles; drift test guards plan lists |
| Webhook latency | p95 ≤ +5 % | p95 ≤ +5 % with 1,000 enrolled patients |

## 6. Concurrency
| Race | Guard |
|:-|:-|
| Patient double-taps "Taken" | unique `(clinic_id, source_message_id)`; existing duplicate guard |
| Card dispatch on two workers | `distributed_job_lock("cc_card_dispatch")` + unique `(clinic_id, plan_item_id, due_at)` schedule items + prompt row created before send |
| Patient replies while the evaluate job runs | evaluation insert keyed by inputs hash; tier change by CAS on the enrollment's `last_evaluation_id` |
| Two coordinators act on one case | CAS on `updated_at` → 409 for the second |
| Doctor taps "book" twice | idempotency key on the action; slot index rejects the second booking |
| RI scan overlapping a bill being issued | finding re-checked next night; auto-resolves as `billed` |

## 7. Residual risks (accepted, owner-visible)
1. Meta can re-categorise a utility template as marketing or pause it — monitored; CC stops for that clinic, nothing
   else does (ADR-CC-17).
2. Clinical thresholds are the medical director's responsibility (signed activation); Kriya owns code correctness.
3. Outcomes are associations, not causal proof (OUTCOMES.md).
4. Voice speech recognition errors — mitigated by read-back confirmation (no DTMF: ADR-CC-16 revision).
5. Migrations 125–142 are still not applied on Supabase; every new lane depends on that order.
6. Line numbers in this file drift with `main`; re-locate by symbol (status §7 of each lane).

## 8. Review round 2 — UI, icons, colours, interaction (2026-11-24)
Method: live tokens, components, icon mechanisms and theme handling read from `admin/index.html` / `admin/platform.html`;
WCAG 2.x contrast computed for every token pair the new pages use (both themes). Result: the binding
[docs/design/NEXT-LANES-UI-SPEC.md](../design/NEXT-LANES-UI-SPEC.md) and the fixes below.

| ID | Finding | Evidence | Fix |
|:-|:-|:-|:-|
| U1 | The Oct design-system doc has drifted from the live panel (title 1.5rem/700 not 1.6rem/600; stat number 2.2rem/800; three icon mechanisms, not one sprite) | `admin/index.html:807`, `:852`, `:10337`, `:10358` | UI spec §1.2 — live wins; design doc table updated with the first new page |
| U2 | CC UI v1 asked for "sparklines" — the panel has no sparkline component; Chart.js is the vendored chart library | `admin/index.html:15` | CC UI §4 + UI spec §5 (Chart.js, target band without plugins) |
| U3 | New charts would not repaint on the theme toggle (only Insights and Corporate charts are repainted) | `admin/index.html:10321-10333` | one guarded repaint line per lane in `toggleTheme()`, listed in ARCHITECTURE §7 / RI SPEC §8 |
| U4 | No icons were specified; three icon mechanisms exist; two natural choices collided with existing glyphs (target = Leads, line chart = DX Dashboard) | nav markup `:4131-4186` | UI spec §3: mechanism per place, Lucide names, collision check |
| U5 | Amber has no border token | `:root` tokens `:19-107` | `color-mix(in srgb, var(--amber) 35%, transparent)` like `.dx-chip.off` |
| U6 | Light-theme amber chip text 4.36:1 and `--text3` 3.99:1 fail AA for small text | computed (UI spec §6.2) | new UI uses `--text2` for information and `--text-strong` text on amber chips in light theme |
| U7 | No skeleton/loading component exists | class inventory | keep previous content dimmed + `aria-busy`; muted "Loading…" on first load; no new pattern |
| U8 | Wide tables would scroll sideways at 390 px unless they use the container-query card pattern | `.dx-table` `@container (max-width: 1120px)` `:1891` | every new table is `table.dx-table` with `data-label` on each cell |
| U9 | Owner console is dark-only with a 16 px radius — clinic-panel rules would look wrong there | `admin/platform.html` `:root` (no light block) | UI spec §7.10, OWNER-CONSOLE §4a |
| U10 | WhatsApp reply-button titles are limited to 20 characters; translations can overflow and Meta rejects the message | CHANNELS §2 | per-language length tests in fixtures |
| U11 | Group badge for urgent cases: the `.nav-group-count` badge already exists | `admin/index.html:749` | CC UI §1 reuses it |
| U12 | Pre-existing, panel-wide: white text on bright fills (2.17–3.76:1), `--text3` in both themes, light amber; plus an invisible light-theme title, unreadable light OPD pills and dark-on-amber tags | `:644`, `:48`, `:128`, `:143`, `:4473` | **Approved** — planned in [A11Y-CONTRAST-PLAN.md](../design/A11Y-CONTRAST-PLAN.md) (A11Y-1…6), to land before the first new screen |
