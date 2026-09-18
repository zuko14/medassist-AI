"""Who decides the treatment: the patient, or the doctor (migration 081).

Raised by an eye hospital evaluating the plan: "our doctor examines the
patient and THEN decides the treatment." True for eye, dental and IVF; only
partly true for dermatology, where a patient really does arrive saying "acne".

Before this column, "Comprehensive Eye Check-up" and "Cataract Surgery" were
the same kind of row -- one tap that books a consultation -- so a patient
tapping Cataract Surgery believed they were booking surgery.

The load-bearing property for the LIVE derma tenant is TestUnclassifiedIsUnchanged:
a catalogue that classifies nothing must behave exactly as it does today.
"""

import io
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow as sf
from app.services.conversation import ConversationManager

PHONE = "+919000000001"
EYE = {"id": "clinic-1", "plan": "eye", "features": {}}
DERMA = {"id": "clinic-2", "plan": "derma", "features": {}}


def _treatment(i, name, pathway="direct", category="Cataract", **extra):
    row = {
        "id": f"{i:08d}-0000-0000-0000-000000000000",
        "name": name,
        "short_name": None,
        "category": category,
        "display_order": i,
        "is_active": True,
        "description": f"What {name} is.\nWhat to expect.",
        "price_from_paise": 0,
        "care_pathway": pathway,
    }
    row.update(extra)
    return row


ENTRY = _treatment(1, "Comprehensive Eye Check-up", "entry", "Eye Check-up")
DIRECT = _treatment(2, "LASIK Evaluation", "direct", "Specs Removal")
ASSESS = _treatment(3, "Cataract Surgery", "assessment_first")
CATALOGUE = [ENTRY, DIRECT, ASSESS]
BY_ID = {t["id"]: t for t in CATALOGUE}


async def _by_id(_clinic_id, treatment_id, active_only=True):
    return BY_ID.get(str(treatment_id))


def _manager():
    m = MagicMock()
    m.whatsapp.send_text = AsyncMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    m.whatsapp.send_interactive_buttons = AsyncMock()
    m.update_state = AsyncMock()
    m._send_main_menu = AsyncMock()
    m._start_booking = AsyncMock()
    m._handle_emergency = AsyncMock()
    # The real pager: these lists must keep fitting Meta's 10-row cap.
    m._page_rows = ConversationManager.__new__(ConversationManager)._page_rows
    return m


def _catalogue(rows=None):
    """Patch every read the flow makes against the treatments catalogue."""
    rows = CATALOGUE if rows is None else rows
    return (
        patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=rows)),
        patch.object(sf, "get_treatment_by_id", _by_id),
        patch.object(sf, "get_patient_by_phone", AsyncMock(return_value={"id": "p1"})),
    )


# -- The live-tenant guarantee ------------------------------------------------


class TestUnclassifiedIsUnchanged:
    def test_a_row_without_the_column_is_direct(self):
        """Every row read before migration 081 lands here."""
        assert sf.care_pathway({"id": "x", "name": "Acne Treatment"}) == sf.PATHWAY_DIRECT

    @pytest.mark.parametrize("value", [None, "", "   ", "surgery", "ENTRY_POINT", 7])
    def test_anything_unrecognised_is_direct_not_an_error(self, value):
        assert sf.care_pathway({"care_pathway": value}) == sf.PATHWAY_DIRECT

    def test_menu_rows_are_untouched_without_a_first_visit_row(self):
        for lang in ("en", "hi", "te"):
            rows = sf.treatment_menu_rows(lang)
            assert [r["id"] for r in rows] == ["menu_treatments", "menu_concern"]

    @pytest.mark.asyncio
    async def test_an_unclassified_card_books_a_consultation_as_before(self):
        m = _manager()
        acne = _treatment(9, "Acne Treatment", category="Acne & Scars")
        acne.pop("care_pathway")
        with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=acne)), \
             patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[])):
            await sf.show_treatment_card(m, DERMA, PHONE, acne["id"], "en")

        kwargs = m.whatsapp.send_interactive_buttons.await_args.kwargs
        assert kwargs["buttons"][0]["id"] == f"trtbook_{acne['id']}"
        assert "not booked directly" not in kwargs["body"]
        assert "Our specialist will examine you" in kwargs["body"]


