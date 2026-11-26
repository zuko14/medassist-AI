# Kriya Pharmacy OS for standalone pharmacies — the `pharmacy` plan (closes GAP G30 / RX-D6)

> Lane **RXR**. Builds on the finished Pharmacy OS (P0–P11, migrations 134–142) without changing it for hospitals.
> Read [status.md](status.md), [ADR.md](ADR.md) ADR-RX-09 and [GAP-ANALYSIS.md](GAP-ANALYSIS.md) G30 first.

## 1. Goal
Sell Pharmacy OS to **medical stores and pharmacy chains that have no doctors**, as their own product line in the
owner platform — separate from hospitals, clinics and diagnostic centres — while hospital pharmacies keep working
exactly as today.

## 2. Why it does not work today (evidence)
| Blocker | Evidence |
|:-|:-|
| `rx_eligible(clinic) = opd_enabled(clinic)` | `app/services/tenant.py:730` |
| `opd_enabled` needs the owner OPD opt-in **and** `has_feature(clinic, "booking")` — a shop has no doctors/bookings | `app/services/tenant.py:755` |
| Cash sales need the cashier's open shift; shifts are opened only through `/admin/opd/shifts/open`, gated by `_opd_scope` → `opd_enabled` | `app/routers/rx.py:793` (`_shift` → `opd_billing.resolve_counter_shift`); `app/routers/opd.py:515`, `:1773` |
| The shift **service** itself is flag-independent (`open_shift` only checks the login has a drawer) | `app/services/opd_billing.py:885` |
| GSTIN and letterhead come from `clinics.opd_settings` (written by the OPD setup wizard) | `migrations/117_gst_billing_and_credit_notes.sql:47` (`opd_clinic_gstin`); `app/services/rx_pos_docs.py:86`; `app/services/rx_purchase.py:337` |
| Numbering uses `opd_next_counter` / `opd_format_number` (SQL, flag-independent) | ADR-RX-10 |
| External prescriptions already supported (prescriber snapshot, external ref, fingerprint) | `migrations/138_rx_pos.sql` `rx_sales` (`external_rx_ref`, `prescriber_snapshot`, `external_rx_fingerprint`) |
| Drug licence per store already stored | `rx_stores.drug_licence_no` (used by `app/services/rx_registers.py:67`) |

So the gap is **eligibility, three thin route/setting bridges, a plan registration and a patient front door** — not a
new pharmacy engine.

