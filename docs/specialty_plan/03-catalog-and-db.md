# Task 3 — DB helpers, starter treatment lists, auto-seed at onboarding

**Files:**
- Modify: `app/database.py` (add `import uuid` at the top; add helpers directly after `get_lab_test_by_id`)
- Create: `app/services/specialty_catalog.py`
- Modify: `app/routers/clinics.py` (`provision_clinic`)
- Test: `tests/test_specialty_catalog.py`

**Interfaces:**
- Consumes: Task 1 tables; Task 2 `SPECIALTY_BY_PLAN`.
- Produces:
  - `app.database.is_uuid(value) -> bool`
  - `async app.database.get_specialty_treatments(clinic_id: str, active_only: bool = True) -> list[dict]` (max 500 rows, ordered by display_order, name, id)
  - `async app.database.has_active_treatments(clinic_id: str) -> bool` (False on any error)
  - `async app.database.get_treatment_by_id(clinic_id: str, treatment_id, active_only: bool = True) -> Optional[dict]`
  - `async app.database.get_treatment_doctor_ids(clinic_id: str, treatment_id) -> set[str]`
  - `app.services.specialty_catalog.STARTER_TREATMENTS: dict[str, list[dict]]`, keyed by specialty
  - `app.services.specialty_catalog.CONCERN_EXAMPLES: dict[str, str]`
  - `async app.services.specialty_catalog.seed_starter_treatments(clinic_id: str, specialty: str) -> dict` returning `{"added": int, "skipped": int}`
  - The `provision_clinic()` response gains `"starter_treatments": {"added", "skipped"} | None`

Starter treatments are inserted **hidden** (`is_active=false`, `source='starter'`) with price 0. The admin reviews them, sets prices, shows or hides them, edits or deletes them (Task 6). A newly onboarded specialty clinic therefore has no active treatments, and its WhatsApp menu stays the normal menu until the admin publishes at least one.

---

- [x] **Step 1: Write the failing test** — create `tests/test_specialty_catalog.py`:

```python
"""Starter treatment lists are safe to show patients, and seeding never
publishes anything or overwrites a clinic's own rows."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.clinical_firewall import validate_llm_output
from app.services.specialty_catalog import CONCERN_EXAMPLES, STARTER_TREATMENTS, seed_starter_treatments
from app.services.tenant import SPECIALTY_BY_PLAN

FORBIDDEN = ("painless", "pain-free", "guarantee", "permanent", "cure", "success rate",
             "best", "risk-free", "no side effect", "instant result", "6/6", "miracle",
             "boy", "girl", "gender", "100%")


@pytest.mark.parametrize("specialty", sorted(set(SPECIALTY_BY_PLAN.values())))
def test_every_specialty_has_a_starter_list_and_examples(specialty):
    assert len(STARTER_TREATMENTS[specialty]) >= 10
    assert CONCERN_EXAMPLES[specialty]


@pytest.mark.parametrize("specialty", sorted(STARTER_TREATMENTS))
def test_starter_rows_fit_the_database_and_whatsapp_limits(specialty):
    names = set()
    for item in STARTER_TREATMENTS[specialty]:
        name = item["name"]
        assert name.strip().lower() not in names, f"duplicate {name}"
        names.add(name.strip().lower())
        assert 1 <= len(name) <= 120
        assert 1 <= len(item["category"]) <= 60
        title = item.get("short_name") or name
        assert len(title) <= 24, f"'{title}' is too long for a WhatsApp list row"
        if item.get("short_name") is not None:
            assert len(item["short_name"]) <= 24
        assert len(item["description"]) <= 400
        assert item["description"].count("\n") == 1, f"{name}: description must be exactly 2 lines"
        assert item["concerns"] and len(item["concerns"]) <= 500
        assert item["duration_minutes"] is None or 5 <= item["duration_minutes"] <= 1440
        assert item.get("prep_instructions") is None or len(item["prep_instructions"]) <= 600


@pytest.mark.parametrize("specialty", sorted(STARTER_TREATMENTS))
def test_starter_copy_makes_no_promises_and_names_no_medicines(specialty):
    for item in STARTER_TREATMENTS[specialty]:
        text = " ".join(filter(None, [item["name"], item["description"], item.get("prep_instructions")])).lower()
        for word in FORBIDDEN:
            assert word not in text, f"{specialty}/{item['name']} contains '{word}'"
        assert validate_llm_output(item["description"], "en")[0], item["name"]
        if item.get("prep_instructions"):
            assert validate_llm_output(item["prep_instructions"], "en")[0], item["name"]


@pytest.mark.asyncio
async def test_seed_inserts_hidden_starter_rows_and_skips_existing_names():
    existing = MagicMock(data=[{"name": "  hair prp therapy "}])
    inserted = MagicMock(data=[])
    fake_supabase = MagicMock()
    with patch("app.services.specialty_catalog.supabase", fake_supabase), \
         patch("app.services.specialty_catalog.sb", AsyncMock(side_effect=[existing, inserted])):
        result = await seed_starter_treatments("clinic-1", "dermatology")

    rows = fake_supabase.table.return_value.insert.call_args.args[0]
    total = len(STARTER_TREATMENTS["dermatology"])
    assert result == {"added": total - 1, "skipped": 1}
    assert len(rows) == total - 1
    assert all(r["clinic_id"] == "clinic-1" for r in rows)
    assert all(r["is_active"] is False and r["source"] == "starter" and r["price_from_paise"] == 0 for r in rows)
    assert "Hair PRP Therapy" not in {r["name"] for r in rows}


@pytest.mark.asyncio
async def test_seed_for_unknown_specialty_touches_nothing():
    with patch("app.services.specialty_catalog.sb", AsyncMock()) as sb:
        assert await seed_starter_treatments("clinic-1", "general") == {"added": 0, "skipped": 0}
    sb.assert_not_awaited()


@pytest.mark.asyncio
async def test_seed_with_everything_present_inserts_nothing():
    names = [{"name": i["name"]} for i in STARTER_TREATMENTS["dental"]]
    with patch("app.services.specialty_catalog.sb", AsyncMock(return_value=MagicMock(data=names))) as sb:
        result = await seed_starter_treatments("clinic-1", "dental")
    assert result == {"added": 0, "skipped": len(names)}
    assert sb.await_count == 1


def test_is_uuid():
    from app.database import is_uuid

    assert is_uuid("11111111-1111-1111-1111-111111111111")
    for bad in (None, "", "more", "trt_1", 5, "11111111-1111-1111-1111"):
        assert not is_uuid(bad)


@pytest.mark.asyncio
async def test_treatment_lookups_refuse_non_uuid_ids_without_querying():
    from app import database

    with patch("app.database.sb", AsyncMock()) as sb:
        assert await database.get_treatment_by_id("clinic-1", "more") is None
        assert await database.get_treatment_doctor_ids("clinic-1", "") == set()
    sb.assert_not_awaited()


@pytest.mark.asyncio
async def test_has_active_treatments_fails_closed_to_false():
    from app import database

    with patch("app.database.sb", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await database.has_active_treatments("clinic-1") is False


@pytest.mark.asyncio
async def test_provisioning_a_specialty_clinic_seeds_its_starter_list():
    from app.routers import clinics as clinics_router

    req = clinics_router.CreateClinicRequest(
        name="Smile Dental", whatsapp_number="+919876500001", plan="dental",
        meta_phone_number_id="", meta_access_token="EAAG_test",
    )
    insert_ok = MagicMock(data=[{"id": "clinic-9", "plan": "dental"}])
    with patch("app.routers.clinics.sb", AsyncMock(side_effect=[insert_ok, MagicMock(data=[{"id": "a1"}])])), \
         patch("app.routers.clinics.seed_starter_treatments", AsyncMock(return_value={"added": 14, "skipped": 0})) as seed:
        result = await clinics_router.provision_clinic(req)
    seed.assert_awaited_once_with("clinic-9", "dental")
    assert result["starter_treatments"] == {"added": 14, "skipped": 0}


@pytest.mark.asyncio
async def test_provisioning_an_existing_plan_never_seeds():
    from app.routers import clinics as clinics_router

    req = clinics_router.CreateClinicRequest(
        name="City Poly", whatsapp_number="+919876500002", plan="polyclinic",
        meta_phone_number_id="", meta_access_token="EAAG_test",
    )
    insert_ok = MagicMock(data=[{"id": "clinic-8", "plan": "polyclinic"}])
    with patch("app.routers.clinics.sb", AsyncMock(side_effect=[insert_ok, MagicMock(data=[{"id": "a1"}])])), \
         patch("app.routers.clinics.seed_starter_treatments", AsyncMock()) as seed:
        result = await clinics_router.provision_clinic(req)
    seed.assert_not_awaited()
    assert result["starter_treatments"] is None
```

