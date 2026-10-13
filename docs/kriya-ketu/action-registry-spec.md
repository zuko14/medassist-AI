# Kriya Ketu — Action Registry Specification (v2: full-system coverage)

Parent: [spec.md](spec.md) · Playbooks: [playbooks-spec.md](playbooks-spec.md) · Roles:
[staff-journeys.md](staff-journeys.md) · Decision: [ADR-004](decisions/ADR-004-action-execution-and-verification.md)

Every action maps to an endpoint **that exists at `d002b3b`** (verified by reading the router), unless it
is marked **NEW-BE** (small additive backend change, needs approval) or **PLANNED**. Nothing here invents
behaviour the backend does not have.

v2 changes from v1 (2026-10-14 review): coverage extended to every clinic-panel module (leads → voice
calls, catalogue editing and pricing, treatments, dental, OPD registration and billing, home collection,
voice settings and knowledge); a **Hand-off (H)** tier for sensitive forms; corrected permission sources
(several are checked *inside* handlers); verified parameter names.

---

## 1. ActionSpec (proposed `app/assistant/registry.py`)

```python title="app/assistant/registry.py (proposed)"
@dataclass(frozen=True)
class ActionSpec:
    id: str                       # "lab_test.update"
    module: str
    summary: str                  # preview title
    risk: str                     # "R0" | "R1" | "R2" | "R3" | "H"
    permission: Optional[str]     # PERMISSIONS value, or None
    perm_source: str              # "dependency" | "handler" | "role" | "none"  (see §1.1)
    roles: frozenset = ALL_ROLES  # R3 → {"clinic_admin", "super_admin"} unless stated
    feature: Optional[Callable]   # SAME gate helper the route uses (opd_enabled, ai_receptionist_enabled, has_feature…)
    params: type[BaseModel]       # resolved params, extra="forbid"
    triggers: tuple[str, ...]     # en / te-Latin / Telugu / hi-Latin / Devanagari
    slots: tuple[SlotSpec, ...]
    preview: Optional[Callable]   # reads only → rows, warnings, hash snapshot
    call: Callable                # → (method, path, query, json | multipart) of the EXISTING endpoint
    verify: Optional[Callable]    # read-back → (ok, evidence); required for R1–R3
    handoff: Optional[Handoff]    # H tier: page id + {form element id: value}; never submits
    page: Optional[str]
    max_items: int = 1
```

### 1.1 Permission sources (correction found in the review)
| `perm_source` | Where the route checks | Examples | How the drift test checks |
|---|---|---|---|
| `dependency` | `Depends(require_permission(P))` | leaves, lab tests, treatments, connectors, holidays, doctors | Introspect `route.dependant` |
| `handler` | Inside the handler: `_scope(user, clinic_id, P)` (voice), `_opd_scope(..., P, live=True)` (OPD), `_LEADS` dependency alias (leads) | `/admin/voice/*`, `/admin/opd/*`, `/admin/leads*` | **Behavioural test**: call as a staff user without P → expect 403 |
| `role` | `Depends(require_admin)` (staff → 403) | refund, profile, payment settings, insights summary | Behavioural test |
| `none` | `verify_credentials` only | check-in, cancel, bookings confirm/reject, `GET /admin/appointments` | Registry asserts `permission=None` (no false sense of a check) |

Registration rules (`tests/test_assistant_registry.py`): the permission matches its source; every R1–R3
action has `verify`; every R3 has `max_items` and an admin-only role set; H actions have `handoff`, and
their element ids exist in `admin/index.html`; no action path matches the **P list** (§2).

---

## 2. Risk tiers
| Tier | Meaning | Execution | Confirmation | Examples |
|---|---|---|---|---|
| **R0** | Read | Immediately | None | Leads list, briefing, deliveries |
| **R1** | Single low-impact write, idempotent or reversible | Kriya executes | One tap | Check-in, call-next, connector run-now |
| **R2** | Single write with patient, price or message side effects | Kriya executes | Preview + Confirm | Leave, cancel, price edit, register patient, ≤ 10 lead calls |
| **R3** | Bulk, clinic-wide or money | Kriya executes | Preview + **typed count/amount**; clinic_admin/super_admin (or the stated permission) | Bulk price change, > 10 lead calls, holiday, refund, catalogue import/cleanup |
| **H** | Sensitive form | Kriya **opens the page and pre-fills** the existing form; **the human clicks the page's own Save** | The page's own flow | Staff accounts, permissions, passwords, payment credentials, UPI id, cash receipts, voids, e-prescription signing, data import/export, branch/doctor deletion |
| **P** | Prohibited | Never | — | `/platform/*` (cross-tenant), AI-authored diagnosis or prescribing (NMC), sex determination (PCPNDT, `is_sex_determination_request`), inferring consent, bypassing opt-out / the 24 h window / the outbound window, DPDP erasure on behalf of a patient |