## 3. Decisions
| ID | Decision |
|:-|:-|
| RXR-ADR-1 | New plan slug **`pharmacy`** ("Pharmacy / Medical Store"). `PLAN_FEATURES["pharmacy"] = {multilingual, emergency_escalation, clinical_firewall, admin_dashboard, compliance_dpdp, compliance_nmc, multi_branch, payments_razorpay, holiday_calendar, staff_training}` — no `booking`, no `reminders`, no lab features. Not in `SPECIALTY_BY_PLAN` / `HYBRID_SPECIALTY_PLANS`. |
| RXR-ADR-2 | `PHARMACY_PLANS = frozenset({"pharmacy"})`; `rx_eligible(clinic) = opd_enabled(clinic) or (tenant and clinic.plan in PHARMACY_PLANS)`. Every other plan's answer is unchanged (test pins it for all 12 existing plans). `pharmacy_enabled` is still the owner's explicit opt-in (never the wildcard); the pharmacy onboarding wizard sets it and provisions the default store with the existing RX-06 code path. |
| RXR-ADR-3 | Shifts without OPD: `/admin/pharmacy/shifts/open`, `/current`, `/{id}/close`, `GET /shifts` — thin routes under `_rx_scope(…, "RX_DISPENSE")` calling the **same** `opd_billing.open_shift / get_current_shift / close_shift / list_shifts`. Same table, same blind close, same `opd_close_shift` (which already adds pharmacy cash, 138). Hospitals keep using the OPD routes; both see the same shifts. |
| RXR-ADR-4 | Business profile without OPD: the Pharmacy Setup page gains a "Business & GST" card that writes **only** `opd_settings.gstin` and the letterhead keys through the OPD settings validator, and only when `opd_enabled` is false (OPD clinics keep editing them in OPD setup). Licence numbers stay on `rx_stores.drug_licence_no`; pharmacist name + State Pharmacy Council registration added to `rx_settings` (validated). |
| RXR-ADR-5 | Patient front door (WhatsApp): `_send_main_menu` gains an early `pharmacy_only` branch **only for plan `pharmacy`** (same shape as the `diagnostics_only` branch, AUDIT CC A35): rows **Refill my medicines** (existing refill plans/requests), **Send prescription** (photo), **Is it in stock?**, **Timings & location**, **Talk to pharmacist**. Every other plan's menu is byte-identical (snapshot test). |
| RXR-ADR-6 | "Send prescription": an image is accepted **only** in the state `rx_upload_rx` of a pharmacy-plan clinic (same guarded pattern as the home-collection location pin in `conversation.py`); stored privately (signed URLs); creates an `pharmacy_order_requests` row for the pharmacist. **No OCR/LLM reads the prescription** (ADR-RX-06): the pharmacist reads it, builds the sale with the external-Rx fields, and confirms pickup time. |
| RXR-ADR-7 | "Is it in stock?": deterministic resolver (RX-15 `/items/resolve`) answers *available / not available / ask at counter*; Schedule H/H1/X items add "bring your prescription"; never a dose, substitute or advice. |
| RXR-ADR-8 | Fulfilment: pickup by default; home delivery only when the owner records the store's delivery licence/partner (RX owner decision O-6). |
| RXR-ADR-10 | **Customer registry without OPD** (VERIFICATION D13): Pharmacy OS has no patient-create route — registration is `/admin/opd/patients` → `register_patient` under `_opd_scope` (`app/routers/opd.py:729`), and refill plans need a `patient_id`. Add `/admin/pharmacy/customers` (create / search) under `_rx_scope(…, "RX_DISPENSE")` calling the **same** `register_patient` service (MRN via `opd_assign_mrn`); hospitals keep using the OPD route. |
| RXR-ADR-11 | **One release, never half a plan** (VERIFICATION D14): the `pharmacy` option is not selectable in the owner console until the WhatsApp menu branch (RXR-07), the voice scope (RXR-09), the admin-panel gating (RXR-05) and the customer registry ship together — otherwise shop customers would be offered doctor bookings by WhatsApp and voice. A test asserts the pharmacy menu and voice scope contain no booking offer. |
| RXR-ADR-9 | Voice: the receptionist's plan scope (SESSION_56) gains a pharmacy scope — refill request (exists, `_wf_pharmacy_refill`), timings, stock yes/no, transfer to pharmacist. |

## 4. Data model — migration 150 `150_retail_pharmacy.sql` (+ `rollback/150_rollback_retail_pharmacy.sql`)
- Widen `clinics.plan` and `plan_tiers.plan_name` CHECKs with `'pharmacy'` (dynamic constraint inspection, the 077/082
  pattern); seed `plan_tiers` row (display "Pharmacy / Medical Store", included messages 1000 — owner editable).
- `pharmacy_order_requests` — `clinic_id, store_id NULL, patient_id NULL, phone, phone_masked, kind (prescription/refill/
  stock_query/callback), source (whatsapp_photo/whatsapp_text/voice/counter), media_ref (Meta media id, fetched only when
  staff open it), note, status (new/reviewing/ready/collected/rejected/cancelled), rejected_reason, sale_id, pickup_by…`;
  one open request per (clinic, phone, kind); references via the same-clinic guard (no FK into earlier migrations);
  in `TENANT_OWNED_TABLES`; `rx_purge_clinic` v10.
- No change to any RX table used by hospitals.