# -- Menu shape ---------------------------------------------------------------


class TestMenuLeadsWithBookingWhenAFirstVisitExists:
    def test_book_consultation_leads_and_catalogue_moves_last(self):
        rows = sf.treatment_menu_rows("en", has_entry=True)
        assert [r["id"] for r in rows] == [
            "menu_entry_consult", "menu_concern", "menu_treatments",
        ]

    def test_concern_row_stops_demanding_a_diagnosis(self):
        rows = sf.treatment_menu_rows("en", has_entry=True)
        assert rows[1]["title"] == "🔍 Not sure? Tell us"

    @pytest.mark.parametrize("lang", ["en", "hi", "te"])
    def test_rows_fit_whatsapp_limits_in_every_language(self, lang):
        for rows in (sf.treatment_menu_rows(lang), sf.treatment_menu_rows(lang, has_entry=True)):
            assert all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in rows)

    def test_the_menu_button_is_routed(self):
        """Unrouted, the row would fall through to the unknown-button branch."""
        assert "menu_entry_consult" in sf.TREATMENT_BUTTON_IDS


# -- The card -----------------------------------------------------------------


class TestDoctorDecidedCard:
    @pytest.mark.asyncio
    async def test_it_says_in_words_that_it_is_not_booked_directly(self):
        m = _manager()
        with patch.object(sf, "get_treatment_by_id", _by_id), \
             patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[])):
            await sf.show_treatment_card(m, EYE, PHONE, ASSESS["id"], "en")

        body = m.whatsapp.send_interactive_buttons.await_args.kwargs["body"]
        assert "not booked directly" in body
        assert "Planned by your specialist after an examination" in body

    @pytest.mark.asyncio
    async def test_its_button_books_an_examination_not_the_procedure(self):
        m = _manager()
        with patch.object(sf, "get_treatment_by_id", _by_id), \
             patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[])):
            await sf.show_treatment_card(m, EYE, PHONE, ASSESS["id"], "en")

        ids = [b["id"] for b in m.whatsapp.send_interactive_buttons.await_args.kwargs["buttons"]]
        assert ids == [f"trtexam_{ASSESS['id']}", f"trtcall_{ASSESS['id']}", "menu_treatments"]

    @pytest.mark.parametrize("pathway", ["direct", "entry"])
    def test_a_patient_choosable_row_keeps_its_booking_button(self, pathway):
        row = _treatment(4, "Teeth Cleaning", pathway)
        assert sf._card_buttons(row, "en")[0]["id"] == f"trtbook_{row['id']}"

    @pytest.mark.parametrize("lang", ["en", "hi", "te"])
    def test_every_button_still_fits_whatsapp_in_every_language(self, lang):
        for row in (ENTRY, DIRECT, ASSESS):
            assert all(len(b["title"]) <= 20 for b in sf._card_buttons(row, lang))

    def test_the_examination_prefix_is_routed_before_the_card_prefix(self):
        """"trtexam_" must be reached before the generic "trt_" card branch."""
        assert "trtexam_" in sf.TREATMENT_BUTTON_PREFIXES
        src = io.open(sf.__file__, encoding="utf-8").read()
        assert src.index('button_id.startswith("trtexam_")') < src.index('button_id.startswith("trt_")')


# -- Booking the first visit --------------------------------------------------


