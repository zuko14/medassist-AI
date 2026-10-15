# Kriya IPD OS — Workflow Specification

Roles below are permissions (see [SECURITY.md](SECURITY.md) §3). "Doctor" = a login linked to a doctor of the clinic.
Every write is audit-logged (`admin_audit_logs`) and every stay transition writes an `ipd_events` row in the same
transaction.

## W1 Configure (IPD_ADMIN)
Owner enables IPD (OPD must be on) → **IPD Setup**: bed tariffs per category (₹/day), discharge cut-off hour,
reservation hold minutes → create wards (code, type, optional gender restriction, branch) → add beds in ranges
(code, category). Beds can be deactivated (not while occupied/reserved), taken out of service (maintenance/blocked,
reason required) and returned. Wind-down: new wards/beds refused.

## W2 Admit (IPD_ADMISSIONS; registry search needs OPD_FRONT_DESK)
1. Find the patient in the OPD registry (name/phone/MRN). New patients are registered at OPD Front Desk first —
   one patient master, DPDP consent captured there.
2. Choose admitting (+ optional attending) doctor, source (OPD/emergency/direct/referral/transfer-in), priority,
   reason, optional provisional diagnosis, payer, attendant, consent flag, expected discharge.
3. Choose a free bed or "waiting list". Submit carries a client idempotency key (double click = one admission).
4. Server: `ipd_admit` → checks patient/family/doctors belong to the clinic, one active stay per person, then (if a
   bed was chosen) locks the bed and checks eligibility: active bed + ward, status free (or own/expired hold),
   branch match, ward gender restriction vs recorded gender (unknown gender is refused for restricted wards).
   **Failure**: the whole admission rolls back (no orphan pending stay when the bed was taken).
Exceptions: bed taken → 409, pick another; patient already admitted → 409; wind-down → 409.

## W3 Waiting list, reservation, assignment (IPD_ADMISSIONS)
Pending stays appear under "Waiting for a bed". Reserve holds a bed for N minutes (one hold per stay; moving the
hold frees the old bed). Assign places the patient (own hold honoured; expired holds of others are released in the
same transaction). Cancel (reason) releases any hold. An expired hold is shown as "hold expired" — the bed is not
shown as free until an assignment takes it.

## W4 Nursing station (IPD_NURSING or IPD_CLINICAL)
- **Observations**: vitals (BP pair, pulse, RR, temperature, SpO₂, pain, glucose, consciousness) or intake/output
  (route + ml), with the time actually observed (up to 7 days back, not in the future). Bounds reject typos only;
  **no clinical thresholds or alerts are applied** (owner decision O-5).
- **Correction**: mark an entry "entered in error" with a reason, optionally with a replacement that points at it.
  Nothing is deleted or overwritten.
- **Notes**: nursing and shift-handover notes; the author may amend (new version, reason; original kept).
- **MAR**: against an active medication order of the same stay: given (dose) / refused / held / delayed / omitted
  (reason required unless given). Idempotent per click. Errors are marked, not edited.
- **Bed turnover**: "Mark clean" on beds in cleaning.

## W5 Doctor rounds and orders (IPD_CLINICAL + doctor link)
Progress / plan / procedure notes are doctor-only. Orders: medication (drug, dose, route, frequency, PRN),
investigation (optional lab test reference), procedure, diet, nursing care; priority routine/urgent/STAT;
idempotent. Medication orders are stopped only by a doctor; nurses may mark non-drug tasks done. Orders are never
edited: stop and re-order.

## W6 Transfer (IPD_ADMISSIONS or IPD_NURSING)
Pick a free bed and give a reason. The request carries the bed the screen showed (`expected_bed_id`); if the
patient was moved meanwhile → 409. One transaction ends the old assignment, starts the new one, moves the stay and
sends the old bed to cleaning. The admission number and record are unchanged (no second admission, no billing event
— bed-day charges follow the bed held at the end of each day).

## W7 Discharge
1. **Start** (`admitted → discharge_pending`): regular / death / transfer-out by a doctor; LAMA / DAMA / absconded
   may be recorded by the desk. Can be undone (reason) until a final bill exists.
2. **Summary**: clinical staff draft (CAS on `updated_at` — two editors cannot silently overwrite); only the
   **attending doctor** with a registration number signs. Changing a signed summary needs an amendment reason and a
   new signature. Print view from the signed version only.
3. **Orders**: every order completed or discontinued.
4. **Billing** (IPD_BILLING): add manual charges (client `source_ref`), calculate bed-day charges (missing tariff
   reported), post (immutable), generate the final bill (draft OPD invoice), then issue and collect in **OPD
   Billing** (receipts, split payments, payment links, shifts).
5. **Complete** (IPD_ADMISSIONS or IPD_BILLING): the checklist is re-run under the admission lock: initiated,
   summary signed (not required for absconded), orders closed, no proposed charges, final bill issued, all posted
   charges billed; **bill settled** — or an IPD admin records a waiver reason (e.g. cashless insurance pending).
   Then: assignment ended, bed → cleaning, stay locked.

### Bed-day billing rule (owner decision O-1, configurable cut-off)
Every IST calendar date the patient holds a bed is one bed-day, charged at the category of the bed held at the end
of that date. Once discharge is started, the final date is not charged if it is after the admission date and the
time is before `discharge_cutoff_hour` (default 12:00). Charges are *proposed*; billing reviews and posts them.

## W8 Module disable / re-enable (platform owner)
See ADR-IPD-07. Owner console shows the preview and asks for confirmation before wind-down.

## Recovery paths
| Problem | Recovery |
|---|---|
| Wrong vitals / MAR entry | Mark entered in error (+ replacement) |
| Wrong note | Author amends; others add a new note |
| Wrong charge | Void before billing (reason); after billing: void the bill in OPD Billing, then fix and regenerate |
| Discharge started by mistake | Undo (reason) unless a live bill exists |
| Patient moved to wrong bed | Transfer again (both moves stay in the history) |
| Admission created by mistake | Cancel while pending; an admitted stay is discharged (type per policy) — never deleted |
| Request timed out | Repeat it: admissions, orders, MAR and charges are idempotent; functions are atomic |
