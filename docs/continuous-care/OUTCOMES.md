# Kriya Continuous Care — Outcome and Revenue Measurement ("proof, not promises")

> Method version `outcomes-1`. Principles (ADR-CC-15): cohort vs its own 90-day pre-enrollment baseline; a
> non-enrolled reference group with the same ICD-10 codes is shown, never called a control; revenue counts only
> verified events; the report says "associated with".

## 1. Definitions (per pack, per period)
| Metric | Numerator | Denominator |
|:-|:-|:-|
| Enrolled | enrollments that reached `active` in the period | — |
| Active | enrollments `active` on the last day | — |
| **Return rate (90 d)** | active-for-≥ 90 d patients with ≥ 1 verified visit (check-in / signed encounter) in their first 90 days | active-for-≥ 90 d patients |
| Baseline return rate | same patients with ≥ 1 checked-in visit in the 90 days before enrollment | same |
| **Labs on time** | pack lab items with a validated result within due date + grace | pack lab items due in the period |
| **At target** | patients whose latest value in the period is at target (t2dm: HbA1c < signed target; htn: 7-day mean < target) | patients with a value in the period |
| Refills on time | refill-plan cycles with a completed sale before `covered_until + 3 d` | cycles ending in the period |
| Answered-card rate | prompts `answered` | prompts `sent` |
| Safety response | urgent cases acknowledged within SLA | urgent cases |
| **Verified revenue (₹)** | sum of `cc_verifications.revenue_paise` (paid consultation invoice lines, billed validated labs, completed refill sale lines) for actions CC created | — |
| Baseline revenue (₹) | same patients' paid visits/labs/refills in the 90 days before enrollment | — |

Reference group: patients with the pack's ICD-10 codes, seen in the 180 days before the period, never enrolled —
return rate and labs on time over the same period.

## 2. What is not counted
Appointments with `status='completed'` but no check-in/encounter (no-shows, AUDIT A10); revenue from visits CC did not
book; staff-entered readings in "patient engagement"; enrollments without a doctor's signature (X1).

## 3. Report layout
Funnel (candidates → invited → consented → active → graduated/withdrawn/lost) · return rate vs baseline vs reference ·
labs on time · at target · refills · safety response · verified ₹ vs CC fee · method note and version.

## 4. Worked example (fixture for `tests/test_cc_outcomes.py`)
Ten T2DM patients active ≥ 90 days.
- Baseline: 4 of 10 had a checked-in visit in the prior 90 days → baseline return rate **40 %**.
- First 90 days: 7 of 10 had a verified visit (one more had `completed` with no check-in → not counted) → **70 %**.
- HbA1c due for 10, validated on time for 6 → labs on time **60 %**; of the 6 tested, 2 < 7.0 % → at target **33 %**.
- Verified revenue: 7 consultations × ₹500 paid + 6 HbA1c × ₹450 billed + 12 refill sales totalling ₹9,600 =
  ₹3,500 + ₹2,700 + ₹9,600 = **₹15,800**. Baseline revenue same patients prior 90 d: ₹7,100. Delta **₹8,700**.
- Expected report lines: "Return rate 70 % (baseline 40 %)", "Labs on time 60 %", "At target 33 % of tested",
  "Verified revenue ₹15,800 (baseline ₹7,100)".