Model confidence never changes a tier. LLM-routed and rule-routed actions get identical policy.

**Why H instead of executing:** these forms carry secrets (passwords, gateway keys), legal signatures
(DSC e-Rx), physical cash, or irreversible deletions with a production incident history (KRIYA-TENANT-001
doctor deletion). Pre-filling still saves the staff member the typing. The click stays human. Passwords
are never pre-filled and never pass through the assistant or `assistant_runs`.

---

## 3. Slot resolvers (zero LLM)
| Slot | Resolver (reused code first) |
|---|---|
| doctor | `app/voice/lexicon.match_doctors(text, doctors, tenant_entries)`: pure, returns several on ambiguity, honours clinic `doctor_alias` entries. Doctors come from `GET /admin/doctors` (branch-scoped for pinned staff). |
| date / range | `app/voice/dates.parse_date`, `today_ist` (IST) + "from X to Y" |
| lab test / treatment | `difflib` over the clinic catalogue (`GET /admin/lab-tests`, `GET /admin/treatments`); `hybrid_search` synonyms for vernacular test names (`app/services/hybrid_search.py`) |
| patient | phone normalisation → `GET /admin/opd/patients/search` (OPD live) else `GET /admin/patients` |
| appointment | booking-ref regex, or `GET /admin/appointments?q=&date_from=&date_to=` |
| lead filter | segment ∈ `leads.SEGMENTS` {all, hot, open, booked, dnc}; interest ∈ `GET /admin/leads.interest_labels` |
| money / percent | Regex (₹, rs, rupees, "%", "percent", "taka"), rounding rule; ≤ 0 refused |
| page | Page table (§5) |

LLM output (fallback only) supplies surface strings that go through the same resolvers. Its ids are
discarded.

---

## 4. Catalogue (by module)

Columns: **Action · Tier · Endpoint · Permission (source) · Verify / guard**.

### 4.1 Navigation, help, briefings
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `nav.open` | R0 | client `go(page)` | page visibility rules | — |
| `help.howto` | R0 | static | — | — |
| `briefing.morning` (PB-12) / `briefing.eod` (PB-13) | R0 | many GETs | each section filtered by its own route | Sections the user can't read are omitted |

### 4.2 Dashboard and analytics
| `stats.summary` | R0 | `GET /admin/stats` | none | |
|---|---|---|---|---|
| `insights.view` | R0 | `GET /admin/insights` | route | |
| `insights.summary_generate` | R1 | `POST /admin/insights/summary/generate` | admin (role) | An existing LLM feature: cost shown |
| `opd.analytics` | R0 | `GET /admin/opd/analytics` | OPD_ADMIN (handler) | |

### 4.3 Appointments, bookings, payments
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `appointments.search` | R0 | `GET /admin/appointments?q&date_from&date_to&status` | none (DOCTOR/CASHIER need OPD_FRONT_DESK) | |
| `appointment.check_in` | R1 | `POST /admin/appointments/{id}/check-in` | none | Re-read `checked_in`; 409 → "already handled" |
| `appointment.cancel` | R2 | `DELETE /admin/appointments/{id}` | none | Only from `confirmed` (`admin_cancel_confirmed_booking`); re-read status `cancelled`/refund path; D-04 audit fix |
| `appointment.cancel_bulk` | R3 | N × the above | admin role | ≤ 25, per-row report |
| `bookings.pending_review` | R0 | `GET /admin/bookings/pending-review` | none | |
| `booking.confirm` / `booking.reject` | R2 | `POST /admin/bookings/{id}/confirm|reject` | none | Endpoint CAS; re-read |
| `booking.refund` | R3 | `POST /admin/bookings/{id}/refund` | admin (role) | Typed **amount**; single booking only; re-read payment events |
| `payments.reconciliation` / `payments.stats` | R0 | `GET /admin/payments/reconciliation`, `/stats` | route | |

