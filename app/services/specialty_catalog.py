"""Starter treatment lists for specialty plans (migration 077).

Seeded HIDDEN (is_active=false) when a specialty clinic is onboarded, or when
an admin clicks "Load starter treatments". Nothing here reaches a patient until
the clinic's admin reviews the row and chooses to show it.

Copy rules (tests/test_specialty_catalog.py enforces them):
  * exactly two lines per description — what it is, then what to expect;
  * no promises (painless, guaranteed, permanent, cure, success rate, best …);
  * no medicine names or doses; no sex-selection or gender language;
  * list titles (short_name or name) of 24 characters or fewer.
"""

import logging
from typing import Optional

from app.database import sb, supabase

logger = logging.getLogger(__name__)


#: Care pathway per starter treatment (migration 081). Anything not listed is
#: 'direct' -- the patient may ask for it by name, which is how every row
#: behaved before this column existed.
#:
#:   entry            the first visit; where "I'm not sure what I need" leads
#:   assessment_first the doctor decides this AFTER examining the patient, so
#:                    the card is information and its button books an exam
#:
#: Kept in step with the backfill list in migrations/081_treatment_care_pathway.sql
#: -- tests/test_treatment_care_pathway.py fails if the two drift apart.
#:
#: Dermatology is deliberately absent: a derma patient arrives already saying
#: "acne" or "hair fall", which is why that plan works as it does.
_PATHWAY_BY_NAME: dict[str, str] = {
    # Ophthalmology -- the examination is the product; surgery follows it.
    "Comprehensive Eye Check-up": "entry",
    "Cataract Surgery": "assessment_first",
    "Anti-VEGF Injection": "assessment_first",
    # Dental -- check-up first; RCT, extraction or a crown is the dentist's call.
    "Dental Check-up": "entry",
    "Root Canal Treatment": "assessment_first",
    "Crowns & Bridges": "assessment_first",
    "Dentures": "assessment_first",
    "Clear Aligners": "assessment_first",
    "Veneers & Smile Design": "assessment_first",
    "Wisdom Tooth Removal": "assessment_first",
    # Fertility -- nobody books an IVF cycle off a menu.
    "Fertility Consultation": "entry",
    "IUI (Intrauterine Insemination)": "assessment_first",
    "IVF (In Vitro Fertilisation)": "assessment_first",
    "ICSI": "assessment_first",
    "Frozen Embryo Transfer": "assessment_first",
    "Egg Freezing": "assessment_first",
    "Hysteroscopy": "assessment_first",
    "Laparoscopy for Fertility": "assessment_first",
    # Dermatology -- the one row a dermatologist orders, never a patient.
    "Skin Biopsy": "assessment_first",
    # ── First shipped with migration 082 (no rows existed to backfill) ──
    # Child Care -- a parent describes the child, the paediatrician decides.
    "Paediatric Consultation": "entry",
    "Newborn Intensive Care (NICU)": "assessment_first",
    # Women Care -- how a baby is delivered is the obstetrician's call.
    "Gynaecology Consultation": "entry",
    "Childbirth & Delivery Care": "assessment_first",
    "Labour Pain Relief (Epidural)": "assessment_first",
    "Birth After Caesarean (VBAC)": "assessment_first",
    "Gynaecological Laparoscopy": "assessment_first",
    "Gynaecological Hysteroscopy": "assessment_first",
}

#: Names above that first shipped after migration 081, so its backfill never
#: listed them: no clinic can have held a starter row by these names before.
#: tests/test_treatment_care_pathway.py uses this to keep the parity check
#: strict for every name 081 did list.
PATHWAYS_ADDED_AFTER_081: frozenset[str] = frozenset({
    "Paediatric Consultation",
    "Newborn Intensive Care (NICU)",
    "Gynaecology Consultation",
    "Childbirth & Delivery Care",
    "Labour Pain Relief (Epidural)",
    "Birth After Caesarean (VBAC)",
    "Gynaecological Laparoscopy",
    "Gynaecological Hysteroscopy",
})


#: Service lines (migration 082): the sections a hospital files its treatments
#: under -- "Child Care", "Women Care", "Fertility Care". Slug -> emoji and
#: patient-facing label per language. The slugs are the CHECK constraint in
#: migrations/082_women_child_plan.sql; tests fail if the two drift.
SERVICE_LINES: dict[str, dict[str, str]] = {
    "child_care": {"emoji": "👶", "en": "Child Care", "hi": "शिशु व बाल देखभाल", "te": "పిల్లల సంరక్షణ"},
    "women_care": {"emoji": "🌸", "en": "Women Care", "hi": "महिला स्वास्थ्य", "te": "మహిళల ఆరోగ్యం"},
    "fertility_care": {"emoji": "🌱", "en": "Fertility Care", "hi": "प्रजनन देखभाल", "te": "సంతాన సాఫల్యం"},
    "skin_hair": {"emoji": "✨", "en": "Skin & Hair", "hi": "त्वचा और बाल", "te": "చర్మం & జుట్టు"},
    "eye_care": {"emoji": "👁️", "en": "Eye Care", "hi": "नेत्र देखभाल", "te": "కంటి సంరక్షణ"},
    "dental_care": {"emoji": "🦷", "en": "Dental Care", "hi": "दंत चिकित्सा", "te": "దంత సంరక్షణ"},
}

