"""specialty_flow in isolation: WhatsApp limits, gating, and safe fallbacks."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import specialty_flow as sf

PHONE = "+919000000001"
DERMA = {"id": "clinic-1", "plan": "derma", "features": {}}
POLY = {"id": "clinic-2", "plan": "polyclinic", "features": {}}
T1 = "11111111-1111-1111-1111-111111111111"
D1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
D2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _manager():
    m = MagicMock()
    m.whatsapp.send_text = AsyncMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    m.whatsapp.send_interactive_buttons = AsyncMock()
    m.update_state = AsyncMock()
    m._send_main_menu = AsyncMock()
    m._handle_emergency = AsyncMock()
    m._start_booking = AsyncMock()

    from app.services.conversation import ConversationManager
    real = ConversationManager.__new__(ConversationManager)
    m._page_rows = real._page_rows
    return m


def _treatment(i, category="Hair & Scalp", **extra):
    return {"id": f"{i:08d}-0000-0000-0000-000000000000", "name": f"Treatment {i}",
            "category": category, "display_order": i, "is_active": True,
            "description": f"Line one {i}.\nLine two.", "price_from_paise": 0, **extra}


def _rows(call):
    return [r for s in call.kwargs["sections"] for r in s["rows"]]


# ── gating ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_menu_is_inactive_for_existing_plans_without_touching_the_db():
    with patch.object(sf, "has_active_treatments", AsyncMock(return_value=True)) as active:
        assert await sf.treatment_menu_active(POLY) is False
        assert await sf.treatment_menu_active({"id": "c", "plan": "enterprise"}) is False
    active.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_needs_at_least_one_active_treatment():
    with patch.object(sf, "has_active_treatments", AsyncMock(return_value=False)):
        assert await sf.treatment_menu_active(DERMA) is False
    with patch.object(sf, "has_active_treatments", AsyncMock(return_value=True)):
        assert await sf.treatment_menu_active(DERMA) is True


def test_menu_rows_fit_whatsapp_limits_in_every_language():
    for lang in ("en", "hi", "te"):
        rows = sf.treatment_menu_rows(lang)
        assert [r["id"] for r in rows] == ["menu_treatments", "menu_concern"]
        assert all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in rows)


@pytest.mark.asyncio
async def test_stale_button_tap_after_downgrade_returns_main_menu():
    m = _manager()
    await sf.handle_treatment_button(m, POLY, PHONE, f"trtbook_{T1}", {"context": {}}, "en")
    m._send_main_menu.assert_awaited_once()
    m._start_booking.assert_not_awaited()


def test_clear_treatment_context_only_removes_treatment_keys():
    ctx = {"treatment_id": T1, "treatment_name": "X", "treatment_page": 2, "branch_id": "b1"}
    assert sf.clear_treatment_context(ctx) == {"branch_id": "b1"}


# ── browsing ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_categories_list_is_paginated_and_titles_fit():
    treatments = [_treatment(i, category=f"Category number {i:02d} with a long name") for i in range(14)]
    m = _manager()
    with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=treatments)):
        await sf.show_treatment_categories(m, DERMA, PHONE, "en")
    rows = _rows(m.whatsapp.send_interactive_list.await_args)
    assert len(rows) == 10 and rows[-1]["id"] == "trtcat_more"
    assert all(len(r["title"]) <= 24 for r in rows)
    state_ctx = m.update_state.await_args.args[3]
    assert m.update_state.await_args.args[2] == "browsing_treatments"
    assert len(state_ctx["treatment_categories"]) == 14


@pytest.mark.asyncio
async def test_single_category_skips_straight_to_treatments():
    treatments = [_treatment(i) for i in range(3)]
    m = _manager()
    with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=treatments)):
        await sf.show_treatment_categories(m, DERMA, PHONE, "en")
    rows = _rows(m.whatsapp.send_interactive_list.await_args)
    assert [r["id"] for r in rows] == [f"trt_{t['id']}" for t in treatments]


@pytest.mark.asyncio
async def test_empty_catalogue_falls_back_to_main_menu():
    m = _manager()
    with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=[])):
        await sf.show_treatment_categories(m, DERMA, PHONE, "en")
    m._send_main_menu.assert_awaited_once()
    m.whatsapp.send_interactive_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_treatments_in_category_paginate_with_trt_more():
    treatments = [_treatment(i, short_name="A very long short name that is long") for i in range(25)]
    m = _manager()
    await sf.show_treatments_in_category(m, DERMA, PHONE, "Hair & Scalp", "en", treatments=treatments)
    rows = _rows(m.whatsapp.send_interactive_list.await_args)
    assert len(rows) == 10 and rows[-1]["id"] == "trt_more"
    assert all(len(r["title"]) <= 24 and len(r["description"]) <= 72 for r in rows)


@pytest.mark.asyncio
async def test_category_tap_resolves_from_context_list():
    m = _manager()
    session = {"context": {"treatment_categories": ["Acne & Scars", "Hair & Scalp"]}}
    with patch.object(sf, "show_treatments_in_category", AsyncMock()) as show:
        await sf.handle_treatment_button(m, DERMA, PHONE, "trtcat_1", session, "en")
    assert show.await_args.args[3] == "Hair & Scalp"


@pytest.mark.asyncio
async def test_card_has_three_buttons_and_fits_the_body_limit():
    t = _treatment(1, description="x" * 390 + "\ny", prep_instructions="p" * 600, duration_minutes=45,
                   price_from_paise=250000)
    m = _manager()
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=t)), \
         patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[{"id": D1, "name": "Dr. Rao"}])):
        await sf.show_treatment_card(m, DERMA, PHONE, t["id"], "en")
    call = m.whatsapp.send_interactive_buttons.await_args
    assert len(call.kwargs["body"]) <= 1024
    assert "₹2,500" in call.kwargs["body"] and "Dr. Rao" in call.kwargs["body"]
    ids = [b["id"] for b in call.kwargs["buttons"]]
    assert ids == [f"trtbook_{t['id']}", f"trtcall_{t['id']}", "menu_treatments"]
    for lang in ("en", "hi", "te"):
        assert all(len(b["title"]) <= 20 for b in sf._card_buttons(t["id"], lang))


@pytest.mark.asyncio
async def test_card_uses_patient_language_and_price_on_consultation():
    t = _treatment(1, description_hi="हिंदी पंक्ति एक।\nपंक्ति दो।")
    m = _manager()
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=t)), \
         patch.object(sf, "_treatment_doctors", AsyncMock(return_value=[])):
        await sf.show_treatment_card(m, DERMA, PHONE, t["id"], "hi")
    body = m.whatsapp.send_interactive_buttons.await_args.kwargs["body"]
    assert "हिंदी पंक्ति एक।" in body
    assert "परामर्श" in body


@pytest.mark.asyncio
async def test_hidden_or_deleted_treatment_card_shows_categories_instead():
    m = _manager()
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=None)), \
         patch.object(sf, "show_treatment_categories", AsyncMock()) as cats:
        await sf.show_treatment_card(m, DERMA, PHONE, T1, "en")
    cats.assert_awaited_once()
    m.whatsapp.send_interactive_buttons.assert_not_awaited()


# ── booking hand-off and doctors ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_book_button_seeds_the_normal_booking_flow():
    m = _manager()
    t = _treatment(1)
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=t)), \
         patch.object(sf, "get_patient_by_phone", AsyncMock(return_value={"language": "en", "name": "Asha Rao"})):
        await sf.start_treatment_booking(m, DERMA, PHONE, t["id"], "en")
    kwargs = m._start_booking.await_args.kwargs
    assert kwargs["seed_context"] == {"treatment_id": t["id"], "treatment_name": "Treatment 1"}


@pytest.mark.asyncio
async def test_route_is_a_no_op_without_a_treatment():
    m = _manager()
    assert await sf.route_to_treatment_doctors(m, DERMA, PHONE, {"booking_name": "A"}, "en") is False
    m.whatsapp.send_interactive_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_doctor_list_is_filtered_by_mapping_and_uses_doc_ids():
    t = _treatment(1)
    doctors = [{"id": D1, "name": "Dr. A", "specialization": "Dermatologist", "consultation_fee": 500},
               {"id": D2, "name": "Dr. B", "specialization": "Trichologist", "consultation_fee": None}]
    m = _manager()
    ctx = {"treatment_id": t["id"], "treatment_name": t["name"], "branch_id": "branch-1"}
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=t)), \
         patch.object(sf, "get_doctors", AsyncMock(return_value=doctors)) as get_docs, \
         patch.object(sf, "get_treatment_doctor_ids", AsyncMock(return_value={D2})):
        assert await sf.route_to_treatment_doctors(m, DERMA, PHONE, ctx, "en") is True
    assert get_docs.await_args.kwargs["branch_id"] == "branch-1"
    rows = _rows(m.whatsapp.send_interactive_list.await_args)
    assert [r["id"] for r in rows] == [f"doc_{D2}"]
    state, new_ctx = m.update_state.await_args.args[2], m.update_state.await_args.args[3]
    assert state == "selecting_doctor"
    assert new_ctx["symptoms"] == "Treatment: Treatment 1"


@pytest.mark.asyncio
async def test_no_available_specialist_offers_a_callback():
    t = _treatment(1)
    m = _manager()
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=t)), \
         patch.object(sf, "get_doctors", AsyncMock(return_value=[{"id": D1, "name": "Dr. A"}])), \
         patch.object(sf, "get_treatment_doctor_ids", AsyncMock(return_value={D2})):
        await sf.show_treatment_doctors(m, DERMA, PHONE, {"treatment_id": t["id"]}, "en")
    buttons = m.whatsapp.send_interactive_buttons.await_args.kwargs["buttons"]
    assert buttons[0]["id"] == f"trtcall_{t['id']}"
    m.whatsapp.send_interactive_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_revalidate_drops_a_deleted_treatment_but_keeps_a_hidden_one():
    ctx = {"treatment_id": T1, "treatment_name": "X", "doctor_name": "Dr. A"}
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=None)):
        await sf.revalidate_treatment(DERMA, ctx)
    assert "treatment_id" not in ctx and ctx["doctor_name"] == "Dr. A"

    ctx = {"treatment_id": T1, "treatment_name": "X"}
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value={"id": T1, "is_active": False})) as get:
        await sf.revalidate_treatment(DERMA, ctx)
    assert ctx["treatment_id"] == T1
    assert get.await_args.kwargs["active_only"] is False


@pytest.mark.asyncio
async def test_prep_note_is_sent_only_when_there_is_one_and_never_raises():
    wa = MagicMock(send_text=AsyncMock())
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value={"name": "LASIK", "prep_instructions": "Stop lenses."})):
        await sf.send_prep_note(wa, DERMA, PHONE, T1, "en")
    assert "Stop lenses." in wa.send_text.await_args.args[2]
    wa.send_text.reset_mock()
    await sf.send_prep_note(wa, DERMA, PHONE, None, "en")
    with patch.object(sf, "get_treatment_by_id", AsyncMock(side_effect=RuntimeError("db"))):
        await sf.send_prep_note(wa, DERMA, PHONE, T1, "en")
    wa.send_text.assert_not_awaited()


# ── callback lead ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_notifies_the_clinic_once_per_day():
    t = _treatment(1)
    m = _manager()
    fake_supabase = MagicMock()
    alert = AsyncMock()
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=t)), \
         patch.object(sf, "get_conversation", AsyncMock(return_value={"context": {}})), \
         patch.object(sf, "get_patient_by_phone", AsyncMock(return_value={"name": "Asha Rao"})), \
         patch.object(sf, "supabase", fake_supabase), \
         patch.object(sf, "sb", AsyncMock(return_value=MagicMock(data=[]))), \
         patch.object(sf, "update_conversation", AsyncMock()) as save_ctx, \
         patch.object(sf, "log_analytics_event", AsyncMock()), \
         patch("app.services.payment.payment_service._alert_admin", alert):
        await sf.request_callback(m, DERMA, PHONE, t["id"], "en")
    row = fake_supabase.table.return_value.insert.call_args.args[0]
    assert row["clinic_id"] == "clinic-1" and "Treatment 1" in row["title"]
    alert.assert_awaited_once()
    saved = save_ctx.await_args.args[2]["context"]["treatment_callbacks"]
    assert t["id"] in saved


@pytest.mark.asyncio
async def test_repeat_callback_within_24h_is_not_sent_again():
    t = _treatment(1)
    recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    m = _manager()
    fake_supabase = MagicMock()
    with patch.object(sf, "get_treatment_by_id", AsyncMock(return_value=t)), \
         patch.object(sf, "get_conversation", AsyncMock(return_value={"context": {"treatment_callbacks": {t["id"]: recent}}})), \
         patch.object(sf, "supabase", fake_supabase):
        await sf.request_callback(m, DERMA, PHONE, t["id"], "en")
    fake_supabase.table.assert_not_called()
    assert "already" in m.whatsapp.send_text.await_args.args[2].lower()


# ── concern search ───────────────────────────────────────────────────────────

def test_keyword_match_ranks_phrase_hits_first():
    ts = [
        {"id": "1", "name": "Laser Hair Reduction", "category": "Laser", "concerns": "unwanted hair", "display_order": 1},
        {"id": "2", "name": "Hair PRP Therapy", "category": "Hair & Scalp", "concerns": "hair fall, thinning", "display_order": 2},
        {"id": "3", "name": "Chemical Peel", "category": "Pigmentation", "concerns": "tan", "display_order": 3},
    ]
    assert [t["id"] for t in sf.match_treatments(ts, "Hair fall")] == ["2", "1"]
    assert sf.match_treatments(ts, "the and for") == []
    assert sf.match_treatments(ts, "") == []


@pytest.mark.asyncio
async def test_search_uses_keywords_then_ai_then_categories():
    ts = [_treatment(1, concerns="hair fall"), _treatment(2, category="Acne & Scars", concerns="acne scars")]
    m = _manager()
    with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=ts)), \
         patch.object(sf, "rank_treatments_for_concern", AsyncMock()) as rank:
        await sf.handle_treatment_search_text(m, DERMA, PHONE, "hair fall", "en")
    rank.assert_not_awaited()
    assert [r["id"] for r in _rows(m.whatsapp.send_interactive_list.await_args)] == [f"trt_{ts[0]['id']}"]

    m = _manager()
    with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=ts)), \
         patch.object(sf, "rank_treatments_for_concern", AsyncMock(return_value=[ts[1]["id"], "not-in-catalogue"])):
        await sf.handle_treatment_search_text(m, DERMA, PHONE, "marks after pimples", "en")
    assert [r["id"] for r in _rows(m.whatsapp.send_interactive_list.await_args)] == [f"trt_{ts[1]['id']}"]

    m = _manager()
    with patch.object(sf, "get_specialty_treatments", AsyncMock(return_value=ts)), \
         patch.object(sf, "rank_treatments_for_concern", AsyncMock(return_value=[])), \
         patch.object(sf, "show_treatment_categories", AsyncMock()) as cats:
        await sf.handle_treatment_search_text(m, DERMA, PHONE, "something unrelated", "en")
    cats.assert_awaited_once()


@pytest.mark.asyncio
async def test_emergency_words_in_search_go_to_emergency():
    from app.services.ai_engine import EMERGENCY_KEYWORDS

    word = sorted(EMERGENCY_KEYWORDS)[0]
    m = _manager()
    with patch.object(sf, "get_specialty_treatments", AsyncMock()) as get:
        await sf.handle_treatment_search_text(m, DERMA, PHONE, f"I have {word}", "en")
    m._handle_emergency.assert_awaited_once()
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_firewall_offer_is_silent_for_existing_plans():
    m = _manager()
    await sf.offer_treatment_browse(m, POLY, PHONE, "en")
    m.whatsapp.send_interactive_buttons.assert_not_awaited()