### 4.4 Patients and OPD OS (only when `opd_enabled`)
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `patients.search` | R0 | `GET /admin/opd/patients/search?q` / `GET /admin/patients` | OPD_FRONT_DESK (handler) / none | |
| `patient.view` | R0 | `GET /admin/opd/patients/{id}` | handler | |
| `patient.duplicate_check` | R0 | `POST /admin/opd/patients/duplicate-check` | handler | Read-only POST |
| `patient.register` (PB-06) | R2 | `POST /admin/opd/patients` (`PatientRegisterIn`) | OPD_FRONT_DESK (handler) | **Consent fields only from the explicit consent card**; re-read |
| `patient.update` | R2 | `PATCH /admin/opd/patients/{id}` | handler | Diff preview; re-read |
| `opd.walk_in` | R2 | `POST /admin/opd/walk-ins` (`WalkInIn`) | handler | Queue shows the token |
| `opd.arrive` | R1 | `POST /admin/opd/appointments/{id}/arrive` | handler | Queue row |
| `opd.queue` | R0 | `GET /admin/opd/queue` | handler | |
| `opd.call_next` / `opd.recall` / `opd.stage` | R1 | `POST /admin/opd/queue/call-next`, `/{id}/recall`, `/{id}/stage` | handler | Queue re-read (CAS in service) |
| `opd.invoice_create` | R2 | `POST /admin/opd/appointments/{id}/invoice` then `POST /admin/opd/invoices/{id}/issue` | OPD_BILLING (handler) | Re-read the invoice |
| `opd.payment_link` | R2 | `POST /admin/opd/invoices/{id}/payment-link` | handler | Sends WhatsApp; re-read |
| `opd.receipt` / `opd.refund` / `opd.void` / `opd.shift_*` | **H** | Billing page | — | Cash and reversals are done by a human |
| encounter notes, e-prescription sign/amend | **H** | Consultation workspace | — | Doctor signs; no AI content (NMC) |

### 4.5 Doctors, schedules, leaves, holidays, branches
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `doctors.list` | R0 | `GET /admin/doctors` | none | |
| `doctor.create` | R2 | `POST /admin/doctors` (`DoctorCreate`) | DOCTORS_CREATE (dep) | Generated-slot preview; re-read |
| `doctor.update` / schedule (PB-11) | R2 | `PUT /admin/doctors/{id}` | DOCTORS_UPDATE (dep) | Lists bookings outside the new hours; multi-branch branch fields omitted |
| `doctor.delete` | **H** | Doctors page | — | Incident history |
| `doctor_leave.impact` | R0 | composite | none | Mirrors `check_doctor_leaves` |
| `doctor_leave.create` (PB-01) | R2/R3 | `POST /admin/leaves` | DOCTOR_LEAVES_CREATE (dep) | Pre-check duplicates (D-11); D-01 warning |
| `doctor_leave.delete` | R2 | `DELETE /admin/leaves/{id}` | DOCTOR_LEAVES_DELETE (dep) | Cancelled bookings are not restored (stated) |
| `holiday.create` / `holiday.delete` | R3 / R2 | `POST /admin/holidays`, `DELETE /admin/holidays/{date}` | HOLIDAYS_CREATE / _DELETE (dep) | Typed count of bookings that day |
| `branch.doctor_assign` | R2 | `POST /admin/branches/{id}/doctors` | DOCTOR_BRANCH_ASSIGN | Session rule (Session 22) enforced by the endpoint |
| branch create/edit/delete | **H** | Locations page | BRANCHES_MANAGE | Branches are what patients see on WhatsApp |

### 4.6 Lab catalogue (diagnostic plans)
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `lab_tests.list` | R0 | `GET /admin/lab-tests` | route | |
| `lab_test.create` | R2 | `POST /admin/lab-tests` | LAB_TESTS_MANAGE (dep) | price > 0 (create validator) |
| `lab_test.update` | R2 | `PUT /admin/lab-tests/{id}` (`LabTestUpdate`) | LAB_TESTS_MANAGE | price > 0 enforced by the endpoint (update validator already exists: `LabTestUpdate.validate_price`; D-10 withdrawn) |
| `lab_test.price_bulk` (PB-07) | R3 | N × PUT | LAB_TESTS_MANAGE | ≤ 200, typed count, old prices kept for a reversal plan |
| `lab_test.deactivate` | R2 | `PUT … {is_active:false}` | LAB_TESTS_MANAGE | Preferred over delete |
| `lab_test.delete`, `lab_tests.bulk_delete` | **H** | Test Catalogue page | — | Irreversible |
| `lab_tests.cleanup` (PB-08) | R0 → R3 | `GET /admin/lab-tests/cleanup-suggestions` → `POST /admin/lab-tests/cleanup-apply` | LAB_TESTS_MANAGE | Deterministic suggestions; nothing pre-ticked |
| `lab_tests.import` (PB-09) | R3 | `POST import-preview` (multipart ≤ 10 MB) → `GET import-preview/{id}` → `POST import-apply/{id}` | LAB_TESTS_MANAGE | Diff preview by the existing parser |
| `lab_tests.auto_classify` | R2 | `POST /admin/lab-tests/auto-classify` | LAB_TESTS_MANAGE | Existing LLM feature, cost shown |
| `lab_test.generate_details` | R2 | `POST /admin/lab-tests/{id}/generate-details` | LAB_TESTS_MANAGE | Existing LLM feature |
| `lab_collection_window.update` | R2 | `PUT /admin/lab-collection-window` (`LabCollectionWindowUpdate`) | route | Re-read |