Notes for the implementer:
- `meta_phone_number_id=""` skips the Meta auto-registration HTTP call in `provision_clinic`.
- If `provision_clinic` makes more `sb` calls than mocked (e.g. branches), extend the `side_effect` list. Never change production code for a test.

- [x] **Step 2: Run and confirm failure**

```bash
pytest tests/test_specialty_catalog.py -q
```
Expected: `ModuleNotFoundError: No module named 'app.services.specialty_catalog'`.

- [x] **Step 3: `app/database.py`, import.** If `import uuid` is not present among the top-level imports, add it next to `import time`.

- [x] **Step 4: `app/database.py`, helpers.** Insert directly after the end of `get_lab_test_by_id` (after its `return None` inside the final `except`) and before `#: Used when neither the branch nor the clinic has configured hours.`:

```python
# ── Specialty treatments (migration 077) ─────────────────────────────────────
#: A specialty clinic lists tens of treatments, not thousands; the admin API
#: refuses to create more than this, so one bounded read is always complete.
_TREATMENT_MAX_ROWS = 500


def is_uuid(value) -> bool:
    """True if value is a UUID string. Guards ids that arrive from WhatsApp
    list taps or URLs before they reach a PostgREST filter."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


async def get_specialty_treatments(clinic_id: str, active_only: bool = True) -> list:
    """A clinic's treatments catalogue, ordered as the admin arranged it."""
    try:
        query = scoped_query("specialty_treatments", clinic_id)
        if active_only:
            query = query.eq("is_active", True)
        result = await sb(
            query.order("display_order").order("name").order("id").limit(_TREATMENT_MAX_ROWS)
        )
        return result.data or []
    except Exception as e:
        logger.error(f"Error getting specialty treatments for clinic {clinic_id}: {e}")
        return []


async def has_active_treatments(clinic_id: str) -> bool:
    """Cheap existence check used by the WhatsApp main menu.

    Fails closed to False: on a database error the patient sees the clinic's
    ordinary menu rather than a treatments row that leads nowhere.
    """
    try:
        result = await sb(
            scoped_query("specialty_treatments", clinic_id, select_fields="id")
            .eq("is_active", True)
            .limit(1)
        )
        return bool(result.data)
    except Exception as e:
        logger.error(f"Error checking active treatments for clinic {clinic_id}: {e}")
        return False


async def get_treatment_by_id(
    clinic_id: str, treatment_id, active_only: bool = True
) -> Optional[dict]:
    """One treatment, scoped to the clinic. None for a non-UUID id, a row from
    another clinic, or (with active_only) a hidden treatment."""
    if not is_uuid(treatment_id):
        return None
    try:
        query = scoped_query("specialty_treatments", clinic_id).eq("id", str(treatment_id))
        if active_only:
            query = query.eq("is_active", True)
        result = await sb(query)
        return result.data[0] if result.data else None
    except Exception as e:
        logger.error(f"Error getting treatment {treatment_id}: {e}")
        return None


async def get_treatment_doctor_ids(clinic_id: str, treatment_id) -> set:
    """Doctor ids mapped to a treatment. Empty set means "any active doctor"."""
    if not is_uuid(treatment_id):
        return set()
    try:
        result = await sb(
            scoped_query("treatment_doctors", clinic_id, select_fields="doctor_id")
            .eq("treatment_id", str(treatment_id))
        )
        return {str(r["doctor_id"]) for r in (result.data or []) if r.get("doctor_id")}
    except Exception as e:
        logger.error(f"Error getting doctors for treatment {treatment_id}: {e}")
        return set()

```

