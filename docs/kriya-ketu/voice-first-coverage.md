# Kriya Ketu — voice-first coverage

Owner direction (2026-10-30): anything staff type in the admin panel, OPD OS or IPD OS should be doable by
saying it once to Kriya Ketu. Every write still shows a preview (old → new, read fresh), needs one confirm
(R3: type the amount or count), and is read back after it runs (ADR-004). The endpoint stays the authority
for permissions, branch scope and validation.

## Done

| Section | Say | Action | Tier |
|---|---|---|---|
| Doctors | "the doctor's name is Ramesh Kumar, dermatologist, ₹100, mornings 10 to 1, evenings 5 to 8" | `doctor.create` | R2 |
| Doctors | "change Dr Meena's fee to 600" / "increase … by 10%" | `doctor.fee` | R2 |
| Doctors | "change Dr Rao's department from ENT to Skin" | `doctor.details` | R2 |
| Doctors | "Dr Rao's evening is now 5 to 8" | `doctor.schedule` | R2 |
| Doctors | "remove Dr Rao" (switches off) / "reactivate Dr Rao" | `doctor.active` | R2 |
| OPD queue | "token 5, BP 120 over 80, pulse 88, weight 62 kg" | `opd.vitals` | R2 |
| OPD billing | "Ravi paid 500 cash" / "token 7 paid 300 UPI, UTR …" | `opd.record_payment` | R3 |
| IPD chart | "bed 12, BP 110/70, sugar 140" | `ipd.vitals` | R2 |
| IPD chart | "shift bed 12 to bed 15" | `ipd.transfer` | R2 |
| IPD admissions | "admit Ravi Kumar under Dr Rao to bed 15 for fever" (no bed: waiting list) | `ipd.admit` | R2 |
| IPD chart | "discharge bed 12" / "bed 12 left against medical advice" | `ipd.discharge_start` | R2 |
| Bed board | "bed 16 is clean" / "bed 7 under maintenance because …" | `ipd.bed_status` | R2 |
| Service catalogue | "increase room charges by 10%" / "X-ray chest price to 500" | `services.price` | R2 |
| OPD billing | "refund 200 to Ravi in cash because the test was not done" (OPD admin) | `opd.refund` | R3 |
| OPD billing | "void Ravi's bill because it was made twice" (unpaid bills, OPD admin) | `opd.void` | R3 |
| IPD finance | "deposit 10,000 cash for bed 12" / "bed 12 advance 5,000 UPI, UTR …" | `ipd_finance.deposit` | R3 |
| Casualty | "emergency: road accident, unknown male about 30, triage red, by ambulance, BP 90/60" | `emergency.register` | R2 |
| OT | "book knee replacement for bed 12 tomorrow 10 to 12 in OT 1 with Dr Rao for osteoarthritis knee" | `ot.schedule` | R2 |
| IPD chart | "bed 12 insurance Star Health policy SH12345" / "attendant Ramu, son, 98765…" / "expected discharge Monday" | `ipd.admission_update` | R2 |
| IPD chart | "complete discharge for bed 12" (only when the checklist is all done) | `ipd.discharge_complete` | R2 |
| OT | "start surgery for Sita" / "Sita to recovery" / "postpone bed 12 surgery because …" | `ot.status` | R2 |
| Staff | "deactivate Ramesh's login" / "make Priya a cashier" | `staff.active`, `staff.role` | R2 |
| Branches | "add a branch at Madhurawada" / "rename Gajuwaka branch to Gajuwaka Main" | `branch.create`, `branch.rename` | R2 |
| Doctors | "Dr Rao works Monday to Saturday" / "Dr Rao slots 15 minutes each" | `doctor.days`, `doctor.slot_length` | R2 |
| Service catalogue | "add a new service ECG at 300 rupees" (code made from the name, GST 0%) | `services.create` | R2 |
| OPD billing | "credit note of 200 on Ravi's bill because he was charged twice" (+ "refund in cash" when paid) | `billing.credit_note` | R3 |
| Corporate health | "add Tata Steel as a corporate client" | `corporate.create` | R2 |
| Every action | spoken numbers: "six hundred", "one twenty over eighty", "fifteen hundred", "two lakh", "token five"; Hindi / Telugu ("पाँच सौ", "डेढ़ हज़ार", "ఐదు వందలు", "రెండు వేలు", "do hazaar"); clock words ("ten thirty", "half past five") | `resolve.spoken_numbers` | — |
| IPD setup | "add a new ward Maternity B" / "add beds 101 to 110 to the general ward" / "add 5 beds to ICU" | `ipd.ward_create`, `ipd.beds_create` | R2, R3 |
| IPD setup | "set ICU bed tariff to 5000 per day" / "increase private room rent by 10%" | `ipd.bed_tariff` | R2 |
| Every bed action | the ward with the bed: "bed 5 in ICU", "to bed 3 in the private ward"; the same code in two wards is asked | `resolve._with_ward` | — |
| OPD setup | "make vitals mandatory" / "patients pay before consultation" / "accept cash and UPI only" / "turn off auto open shift" / "after check-in send patients to waiting" | `opd.setting` | R2 |
| OPD setup | "Dr Rao sits in room 3" | `opd.room` | R2 |
| IPD setup | "discharge cut-off 11 am" / "hold beds for 2 hours" | `ipd.settings` | R2 |
| Insurance | "add TPA Medi Assist" / "add insurer Star Health" | `insurance.payer_create` | R2 |
| Insurance | "Star Health paid 45,000 on claim CLM-0012, UTR AXIS123456, TDS 4,500" | `insurance.settlement` | R3 |
| Insurance | "claim CLM-0012 query: discharge summary missing" / "mark claim … in process" / "withdraw Sita's claim because …" | `insurance.claim_status` | R2 |
| IPD finance | "give 2000 discount to bed 12 because of family hardship" | `ipd.discount_request` | R3 |
| IPD finance | "approve the discount for bed 12" / "reject Sita's discount because …" (IPD admin) | `ipd.discount_decision` | R3 |
| IPD finance | "put bed 12 on the normal delivery package" | `ipd.package_apply` | R2 |
| Insurance | "Star Health approved 50,000 for bed 12, approval ref AP123" (the TOTAL) | `insurance.preauth_approve` | R3 |
| Insurance | "insurer queried bed 12 pre-auth: need ultrasound report" / "Sita's pre-auth rejected because …" | `insurance.preauth_reply` | R2 |
| Insurance | "submit bed 12 pre-auth" / "request enhancement of 20,000 for bed 12" | `insurance.preauth_action` | R2 |
| IPD setup | "create a package Normal Delivery at 25,000 covering bed, nursing and doctor visits for 3 days" | `ipd.package_create` | R2 |
| IPD finance | "authorise credit of 50,000 for bed 12 from Infosys, reference PO-778" | `ipd_finance.credit` | R3 |
| Insurance | "raise pre-auth of 60,000 for bed 12 for dengue, 3 days" (the patient's valid policy; "under Star Health" if several) | `insurance.preauth_create` | R2 |
| Insurance | "Star Health disallowed 2,000 on claim CLM-0012 for non-payable items" (reason-coded) | `insurance.disallowance` | R3 |
| Earlier | leaves, holidays, bookings, walk-ins, queue moves, bills, payment links, lab/treatment prices, home visits, receptionist knowledge, branch assignment, dental plans | see `registry.py` | R1–R3 |

## Next, in this order

| # | Section | What staff type today | Proposed action |
|---|---|---|---|
| 1 | Browser check | batches 6-9 in the real panel | Playwright run |
| 2 | Insurance | cashless bill split (non-payable, co-pay), write-off / recover a disallowance | R3 |
| 3 | Speech | Telugu / Hindi patient names and digits said one by one in Indic script | resolver + STT eval |

## Stays with a person, on purpose

| What | Why |
|---|---|
| Passwords, individual permissions, deleting a login, payment-gateway keys, the counter UPI payee, the GSTIN, WhatsApp template names | A secret must never pass through speech, logs or the assistant; a mis-heard payee or GSTIN would send patients' money elsewhere or print a wrong tax ID on every bill. |
| Signing a visit, prescription, lab report, radiology report (and PCPNDT Form F) or discharge summary; waiving a discharge settlement | The clinician's signature is their legal act; Kriya never writes clinical content. |
| Consents, medico-legal cases, the WHO surgical checklist, the post-op note | Each needs the person present to attest it. |
| Nursing notes, intake/output, medicines given | Clinical writing on the chart (vitals are numbers and are now by voice). |
| Marking a sample collected | The tube's barcode is scanned: a mis-heard digit would file another patient's result here. |
| A death, or a casualty brought dead (triage black) | Recorded by the doctor. |
| Hard-deleting a doctor, test or treatment | Voice switches them off instead; deletion loses history. |
| Data export / import, taking over a live call | Files and a live patient call. |