## 5. Registry checklist (every place a plan slug lives — from docs/specialty_plan/02-plan-registry.md)
| File | Change |
|:-|:-|
| `app/services/tenant.py` | plan comment block, `PLAN_FEATURES["pharmacy"]`, `PHARMACY_PLANS`, `rx_eligible` |
| `app/routers/clinics.py:63`, `:312` | add `"pharmacy"` to Create/Update plan lists |
| `app/routers/platform.py:379` (`clinics_by_plan`), `:2346` (`valid_plans` in `update_plan_tier`) | add `"pharmacy"` |
| `app/services/message_accounting.py:272` | fallback `{"pharmacy": {"included_messages_month": 1000, "display_name": "Pharmacy / Medical Store"}}` |
| `admin/platform.html` `:586` badge CSS, `#planFilter` (`:1227`), `#ccPlan` (`:2405`) | add option/badge; `applyPlanFieldVisibility` already hides the daily report limit for plans without lab reports |
| `admin/index.html` | plan `pharmacy` shows the Pharmacy nav group, WhatsApp/Leads, Staff, Settings, Ketu; hides Appointments/Doctors/OPD (gated on `/admin/me.plan`) |
| `app/services/conversation.py` | `pharmacy_only` menu branch + guarded image state (RXR-ADR-5/6) |
| tests | new `tests/test_pharmacy_plan.py`; **do not edit** `tests/test_specialty_plans.py` (its snapshot of existing plans must still pass) |