class TestExaminationBooking:
    @pytest.mark.asyncio
    async def test_book_examination_books_the_first_visit_not_the_procedure(self):
        m = _manager()
        p1, p2, p3 = _catalogue()
        with p1, p2, p3:
            await sf.start_examination_for(m, EYE, PHONE, ASSESS["id"], "en")

        seed = m._start_booking.await_args.kwargs["seed_context"]
        assert seed["treatment_id"] == ENTRY["id"]
        assert seed["treatment_name"] == "Comprehensive Eye Check-up"
        # ...but the clinic still learns why the patient came.
        assert seed["treatment_interest"] == "Cataract Surgery"

    @pytest.mark.asyncio
    async def test_the_main_menu_row_books_the_first_visit_with_no_interest_tag(self):
        m = _manager()
        p1, p2, p3 = _catalogue()
        with p1, p2, p3:
            await sf.start_entry_consultation(m, EYE, PHONE, "en")

        seed = m._start_booking.await_args.kwargs["seed_context"]
        assert seed["treatment_id"] == ENTRY["id"]
        assert "treatment_interest" not in seed

    @pytest.mark.asyncio
    async def test_without_a_first_visit_row_it_books_the_treatment_itself(self):
        """Still a consultation: a treatment is only ever a tag on one
        (migration 077), so the promise holds even unconfigured."""
        m = _manager()
        p1, p2, p3 = _catalogue([ASSESS])
        with p1, p2, p3:
            await sf.start_examination_for(m, EYE, PHONE, ASSESS["id"], "en")

        seed = m._start_booking.await_args.kwargs["seed_context"]
        assert seed["treatment_id"] == ASSESS["id"]

    @pytest.mark.asyncio
    async def test_with_no_catalogue_at_all_it_falls_back_to_ordinary_booking(self):
        m = _manager()
        p1, p2, p3 = _catalogue([])
        with p1, p2, p3:
            await sf.start_entry_consultation(m, EYE, PHONE, "en")

        assert m._start_booking.await_args.kwargs.get("seed_context") is None

    @pytest.mark.asyncio
    async def test_several_first_visit_rows_are_offered_rather_than_guessed(self):
        m = _manager()
        kids = _treatment(5, "Child Eye Check-up", "entry", "Kids & Squint")
        p1, p2, p3 = _catalogue([ENTRY, kids, ASSESS])
        with p1, p2, p3:
            await sf.start_entry_consultation(m, EYE, PHONE, "en")

        m._start_booking.assert_not_awaited()
        rows = m.whatsapp.send_interactive_list.await_args.kwargs["sections"][0]["rows"]
        assert [r["id"] for r in rows] == [f"trtbook_{ENTRY['id']}", f"trtbook_{kids['id']}"]

    @pytest.mark.asyncio
    async def test_a_long_first_visit_list_leaves_no_row_unreachable(self):
        """The pager id must be its own. "trt_more" means "next page of the
        chosen CATEGORY", and with no category set it falls back to the
        category list -- so row 10 onwards was silently unreachable."""
        m = _manager()
        many = [
            _treatment(i, f"Entry Consultation {i}", "entry", "Checks")
            for i in range(1, 15)
        ]
        p1, p2, p3 = _catalogue(many)
        reachable, page = set(), 0
        with p1, p2, p3:
            for _ in range(3):
                await sf.start_entry_consultation(m, EYE, PHONE, "en", page=page)
                rows = m.whatsapp.send_interactive_list.await_args.kwargs["sections"][0]["rows"]
                assert len(rows) <= 10
                reachable.update(r["id"] for r in rows if r["id"].startswith("trtbook_"))
                if not any(r["id"] == "trtentry_more" for r in rows):
                    break
                page += 1

        assert len(reachable) == len(many)
        assert "trtentry_more" in sf.TREATMENT_BUTTON_IDS
        assert "treatment_entry_page" in sf.TREATMENT_CONTEXT_KEYS

    @pytest.mark.asyncio
    async def test_the_interest_reaches_the_appointment_the_doctor_reads(self):
        m = _manager()
        context = {"treatment_id": ENTRY["id"], "treatment_interest": "Cataract Surgery"}
        with patch.object(sf, "get_treatment_by_id", _by_id), \
             patch.object(sf, "_treatment_doctors",
                          AsyncMock(return_value=[{"id": "d1", "name": "Dr. Rao"}])):
            await sf.show_treatment_doctors(m, EYE, PHONE, context, "en")

        assert context["symptoms"] == (
            "Treatment: Comprehensive Eye Check-up (asked about: Cataract Surgery)"
        )

    @pytest.mark.asyncio
    async def test_without_an_interest_the_symptom_line_is_unchanged(self):
        m = _manager()
        context = {"treatment_id": ENTRY["id"]}
        with patch.object(sf, "get_treatment_by_id", _by_id), \
             patch.object(sf, "_treatment_doctors",
                          AsyncMock(return_value=[{"id": "d1", "name": "Dr. Rao"}])):
            await sf.show_treatment_doctors(m, EYE, PHONE, context, "en")

        assert context["symptoms"] == "Treatment: Comprehensive Eye Check-up"

    def test_the_interest_tag_is_cleared_with_the_rest_of_the_flow(self):
        """Otherwise an abandoned enquiry labels a later, unrelated booking."""
        assert "treatment_interest" in sf.TREATMENT_CONTEXT_KEYS
        ctx = {"treatment_interest": "IVF", "keep": 1}
        assert sf.clear_treatment_context(ctx) == {"keep": 1}