### 4.7 Specialty treatments and dental
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `treatments.list` | R0 | `GET /admin/treatments` | route | |
| `treatment.create` / `treatment.update` | R2 | `POST /admin/treatments`, `PUT /admin/treatments/{id}` | TREATMENTS_MANAGE (dep) | Pydantic bounds exist (price 0–1 crore) |
| `treatment.price_bulk` (PB-10) | R3 | N × PUT | TREATMENTS_MANAGE | ≤ 100 |
| `treatments.status` | R2 | `POST /admin/treatments/status` (`TreatmentBulkStatus`) | TREATMENTS_MANAGE | |
| `treatment.doctors` | R2 | `PUT /admin/treatments/{id}/doctors` (`TreatmentDoctorsUpdate`) | TREATMENTS_MANAGE | Current vs new mapping preview |
| `treatment.ai_description` | R1 (draft) | `POST /admin/treatments/ai-description` | TREATMENTS_MANAGE | Draft only; saving = `treatment.update` |
| `treatment.delete` | **H** | Procedures page | — | |
| `dental.plan_create` / `dental.sitting_book` / `dental.sitting_complete` / `dental.sitting_cancel` / `dental.notify` (PB-15) | R2 | `/admin/dental/*` | DENTAL_PLANS_MANAGE | Sittings go through `book_appointment` |

### 4.8 Lab reports, connectors, home collection
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `reports.deliveries` | R0 | `GET /admin/reports/deliveries?days&state` | REPORTS_VIEW (dep) | Masked phones |
| `report.resend` (PB-02) | R2/R3 | `POST /admin/reports/{id}/resend` | REPORTS_RESOLVE (dep) | Duplicate-risk tick |
| `reports.queue` / `report.resolve_match` / `report.dismiss` | R0 / R2 / R2 | `/admin/reports/*` | route | Match by phone (`ResolveMatchRequest`) |
| `connectors.failed` / `connector.failed_resolve` | R0 / R2 | `/admin/connectors/failed-reports*` | route | |
| `connector.run_now` / `connector.test` | R1 | `POST /admin/connectors/{id}/run-now` / `/test` | CONNECTOR_MANAGE (dep) | Audit-log read |
| `homecollect.visits` | R0 | `GET /admin/home-collection/visits?date&branch_id` | HOME_COLLECTION_MANAGE (handler) | |
| `homecollect.assign` / `.auto_assign` (PB-14) | R2 | `POST /visits/{id}/assign`, `POST /auto-assign` | handler | Re-read visits |
| `homecollect.phleb_off_dates` | R2 | `PUT /phlebotomists/{id}/off-dates` | handler | |

### 4.9 WhatsApp leads and messaging
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `leads.list` | R0 | `GET /admin/leads?segment&interest&q&days&limit&offset` | LEADS_MANAGE (`_LEADS`) | |
| `lead.message` | R2 | `POST /admin/leads/message` | LEADS_MANAGE | Endpoint: own-contact only, opt-out, 24 h window |
| `messaging.usage` | R0 | `GET /admin/messaging-usage` | route | |
| broadcast to many patients | **P (not implemented)** | — | — | No such capability (D-07) |

