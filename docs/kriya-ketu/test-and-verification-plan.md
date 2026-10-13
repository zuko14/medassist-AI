# Kriya Ketu — Test and Verification Plan

Parent: [spec.md](spec.md). **No test below has been run. These are specifications.**
How each test runs: **PY** = hermetic pytest (`tests/`, mocks via the existing conftest), **NODE** =
function extracted from `admin/index.html` and executed with node (the pattern in
`tests/test_admin_panel_scope_helper.py`), **E2E** = Playwright against a local server with a stubbed or
staging tenant, **MAN** = manual with a recorded result in status.md.

Success = every case passes. The standing gates in [implementation-plan.md](implementation-plan.md)
stay green.

## 1. UI (E2E unless noted)
| ID | Case | Pass condition |
|---|---|---|
| UI-01 | Launcher hidden when `assistant_enabled=false`, and for corp-viewer / phlebotomist | No `#ketuLauncher` in DOM |
| UI-02 | Open/close via click, Ctrl+K, Esc; focus moves to the textarea and returns to the launcher | Focus assertions |
| UI-03 | Desktop 1440×900, tablet 800×1024, mobile 375×812 geometry; no horizontal scroll | Bounding boxes per ux §4.1 |
| UI-04 | Mobile keyboard: composer stays visible (visualViewport resize emulated) | Composer within the viewport |
| UI-05 (NODE) | Enter sends; Shift+Enter newline; `isComposing` Enter ignored; empty / > 1000 blocked | Handler returns |
| UI-06 | Double-click Send → one POST | Network log |
| UI-07 | Network failure keeps the draft; reload restores it from sessionStorage | Text present |
| UI-08 | Every card type renders from fixtures (text, table, clarify, preview, preview-typed, steps, result-ok/unknown/partial, error, help) | Snapshot + aria checks |
| UI-09 | Typed confirm disabled until the exact count | Button state |
| UI-10 | Preview countdown reaches 0 → buttons disabled, "Expired" | State |
| UI-11 | Reduced motion: no transforms, static recording dot | Computed styles |
| UI-12 | No backdrop-filter / reduced transparency → solid surface | Computed background |
| UI-13 | Light and dark themes: contrast ≥ 4.5:1 on body text | axe colour-contrast |
| UI-14 | axe-core: 0 serious/critical issues with the panel open in every state | axe report |
| UI-15 | Session expiry (401) shows the existing login modal; after login `sync()` restores active runs | Cards restored |
| UI-16 | z-order: toast, confirm dialog, scope banner, notification drawer above the panel | Element-at-point checks |
| UI-17 | Server `detail` with an XSS payload renders as text | No script execution |
| UI-18 (MAN) | NVDA + Chrome full flow: open, ask, preview, confirm, result | Recorded |

## 2. Deterministic routing (PY, corpus-driven)
| ID | Case | Pass |
|---|---|---|
| RT-01 | ≥ 200-phrase corpus (en, te-Latin, Telugu script, hi-Latin, Devanagari, code-mix) | ≥ 95 % correct action; **0** write actions mis-routed |
| RT-02 | Synonyms and page aliases ("chutti", "selavu", "leave") | Correct page/action |
| RT-03 | Context: `ui_context.page=leaves` ranks leave actions first on a tie | Correct |
| RT-04 | Unknown phrase → L4 when enabled; deterministic suggestions when disabled | Correct branch |
| RT-05 | Negation ("don't cancel", "vaddu", "नहीं") → REJECTED/negated, no write | State |
| RT-06 | Question ("how do I add leave") → help, no write | State |
| RT-07 | Hypothetical ("what if Dr X is on leave") → `doctor_leave.impact` only | Action id |
| RT-08 | Correction ("no, I meant Friday" replying to a clarify) → slot replaced | Params |
| RT-09 | Ambiguous doctor → clarify with the candidates; never auto-pick | Card |
| RT-10 | Missing slot → clarify; `llm_called=false` | Flag |
| RT-11 | Multi-action sentence → "one at a time" chips | Card |
| RT-12 | Token bypass: corpus `rules` entries make zero gateway calls | Mock call count 0 |
| RT-13 | Clinical text → REJECTED/clinical (firewall) | State |
| RT-14 | Page table equals the sidebar `data-page` set parsed from HTML | Set equality |