# -- "I don't know what I need" ----------------------------------------------


class TestPatientCannotNameTheProblem:
    @pytest.mark.asyncio
    async def test_the_concern_prompt_offers_an_examination(self):
        m = _manager()
        with patch.object(sf, "has_entry_treatment", AsyncMock(return_value=True)):
            await sf.prompt_concern(m, EYE, PHONE, "en")

        kwargs = m.whatsapp.send_interactive_buttons.await_args.kwargs
        assert kwargs["buttons"][0]["id"] == "menu_entry_consult"
        assert "Not sure how to describe it" in kwargs["body"]
        m.whatsapp.send_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_clinic_without_a_first_visit_row_keeps_the_old_prompt(self):
        m = _manager()
        with patch.object(sf, "has_entry_treatment", AsyncMock(return_value=False)):
            await sf.prompt_concern(m, DERMA, PHONE, "en")

        m.whatsapp.send_interactive_buttons.assert_not_awaited()
        assert "Tell us your concern" in m.whatsapp.send_text.await_args.args[2]

    @pytest.mark.asyncio
    async def test_no_match_offers_the_examination_instead_of_a_category_dump(self):
        m = _manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=CATALOGUE)), \
             patch.object(sf, "rank_treatments_for_concern", AsyncMock(return_value=[])), \
             patch.object(sf, "show_treatment_categories", AsyncMock()) as cats:
            await sf.handle_treatment_search_text(m, EYE, PHONE, "zzzznotathing", "en")

        cats.assert_not_awaited()
        kwargs = m.whatsapp.send_interactive_buttons.await_args.kwargs
        assert [b["id"] for b in kwargs["buttons"]] == [
            "menu_entry_consult", "menu_treatments", "menu_human",
        ]
        # The clinical promise, stated to the patient.
        assert "not suggest a treatment before a doctor has seen you" in kwargs["body"]

    @pytest.mark.asyncio
    async def test_no_match_without_a_first_visit_row_keeps_the_category_list(self):
        m = _manager()
        derma_rows = [_treatment(9, "Acne Treatment", "direct", "Acne & Scars")]
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=derma_rows)), \
             patch.object(sf, "rank_treatments_for_concern", AsyncMock(return_value=[])), \
             patch.object(sf, "show_treatment_categories", AsyncMock()) as cats:
            await sf.handle_treatment_search_text(m, DERMA, PHONE, "zzzznotathing", "en")

        cats.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_results_end_with_an_examination_row(self):
        m = _manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=CATALOGUE)):
            await sf.handle_treatment_search_text(m, EYE, PHONE, "cataract", "en")

        kwargs = m.whatsapp.send_interactive_list.await_args.kwargs
        rows = kwargs["sections"][0]["rows"]
        assert rows[-1]["id"] == "menu_entry_consult"
        assert len(rows) <= 10
        assert all(len(r["title"]) <= 24 for r in rows)

    @pytest.mark.asyncio
    async def test_search_results_do_not_read_as_a_diagnosis(self):
        m = _manager()
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=CATALOGUE)):
            await sf.handle_treatment_search_text(m, EYE, PHONE, "cataract", "en")

        body = m.whatsapp.send_interactive_list.await_args.kwargs["body"]
        assert "This is not a diagnosis" in body
        assert "examines you first" in body

    @pytest.mark.asyncio
    async def test_a_derma_search_keeps_its_result_list_unchanged(self):
        m = _manager()
        derma_rows = [_treatment(9, "Acne Treatment", "direct", "Acne & Scars")]
        with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=derma_rows)):
            await sf.handle_treatment_search_text(m, DERMA, PHONE, "acne", "en")

        rows = m.whatsapp.send_interactive_list.await_args.kwargs["sections"][0]["rows"]
        assert [r["id"] for r in rows] == [f"trt_{derma_rows[0]['id']}"]


# -- The shipped starter catalogues ------------------------------------------