- [x] **Step 5: Create `app/services/specialty_catalog.py`** (exact content):

```python
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

from app.database import sb, supabase

logger = logging.getLogger(__name__)


def _t(category, name, description, concerns, duration_minutes=None, prep=None, short_name=None):
    return {
        "category": category,
        "name": name,
        "short_name": short_name,
        "description": description,
        "concerns": concerns,
        "duration_minutes": duration_minutes,
        "prep_instructions": prep,
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
}

#: Shown in the "Find by Concern" prompt, one line per specialty.
CONCERN_EXAMPLES: dict[str, str] = {
    "dermatology": "acne scars, hair fall, pigmentation",
    "ophthalmology": "blurred vision, cataract, dry eyes",
    "dental": "tooth pain, bleeding gums, crooked teeth",
    "fertility": "trying to conceive, irregular periods, low sperm count",
}


async def seed_starter_treatments(clinic_id: str, specialty: str) -> dict:
    """Insert the specialty's starter treatments the clinic does not already have.

    Every row goes in HIDDEN, priced 0, source='starter'. Existing rows are
    never modified: the clinic's own edits always win. Names are compared
    stripped and lowercased, matching the unique index from migration 077.
    """
    starters = STARTER_TREATMENTS.get(specialty) or []
    if not starters:
        return {"added": 0, "skipped": 0}

    existing = await sb(
        supabase.table("specialty_treatments").select("name").eq("clinic_id", clinic_id).limit(1000)
    )
    taken = {(r.get("name") or "").strip().lower() for r in (existing.data or [])}

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
            "price_from_paise": 0,
            "is_active": False,
            "display_order": (position + 1) * 10,
            "source": "starter",
        })

    if rows:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("specialty_treatments").insert(rows))
        logger.info(f"Seeded {len(rows)} starter treatments ({specialty}) for clinic {clinic_id}")
    return {"added": len(rows), "skipped": len(starters) - len(rows)}
```

- [x] **Step 6: `app/routers/clinics.py`, imports.** Add near the other `app.services` imports at the top of the file:
```python
from app.services.specialty_catalog import seed_starter_treatments
from app.services.tenant import SPECIALTY_BY_PLAN
```
If `app.services.tenant` is already imported with a `from … import (…)` list (it provides `invalidate_branch_cache`), add `SPECIALTY_BY_PLAN` to that list instead of adding a second import line.

- [x] **Step 7: `app/routers/clinics.py`, seed during provisioning.** In `provision_clinic`, replace exactly:
```python
        return {
            "success": True,
            "clinic": clinic,
            "branches": created_branches if created_branches else None,
            "clinic_admin": clinic_admin,
        }
```
with:
```python
        # ── Specialty plans: load the starter treatments, hidden ──
        # Best-effort, like the admin login above: a failure here must never
        # fail onboarding. The admin can load the list later from the panel.
        starter_treatments = None
        specialty = SPECIALTY_BY_PLAN.get(req.plan)
        if specialty:
            try:
                starter_treatments = await seed_starter_treatments(clinic_id, specialty)
            except Exception as se:
                logger.warning(f"Failed to seed starter treatments for clinic {clinic_id}: {se}")

        return {
            "success": True,
            "clinic": clinic,
            "branches": created_branches if created_branches else None,
            "clinic_admin": clinic_admin,
            "starter_treatments": starter_treatments,
        }
```

- [x] **Step 8: Run tests**

```bash
pytest tests/test_specialty_catalog.py tests/test_specialty_plans.py tests/test_lint_unscoped_queries.py tests/test_phase2_unscoped_query_linter.py tests/test_clinic_settings.py -q
```
Expected: all PASS.
- If a starter item fails the copy test, reword **that item's text** so it complies (no promise words, no medicine names). Do not weaken the test.
- If the linter flags `seed_starter_treatments`, make sure the annotation line sits directly above the `await sb(supabase.table(...).insert(rows))` line.

Run the orphan check.

- [x] **Step 9: Commit**

```bash
git add app/database.py app/services/specialty_catalog.py app/routers/clinics.py tests/test_specialty_catalog.py
git commit -m "feat(treatments): catalogue DB helpers, starter lists, hidden seeding at onboarding

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