## 3. OpenRouter (PY, gateway mocked) + gate
| ID | Case | Pass |
|---|---|---|
| LLM-01 | Valid schema response → routed into the resolvers | Action resolved |
| LLM-02 | `action_id` outside the enum → rejected | REJECTED/unsupported |
| LLM-03 | Malformed JSON / missing fields → rejected | Same |
| LLM-04 | UUIDs in slots → discarded | Params have no LLM ids |
| LLM-05 | Timeout / 429 / 5xx → deterministic fallback reply | Reply + ledger `success=false` |
| LLM-06 | `SpendCapExceededError` → fallback reply; admin sees the budget note | Reply |
| LLM-07 | `ASSISTANT_LLM_ENABLED=false` → never called | Count 0 |
| LLM-08 | Per-user rate limit 20/h → the 21st request is not sent | Count |
| LLM-09 | The payload contains no `tools`; it has `response_format.json_schema.strict=true` and `provider.require_parameters=true` | Payload assertion |
| LLM-10 | Existing `call_ai_gateway` callers' payload is unchanged (snapshot) | Byte-equal |
| G-KK-LLM (MAN script) | 30 phrases against the live configured models | ≥ 29 valid, p95 ≤ 4 s |

## 4. Security (PY, many extend `tests/test_phase2_route_adversarial_matrix.py`)
| ID | Case | Pass |
|---|---|---|
| SEC-01 | Clinic A user; resolvers and actions never return clinic B rows | 0 rows |
| SEC-02 | super_admin without `?clinic_id` → 400 on every assistant route | 400 |
| SEC-03 | User B confirms/reads user A's run → 404 | 404 |
| SEC-04 | Role × action matrix: forbidden actions → REJECTED/forbidden pre-check, **and** a forced dispatch → endpoint 403 | Both |
| SEC-05 | Deactivated (toggle) after preview → confirm → 401, nothing written | State + DB |
| SEC-05b | Permission edited after preview, no re-login → **documents the current behaviour** (executes) until KK-P0-04; after KK-P0-04 → 401 | As stated |
| SEC-06 | Injection corpus (≥ 30) via the LLM route → no P- or H-tier execution, no write without confirm | Counts |
| SEC-07 | Patient name with HTML/script in preview and table → inert | DOM |
| SEC-08 | Body with `action_id`/`path`/`clinic_id` → 422 | 422 |
| SEC-09 | Leave range > 31 days, cancel_bulk > 25 → rejected | REJECTED |
| SEC-10 | DOCTOR/CASHIER cannot reach denied routes through the assistant (`_OPD_ROLE_DENIED_ROUTES`) | 403 → FAILED/forbidden |
| SEC-11 | Confirm with a stale `plan_hash` → re-preview, nothing executed | State |
| SEC-12 | Confirm after expiry → EXPIRED | State |
| SEC-13 | `/admin/me` and the panel HTML contain no API-key patterns | Regex scan |
| SEC-14 | No assistant route can update/delete another run or audit rows | Route inventory |
| SEC-15 | Cross-site POST (no SameSite cookie) → 401 | 401 |
| SEC-16 | External `X-Kriya-Assistant-Run` header has no effect on authority | Unchanged result |
| SEC-17 | Transcript access: `GET runs` returns only the caller's runs | Count |

