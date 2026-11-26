# Kriya Dermatology, Hair & Aesthetics OS (`derma` plan) — gaps and build plan

## 1. The segment
Clinical dermatology (acne, psoriasis, vitiligo, eczema, infections) plus aesthetic and hair procedures sold as
**session packages** (laser hair reduction ×6, PRP ×4, peels, Q-switch, MNRF), cosmeceutical retail, and memberships.
Typical software: aesthetic/med-spa platforms (e.g. Zenoti, Clinicea) for packages, photos and memberships, or a general
EMR with derma templates (e.g. Healthplix, Practo).

## 2. What Kriya has (evidence)
Treatment catalogue with categories, "price from", duration, pre/post instructions, AI descriptions, concern search,
callback leads (077–081); OPD OS + GST engine that taxes a service when its catalogue entry carries a rate (117);
Pharmacy OS for cosmeceutical retail (134–142); `clinical_consents` (119).

## 3. Gap register
| ID | Gap | Close with | Priority |
|:-|:-|:-|:-|
| DER-1 | **Session packages**: sell 6 sessions once, consume per visit, balance on WhatsApp ("3 of 6 left — book next"), expiry, transfer rules, refunds pro-rata | S3 packages | P1 |
| DER-2 | **Before/after photos** with consent, standard capture angles, side-by-side compare over sessions, share to patient only on explicit staff action | S1 media vault | P1 |
| DER-3 | Procedure session record (device, wavelength/fluence/spot size, shots, area, skin type Fitzpatrick I–VI, test patch result, adverse event) | S2 `derma_procedure_v1` | P2 |
| DER-4 | Lesion / body map and severity scores (PASI, SCORAD, MASI, SALT for alopecia) computed in code from entered components | S2 `derma_exam_v1` (scores are arithmetic on entered values — deterministic) | P2 |
| DER-5 | Isotretinoin and methotrexate monitoring | CC packs (PROTOCOLS §6) | P2 |
| DER-6 | Series reminders and aftercare (sunscreen, no waxing before laser) as fixed approved texts | CC `derma_procedure_series` | P2 |
| DER-7 | Memberships / wallets (prepaid credit) | S3 wallet (later; needs refund + GST advice — owner decision) | P3 |
| DER-8 | Consent templates (laser, chemical peel, PRP, botulinum/filler) | S4 | P2 |
| DER-9 | Product recommendation at checkout (cosmeceuticals) | Pharmacy OS POS with the doctor's signed Rx lines; **no AI product suggestions** | — |

## 4. Design notes
- **GST:** cosmetic and aesthetic procedures are not exempt healthcare services (Notification 12/2017-CT(Rate) excludes
  cosmetic/plastic surgery except restorative). The existing engine already handles it: an OPD service-catalogue entry
  with `tax_rate_bps` (e.g. 1800) is taxed when the clinic has a GSTIN (`migrations/117_gst_billing_and_credit_notes.sql`
  header). Gap: the specialty treatment catalogue (077) does not carry a tax rate — the package/bill line must take it
  from the linked OPD service entry, never from the WhatsApp catalogue. Packages split tax per line.
- **Photos:** EXIF stripped; stored under `{clinic_id}/clinical-media/{patient}/{random}`; never used for AI analysis
  (that would be diagnosis); access logged; deletion follows DPDP erasure.
- **Package consumption** is a row in the package ledger (`opd_package_consumptions`: package sale, visit, session n)
  — never a zero-value invoice line — so OPD invoices, receipts, GSTR-1 and refunds work unchanged; a pro-rata refund
  is an ordinary credit note on the package sale invoice (117).

## 5. Tasks
SP-DER-01 packages (S3) + WhatsApp balance · SP-DER-02 media vault (S1) + compare UI · SP-DER-03 procedure form ·
SP-DER-04 exam form + scores · SP-DER-05 CC derma packs (CC-50) · SP-DER-06 consent templates · SP-DER-07 wallet (after
owner decision).