#: Which service line each starter list files its rows under.
STARTER_SERVICE_LINE: dict[str, str] = {
    "pediatrics": "child_care",
    "womens_health": "women_care",
    "fertility": "fertility_care",
    "dermatology": "skin_hair",
    "ophthalmology": "eye_care",
    "dental": "dental_care",
}

#: Hybrid plans whose starter lists are known in advance, seeded (hidden) at
#: onboarding. multispecialty is deliberately absent: which specialties a
#: general hospital runs is only known once its admin picks them.
STARTER_LISTS_BY_PLAN: dict[str, tuple[str, ...]] = {
    "womenchild": ("pediatrics", "womens_health", "fertility"),
}


def _t(category, name, description, concerns, duration_minutes=None, prep=None, short_name=None):
    return {
        "category": category,
        "name": name,
        "short_name": short_name,
        "description": description,
        "concerns": concerns,
        "duration_minutes": duration_minutes,
        "prep_instructions": prep,
        # Looked up rather than passed at each call site: the lookup table reads
        # as one reviewable list beside the SQL backfill it must match, where
        # twenty scattered keyword arguments would not.
        "care_pathway": _PATHWAY_BY_NAME.get(name, "direct"),
    }


STARTER_TREATMENTS: dict[str, list[dict]] = {
    "dermatology": [
        _t("Acne & Scars", "Acne Treatment",
           "Doctor-guided care for active pimples, blackheads and breakouts.\n"
           "Your dermatologist checks your skin type and plans treatment step by step.",
           "acne, pimples, breakouts, blackheads, whiteheads, oily skin", 20,
           "Come with a clean face, without makeup. Bring any creams or tablets you currently use."),
        _t("Acne & Scars", "Acne Scar Treatment",
           "Procedures such as microneedling, subcision or laser to improve the look of acne scars.\n"
           "The doctor examines your scars first and suggests the option that suits your skin.",
           "acne scars, pits, marks, uneven skin texture, holes on face", 45,
           "Avoid sun exposure, bleaching and facial waxing for one week before your visit."),
        _t("Pigmentation", "Chemical Peel",
           "A controlled peel that removes the dull outer skin layer to help with tan, dark spots and uneven tone.\n"
           "Sessions are planned after the doctor checks how sensitive your skin is.",
           "tan, dark spots, dull skin, uneven skin tone, pigmentation, open pores", 30,
           "Stop exfoliating creams 5 days before. Avoid facial waxing and threading for one week."),
        _t("Pigmentation", "Pigmentation & Melasma Care",
           "Assessment and a treatment plan for melasma, freckles and patchy skin darkening.\n"
           "It may combine creams, peels or laser, as decided by your dermatologist.",
           "melasma, pigmentation, dark patches, freckles, dark spots, blemishes", 30,
           "Bring a list of the creams you have used on your face in the last 3 months.",
           short_name="Pigmentation Care"),
        _t("Skin Conditions", "Psoriasis & Eczema Care",
           "Evaluation and long-term management for itchy, scaly or inflamed skin conditions.\n"
           "The dermatologist reviews triggers and plans care you can follow at home.",
           "psoriasis, eczema, itching, scaly skin, dry patches, dermatitis, rashes", 20,
           "Bring previous prescriptions, and photos of flare-ups if the rash is not visible today.",
           short_name="Psoriasis & Eczema"),
        _t("Skin Conditions", "Vitiligo Care",
           "Evaluation of white patches and a treatment plan that may include phototherapy.\n"
           "Your doctor explains the options and the follow-up schedule.",
           "white patches, vitiligo, leucoderma, loss of skin colour", 20,
           "Bring any previous test reports and prescriptions."),
        _t("Skin Conditions", "Urticaria & Skin Allergy",
           "Assessment for hives, recurring rashes and skin allergies to find possible triggers.\n"
           "The doctor may advise tests before planning treatment.",
           "hives, urticaria, allergy, rashes, itching, swelling of skin", 20,
           "Note down foods, products or tablets you used before the rash appeared.",
           short_name="Skin Allergy Care"),
        _t("Hair & Scalp", "Hair Fall Evaluation",
           "Scalp examination to understand the cause of hair fall or thinning.\n"
           "The doctor may suggest blood tests before planning treatment.",
           "hair fall, hair loss, thinning hair, baldness, bald patches, dandruff", 30,
           "Wash your hair the day before. Do not apply oil, gel or hair colour on the day of your visit."),
        _t("Hair & Scalp", "Hair PRP Therapy",
           "Platelet-rich plasma prepared from your own blood is injected into the scalp to support hair growth.\n"
           "Usually done as a series of sessions after the doctor confirms you are suitable.",
           "hair fall, thinning hair, prp, hair regrowth, receding hairline", 60,
           "Eat a light meal before the session. Tell the doctor if you take blood thinners. Wash hair the day before."),
        _t("Hair & Scalp", "GFC Hair Therapy",
           "Growth factor concentrate prepared from your blood is applied to thinning areas of the scalp.\n"
           "The doctor decides the number of sessions after examining your scalp.",
           "hair fall, thinning hair, gfc, hair regrowth", 60,
           "Eat a light meal before the session. Tell the doctor if you take blood thinners. Wash hair the day before."),
        _t("Hair & Scalp", "Hair Transplant Consultation",
           "Consultation to assess donor hair, your hair loss pattern and whether a transplant suits you.\n"
           "The surgeon explains the procedure, recovery and next steps.",
           "baldness, hair transplant, receding hairline, fue", 30,
           "Bring photos of your hairline from earlier years if available.",
           short_name="Hair Transplant Consult"),
        _t("Laser & Aesthetic", "Laser Hair Reduction",
           "Laser sessions that reduce unwanted facial or body hair over time.\n"
           "The number of sessions depends on hair type and area, as assessed by the doctor.",
           "unwanted hair, facial hair, body hair, laser hair removal, pcos hair", 45,
           "Shave the area 1 day before. Do not wax, thread or bleach for 4 weeks before. Avoid sun tanning."),
        _t("Skin Surgery", "Wart, Mole & Skin Tag Removal",
           "Removal of warts, skin tags or moles using cautery, laser or minor surgery.\n"
           "The doctor examines the growth first and may advise a biopsy if needed.",
           "warts, skin tags, moles, growths on skin, corn", 30,
           "Tell the doctor if you take blood thinners or have diabetes.",
           short_name="Wart & Skin Tag Removal"),
        _t("Skin Surgery", "Keloid Treatment",
           "Treatment for raised, thick scars using injections, pressure or other methods.\n"
           "Planned after the doctor examines the scar.",
           "keloid, raised scar, thick scar, hypertrophic scar", 30,
           "Bring details of when the scar formed and any earlier treatment."),
        _t("Skin Surgery", "Skin Biopsy",
           "A small skin sample taken under local anaesthesia to help confirm a diagnosis.\n"
           "The doctor explains why it is needed and when the report will be ready.",
           "biopsy, suspicious mole, non-healing wound, skin growth", 30,
           "Tell the doctor about blood thinners or any allergy to anaesthesia."),
    ],
    "ophthalmology": [
        _t("Eye Check-up", "Comprehensive Eye Check-up",
           "Complete examination of vision, eye pressure and eye health by an eye specialist.\n"
           "Your pupils may be dilated, so vision can stay blurred for a few hours.",
           "blurred vision, eye check, spectacle power, headache, eye strain, eye test", 60,
           "Bring your current glasses and old prescriptions. Do not drive yourself if your pupils will be dilated.",
           short_name="Eye Check-up"),
        _t("Cataract", "Cataract Evaluation",
           "Examination to check whether cataract is affecting your vision and whether surgery is needed.\n"
           "The doctor explains lens options and measurements before any decision.",
           "cloudy vision, blurred vision, glare, night driving difficulty, cataract, faded colours", 60,
           "Bring current glasses, sugar and BP reports and your medicine list. Come with a companion; pupils will be dilated."),
        _t("Cataract", "Cataract Surgery",
           "Removal of the cloudy natural lens and placement of an artificial lens (IOL).\n"
           "Planned after a detailed evaluation and lens measurement by your surgeon.",
           "cataract, cloudy vision, cataract operation, lens replacement", 60,
           "A pre-surgery evaluation is needed first. Bring blood sugar and BP reports and your medicine list."),
        _t("Specs Removal", "LASIK / Specs Removal Evaluation",
           "Detailed corneal tests to check whether LASIK, SMILE or ICL is suitable for removing glasses.\n"
           "The doctor explains which option fits your eyes after the tests.",
           "remove glasses, specs removal, lasik, smile, contoura, icl, high power, contact lens", 90,
           "Stop soft contact lenses 7 days before and hard lenses 3 weeks before. Come with a companion.",
           short_name="Specs Removal Check"),
        _t("Retina", "Diabetic Retina Screening",
           "Retina examination for people with diabetes to detect changes early.\n"
           "Recommended regularly, even when vision seems normal.",
           "diabetes, sugar, diabetic retinopathy, retina check, floaters", 45,
           "Bring your latest blood sugar or HbA1c report. Pupils will be dilated; avoid driving back.",
           short_name="Diabetic Eye Screening"),
        _t("Retina", "Retina Consultation",
           "Evaluation for floaters, flashes of light, sudden vision loss or macular problems.\n"
           "The specialist may advise scans such as OCT before planning treatment.",
           "floaters, flashes of light, retina, macular degeneration, distorted vision, retinal detachment", 45,
           "Sudden loss of vision or a curtain over your vision needs same-day care: call the hospital directly."),
        _t("Retina", "Anti-VEGF Injection",
           "An eye injection given for certain retina conditions such as wet macular degeneration.\n"
           "Given only after the retina specialist confirms it is needed.",
           "anti vegf, eye injection, macular oedema, wet amd", 60,
           "Tell the doctor about any eye redness or infection. Bring previous scan reports."),
        _t("Glaucoma", "Glaucoma Evaluation",
           "Eye pressure measurement, optic nerve check and visual field test for glaucoma.\n"
           "Early detection helps protect vision over time.",
           "glaucoma, eye pressure, family history of glaucoma, side vision loss", 60,
           "Bring the eye drops you currently use and previous field test reports."),
        _t("Cornea", "Keratoconus & Cornea Care",
           "Evaluation of corneal conditions such as keratoconus, corneal infections or scars.\n"
           "Treatment such as C3R or special lenses is planned after tests.",
           "keratoconus, cornea, frequently changing power, corneal ulcer, c3r", 45,
           "Stop contact lenses 3 days before unless your doctor advised otherwise.",
           short_name="Cornea Care"),
        _t("Kids & Squint", "Squint & Lazy Eye Care",
           "Eye examination for children and adults with squint, lazy eye or eye alignment problems.\n"
           "The doctor explains glasses, patching, exercises or surgery as needed.",
           "squint, crossed eyes, lazy eye, amblyopia, child eye check", 45,
           "Bring previous glasses and prescriptions. Pupils may be dilated.",
           short_name="Squint & Lazy Eye"),
        _t("Dry Eye & Eyelids", "Dry Eye Treatment",
           "Assessment for burning, gritty or watery eyes and a plan to manage dry eye.\n"
           "It may include lubricating care, eyelid care or in-clinic procedures.",
           "dry eyes, burning eyes, watering, itchy eyes, screen strain, redness", 45,
           "Avoid eye makeup on the day of your visit."),
        _t("Dry Eye & Eyelids", "Eyelid & Oculoplasty Consultation",
           "Consultation for drooping eyelids, eyelid lumps or watering due to blocked tear ducts.\n"
           "The surgeon explains the options after examination.",
           "ptosis, droopy eyelid, eyelid swelling, chalazion, stye, blocked tear duct", 30,
           "Bring older photos if the eyelid droop has increased over time.",
           short_name="Eyelid Surgery Consult"),
    ],
    "dental": [
        _t("Check-up & Cleaning", "Dental Check-up",
           "Full examination of teeth and gums, with an X-ray if needed.\n"
           "The dentist explains the findings and a treatment plan with costs before starting.",
           "dental check up, tooth check, cavity, bad breath", 20,
           "Brush before your visit. Bring previous dental X-rays if you have them."),
        _t("Check-up & Cleaning", "Scaling & Polishing",
           "Professional cleaning to remove tartar and stains from the teeth.\n"
           "Usually completed in one visit.",
           "tartar, yellow teeth, stains, bleeding gums, bad breath, teeth cleaning", 45,
           "Tell the dentist if you take blood thinners.",
           short_name="Teeth Cleaning"),
        _t("Tooth Pain & Root Canal", "Root Canal Treatment",
           "Treatment that removes infected pulp from inside a tooth and seals it, so the natural tooth can be kept.\n"
           "Done under local anaesthesia; some teeth need more than one sitting.",
           "tooth pain, toothache, sensitivity, swelling, infected tooth, root canal, rct", 60,
           "Eat a light meal before. Bring any dental X-ray of the painful tooth.",
           short_name="Root Canal (RCT)"),
        _t("Tooth Pain & Root Canal", "Tooth Filling",
           "Tooth-coloured fillings for cavities and small chips.\n"
           "The dentist checks how deep the cavity is before filling.",
           "cavity, hole in tooth, food getting stuck, sensitivity, chipped tooth", 30),
        _t("Implants & Missing Teeth", "Crowns & Bridges",
           "Caps to protect weak or root-canal-treated teeth, and bridges to replace a missing tooth.\n"
           "Material options such as zirconia or metal-ceramic are explained with costs.",
           "crown, cap, bridge, broken tooth, missing tooth", 45),
        _t("Implants & Missing Teeth", "Dental Implant Consultation",
           "Assessment of bone and gums to check whether an implant can replace a missing tooth.\n"
           "A 3D scan (CBCT) may be needed before planning.",
           "missing tooth, implant, gap in teeth, loose denture", 30,
           "Tell the dentist about diabetes, smoking and any blood thinners.",
           short_name="Dental Implant Consult"),
        _t("Implants & Missing Teeth", "Dentures",
           "Complete or partial removable dentures to replace missing teeth.\n"
           "Made over a few visits with measurements and trials.",
           "dentures, false teeth, missing teeth, loose denture", 30),
        _t("Braces & Aligners", "Braces Consultation",
           "Orthodontic assessment for crooked, gapped or forward teeth.\n"
           "The orthodontist explains metal, ceramic and aligner options.",
           "crooked teeth, gaps, braces, forward teeth, overbite, misaligned teeth", 30,
           "Bring previous dental X-rays if you have them."),
        _t("Braces & Aligners", "Clear Aligners",
           "Removable transparent trays that move teeth gradually.\n"
           "Suitability and duration are decided after scans and records.",
           "invisible braces, aligners, crooked teeth, gaps", 45),
        _t("Smile Design", "Teeth Whitening",
           "In-clinic whitening to lighten tooth stains.\n"
           "The dentist first checks for sensitivity, cavities or gum problems.",
           "yellow teeth, stains, whitening, discoloured teeth", 60,
           "A cleaning may be needed first."),
        _t("Smile Design", "Veneers & Smile Design",
           "Thin shells or bonding to improve the shape, colour or gaps of front teeth.\n"
           "Planned with you after a smile assessment.",
           "veneers, smile makeover, chipped teeth, gaps, uneven teeth", 45),
        _t("Gums", "Gum Treatment",
           "Deep cleaning and gum care for bleeding, swollen or receding gums.\n"
           "The dentist measures gum pockets before planning.",
           "bleeding gums, swollen gums, loose teeth, gum disease, receding gums, pyorrhea", 45,
           "Tell the dentist about diabetes or smoking."),
        _t("Oral Surgery", "Wisdom Tooth Removal",
           "Removal of a painful or impacted wisdom tooth under local anaesthesia.\n"
           "An X-ray is taken first to plan the procedure.",
           "wisdom tooth, third molar, jaw pain, swelling behind teeth, impacted tooth", 60,
           "Eat before the procedure. Tell the dentist about blood thinners, diabetes or BP."),
        _t("Kids Dentistry", "Kids Dental Care",
           "Check-ups, cavity care and preventive care such as fluoride and sealants for children.\n"
           "A gentle first visit helps children get comfortable.",
           "child tooth pain, milk teeth, cavities in kids, kids dentist, fluoride", 30,
           "Bring the child's previous dental records if any."),
    ],
    "fertility": [
        _t("Fertility Consultation", "Fertility Consultation",
           "A detailed discussion of your history with a fertility specialist to plan the right tests.\n"
           "Both partners are encouraged to attend.",
           "trying to conceive, not getting pregnant, infertility, delay in pregnancy, fertility", 45,
           "Bring all previous reports, scans and treatment records for both partners."),
        _t("Tests & Evaluation", "Female Fertility Evaluation",
           "Tests such as AMH, hormone profile and ultrasound to assess egg reserve and the uterus.\n"
           "Some tests are timed to specific days of the menstrual cycle.",
           "amh, egg reserve, irregular periods, pcos, pcod, hormone test, follicle scan", 30,
           "Note the first day of your last period; the team will tell you which cycle day to come.",
           short_name="Female Fertility Tests"),
        _t("Male Fertility", "Semen Analysis",
           "A laboratory test of semen to check sperm count, movement and shape.\n"
           "Done privately at the centre.",
           "low sperm count, semen test, male infertility, sperm analysis", 30,
           "Abstain from ejaculation for 2 to 5 days before the test. Tell the team about any recent fever."),
        _t("Tests & Evaluation", "Follicular Monitoring",
           "A series of ultrasound scans to track egg growth and ovulation.\n"
           "Helps time natural conception or IUI.",
           "ovulation, follicle scan, egg growth, timing", 20,
           "Start on the cycle day advised by your doctor.",
           short_name="Follicle Monitoring"),
        _t("Treatments", "IUI (Intrauterine Insemination)",
           "Washed sperm is placed directly into the uterus around the time of ovulation.\n"
           "Suitability is decided after tests of both partners.",
           "iui, insemination, mild male factor, unexplained infertility", 30,
           "Your doctor will schedule the day based on follicle scans.",
           short_name="IUI"),
        _t("Treatments", "IVF (In Vitro Fertilisation)",
           "Eggs and sperm are combined in the lab and a resulting embryo is placed in the uterus.\n"
           "The team explains every step, the timeline and costs after your evaluation.",
           "ivf, test tube baby, blocked tubes, failed iui, infertility", 45,
           "A consultation and tests are needed before starting a cycle.",
           short_name="IVF"),
        _t("Treatments", "ICSI",
           "A single sperm is injected into each egg in the lab, often advised for male factor infertility.\n"
           "Decided by your specialist after reviewing reports.",
           "icsi, low sperm count, poor sperm quality, failed fertilisation", 45,
           "A consultation and tests are needed before starting a cycle."),
        _t("Treatments", "Frozen Embryo Transfer",
           "Transfer of a previously frozen embryo into the prepared uterus.\n"
           "Timing is planned with scans and your cycle.",
           "fet, frozen embryo, embryo transfer", 30,
           "Follow the preparation schedule given by your doctor."),
        _t("Fertility Preservation", "Egg Freezing",
           "Eggs are collected and frozen to preserve fertility for the future.\n"
           "The specialist explains suitability based on age and egg reserve.",
           "egg freezing, delay pregnancy, fertility preservation, oocyte freezing", 45,
           "Bring any AMH report if already done."),
        _t("Fertility Preservation", "Sperm Freezing",
           "Semen samples are frozen and stored for future use.\n"
           "Often advised before cancer treatment or long work travel.",
           "sperm freezing, sperm banking, before chemotherapy", 30,
           "Abstain from ejaculation for 2 to 5 days before."),
        _t("Surgery", "Hysteroscopy",
           "A thin camera is used to examine the inside of the uterus and treat issues such as polyps.\n"
           "Advised by your doctor after scans.",
           "hysteroscopy, uterine polyp, septum, repeated ivf failure, endometrium", 60,
           "Usually planned soon after your period ends. The team will share fasting instructions."),
        _t("Surgery", "Laparoscopy for Fertility",
           "Keyhole surgery to check the tubes and treat conditions such as endometriosis or cysts.\n"
           "Planned after evaluation, with the team explaining recovery.",
           "laparoscopy, endometriosis, blocked tubes, ovarian cyst, fibroids", 60,
           "The team will share fasting and pre-surgery test instructions.",
           short_name="Fertility Laparoscopy"),
    ],
    # ── Women & Child hospitals (migration 082) ─────────────────────────────
    # Parents describe the child, not a sub-specialty, so the first row is the
    # paediatrician (entry). The specialist rows stay 'direct': a parent who
    # was referred to a child heart specialist can ask for one by name, and
    # the booking is still a consultation with that specialist.
    "pediatrics": [
        _t("Child Consultations", "Paediatric Consultation",
           "A consultation with a paediatrician for fever, cough, feeding or any worry about your child.\n"
           "The doctor examines your child and explains the next steps, including any tests.",
           "fever in child, child cough, cold, vomiting, loose motions, not eating, child sick, baby unwell, "
           "paediatrician, pediatrician, kids doctor, child specialist", 20,
           "Bring the child's previous prescriptions, reports and vaccination card.",
           short_name="Child Consultation"),
        _t("Child Consultations", "Newborn Check-up",
           "A check of your newborn's feeding, weight, jaundice and general well-being.\n"
           "Advised in the first weeks after birth, and whenever you are worried.",
           "newborn, new born baby, baby weight, jaundice in baby, yellow baby, feeding problem, baby check up", 20,
           "Bring the discharge summary from the birth and the baby's vaccination card."),
        _t("Child Consultations", "Vaccination",
           "Vaccines for babies and children as per the recommended immunisation schedule.\n"
           "The team checks the vaccination card and plans the due and missed vaccines.",
           "vaccination, vaccine, immunisation, immunization, injection for baby, missed vaccine, booster", 15,
           "Bring the child's vaccination card. Tell the team if the child has fever today.",
           short_name="Child Vaccination"),
        _t("Child Consultations", "Growth & Development Check",
           "Assessment of height, weight, milestones, speech and behaviour for your child's age.\n"
           "The doctor advises on nutrition, therapy or further tests if needed.",
           "growth, short height, not gaining weight, milestones, late walking, late talking, "
           "development delay, autism, adhd", 30,
           "Bring earlier height and weight records if you have them.",
           short_name="Growth & Development"),
        _t("Newborn Care", "Newborn Intensive Care (NICU)",
           "Specialised care for premature or unwell newborns by neonatologists and trained nurses.\n"
           "Admission is decided by the doctor; in an emergency, call the hospital directly.",
           "nicu, premature baby, preterm baby, low birth weight, newborn admission, baby in incubator", None, None,
           short_name="NICU Care"),
        _t("Child Specialists", "Child Heart Care",
           "Consultation with a paediatric cardiologist for heart murmurs, heart defects or tiredness while feeding.\n"
           "The specialist may advise an echo scan before planning care.",
           "heart murmur, hole in heart, child heart problem, congenital heart, sweating while feeding", 30,
           "Bring all previous echo reports and discharge summaries."),
        _t("Child Specialists", "Child Brain & Nerve Care",
           "Consultation with a paediatric neurologist for seizures, headaches, weakness or delayed milestones.\n"
           "The specialist may advise an EEG or a scan before planning care.",
           "epilepsy, headache in child, delayed milestones, weakness, cerebral palsy, head size", 30,
           "Bring previous EEG and scan reports, and a video of any episode if you have one.",
           short_name="Child Neurology"),
        _t("Child Specialists", "Child Stomach & Liver Care",
           "Consultation for long-standing tummy pain, vomiting, constipation, jaundice or poor growth.\n"
           "The specialist may advise tests or an endoscopy after examining your child.",
           "stomach pain in child, tummy pain, constipation, vomiting, jaundice, liver, poor weight gain", 30,
           "Bring previous reports and a note of the child's diet and bowel habits.",
           short_name="Child Stomach & Liver"),
        _t("Child Specialists", "Child Kidney & Urine Care",
           "Consultation for urine infections, swelling, bedwetting or kidney problems in children.\n"
           "The specialist may advise urine tests or a scan.",
           "urine infection, bedwetting, swelling of face, kidney problem, burning urine", 30,
           "Bring previous urine and scan reports.",
           short_name="Child Kidney Care"),
        _t("Child Specialists", "Child Asthma & Allergy",
           "Care for wheezing, frequent cough, breathing allergies and food allergies in children.\n"
           "The doctor looks for triggers and explains how to manage them at home.",
           "asthma, wheezing, frequent cough, allergy, food allergy, sneezing, breathing problem", 30,
           "Bring the inhalers your child uses and any previous reports."),
        _t("Child Specialists", "Child Hormone & Growth",
           "Consultation for short height, early or late puberty, thyroid problems or diabetes in children.\n"
           "The specialist may advise blood tests before planning care.",
           "short height, puberty, early puberty, thyroid in child, child diabetes, obesity, hormone", 30,
           "Bring previous blood reports and a record of the child's height if available."),
        _t("Child Specialists", "Child Surgery Consultation",
           "Consultation with a paediatric surgeon for hernia, undescended testis, lumps or other surgical problems.\n"
           "The surgeon explains whether surgery is needed and what recovery involves.",
           "hernia, hydrocele, undescended testis, lump, circumcision, child surgery", 30,
           "Bring previous scan reports.",
           short_name="Child Surgery Consult"),
        _t("Therapy & Development", "Speech & Language Therapy",
           "Assessment and therapy for delayed speech, stammering or difficulty understanding language.\n"
           "The therapist sets goals and home activities with the family.",
           "speech delay, not talking, stammering, late talking, speech therapy, language delay", 45, None,
           short_name="Speech Therapy"),
        _t("Therapy & Development", "Occupational Therapy",
           "Therapy for fine motor skills, sensory issues, attention and daily activities in children.\n"
           "Planned after an assessment by the therapist.",
           "occupational therapy, sensory issues, handwriting, attention, autism, adhd, motor skills", 45, None),
        _t("Therapy & Development", "Child Psychology",
           "Support for behaviour, emotional, learning or school difficulties in children and teenagers.\n"
           "The psychologist meets the child and parents to understand the concern first.",
           "behaviour problem, anger, anxiety in child, learning difficulty, school problems, screen addiction, "
           "teenager", 45, None),
        _t("Therapy & Development", "Child Nutrition",
           "Diet guidance for picky eating, poor weight gain, overweight or special diets in children.\n"
           "The nutritionist plans meals that suit your child's age and routine.",
           "picky eater, not eating, underweight child, overweight child, child diet, nutrition", 30,
           "Note down what your child eats on a typical day."),
    ],
    # Pregnancy and delivery are consult-led: how a baby is delivered, and
    # whether an epidural or a VBAC is safe, is the obstetrician's decision.
    # Copy never mentions the sex of the baby except to say it is not
    # disclosed -- PCPNDT Act, 1994.
    "womens_health": [
        _t("Gynaecology", "Gynaecology Consultation",
           "A consultation with a gynaecologist for periods, pain, discharge, pregnancy planning or any women's health concern.\n"
           "The doctor examines you and explains the next steps, including any tests.",
           "gynaecologist, gynecologist, lady doctor, women doctor, periods problem, white discharge, "
           "pelvic pain, lower abdomen pain", 20,
           "Note the first day of your last period. Bring previous reports and prescriptions.",
           short_name="Gynae Consultation"),
        _t("Pregnancy Care", "Pregnancy Check-up",
           "Regular antenatal visits to check your health and your baby's growth through pregnancy.\n"
           "Your obstetrician plans the scans and tests for each stage.",
           "pregnant, pregnancy, antenatal, missed period, positive pregnancy test, pregnancy check up, anc, "
           "obstetrician", 30,
           "Bring your pregnancy card, scan reports and the date of your last period."),
        _t("Pregnancy Care", "High-Risk Pregnancy Care",
           "Closer care for pregnancies with high BP, diabetes, twins or complications in an earlier pregnancy.\n"
           "Your obstetrician plans extra monitoring and visits as needed.",
           "high risk pregnancy, bp in pregnancy, twins, previous miscarriage, previous caesarean, "
           "thyroid in pregnancy", 30,
           "Bring all pregnancy reports and the records of earlier pregnancies.",
           short_name="High-Risk Pregnancy"),
        _t("Pregnancy Care", "Pregnancy Diabetes Care",
           "Care for diabetes that starts in, or affects, pregnancy, with sugar monitoring and diet guidance.\n"
           "Planned together with your obstetrician.",
           "gestational diabetes, sugar in pregnancy, diabetes in pregnancy, gdm", 30,
           "Bring your latest sugar test reports."),
        _t("Pregnancy Care", "Fetal Medicine & Scans",
           "Specialised pregnancy scans, such as NT and anomaly scans, to check the baby's growth and development.\n"
           "The fetal medicine specialist explains the findings with you.",
           "pregnancy scan, nt scan, anomaly scan, growth scan, baby growth, fetal echo, double marker", 45,
           "Bring earlier scan reports. As required by law, the sex of the baby is not disclosed."),
        _t("Pregnancy Care", "Pre-Pregnancy Check-up",
           "A health check before planning a pregnancy, with history, tests and lifestyle advice.\n"
           "It helps you prepare for a healthy pregnancy.",
           "planning pregnancy, pre pregnancy, before pregnancy, preconception", 30,
           "Bring previous reports and details of any earlier pregnancies.",
           short_name="Pre-Pregnancy Check"),
        _t("Delivery & Birth", "Childbirth & Delivery Care",
           "Care through labour and birth, including normal delivery and caesarean section when needed.\n"
           "Your obstetrician decides the safest way to deliver for you and your baby.",
           "delivery, normal delivery, c section, caesarean, cesarean, delivery package, labour room", None,
           "Register for delivery during your pregnancy visits so your records are ready.",
           short_name="Delivery Care"),
        _t("Delivery & Birth", "Labour Pain Relief (Epidural)",
           "Options to ease pain during labour, such as an epidural given by an anaesthetist.\n"
           "Suitability is decided during your pregnancy visits and in labour.",
           "epidural, pain relief in labour, labour pain relief, painless delivery", None, None,
           short_name="Labour Pain Relief"),
        _t("Delivery & Birth", "Birth After Caesarean (VBAC)",
           "Assessment of whether a vaginal birth is safe for you after an earlier caesarean.\n"
           "The obstetrician reviews your previous surgery records before deciding.",
           "vbac, normal delivery after c section, previous caesarean, vaginal birth after caesarean", 30,
           "Bring the operation notes from your previous caesarean.",
           short_name="VBAC Assessment"),
        _t("Delivery & Birth", "Childbirth Preparation Classes",
           "Classes for expecting parents on labour, breathing, feeding and newborn care.\n"
           "Partners are welcome to attend.",
           "antenatal classes, birth classes, pregnancy classes, lamaze, preparing for delivery", 60, None,
           short_name="Childbirth Classes"),
        _t("Delivery & Birth", "Breastfeeding Support",
           "Help from a lactation specialist with latching, milk supply and feeding problems.\n"
           "Available during pregnancy and after your baby is born.",
           "breastfeeding, lactation, latching, low milk supply, breast pain while feeding, feeding baby", 30, None),
        _t("Women's Health", "Periods & PCOS Care",
           "Evaluation of irregular, heavy or painful periods and PCOS.\n"
           "The doctor may advise blood tests or a scan before planning care.",
           "irregular periods, pcos, pcod, heavy periods, painful periods, missed periods, facial hair", 20,
           "Note the dates of your last three periods."),
        _t("Women's Health", "Menopause Care",
           "Support for hot flushes, sleep problems, mood changes and bone health around menopause.\n"
           "The doctor explains the options that suit your health.",
           "menopause, hot flushes, perimenopause, periods stopped, mood changes, bone health", 30,
           "Bring previous reports and a list of the medicines you take."),
        _t("Women's Health", "Well Woman Check-up",
           "Routine health screening for women, including a Pap smear and breast examination.\n"
           "Recommended regularly, even when you feel well.",
           "health check up, pap smear, cervical screening, women health checkup, screening", 45,
           "Avoid booking during your periods if a Pap smear is planned."),
        _t("Women's Health", "Breast Care Clinic",
           "Evaluation of breast lumps, breast pain or nipple discharge by a specialist.\n"
           "The doctor may advise a scan or mammogram after the examination.",
           "breast lump, breast pain, nipple discharge, mammogram, breast check", 30,
           "Bring previous mammogram or scan reports."),
        _t("Women's Health", "Urogynaecology Care",
           "Evaluation for urine leakage, frequent urination or pelvic organ prolapse.\n"
           "The doctor explains exercises, therapy or surgery as needed.",
           "urine leakage, incontinence, prolapse, frequent urination, pelvic floor", 30, None),
        _t("Women's Health", "Women's Physiotherapy",
           "Physiotherapy during and after pregnancy for back pain, the pelvic floor and recovery.\n"
           "Planned after an assessment by the physiotherapist.",
           "back pain in pregnancy, pelvic floor exercises, postnatal recovery, diastasis, physiotherapy", 45, None,
           short_name="Women Physiotherapy"),
        _t("Women's Health", "Perinatal Mental Health",
           "Support for anxiety, low mood or stress during pregnancy and after childbirth.\n"
           "A confidential conversation with a trained specialist.",
           "postpartum depression, anxiety in pregnancy, low mood after delivery, stress, crying spells", 45, None),
        _t("Gynae Surgery", "Gynaecological Laparoscopy",
           "Keyhole surgery for fibroids, ovarian cysts, endometriosis or removal of the uterus.\n"
           "Planned after evaluation, with the surgeon explaining recovery.",
           "fibroids, ovarian cyst, endometriosis, hysterectomy, uterus removal, laparoscopic surgery", 60,
           "The team will share fasting and pre-surgery test instructions.",
           short_name="Gynae Laparoscopy"),
        _t("Gynae Surgery", "Gynaecological Hysteroscopy",
           "A thin camera is used to look inside the uterus and treat polyps or abnormal bleeding.\n"
           "Advised by your gynaecologist after a scan.",
           "hysteroscopy, uterine polyp, abnormal periods, uterus camera", 60,
           "Usually planned soon after your period ends. The team will share fasting instructions.",
           short_name="Gynae Hysteroscopy"),
    ],
}

