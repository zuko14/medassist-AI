# Kriya Continuous Care — Protocol Engine and Protocol Packs

> **Clinical content in this file is a starting point for the hospital's medical director, not medical advice and
> not a Kriya default that goes live by itself.** Every threshold is a parameter with a cited source and hard bounds;
> a pack runs only after a registered doctor signs the clinic's activation (ADR-CC-06). Packs never change a
> medicine or dose (ADR-CC-04); "protocol step due" is text shown to the doctor.

## 1. Engine contract

```python
# app/services/cc_protocols/base.py  (pure; no I/O, no clock, no AI — tests/test_cc_no_llm.py)
@dataclass(frozen=True)
class Inputs:
    today: date                      # IST, passed in
    plan_items: tuple[PlanItem, ...]
    observations: tuple[Obs, ...]    # plausible only, newest first, ≤ 120 days
    labs: tuple[LabValue, ...]       # from cc_measure_sources mapping, validated results only
    adherence: tuple[Obs, ...]
    enrollment: EnrollmentFacts      # enrolled_at, baseline, age band, sex, pregnancy flag (from plan), etc.

@dataclass(frozen=True)
class Evaluation:
    tier: Literal["stable", "watch", "urgent"]
    reasons: tuple[Reason, ...]      # code + values + window — rendered as "why" on screen
    proposed: tuple[str, ...]        # action codes only (ADR-CC-04 list)
    safety_card: Optional[str]       # fixed card key, sent immediately by the caller
    next_check: Optional[date]

def evaluate(params: Mapping, inputs: Inputs) -> Evaluation: ...
```

Combination rules: the tier is the **maximum** over all rules; reasons are all rules that fired; a reason that
already has an open case is still recorded (the case gets an event, not a duplicate — ADR-CC-14); a `cooldown_days`
per reason stops a stable patient being re-flagged daily for the same old value.

Rule primitives (the only ones a pack may use — reviewed once, reused everywhere):
`any_below(measure, x, days)`, `any_above(measure, x, days)`, `count_above(measure, x, days) >= n`,
`k_of_last_n(measure, pred, k, n)`, `mean_over(measure, days) >= x` (needs ≥ m readings), `delta_from_baseline(measure) >= x`,
`days_since(measure or lab) > d`, `missed_doses(days) >= n`, `item_answer(code, value)`, `unanswered_prompts >= n`,
`due_date_passed(plan_item_kind, grace_days)`.

## 2. Parameter rules

Each `PARAMS` entry: `default`, `min`, `max`, `unit`, `source`. The activation form shows the source next to every
number. Values outside `[min, max]` → 422. A change of any parameter = a new activation row signed again.

## 3. Core packs (P2)

