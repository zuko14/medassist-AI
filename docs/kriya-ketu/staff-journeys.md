# Kriya Ketu — Staff Journeys (per role, a day in the life)

Parent: [spec.md](spec.md) · Actions: [action-registry-spec.md](action-registry-spec.md) ·
Playbooks: [playbooks-spec.md](playbooks-spec.md)

Every journey uses **real roles and grants** (`app/services/permissions.py`) and **real endpoints**.
Where a role needs a grant it does not have by default, Kriya Ketu says exactly which permission to ask
the clinic admin for. It never works around a missing grant.

Legend: ⓡ = rules route (0 tokens) · ⓛ = LLM fallback (rare) · R0–R3 / H = risk tier
(registry §2) · PB-xx = playbook.

---

## 1. Front desk (RECEPTIONIST / FRONT_DESK) managing OPD + WhatsApp + voice

**Grants needed.** The preset gives `OPD_FRONT_DESK` only. For the WhatsApp and voice work below, the
clinic admin adds `LEADS_MANAGE` and `VOICE_MANAGE` to this account (Staff page). The clinic also needs
the owner opt-ins `opd_enabled` and `ai_receptionist`. Without a grant, Kriya Ketu answers *"You need the
'Leads' permission — ask your clinic admin"* and shows nothing from that module.

| Time | Staff says (any of te / hi / en) | What Kriya Ketu does | Tier |
|---|---|---|---|
| 09:00 | "Good morning" / "today's briefing" / "ఈరోజు ఏముంది" | **PB-12 Morning briefing**: today's appointments by doctor, doctors on leave today, OPD queue now, pending-review bookings, failed report deliveries (24 h), **hot WhatsApp leads** (24 h), yesterday's voice calls and outcomes. One card, each section linking to its page. | R0 ⓡ |
| 09:05 | "Show WhatsApp leads" / "leads details" / "hot leads for dermatology this week" | `leads.list(segment=hot, interest=dermatology, days=7)` → table: name, masked phone, interest, last message, 24 h window open/closed, mid-booking flag. Segments: all / hot / open / booked / dnc (`app/services/leads.py:SEGMENTS`). | R0 ⓡ |
| 09:07 | "Call these leads" / "call all hot dermatology leads" | **PB-03 Leads → voice receptionist calls.** Dry run: who **will** be called and who is **skipped and why** (opted out, already queued, called in 7 days, never contacted the clinic). Window 10:00–19:00, daily cap, ETA. One confirmation (typed count if > 10) → `POST /admin/voice/outbound/bulk` → read back the queued jobs → "12 calls queued, 3 skipped". | R2/R3 ⓡ |
| 09:08 | "Message Ravi that the doctor is in till 1" | `lead.message`: only inside the 24 h WhatsApp window; outside it, Kriya suggests a voice call instead (the existing rule in `leads.send_lead_message`). | R2 ⓡ |
| 09:10 | "Register Lakshmi, 9876543210, 42, female, for Dr Anitha" | **PB-06 Register + token**: duplicate check → if a match is found, pick the existing patient or confirm a new one → **consent card** (staff must ask the patient; Kriya never assumes consent) → register → walk-in token for Dr Anitha → read back from the live queue → "Token A-14, Room 2". | R2 ⓡ |
| 09:30 | "Ravi Kumar has arrived" | `opd.arrive` for his booked appointment today (or check-in when OPD is off) → token. | R1 ⓡ |
| 10:15 | "Call next for Dr Anitha" | `opd.call_next` → the queue reads back the new called token. | R1 ⓡ |
| 11:00 | "Cancel Sita's 4 pm, she called" | `appointment.cancel` with a preview (paid? refund is attempted; the patient gets WhatsApp) → read back. | R2 ⓡ |
| 13:00 | "How did the lead calls go?" | **PB-04 Lead conversion report**: queued → dialled → answered → booked (appointments created by those phones since the call), per interest. | R0 ⓡ |
| 15:00 | "Call leads automatically every day" | **PB-05**: switch on the existing `outbound.auto_leads` (hourly job `autoqueue_leads`) with a daily cap and window → read back the settings. Clinic admin or `VOICE_MANAGE` only. | R2 ⓡ |
| 18:30 | "Wrap up the day" | **PB-13 End of day**: confirmed but never arrived, pending-payment holds about to lapse, failed sends, tomorrow's first slots, any doctor leave tomorrow (with the D-01 warning if it applies). | R0 ⓡ |