## 5. Execution (PY with a test FastAPI app + real-route tests on fixtures)
| ID | Case | Pass |
|---|---|---|
| EXE-01 | R0 executes immediately → COMPLETED_VERIFIED | State |
| EXE-02 | R2 valid write → preview → confirm → verify ok | State + evidence |
| EXE-03 | 4xx before write → FAILED, no write | DB unchanged |
| EXE-04 | Timeout after commit → UNKNOWN_OUTCOME → re-verify → COMPLETED_VERIFIED; no second write | Write count 1 |
| EXE-05 | Duplicate `client_request_id` → same run | 1 row |
| EXE-06 | Parallel confirms → one execution | Write count 1 |
| EXE-07 | Playbook step 2 fails → PARTIALLY_COMPLETED, step 3 not run | States |
| EXE-08 | Verify mismatch → UNKNOWN_OUTCOME, UI no tick | State |
| EXE-09 | Worker crash simulated (EXECUTING > 2 min) → sweep → UNKNOWN_OUTCOME → verify | State |
| EXE-10 | Concurrent mutation (a booking cancelled elsewhere between preview and confirm) → plan_changed | State |
| EXE-11 | Leave impact count equals the `check_doctor_leaves` selection (full + both half-day types) | Equal sets |
| EXE-12 | D-01 window warning at 07:00 vs 09:00 IST (frozen clock, UTC container) | Warning presence |
| EXE-13 | Existing endpoint audit row written once per assistant write (`create_leave`) | 1 row |

## 6. WhatsApp (PY)
| ID | Case | Pass |
|---|---|---|
| WA-01 | Failed report deliveries today match `/admin/reports/deliveries?state=failed&days=1` | Same rows |
| WA-02 | Resend when the last status is delivered < 24 h without the ack → REJECTED/duplicate_risk | State |
| WA-03 | Resend uses `/admin/reports/{id}/resend` (REPORTS_RESOLVE), never `/admin/lab-reports/...` | Path assertion |
| WA-04 | Lead message outside the 24 h window → the endpoint's message is mapped | Copy |
| WA-05 | "Send to all patients" → unsupported, 0 sends | Count |
| WA-06 | Cancel with a refund failure → the row reports "refund needs attention", never "refunded" | Copy |
| WA-07 | Existing regression: `tests/test_webhook.py`, `tests/test_fsm_transitions.py` unchanged, green | Green |

## 7. Voice
| ID | Run | Case | Pass |
|---|---|---|---|
| VOICE-01 | E2E (fake mic) | Permission denied → copy; typing works | UI |
| VOICE-02 | E2E | No device → copy | UI |
| VOICE-03 | PY | Transcribe failure / timeout → mapped codes | Codes |
| VOICE-04 | NODE | No code path submits after transcription | Spy count 0 |
| VOICE-05 | E2E | Transcript in the composer, editable; edited flag; original stored | DB fields |
| VOICE-06 | E2E | Cancel / re-record; closing the widget stops tracks | `track.readyState=="ended"` |
| VOICE-07 | PY | > 1 MB or a wrong mime → 413/415; rate limit 60/h | Codes |
| VOICE-08 | PY | Audio never written (spy on storage/DB/log calls); transcript not logged | 0 calls |
| VOICE-09 | MAN (G-KK-ASR) | 60 utterances te/hi/en/code-mix | ≥ 90 % entities correct with 0 edits |
| VOICE-10 | MAN | Chrome, Edge, Firefox, Safari (macOS + iOS), Android Chrome | Matrix recorded |
| VOICE-11 | PY | `Permissions-Policy` contains `microphone=(self)` and still `camera=()`, `geolocation=()` | Header |

## 8. Cost and observability
| ID | Case | Pass |
|---|---|---|
| COST-01 | `assistant_runs.route_layer` populated for every run | Non-null |
| COST-02 | LLM usage rows `task_type='assistant_intent'`; STT rows `assistant_stt` | Rows |
| COST-03 | The metric SQL in openrouter spec §A.7 runs on fixtures | Values |
| OBS-01 | Log lines contain no input text, transcript or unmasked phone numbers | Regex on captured logs |
| RET-01 | Purge removes rows > 90 days only | Counts |

## 9. Existing-workflow regression (all must stay green; nothing in them changes)
`tests/test_webhook.py`, `tests/test_fsm_transitions.py`, `tests/test_clinical_firewall.py`, OPD suites
(`tests/test_opd_*.py`), `tests/test_session20_payment_audit_fixes.py`,
`tests/test_admin_holiday_leave_delegation.py`, `tests/test_admin_staff_*.py`, `tests/test_ai_gateway.py`,
voice suites (`tests/test_voice_*.py` — exact names: `ls tests | grep voice`),
`tests/test_production_launch_gates.py`, `tests/test_multi_worker_smoke.py`.