### 3.1 `t2dm` — Type 2 diabetes (pilot pack, deck p.12)
Measures: fasting glucose (`fbg`, typed or voice, mg/dL), optional post-meal (`ppbg`), HbA1c (`hba1c`, from LIS),
adherence (from the plan's medication items).

| Rule | Default | Bounds | Tier | Proposed | Card |
|:-|:-|:-|:-|:-|:-|
| `hypo` any glucose < `hypo_mgdl` | 70 | 54–80 | watch (urgent if < 54 or ≥ 2 in 7 d) | `call_patient_now` | `hypo_now` (rule of 15, emergency number) |
| `very_high` any glucose ≥ `very_high_mgdl` | 400 | 300–450 | urgent | `call_patient_now` | `high_sugar_symptoms` |
| `high_x2` glucose ≥ `high_mgdl` twice in 7 d (deck: "two high readings") | 250 | 180–300 | watch | `recheck_in_days`, `book_review_visit` | — |
| `fasting_above_target` `fbg > fbg_target_max` in ≥ 3 of last 5 | 130 | 110–160 | watch | `book_review_visit` (≤ 14 d) | — |
| `a1c_above_target` latest HbA1c ≥ `a1c_target` | 7.0 % | 6.5–8.5 | watch | `book_review_visit` | — |
| `a1c_very_high` latest HbA1c ≥ `a1c_high` | 9.0 % | 8.5–10.0 | watch (priority +) | `book_review_visit` (≤ 7 d) | — |
| `a1c_overdue` days since HbA1c > `a1c_interval` (at target ×2) | 100 | 80–200 | watch | `order_lab` / `book_home_sample` | `lab_due` |
| `missed_doses` (deck: "three missed doses") ≥ `miss_n` in 7 d | 3 | 2–5 | watch | `call_patient_now` | — |
| `eye_screen_due` no eye review in 365 d | 365 | 180–730 | routine | `refer` (ophthalmology / eye-plan partner) | `eye_check_due` |
Sources: ADA Standards of Care 2025 §6 (hypoglycaemia levels < 70 and < 54 mg/dL; individualised A1C ~7 %);
RSSDI–ESI Clinical Practice Recommendations for T2DM 2022; ICMR Guidelines for Management of T2DM 2018.

### 3.2 `htn` — Hypertension (IHCI-style)
Measures: home BP (`bp` sys/dia), adherence.

| Rule | Default | Bounds | Tier | Proposed | Card |
|:-|:-|:-|:-|:-|:-|
| `crisis_range` any sys ≥ `crisis_sys` or dia ≥ `crisis_dia` | 180 / 110 | 160–180 / 100–110 | urgent | `call_patient_now` | `bp_very_high_symptoms` (chest pain, breathlessness, one-sided weakness, severe headache → emergency number) |
| `uncontrolled` 7-day mean ≥ target (≥ 4 readings) | 140 / 90 | 130–140 / 80–90 | watch | `book_review_visit` + doctor sees "protocol step due: <clinic text>" | — |
| `low_bp` sys < `low_sys` | 90 | 85–100 | watch | `call_patient_now` | `low_bp_dizzy` |
| `review_overdue` days since checked-in visit > `review_days` | 35 | 28–90 | watch | `book_review_visit` | `visit_due` |
| `missed_doses` | 3 / 7 d | 2–5 | watch | `call_patient_now` | — |
The clinic types its own step protocol as text (e.g. the state IHCI ladder: amlodipine 5 → 10 mg → add telmisartan
40 → 80 mg → add chlorthalidone); the engine shows only "step n due" to the doctor. Sources: India Hypertension
Control Initiative (WHO SEARO 2021); IHCI outcomes PMC10328822; protocol step-up PMC10959138; ISH 2020 guideline.

### 3.3 `thyroid` — Hypothyroidism on levothyroxine
| Rule | Default | Bounds | Tier | Proposed |
|:-|:-|:-|:-|:-|
| `tsh_after_change` a new signed Rx changed the levothyroxine line text; TSH not done within `post_change_days` | 49 | 42–70 | watch | `order_lab` |
| `tsh_overdue` stable: days since TSH > `stable_interval` | 365 | 180–400 | routine | `order_lab` |
| `tsh_out_of_range` latest TSH flag `high`/`low` (lab's own range) | — | — | watch | `book_review_visit` |
Card: `empty_stomach_timing` (fixed text approved by the clinic). Sources: ATA Guidelines for Hypothyroidism 2014.

### 3.4 `phq9` / `gad7` — Depression and anxiety (MANAS / TEAMcare model: a care manager + timely treatment change)
Questions are sent as buttons (0–3 each); the instrument text must be a validated translation, or one approved by
the clinic's psychiatrist when none exists (owner decision CC-O-9).

| Rule | Default | Bounds | Tier | Proposed | Card |
|:-|:-|:-|:-|:-|:-|
| `self_harm` PHQ-9 item 9 ≥ 1 | — | fixed | **urgent, immediate** | `call_patient_now` | `crisis_support` (Tele-MANAS 14416, hospital number) |
| `severe` total ≥ `severe_cut` | 20 | 15–20 | urgent | `call_patient_now`, `book_review_visit` | — |
| `worsening` total − baseline ≥ `worse_delta` | 5 | 3–7 | watch | `book_review_visit` | — |
| `not_improving` ≥ `weeks_check` weeks enrolled and total ≥ 10 and < 50 % drop | 8 wk | 6–12 | watch | `book_review_visit` (treatment adjustment is the doctor's) | — |
| `due` days since last PHQ-9 > `interval_days` | 14 | 14–28 | routine | — (prompt) | — |
GAD-7 uses the same shape (bands 5/10/15). Sources: Kroenke et al. 2001 (PHQ-9); Spitzer et al. 2006 (GAD-7);
MANAS (PMC3227809); TEAMcare (PMC3726010); National Mental Health Survey 2016; Tele-MANAS (MoHFW).

### 3.5 `post_discharge` — 30 days after an IPD discharge
Items: day-2 card or call (fixed symptom list: fever, wound redness/discharge, breathlessness, uncontrolled pain,
reduced urine), day-7 review visit (from `cc_discharge_followups` or the default), discharge-medication adherence
for 30 days. Any "yes" on a danger symptom → urgent + `post_op_danger` card. Missed day-7 visit → watch.

## 4. Safety card catalogue (fixed texts, en/hi/te, approved per clinic)
| Key | Trigger | Content outline |
|:-|:-|:-|
| `hypo_now` | glucose < threshold | Take 15 g fast sugar (3 tsp sugar in water / glucose tablets), recheck in 15 min, repeat once; if confused or unconscious call <emergency>; do not drive |
| `high_sugar_symptoms` | very high glucose | If vomiting, very thirsty, drowsy, breathless → <emergency> now; else the care team will call |
| `bp_very_high_symptoms` | crisis range | Chest pain, breathlessness, weakness one side, severe headache, blurred vision → <emergency> now; else rest 5 min and send again |
| `crisis_support` | self-harm signal | You are not alone; Tele-MANAS 14416 (24×7, free); <hospital number>; if in danger now call 112 |
| `post_op_danger`, `eye_post_op_danger`, `anc_danger_signs`, `ohss_danger`, `vaccine_fever` | pack-specific | fixed lists in §6 |
Cards are code-selected; the text is a clinic-approved row keyed by `(card_key, language, version)`. A card with no
approved text for the clinic falls back to the Kriya standard text **only** for `crisis_support` and `hypo_now`
(owner-approved defaults); every other card blocks the pack's activation until approved.

## 5. Cross-pack triggers (what makes Kriya different from a reminder app)
| From | Condition | To |
|:-|:-|:-|
| `t2dm` | no eye review in 12 months | `eye_dr_screen` referral (own department, or an eye-plan clinic the hospital partners with) |
| `t2dm`, `htn` | PHQ-2 ≥ 3 on the quarterly two-question screen | offer `phq9` enrollment (doctor approves) |
| `ivf_cycle` | positive beta-hCG validated | offer `anc` enrollment |
| `anc` | delivery recorded | offer `postnatal` + `child_immunisation` for the newborn (guardian consent) |
| `derma_isotretinoin` | PHQ-2 positive | urgent to the dermatologist + offer psychiatry referral |
| any | 3 unanswered prompts | switch channel (WhatsApp → voice) once, then `not_responding` case |

## 6. Specialty packs (P9) — detail and regulatory notes in docs/specialty-os
| Pack | Plan | Core items | Safety card |
|:-|:-|:-|:-|
| `derma_isotretinoin` | derma | monthly review; LFT + lipid at baseline and per dermatologist (default 30 d, 30–90); for women who can become pregnant: monthly pregnancy test due + contraception reminder text approved by the clinic; monthly PHQ-2 | `mood_change` |
| `derma_methotrexate` | derma | CBC/LFT/creatinine on the doctor's schedule (default every 28 d × 3 then 84 d); dose shown **verbatim** (weekly dosing is a custom frequency → display-only; the engine never prompts "daily") ; LIS flag on ALT high / WBC low → urgent | `mtx_warning_signs` (mouth ulcers, fever, bleeding) |
| `derma_procedure_series` | derma | session n of N due every k weeks (laser, PRP, peels) from the treatment's doctor-set interval; aftercare text from the catalogue's `post_instructions` | — |
| `eye_post_cataract` | eye | visits day 1 / week 1 / month 1 (params); drops verbatim (tapers display-only) | `eye_post_op_danger` (pain, vision drop, increasing redness → same day) |
| `eye_anti_vegf` | eye | injection series dates entered by the doctor; missed date → watch (priority +) | `sudden_vision_change` |
| `eye_glaucoma` | eye | drop adherence (OD/BD drops only), IOP review every 3–6 months | — |
| `eye_dr_screen` | eye, any | annual fundus exam for diabetics | — |
| `dental_recall` | dental | hygiene recall 6 months (3–12) | — |
| `dental_ortho` | dental | adjustment every 4–6 weeks; aligner-change reminder at the orthodontist's interval; retainer check after debond | — |
| `dental_implant_review` | dental | suture review 7–10 d, 3-month review, annual | `post_extraction_care` |
| `ivf_cycle` | ivf | scan dates and trigger **time exactly as entered by the doctor** (acknowledge button; not acknowledged in 2 h → coordinator call); injections verbatim; beta-hCG due = transfer + 14 d (9–16) | `ohss_danger` (abdominal swelling, breathlessness, low urine) |
| `ivf_cryo_renewal` | ivf | storage renewal and consent re-confirmation before expiry (ART Act 2021 storage limits) | — |
| `anc` | womenchild, multispecialty | EDD as entered by the doctor; ANC contacts per clinic schedule (MoHFW ≥ 4 / WHO 2016 eight-contact model); dating/NT 11–14 w, anomaly 18–22 w (PCPNDT Form F applies, existing radiology); GDM test 24–28 w (MoHFW 2018 75 g OGTT); home BP ≥ 140/90 → urgent | `anc_danger_signs` (bleeding, severe headache/blurred vision, fits, fever, reduced fetal movement after 28 w, leaking fluid, severe abdominal pain) |
| `postnatal` | womenchild | PNC contacts 48 h, day 3, day 7, 6 weeks; PHQ-9/EPDS screen | `postnatal_danger` |
| `child_immunisation` | womenchild, any with paediatrics | schedule from DOB — clinic chooses IAP 2023 or national UIP; due / overdue reminders; catch-up only by the doctor; vaccine given recorded by staff (brand, batch) | `vaccine_fever` |
| `child_growth` | womenchild | weight / length / head circumference → WHO z-scores computed in code (WHO LMS tables); < −2 SD → watch | — |
| `dx_periodic` (Care Lite) | diagstream, diagbooking | test due from last result + interval; home-sample button | — |
| `rx_adherence` (Care Lite) | pharmacy | existing refill plans + adherence card | — |

## 7. Pack acceptance checklist (every pack, before VERIFIED)
1. Every parameter has default, bounds, unit and a source citation; doctor-facing form renders them.
2. Golden vectors: ≥ 3 per rule (fires / does not fire / boundary) in `tests/cc_vectors/<pack>.json`.
3. Every card key has approved en/hi/te text in the test fixture.
4. No rule reads a free-text field except an exact match on a signed line for "changed since" checks.
5. Pack evaluation is deterministic: same inputs hash → same output (property test).