## 2. Doctor-schedule manager (DOCTOR_SCHEDULE_MANAGER / BRANCH_MANAGER)
| Says | Does | Tier |
|---|---|---|
| "Dr Srinivas on leave tomorrow, find affected appointments" | **PB-01** impact → leave → D-01 warning → optional cancel-and-notify | R2/R3 |
| "Dr Anitha's evening is now 5 to 8" | **PB-11 Schedule change**: preview the new slots and **existing bookings outside the new hours** (not moved automatically; offers per-booking cancel) → `PUT /admin/doctors/{id}` → read back. A multi-branch doctor's branch fields are locked (Session 22 rule). | R2 |
| "Add Dr Kiran, dermatologist, ₹600, Mon–Sat mornings" | `doctor.create` with a preview of the generated slots | R2 |
| "Hospital closed on 2 Nov" | `holiday.create`: typed count of bookings that day; clinic admin only | R3 |

## 3. Diagnostic centre (LAB_OPERATOR / DIAGNOSTIC_OPERATOR / admin) on diagstream / diagbooking plans
| Says | Does | Tier |
|---|---|---|
| "CBC price 350" | `lab_test.update`, diff old→new (the endpoint refuses prices ≤ 0) | R2 |
| "Increase all biochemistry tests by 10%, round to 10" | **PB-07 Bulk price change**: diff table of N tests, typed count → per-row update → read back each → report | R3 |
| "Clean up the test list" / "fix duplicate tests" | **PB-08 Catalogue cleanup**: the deterministic `analyze_catalogue_quality` suggestions (merges, renames, junk rows) → pick → apply → read back | R3 |
| "Update prices from this price list" + attach a file | **PB-09 Import**: the existing `import-preview` (parser + diff) → shows added / changed / unchanged → apply → read back counts | R3 |
| "Report failures today, resend" | **PB-02** | R0 + R2 |
| "Assign tomorrow's home collections" | **PB-14** `auto-assign`, preview per phlebotomist, leave days respected | R2 |
| "Lab collection window 7 to 11 on Sundays" | `lab_collection_window.update` | R2 |

## 4. Specialty clinic (derma / dental / eye / ivf / multispecialty) — admin or `TREATMENTS_MANAGE`
| Says | Does | Tier |
|---|---|---|
| "HydraFacial price from 2500" | `treatment.update(price_from_rupees)` diff | R2 |
| "Raise all laser treatments by 5%" | **PB-10** bulk treatment price | R3 |
| "Publish Chemical Peel" / "hide LASIK" | `treatments.status` | R2 |
| "Only Dr Meera does PRP" | `treatment.doctors` mapping, previewing current vs new | R2 |
| "Write a description for Microneedling" | The existing AI copywriter endpoint (an explicit LLM feature; cost shown) → **draft shown, not saved** → "Save" = `treatment.update` | R1 + R2 |
| (dental, `DENTAL_PLANS_MANAGE`) "Root canal plan for Suresh, 3 sittings, ₹9000, first sitting Monday with Dr Rao" | **PB-15** plan + first sitting (via `book_appointment` rules) + notify | R2 |

## 5. Cashier (CASHIER → `OPD_BILLING`)
Reads: billing queue, today's collections. "Create the bill for token A-14" → `opd.invoice_create` + issue
(R2); "send payment link" (R2). **Cash receipts, refunds, voids and shift close are hand-off (H)**: Kriya
opens the Billing page with the form pre-filled and the cashier completes it. Money handled at the
counter must be counted by a human.

## 6. Doctor (DOCTOR → `OPD_CLINICAL`)
"Who's next?", "my queue", "call next". Clinical notes and e-prescriptions are **hand-off only**: Kriya
opens the consultation workspace for the patient, and the doctor writes and signs. No AI-authored
clinical content (NMC; AGENTS.md §1.5).

## 7. Clinic admin
Everything above, plus R3 actions, voice settings, the receptionist knowledge base ("teach the receptionist
we have free parking" → `voice.knowledge_add`), approving the receptionist's learning suggestions, clinic
profile (hours, fees), reconciliation, AI and messaging usage. **Hand-off** for staff accounts, permissions,
passwords, payment-gateway credentials, UPI destination, data import and export.

## 8. Roles with no assistant
CORPORATE_VIEWER and PHLEBOTOMIST. Their logins are confined to fixed routes. A phlebotomist's day is
already one page (My Collections).