class TestStarterCatalogue:
    def test_dermatology_stays_patient_led(self):
        """The live derma tenant's flow is the one that must not move."""
        from app.services.specialty_catalog import STARTER_TREATMENTS

        rows = STARTER_TREATMENTS["dermatology"]
        assert not [r for r in rows if r["care_pathway"] == "entry"]
        # Only the one row a dermatologist orders and a patient never asks for.
        assert [r["name"] for r in rows if r["care_pathway"] == "assessment_first"] == [
            "Skin Biopsy"
        ]

    @pytest.mark.parametrize("specialty", ["ophthalmology", "dental", "fertility"])
    def test_every_consult_led_specialty_ships_one_first_visit(self, specialty):
        from app.services.specialty_catalog import STARTER_TREATMENTS

        entries = [r for r in STARTER_TREATMENTS[specialty] if r["care_pathway"] == "entry"]
        assert len(entries) == 1, [r["name"] for r in entries]

    @pytest.mark.parametrize(
        "specialty,name",
        [
            ("ophthalmology", "Cataract Surgery"),
            ("dental", "Root Canal Treatment"),
            ("fertility", "IVF (In Vitro Fertilisation)"),
        ],
    )
    def test_the_procedures_a_doctor_prescribes_are_not_patient_bookable(self, specialty, name):
        from app.services.specialty_catalog import STARTER_TREATMENTS

        row = next(r for r in STARTER_TREATMENTS[specialty] if r["name"] == name)
        assert row["care_pathway"] == "assessment_first"

    def test_every_starter_row_carries_a_valid_pathway(self):
        from app.services.specialty_catalog import STARTER_TREATMENTS

        for specialty, rows in STARTER_TREATMENTS.items():
            for row in rows:
                assert row["care_pathway"] in ("entry", "direct", "assessment_first"), (
                    specialty, row["name"]
                )

    def test_the_python_and_sql_classifications_cannot_drift(self):
        """The migration backfills catalogues already seeded; specialty_catalog
        classifies the ones seeded from now on. They must agree, or a clinic's
        rows depend on which side of the deploy it onboarded."""
        from app.services.specialty_catalog import PATHWAYS_ADDED_AFTER_081, _PATHWAY_BY_NAME

        sql = io.open(
            "migrations/081_treatment_care_pathway.sql", encoding="utf-8"
        ).read()
        block = sql.split("FROM (VALUES", 1)[1].split(") AS v(", 1)[0]
        pairs = dict(re.findall(r"\('([^']+)',\s*'(entry|direct|assessment_first)'\)", block))
        # Names first shipped after 081 (migration 082's child / women lists)
        # had no rows to backfill; everything 081 did list must still match.
        assert not (PATHWAYS_ADDED_AFTER_081 & set(pairs))
        assert pairs == {k: v for k, v in _PATHWAY_BY_NAME.items() if k not in PATHWAYS_ADDED_AFTER_081}


# -- Admin API ----------------------------------------------------------------


class TestAdminValidation:
    def test_the_default_is_todays_behaviour(self):
        from app.routers.admin import TreatmentCreate

        assert TreatmentCreate(name="Acne", category="Skin").care_pathway == "direct"

    def test_case_and_padding_are_normalised(self):
        from app.routers.admin import TreatmentCreate

        got = TreatmentCreate(name="X", category="C", care_pathway="  Entry ")
        assert got.care_pathway == "entry"

    @pytest.mark.parametrize("bad", ["surgery", "consult", "ENTRYPOINT", "1"])
    def test_an_unknown_pathway_is_refused(self, bad):
        from pydantic import ValidationError

        from app.routers.admin import TreatmentCreate

        with pytest.raises(ValidationError):
            TreatmentCreate(name="X", category="C", care_pathway=bad)

    def test_an_update_that_omits_it_leaves_it_alone(self):
        from app.routers.admin import TreatmentUpdate

        assert "care_pathway" not in TreatmentUpdate(name="X").model_dump(exclude_unset=True)

    def test_an_update_can_reclassify(self):
        from app.routers.admin import TreatmentUpdate

        payload = TreatmentUpdate(care_pathway="assessment_first")
        assert payload.model_dump(exclude_unset=True) == {"care_pathway": "assessment_first"}