## 6. Owner platform: a separate Pharmacy section
See [docs/specialty-os/OWNER-CONSOLE.md](../specialty-os/OWNER-CONSOLE.md) §3. In short: a **Pharmacies** segment
(own tab/filter and KPI strip: shops, today's sales, expiring stock value, open refill/order requests), a
**Create pharmacy** wizard (name, WhatsApp number, GSTIN, drug licence numbers per shop, pharmacist + council
registration, branches → stores), plan tier and message limits, and the same module toggles (Pharmacy OS forced on,
Ketu and voice optional; OPD/IPD/DX/CC hidden for this plan).

## 7. Retail gaps vs chemist software (e.g. Marg ERP 9+, GoFrugal, Medeil)
| Gap | Plan |
|:-|:-|
| Offline billing during broadband outages (forensic report Gap 2) | v1: documented paper fallback + post-entry with original date and reason (audited). Full offline POS conflicts with gapless numbering and the live stock ledger — separate design (RXR-D1) |
| Distributor ordering (forensic Gap 4) | v1: PO PDF/CSV in distributor-friendly columns + send PO on the distributor's WhatsApp (utility template, clinic credentials); API integrations with B2B ordering platforms later (RXR-D2) |
| Tally export (forensic Gap 5) | sales/purchase vouchers XML export from existing GSTR-1/registers data (RXR-D3) |
| Credit customers / khata | RX owner decision O-1 (RX-D2) — enable for retail with credit limits and statements |
| Loyalty / discounts schemes | simple bill-level discount rules with reason (exists) → loyalty later |
| Free goods / schemes on purchase | confirm GRN free-quantity handling in 137 during RXR-00 audit |
| Near-expiry returns to distributor | exists (A8 planner, `rx_expiry_return_plan`) |
| Generic substitution | only by the pharmacist under ADR-RX-07 rules; never suggested by AI |

## 8. Legal
Drugs and Cosmetics Rules 1945: retail licence per shop (stored per store), a registered pharmacist on duty (name +
registration in settings, printed on bills), prescription required for Schedule H/H1/X (POS enforces), H1/X/narcotic
registers (141). Online sale of prescription drugs to unseen customers is not offered (draft e-pharmacy rules not in
force); WhatsApp orders are reservations for pickup against a prescription the pharmacist verifies.

## 9. Tasks
| ID | Task | Depends | Tests |
|:-|:-|:-|:-|
| RXR-00 | Baseline gates (RG1–RG8); re-verify §2 by symbol | — | RX TEST-PLAN §1 |
| RXR-01 | Migration 150 + rollback + verify block | RXR-00 | `test_migration_rx_retail.py` |
| RXR-02 | Plan registry (§5) + `rx_eligible` + snapshot tests | RXR-01 | `test_pharmacy_plan.py`, `test_specialty_plans.py` unedited, `test_rx_feature_gate.py` unedited |
| RXR-03 | RX shift routes (RXR-ADR-3) + customer registry (RXR-ADR-10) + route matrix | RXR-02 | `test_rx_route_matrix.py` (+ new rows), shift tests |
| RXR-04 | Business & GST card (RXR-ADR-4) | RXR-02 | settings tests |
| RXR-05 | Admin panel plan gating for `pharmacy` (new nav items: Lucide `contact` Customers, `clipboard-list` Order requests; UI spec §3.2) | RXR-02 | UI-G1…G12 (UI spec §8) |
| RXR-06 | Owner console Pharmacies section + wizard (UI spec §7.10, dark-only console tokens) | RXR-02 | UI-G1…G12 + platform tests |
| RXR-07 | WhatsApp `pharmacy_only` menu + stock answer | RXR-02 | menu snapshot for all other plans unchanged; FSM suite unedited |
| RXR-08 | Prescription photo → `pharmacy_order_requests` + pharmacist queue | RXR-07 | hook tests; media only in that state |
| RXR-09 | Voice pharmacy scope | RXR-07 | `tests/voice/` unedited + new tests |
| RXR-10 | PO to distributor on WhatsApp / CSV | RXR-02 | purchase tests |
| RXR-11 | Care Lite `rx_adherence` hook (after CC P8) | CC-D5 | CC tests |
| RXR-12 | Zero-impact + release readiness | all | `test_rx_zero_impact.py` extended |

## 10. Build status (2026-11-25, working tree, uncommitted)
| Task | State | Evidence |
|:-|:-|:-|
| OC-01 plan-registry drift test | VERIFIED | `tests/test_plan_registry_drift.py` (clinics.py ×2, platform.py ×2, message_accounting, platform.html ×3, DB CHECKs) |
| RXR-01 migration 150 + rollback | VERIFIED | `tests/test_migration_rxr.py` (widen, tier, order-request rules, purge v10, rollback refuse / drop / re-apply) |
| RXR-02 registry + `rx_eligible` | VERIFIED | `tests/test_rxr_plan.py` (12 existing plans keep their answer), `test_specialty_plans.py` unedited |
| RXR-03 shifts + customers + queue routes | VERIFIED | `tests/test_rxr_routes.py`; `test_rx_route_matrix.py` pin + 10 rows |
| RXR-04 business & GST | VERIFIED | `GET/PUT /admin/pharmacy/business` + "Business & GST" card on Pharmacy setup (`RXR:business` block): GSTIN (checksum; editable here only when OPD is off — OPD clinics keep it in OPD setup, 409), pharmacist + council registration, drug licence per selling store, PO WhatsApp template; writes only those keys. `tests/test_rxr_business.py` |
| RXR-05 panel gating | VERIFIED (existing gates) | plan without `booking` hides Appointments / Doctors; OPD / RX pages follow module access; Order requests + Customers tabs on the Refills page |
| RXR-06 owner wizard | IMPLEMENTED | `POST /platform/pharmacies` (tested); 3-step modal in `admin/platform.html` (`OC:pharmacy-wizard`) — visual sign-off pending |
| RXR-07 WhatsApp `pharmacy_only` menu + stock answer | VERIFIED | `tests/test_rxr_whatsapp_wiring.py`, `tests/test_rxr_routes.py` (no booking, H/H1/X → bring prescription) |
| RXR-08 prescription photo → queue | VERIFIED | webhook passes the image id only; accepted only in `rx_upload_rx`; no OCR / LLM |
| RXR-09 voice pharmacy scope | VERIFIED | `scope.kind == "pharmacy"`: BOOKING / LAB → NOT_OFFERED; `help_menu_pharmacy` |
| RXR-10 PO to distributor on WhatsApp / CSV | VERIFIED | `GET /admin/pharmacy/purchase-orders/{id}.csv` (distributor columns, formula-safe) + `POST …/whatsapp` (approved PO only; clinic's own utility template with the PO PDF as header document; refused without template / supplier number); routes under /admin/pharmacy so the pinned `/admin/rx` set is unchanged. `tests/test_rxr_po_distributor.py`; PO dialog buttons in the `RXR:po` block |
| RXR-11 Care Lite | DEFERRED | CC-D5 |
| RXR-12 zero-impact | VERIFIED | other plans never reach the pharmacy block; voice / webhook / FSM suites unedited and green |