#: Shown in the "Find by Concern" prompt, one line per specialty.
CONCERN_EXAMPLES: dict[str, str] = {
    "dermatology": "acne scars, hair fall, pigmentation",
    "ophthalmology": "blurred vision, cataract, dry eyes",
    "dental": "tooth pain, bleeding gums, crooked teeth",
    "fertility": "trying to conceive, irregular periods, low sperm count",
    "pediatrics": "fever in child, vaccination, speech delay",
    "womens_health": "pregnancy check-up, irregular periods, PCOS",
}

#: Hybrid plans have no single specialty, so their examples are keyed by plan.
CONCERN_EXAMPLES_BY_PLAN: dict[str, str] = {
    "womenchild": "fever in my child, pregnancy check-up, irregular periods",
}


async def seed_starter_treatments(clinic_id: str, specialty: str, service_line: Optional[str] = None) -> dict:
    """Insert the specialty's starter treatments the clinic does not already have.

    Every row goes in HIDDEN, priced 0, source='starter'. Existing rows are
    never modified: the clinic's own edits always win. Names are compared
    stripped and lowercased, matching the unique index from migration 077.

    `service_line` (migration 082) files the rows under a section such as
    Child Care. Callers pass it only for hybrid plans, so a single-specialty
    clinic's insert is exactly what it was before 082. New rows are ordered
    after the clinic's existing ones, so a second list loaded later never
    interleaves with the first.
    """
    starters = STARTER_TREATMENTS.get(specialty) or []
    if not starters:
        return {"added": 0, "skipped": 0}

    existing = await sb(
        supabase.table("specialty_treatments").select("name, display_order").eq("clinic_id", clinic_id).limit(1000)
    )
    existing_rows = existing.data or []
    taken = {(r.get("name") or "").strip().lower() for r in existing_rows}
    offset = max((int(r.get("display_order") or 0) for r in existing_rows), default=0)

    rows = []
    for position, item in enumerate(starters):
        if item["name"].strip().lower() in taken:
            continue
        rows.append({
            "clinic_id": clinic_id,
            "category": item["category"],
            "name": item["name"],
            "short_name": item["short_name"],
            "description": item["description"],
            "concerns": item["concerns"],
            "duration_minutes": item["duration_minutes"],
            "prep_instructions": item["prep_instructions"],
            "care_pathway": item["care_pathway"],
            "price_from_paise": 0,
            "is_active": False,
            "display_order": offset + (position + 1) * 10,
            "source": "starter",
            **({"service_line": service_line} if service_line in SERVICE_LINES else {}),
        })

    if rows:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("specialty_treatments").insert(rows))
        logger.info(f"Seeded {len(rows)} starter treatments ({specialty}) for clinic {clinic_id}")
    return {"added": len(rows), "skipped": len(starters) - len(rows)}