### 4.10 Voice receptionist (feature `ai_receptionist_enabled`)
| Action | Tier | Endpoint | Perm | Verify / guard |
|---|---|---|---|---|
| `voice.calls` / `voice.call_detail` / `voice.live` / `voice.analytics` / `voice.usage` | R0 | `GET /admin/voice/calls[/{id}]`, `/live`, `/analytics`, `/usage` | VOICE_VIEW (handler) | |
| `voice.outbound_list` | R0 | `GET /admin/voice/outbound` | VOICE_VIEW | Latest 100 jobs |
| `voice.outbound_one` | R2 | `POST /admin/voice/outbound {phone, purpose:"lead_followup", interest}` | VOICE_MANAGE (handler) | 409 `OutboundRefused` mapped; job re-read |
| `voice.outbound_leads` (PB-03) | R2/R3 | `POST /admin/voice/outbound/bulk` (+ **NEW-BE** `dry_run`) | LEADS_MANAGE + VOICE_MANAGE | ≤ 100/run; never `consent_attested` |
| `voice.settings_update` (PB-05) | R2 | `GET` then `PUT /admin/voice/settings` (whole object) | VOICE_MANAGE (handler) | Snapshot in plan hash (D-13); window 09–21 enforced |
| `voice.knowledge_add` / `_delete` | R2 | `POST /admin/voice/knowledge` (`KnowledgeIn`), `DELETE …/{id}` | VOICE_MANAGE | "Teach the receptionist…" |
| `voice.lexicon_add` / `_delete` | R2 | `POST /admin/voice/lexicon` (`LexiconIn`) | VOICE_MANAGE | Doctor aliases also improve Kriya Ketu's own matching |
| `voice.learning_review` | R0 → R2 | `GET /admin/voice/learning/suggestions` → `POST …/{id}/approve|dismiss` | VOICE_MANAGE | |
| `voice.test_turn` | R0 | `POST /admin/voice/test-turn` | VOICE_MANAGE | Simulated, no call |
| takeover / pause a live call; bulk outbound from an imported list (consent attestation) | **H** | Voice page | — | Live patient call / legal attestation |

### 4.11 Administration
| Action | Tier | Endpoint | Perm |
|---|---|---|---|
| `notifications.list` / `.mark_all_read` | R0 / R1 | `/admin/notifications*` | none |
| `audit.recent` | R0 | `GET /admin/audit-logs` | route |
| `ai.usage` / `ai.budget` / `subscription.view` / `data.storage` | R0 | `/admin/ai/usage`, `/ai/budget`, `/subscription`, `/data/storage` | route |
| `profile.update` (hours, fees, address) | R2 | `GET` then `PUT /admin/profile` | admin (role) — snapshot hash |
| `support.message` | R1 | `POST /admin/support/messages` | route |
| staff create/update/toggle/delete, password/username change, payment settings, UPI id, data import/export, patient-records import | **H** | respective pages | — |
| corporate health | **H** | Corporate page | — |

### 4.12 PLANNED (each needs separate approval)
| Item | Need |
|---|---|
| `whatsapp.failed_messages` | New read endpoint over `failed_messages` / `outbound_message_ledger` (D-08) |
| `voice.lead_funnel` | One aggregate read for PB-04 (replaces N+1) |
| `appointment.create_scheduled` | Staff booking of future consultations (no endpoint today; OPD walk-in covers same-day) |
| Platform-owner actions | Separate registry under owner auth (KK-P5) |

---

## 5. Page table for `nav.open`
From `go()` loaders (`admin/index.html:8008`): `dashboard, insights, profile, appointments, doctors, leaves,
holidays, patients, diagreports, labreports, labtests, homecollect, corporate, treatments, prescriptions,
payments, paysettings, branches, connectors, staff, data, dentalplans, leads, voice, opdsetup, opddesk,
opdqueue, opdworkspace, opdbilling, opdearnings, opdanalytics`. Synonyms live beside the table.
Visibility comes from the sidebar's `data-role`/`data-feature`, extracted in a test so it cannot drift.

## 6. Adding an action — checklist
1. Find the endpoint. If there is none → PLANNED, stop.
2. Record the permission **and its source** (§1.1), then run the drift / behavioural test.
3. Pick the tier (§2); if unsure, go higher. Secrets, cash, signatures or irreversible deletes → H.
4. Write `verify` against an existing read.
5. Run the endpoint through the **hardening checklist** ([implementation-plan.md](implementation-plan.md)
   KK-P0-15: bounds, CAS/idempotency, no raw `str(e)`, audit row, off-loop DB, tenant/branch scope)
   **before** registering it. A gap found = a bug task first.
6. ≥ 5 trigger phrases (≥ 1 te-Latin, ≥ 1 hi-Latin) + ≥ 2 negatives in the corpus.