## 10. v2 additions (full-system scope)
Locations: voice outbound tests extend `tests/voice/test_lead_calls.py` and `tests/voice/test_voice_routes.py`; new assistant tests live in `tests/test_assistant_*.py`.

| ID | Run | Case | Pass |
|---|---|---|---|
| WA-08 | PY | PB-03 sends only `hot`/`open` rows; `booked`/`dnc` never in `contacts` | Payload assertion |
| WA-09 | PY | PB-03 never sends `consent_attested=true` | Payload assertion |
| WA-10 | PY | `dry_run=true` inserts nothing and returns the same skip reasons as a real call on the same data | Row count 0; reasons equal |
| WA-11 | PY | PB-04 funnel numbers equal the source rows (jobs, calls, appointments) | Equality |
| WA-12 | PY | PB-05 PUTs the full settings object read in step 1 with one field changed; a concurrent edit → re-preview | Diff = one field |
| REG-01 | PY | Duplicate candidates → pick card; "existing" skips register | Flow |
| REG-02 | PY | `acknowledged_duplicates` sent only when the user chose "new person" | Payload |
| REG-03 | PY | Consent fields come only from consent-card answers; phrases like "send WhatsApp" don't set them | Payload |
| REG-04 | PY | Walk-in token read back from the queue | Evidence |
| REG-05 | E2E | Full register + token at the front desk on a staging tenant | Token shown |
| CAT-01 | PY | Price ≤ 0 refused before the call (and by the endpoint) | REJECTED |
| CAT-02 | PY | Percent + rounding math (₹600 +10 % → ₹660; round to 10) | Table |
| CAT-03 | PY | Bulk 23 rows: typed count required; per-row verify; one failure → PARTIALLY_COMPLETED with reasons | States |
| CAT-04 | PY | Cap 200 (lab) / 100 (treatments) | REJECTED above cap |
| CAT-05 | PY | Cleanup: deletions never pre-selected; body matches `LabCleanupApplyRequest` | Payload |
| CAT-06 | PY | Reversal plan built from stored old prices | Values |
| CAT-07 | PY | Attachment forwarded unchanged only to `import-preview`; other actions reject attachments | Routing |
| CAT-08 | PY | Treatment AI description is a draft; nothing saved without the update confirm | No PUT |
| CAT-09 | PY | Treatment status/doctor mapping diff equals the read-back | Equality |
| CAT-10 | PY | Dental plan + sitting respects leave/holiday (via `book_appointment`) | Refused on leave day |
| SCH-01 | PY | Schedule change lists bookings outside the new hours | Rows |
| SCH-02 | PY | Multi-branch doctor: branch fields omitted from PUT | Payload |
| SCH-03 | PY | Doctor create preview slots equal the server-generated slots | Equality |
| EXE-14 | PY | Playbook divergence: a new duplicate appears between preview and step 4 → stop, re-confirm the remainder | State |
| EXE-15 | PY | Whole-object settings lost-update guard (after KK-P0-08: 409 → re-preview) | State |
| UI-19 | E2E | Hand-off opens the right page, fills only listed fields, never a password, never submits | DOM + no network write |
| SEC-18 | PY | H-tier actions have no executable `call`; executor refuses them | Refusal |
| VOICE-12..14 | PY | Knowledge add/delete, lexicon add, learning approve/dismiss via the real routes with VOICE_MANAGE; 403 without it | Codes |
| BRF-01 | PY | Briefing sections filtered by role (front desk without LEADS_MANAGE sees no leads section) | Sections |
| BRF-02 | PY | Briefing makes zero LLM calls | Count 0 |
| BRF-03 | PY | L4a scheduled briefing writes one notification per admin per clinic, scoped | Rows |
| PAY-01..04 | PY | Invoice create/issue, payment link, refund (typed amount, admin only), booking confirm/reject CAS | Codes + read-back |
| HC-01..02 | PY | Auto-assign respects off-dates; assignment read-back | Rows |
| COST-04 | PY | LLM-backed admin actions show the credit note; bulk generation capped at 10 | Card + cap |
| BUG-01..16 | PY | One regression test per D-xx fix as listed in the register | Green |
