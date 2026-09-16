# Task 7 (Part A) — `app/services/specialty_flow.py`: the WhatsApp specialty flow

Part A creates the module and tests it in isolation. Part B (`07b-conversation-wiring.md`) connects it to `conversation.py`. Commit A before starting B.

**Files:**
- Create: `app/services/specialty_flow.py`
- Test: `tests/test_specialty_whatsapp_flow.py`

**Interfaces:**
- Consumes: Task 2 (`specialty_enabled`, `SPECIALTY_BY_PLAN`), Task 3 (DB helpers, `CONCERN_EXAMPLES`), Task 4 (`rank_treatments_for_concern`), and these `ConversationManager` members, all of which exist today:
  - `manager.whatsapp.send_text(clinic, phone, text, ...)`
  - `manager.whatsapp.send_interactive_list(clinic, phone, body=..., button_text=..., sections=..., header=...)`
  - `manager.whatsapp.send_interactive_buttons(clinic, phone, body=..., buttons=...)`
  - `manager.update_state(clinic, phone, new_state, new_context=None, reset_context=False)`
  - `manager._page_rows(rows, page, more_id, lang) -> (rows, page)`
  - `manager._send_main_menu(clinic, phone, lang)`
  - `manager._handle_emergency(clinic, phone, lang)`
  - `manager._start_booking(clinic, phone, patient, lang, seed_context=None)` (the `seed_context` parameter is added in Part B)
- Produces (Part B and Task 8 use exactly these names):
  - Constants: `TREATMENT_CONTEXT_KEYS`, `TREATMENT_RESET_STATES`, `TREATMENT_BUTTON_IDS`, `TREATMENT_BUTTON_PREFIXES`
  - `clear_treatment_context(context: dict) -> dict`
  - `is_specialty_plan(clinic) -> bool`
  - `async treatment_menu_active(clinic) -> bool`
  - `treatment_menu_rows(lang) -> list[dict]`
  - `match_treatments(treatments, query) -> list[dict]`
  - `ordered_categories(treatments) -> list[str]`
  - `async show_treatment_categories(manager, clinic, phone, lang, page=0)`
  - `async show_treatments_in_category(manager, clinic, phone, category, lang, page=0, treatments=None)`
  - `async show_treatment_card(manager, clinic, phone, treatment_id, lang)`
  - `async start_treatment_booking(manager, clinic, phone, treatment_id, lang)`
  - `async route_to_treatment_doctors(manager, clinic, phone, context, lang) -> bool`
  - `async show_treatment_doctors(manager, clinic, phone, context, lang, page=0)`
  - `async revalidate_treatment(clinic, context) -> None`
  - `async send_prep_note(whatsapp, clinic, phone, treatment_id, lang) -> None`
  - `async request_callback(manager, clinic, phone, treatment_id, lang)`
  - `async prompt_concern(manager, clinic, phone, lang)`
  - `async handle_treatment_search_text(manager, clinic, phone, message, lang)`
  - `async handle_treatment_button(manager, clinic, phone, button_id, session, lang)`
  - `async handle_treatment_state(manager, clinic, phone, lang)`
  - `async offer_treatment_browse(manager, clinic, phone, lang)`

**Conversation states used:** `browsing_treatments` and `searching_treatments`. Both are new and deliberately **not** in `MID_BOOKING_STATES`, so no 30-minute booking timeout applies to them. `browsing_lab_tests` works the same way.

**WhatsApp ids used:** `menu_treatments`, `menu_concern`, `trtcat_<index>`, `trtcat_more`, `trt_<uuid>`, `trt_more`, `trtbook_<uuid>`, `trtcall_<uuid>`, and the existing `doc_<uuid>`, `doc_more` and `main_menu`.

---

- [ ] **Step 1: Write the failing test** — create `tests/test_specialty_whatsapp_flow.py`:

```python
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
```

- [ ] **Step 2: Run and confirm failure**

```bash
pytest tests/test_specialty_whatsapp_flow.py -q
```
Expected: `ImportError: cannot import name 'specialty_flow'`.

- [ ] **Step 3: Create `app/services/specialty_flow.py`** (exact content):

```python
"""WhatsApp flow for specialty hospitals (derma / eye / dental / ivf) — migration 077.

Functions take the ConversationManager as `manager` instead of living on it, so
conversation.py only gains routing lines and this flow can be reviewed alone.

Every entry point returns early unless specialty_enabled(clinic), which is
False for every tenant that existed before this release (see tenant.py for why
that is not has_feature()).

A treatment booking is an ordinary consultation: this module only chooses the
treatment and the doctors who perform it, then hands over to the existing
branch -> who-for -> name -> doctor -> date -> slot -> confirm -> payment flow.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import (
    get_conversation,
    get_doctors,
    get_patient_by_phone,
    get_specialty_treatments,
    get_treatment_by_id,
    get_treatment_doctor_ids,
    has_active_treatments,
    log_analytics_event,
    sb,
    supabase,
    update_conversation,
)
from app.services.ai_engine import EMERGENCY_KEYWORDS, rank_treatments_for_concern
from app.services.specialty_catalog import CONCERN_EXAMPLES
from app.services.tenant import SPECIALTY_BY_PLAN, specialty_enabled

logger = logging.getLogger(__name__)

#: Context keys owned by this flow. update_state() drops them whenever the
#: patient lands in a state the treatment flow never uses, so an abandoned
#: treatment can never tag a later, unrelated booking.
TREATMENT_CONTEXT_KEYS = (
    "treatment_id",
    "treatment_name",
    "treatment_categories",
    "treatment_category",
    "treatment_page",
    "treatment_cat_page",
)
TREATMENT_RESET_STATES = frozenset({
    "main_menu",
    "idle",
    "selecting_department",
    "suggesting_department",
    "collecting_symptoms",
})

TREATMENT_BUTTON_IDS = frozenset({"menu_treatments", "menu_concern", "trtcat_more", "trt_more"})
TREATMENT_BUTTON_PREFIXES = ("trtcat_", "trtbook_", "trtcall_", "trt_")

CALLBACK_COOLDOWN = timedelta(hours=24)
_BODY_LIMIT = 1024
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "have", "has", "from", "since", "very", "about",
    "treatment", "problem", "issue", "doctor", "clinic", "want", "need", "what",
    "which", "some", "there", "this", "that", "please", "help", "get", "got",
})


def _t(lang: str, en: str, hi: str, te: str) -> str:
    return {"en": en, "hi": hi, "te": te}.get(lang, en)


def is_specialty_plan(clinic: Optional[dict]) -> bool:
    return (clinic or {}).get("plan") in SPECIALTY_BY_PLAN


def clear_treatment_context(context: dict) -> dict:
    for key in TREATMENT_CONTEXT_KEYS:
        context.pop(key, None)
    return context


async def treatment_menu_active(clinic: Optional[dict]) -> bool:
    """The specialty rows appear only for an enabled clinic with at least one
    active treatment. Existing plans return before any database call."""
    if not specialty_enabled(clinic) or not (clinic or {}).get("id"):
        return False
    return await has_active_treatments(clinic["id"])


def treatment_menu_rows(lang: str) -> list:
    return [
        {
            "id": "menu_treatments",
            "title": _t(lang, "✨ Our Treatments", "✨ हमारे उपचार", "✨ మా చికిత్సలు")[:24],
            "description": _t(lang, "Explore & book treatments", "उपचार देखें और बुक करें",
                              "చికిత్సలు చూసి బుక్ చేయండి")[:72],
        },
        {
            "id": "menu_concern",
            "title": _t(lang, "🔍 Find by Concern", "🔍 समस्या से खोजें", "🔍 సమస్యతో వెతకండి")[:24],
            "description": _t(lang, "Describe your problem", "अपनी समस्या बताएं", "మీ సమస్య చెప్పండి")[:72],
        },
    ]


def _category(treatment: dict) -> str:
    return (treatment.get("category") or "").strip() or "General"


def _title(treatment: dict) -> str:
    return ((treatment.get("short_name") or treatment.get("name") or "Treatment").strip())[:24]


def localized_description(treatment: dict, lang: str) -> str:
    if lang in ("hi", "te"):
        text = (treatment.get(f"description_{lang}") or "").strip()
        if text:
            return text
    return (treatment.get("description") or "").strip()


def price_line(treatment: dict, lang: str) -> str:
    paise = int(treatment.get("price_from_paise") or 0)
    if paise <= 0:
        return _t(lang, "💰 Price: shared after consultation", "💰 कीमत: परामर्श के बाद बताई जाएगी",
                  "💰 ధర: సంప్రదింపు తర్వాత తెలియజేస్తాము")
    rupees = f"{paise // 100:,}"
    return _t(lang, f"💰 Starts from ₹{rupees}", f"💰 ₹{rupees} से शुरू", f"💰 ₹{rupees} నుండి ప్రారంభం")


def _row_description(treatment: dict, lang: str) -> str:
    desc = localized_description(treatment, lang)
    first_line = desc.split("\n", 1)[0].strip() if desc else ""
    return (first_line or price_line(treatment, lang))[:72]


def _truncate_body(text: str) -> str:
    return text if len(text) <= _BODY_LIMIT else text[: _BODY_LIMIT - 4].rstrip() + "…"


def ordered_categories(treatments: list) -> list:
    """Categories in the order the admin arranged treatments (lowest display_order first)."""
    first_order: dict = {}
    for t in treatments:
        cat = _category(t)
        order = int(t.get("display_order") or 0)
        if cat not in first_order or order < first_order[cat]:
            first_order[cat] = order
    return sorted(first_order, key=lambda c: (first_order[c], c.lower()))


def match_treatments(treatments: list, query: str) -> list:
    """Deterministic, free, instant: rank treatments by words the patient typed.

    A whole-phrase hit outranks scattered word hits; ties keep the admin's order.
    """
    q = (query or "").lower().strip()
    if not q:
        return []
    words = [w for w in re.findall(r"[^\W\d_]+", q) if len(w) >= 3 and w not in _STOPWORDS]
    scored = []
    for t in treatments:
        hay = " ".join(filter(None, [t.get("name"), t.get("short_name"), t.get("category"), t.get("concerns")])).lower()
        score = (5 if len(q) >= 3 and q in hay else 0) + sum(1 for w in words if w in hay)
        if score:
            scored.append((-score, int(t.get("display_order") or 0), (t.get("name") or "").lower(), t))
    scored.sort(key=lambda s: s[:3])
    return [s[3] for s in scored]


async def _treatment_doctors(clinic_id: str, treatment_id: str, branch_id: Optional[str] = None) -> list:
    """Active doctors who perform the treatment, at the branch when one is chosen.

    No mapping means any active doctor. get_doctors() already applies the
    doctor_branches junction when branch_id is given.
    """
    doctors = await get_doctors(clinic_id, branch_id=branch_id)
    mapped = await get_treatment_doctor_ids(clinic_id, treatment_id)
    if mapped:
        doctors = [d for d in doctors if str(d.get("id")) in mapped]
    doctors = [d for d in doctors if d.get("id") and d.get("is_active", True)]
    return sorted(doctors, key=lambda d: (d.get("name") or "").lower())


async def return_to_main_menu(manager, clinic: dict, phone: str, lang: str) -> None:
    await manager.update_state(clinic, phone, "main_menu", {"menu_shown": False})
    await manager._send_main_menu(clinic, phone, lang)


async def _send_unavailable(manager, clinic: dict, phone: str, lang: str) -> None:
    await manager.whatsapp.send_text(
        clinic,
        phone,
        _t(lang,
           "Sorry, this treatment is no longer listed. Here are our current treatments.",
           "क्षमा करें, यह उपचार अब उपलब्ध नहीं है। हमारे मौजूदा उपचार नीचे हैं।",
           "క్షమించండి, ఈ చికిత్స ఇప్పుడు అందుబాటులో లేదు. మా ప్రస్తుత చికిత్సలు ఇవి."),
    )
    await show_treatment_categories(manager, clinic, phone, lang)


# ── Browsing ─────────────────────────────────────────────────────────────────

async def show_treatment_categories(manager, clinic: dict, phone: str, lang: str, page: int = 0) -> None:
    treatments = await get_specialty_treatments(clinic["id"]) if specialty_enabled(clinic) else []
    if not treatments:
        await return_to_main_menu(manager, clinic, phone, lang)
        return

    categories = ordered_categories(treatments)
    if len(categories) == 1:
        await show_treatments_in_category(manager, clinic, phone, categories[0], lang, treatments=treatments)
        return

    counts: dict = {}
    for t in treatments:
        counts[_category(t)] = counts.get(_category(t), 0) + 1

    all_rows = [
        {
            "id": f"trtcat_{i}",
            "title": cat[:24],
            "description": _t(lang, f"{counts[cat]} treatments", f"{counts[cat]} उपचार", f"{counts[cat]} చికిత్సలు")[:72],
        }
        for i, cat in enumerate(categories)
    ]
    rows, page = manager._page_rows(all_rows, page, "trtcat_more", lang)
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=_t(lang, "Our Treatments", "हमारे उपचार", "మా చికిత్సలు")[:60],
        body=_t(lang,
                "Choose a category to see the treatments we offer.",
                "हमारे उपचार देखने के लिए एक श्रेणी चुनें।",
                "మా చికిత్సలు చూడటానికి ఒక విభాగాన్ని ఎంచుకోండి."),
        button_text=_t(lang, "View", "देखें", "చూడండి"),
        sections=[{"title": _t(lang, "Categories", "श्रेणियाँ", "విభాగాలు")[:24], "rows": rows}],
    )
    await manager.update_state(
        clinic, phone, "browsing_treatments",
        {"treatment_categories": categories, "treatment_cat_page": page},
    )


async def show_treatments_in_category(
    manager, clinic: dict, phone: str, category: str, lang: str,
    page: int = 0, treatments: Optional[list] = None,
) -> None:
    if treatments is None:
        treatments = await get_specialty_treatments(clinic["id"])
    in_category = [t for t in treatments if _category(t) == category]
    if not in_category:
        await show_treatment_categories(manager, clinic, phone, lang)
        return

    all_rows = [
        {"id": f"trt_{t['id']}", "title": _title(t), "description": _row_description(t, lang)}
        for t in in_category
    ]
    rows, page = manager._page_rows(all_rows, page, "trt_more", lang)
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=category[:60],
        body=_t(lang,
                "Tap a treatment to learn more and book.",
                "जानकारी और बुकिंग के लिए किसी उपचार पर टैप करें।",
                "వివరాలు, బుకింగ్ కోసం ఒక చికిత్సను నొక్కండి."),
        button_text=_t(lang, "View", "देखें", "చూడండి"),
        sections=[{"title": _t(lang, "Treatments", "उपचार", "చికిత్సలు")[:24], "rows": rows}],
    )
    await manager.update_state(
        clinic, phone, "browsing_treatments",
        {"treatment_category": category, "treatment_page": page},
    )


def _card_buttons(treatment_id: str, lang: str) -> list:
    return [
        {"id": f"trtbook_{treatment_id}", "title": _t(lang, "Book Consultation", "परामर्श बुक करें", "కన్సల్టేషన్ బుక్")},
        {"id": f"trtcall_{treatment_id}", "title": _t(lang, "Request Callback", "कॉल बैक करें", "కాల్ బ్యాక్")},
        {"id": "menu_treatments", "title": _t(lang, "All Treatments", "सभी उपचार", "అన్ని చికిత్సలు")},
    ]


async def show_treatment_card(manager, clinic: dict, phone: str, treatment_id: str, lang: str) -> None:
    treatment = await get_treatment_by_id(clinic["id"], treatment_id)
    if not treatment:
        await _send_unavailable(manager, clinic, phone, lang)
        return

    lines = [f"✨ *{treatment['name']}*", f"_{_category(treatment)}_", ""]
    description = localized_description(treatment, lang)
    if description:
        lines += [description, ""]
    duration = treatment.get("duration_minutes")
    if duration:
        lines.append(_t(lang, f"⏱️ Visit time: about {duration} min",
                        f"⏱️ समय: लगभग {duration} मिनट", f"⏱️ సమయం: సుమారు {duration} నిమిషాలు"))
    lines.append(price_line(treatment, lang))

    doctors = await _treatment_doctors(clinic["id"], treatment["id"])
    if doctors:
        names = ", ".join(d.get("name") or "" for d in doctors[:3])
        if len(doctors) > 3:
            names += f" +{len(doctors) - 3}"
        lines.append(f"👨‍⚕️ {_t(lang, 'Specialists', 'विशेषज्ञ', 'నిపుణులు')}: {names}")

    prep = (treatment.get("prep_instructions") or "").strip()
    if prep:
        lines += ["", f"📝 *{_t(lang, 'Before your visit', 'आने से पहले', 'రావడానికి ముందు')}:* {prep[:300]}"]

    lines += ["", _t(lang,
                     "_Our specialist will examine you and confirm what suits you._",
                     "_हमारे विशेषज्ञ जांच के बाद बताएंगे कि आपके लिए क्या सही है।_",
                     "_మా నిపుణులు పరీక్షించి మీకు ఏది సరిపోతుందో చెబుతారు._")]

    await manager.whatsapp.send_interactive_buttons(
        clinic,
        phone,
        body=_truncate_body("\n".join(lines)),
        buttons=_card_buttons(str(treatment["id"]), lang),
    )
    await manager.update_state(clinic, phone, "browsing_treatments", {})


# ── Booking hand-off ─────────────────────────────────────────────────────────

async def start_treatment_booking(manager, clinic: dict, phone: str, treatment_id: str, lang: str) -> None:
    treatment = await get_treatment_by_id(clinic["id"], treatment_id)
    if not treatment:
        await _send_unavailable(manager, clinic, phone, lang)
        return
    patient = await get_patient_by_phone(clinic["id"], phone)
    await manager._start_booking(
        clinic, phone, patient, lang,
        seed_context={"treatment_id": str(treatment["id"]), "treatment_name": treatment["name"]},
    )


async def route_to_treatment_doctors(manager, clinic: dict, phone: str, context: dict, lang: str) -> bool:
    """Called where the normal flow would ask for symptoms. True = handled."""
    if not context.get("treatment_id"):
        return False
    await show_treatment_doctors(manager, clinic, phone, context, lang)
    return True


def _doctor_row_description(doctor: dict) -> str:
    parts = [p for p in [doctor.get("specialization")] if p]
    if doctor.get("consultation_fee") is not None:
        parts.append(f"₹{doctor['consultation_fee']}")
    return " · ".join(str(p) for p in parts)[:72]


async def show_treatment_doctors(manager, clinic: dict, phone: str, context: dict, lang: str, page: int = 0) -> None:
    treatment = await get_treatment_by_id(clinic["id"], context.get("treatment_id"))
    if not treatment:
        await manager.whatsapp.send_text(
            clinic, phone,
            _t(lang,
               "Sorry, this treatment is no longer listed.",
               "क्षमा करें, यह उपचार अब उपलब्ध नहीं है।",
               "క్షమించండి, ఈ చికిత్స ఇప్పుడు అందుబాటులో లేదు."),
        )
        clear_treatment_context(context)
        await return_to_main_menu(manager, clinic, phone, lang)
        return

    name = treatment["name"]
    doctors = await _treatment_doctors(clinic["id"], treatment["id"], context.get("branch_id"))
    if not doctors:
        branch = context.get("branch_name")
        where = {
            "en": f" at {branch}" if branch else "",
            "hi": f" {branch} में" if branch else "",
            "te": f" {branch}లో" if branch else "",
        }
        await manager.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=_t(lang,
                    f"Sorry, no specialist for *{name}* is available for online booking{where['en']} right now. "
                    "Tap *Request Callback* and our team will call you.",
                    f"क्षमा करें, *{name}* के लिए अभी{where['hi']} ऑनलाइन बुकिंग हेतु कोई विशेषज्ञ उपलब्ध नहीं है। "
                    "*कॉल बैक* दबाएं, हमारी टीम आपको कॉल करेगी।",
                    f"క్షమించండి, *{name}* కోసం ప్రస్తుతం{where['te']} ఆన్‌లైన్ బుకింగ్‌కు నిపుణులు అందుబాటులో లేరు. "
                    "*కాల్ బ్యాక్* నొక్కండి, మా బృందం కాల్ చేస్తుంది."),
            buttons=[
                {"id": f"trtcall_{treatment['id']}", "title": _t(lang, "Request Callback", "कॉल बैक करें", "కాల్ బ్యాక్")},
                {"id": "main_menu", "title": _t(lang, "Main Menu", "मुख्य मेनू", "ప్రధాన మెనూ")},
            ],
        )
        await manager.update_state(clinic, phone, "main_menu", {"menu_shown": False})
        return

    all_rows = [
        {"id": f"doc_{d['id']}", "title": (d.get("name") or "Doctor")[:24], "description": _doctor_row_description(d)}
        for d in doctors
    ]
    rows, page = manager._page_rows(all_rows, page, "doc_more", lang)
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=_t(lang, "Choose Your Specialist", "अपना विशेषज्ञ चुनें", "మీ నిపుణుడిని ఎంచుకోండి")[:60],
        body=_t(lang, f"Specialists for *{name}*:", f"*{name}* के विशेषज्ञ:", f"*{name}* కోసం నిపుణులు:"),
        button_text=_t(lang, "Select Doctor", "डॉक्टर चुनें", "డాక్టర్‌ ఎంచుకోండి"),
        sections=[{"title": _t(lang, "Specialists", "विशेषज्ञ", "నిపుణులు")[:24], "rows": rows}],
    )
    context.update({
        "treatment_id": str(treatment["id"]),
        "treatment_name": name,
        "symptoms": f"Treatment: {name}",
        "doctor_page": page,
    })
    await manager.update_state(clinic, phone, "selecting_doctor", context)


async def revalidate_treatment(clinic: dict, context: dict) -> None:
    """Just before the booking is written. A deleted treatment would fail the
    foreign key and lose the booking, so the tag is dropped and the patient's
    consultation goes ahead. A hidden (inactive) treatment still exists and
    keeps its tag — the patient chose it while it was listed."""
    treatment_id = context.get("treatment_id")
    if not treatment_id:
        return
    row = await get_treatment_by_id(clinic["id"], treatment_id, active_only=False)
    if not row:
        logger.warning(f"Treatment {treatment_id} vanished mid-booking for clinic {clinic.get('id')}; booking without tag")
        clear_treatment_context(context)


async def send_prep_note(whatsapp, clinic: dict, phone: str, treatment_id: Optional[str], lang: str) -> None:
    """'Before your visit' instructions after a confirmed booking. Never raises:
    the booking is already made and must not look failed."""
    if not treatment_id:
        return
    try:
        row = await get_treatment_by_id(clinic["id"], treatment_id, active_only=False)
        prep = ((row or {}).get("prep_instructions") or "").strip()
        if not prep:
            return
        header = _t(lang, "Before your visit", "आने से पहले", "రావడానికి ముందు")
        await whatsapp.send_text(clinic, phone, f"📝 *{header} — {row.get('name', '')}*\n{prep}")
    except Exception as e:
        logger.warning(f"Could not send treatment prep note to {phone[:6]}***: {e}")


# ── Callback lead ────────────────────────────────────────────────────────────

async def request_callback(manager, clinic: dict, phone: str, treatment_id: str, lang: str) -> None:
    treatment = await get_treatment_by_id(clinic["id"], treatment_id, active_only=False)
    if not treatment:
        await _send_unavailable(manager, clinic, phone, lang)
        return
    name = treatment["name"]
    key = str(treatment["id"])

    session = await get_conversation(clinic["id"], phone) or {}
    ctx = dict(session.get("context") or {})
    requested = dict(ctx.get("treatment_callbacks") or {})
    now = datetime.now(timezone.utc)
    last = requested.get(key)
    if last:
        try:
            if now - datetime.fromisoformat(last) < CALLBACK_COOLDOWN:
                await manager.whatsapp.send_text(
                    clinic, phone,
                    _t(lang,
                       f"We already have your callback request for *{name}*. Our team will call you soon.",
                       f"*{name}* के लिए आपका कॉल बैक अनुरोध हमें मिल चुका है। हमारी टीम जल्द ही कॉल करेगी।",
                       f"*{name}* కోసం మీ కాల్ బ్యాక్ అభ్యర్థన ఇప్పటికే అందింది. మా బృందం త్వరలో కాల్ చేస్తుంది."),
                )
                return
        except (TypeError, ValueError):
            pass

    patient = await get_patient_by_phone(clinic["id"], phone)
    patient_name = (patient or {}).get("name") or "Patient"

    try:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("admin_notifications").insert({
            "clinic_id": clinic["id"],
            "admin_id": None,
            "title": f"Callback request: {name}"[:120],
            "message": f"{patient_name} ({phone}) asked for a call about {name} on WhatsApp.",
            "is_read": False,
            "created_at": now.isoformat(),
        }))
    except Exception as e:
        logger.warning(f"Could not create callback notification for clinic {clinic.get('id')}: {e}")

    try:
        from app.services.payment import payment_service

        await payment_service._alert_admin(
            clinic,
            f"📞 *Callback Requested*\n\n👤 {patient_name} ({phone})\n🩺 {name}\n\nPlease call the patient.",
        )
    except Exception as e:
        logger.warning(f"Could not send callback alert for clinic {clinic.get('id')}: {e}")

    requested[key] = now.isoformat()
    ctx["treatment_callbacks"] = requested
    await update_conversation(clinic["id"], phone, {"context": ctx})
    await log_analytics_event(clinic["id"], phone, "treatment_callback_requested")

    await manager.whatsapp.send_interactive_buttons(
        clinic,
        phone,
        body=_t(lang,
                f"✅ Thank you! Our team will call you on this number about *{name}* during clinic hours.",
                f"✅ धन्यवाद! हमारी टीम क्लिनिक समय में *{name}* के बारे में इसी नंबर पर आपको कॉल करेगी।",
                f"✅ ధన్యవాదాలు! మా బృందం క్లినిక్ సమయంలో *{name}* గురించి ఈ నంబర్‌కు కాల్ చేస్తుంది."),
        buttons=[{"id": "main_menu", "title": _t(lang, "Main Menu", "मुख्य मेनू", "ప్రధాన మెనూ")}],
    )


# ── Concern search ───────────────────────────────────────────────────────────

async def prompt_concern(manager, clinic: dict, phone: str, lang: str) -> None:
    examples = CONCERN_EXAMPLES.get(SPECIALTY_BY_PLAN.get(clinic.get("plan")), "hair fall, tooth pain, blurred vision")
    await manager.whatsapp.send_text(
        clinic,
        phone,
        _t(lang,
           f"🔍 Tell us your concern in a few words.\nFor example: _{examples}_\n\n"
           "We will show treatments at our clinic that may help. Our specialist confirms what suits you after an examination.",
           f"🔍 अपनी समस्या कुछ शब्दों में लिखें।\nउदाहरण: _{examples}_\n\n"
           "हम अपने क्लिनिक के ऐसे उपचार दिखाएंगे जो मदद कर सकते हैं। जांच के बाद विशेषज्ञ बताएंगे कि आपके लिए क्या सही है।",
           f"🔍 మీ సమస్యను కొన్ని మాటల్లో రాయండి.\nఉదాహరణ: _{examples}_\n\n"
           "సహాయపడగల మా క్లినిక్ చికిత్సలను చూపిస్తాము. పరీక్ష తర్వాత నిపుణులు మీకు ఏది సరిపోతుందో చెబుతారు."),
    )
    await manager.update_state(clinic, phone, "searching_treatments", {})


async def handle_treatment_search_text(manager, clinic: dict, phone: str, message: str, lang: str) -> None:
    query = (message or "").strip()[:200]
    if any(kw in query.lower() for kw in EMERGENCY_KEYWORDS):
        await manager._handle_emergency(clinic, phone, lang)
        return
    if len(query) < 3:
        await manager.whatsapp.send_text(
            clinic, phone,
            _t(lang,
               "Please type a few words about your concern, for example: hair fall.",
               "कृपया अपनी समस्या कुछ शब्दों में लिखें, जैसे: बाल झड़ना।",
               "దయచేసి మీ సమస్యను కొన్ని మాటల్లో రాయండి, ఉదా: జుట్టు రాలడం."),
        )
        return

    treatments = await get_specialty_treatments(clinic["id"]) if specialty_enabled(clinic) else []
    if not treatments:
        await return_to_main_menu(manager, clinic, phone, lang)
        return

    matches = match_treatments(treatments, query)[:9]
    if not matches:
        by_id = {str(t["id"]): t for t in treatments}
        ranked = await rank_treatments_for_concern(query, treatments, clinic)
        matches = [by_id[i] for i in ranked if i in by_id]

    if not matches:
        await manager.whatsapp.send_text(
            clinic, phone,
            _t(lang,
               "I couldn't find a treatment matching that. Here are all our treatment categories. "
               "You can also choose *Talk to Staff* from the menu.",
               "इससे मिलता उपचार नहीं मिला। हमारी सभी उपचार श्रेणियाँ नीचे हैं। आप मेनू से *Talk to Staff* भी चुन सकते हैं।",
               "దీనికి సరిపోయే చికిత్స దొరకలేదు. మా అన్ని చికిత్స విభాగాలు ఇవి. మెనూ నుండి *Talk to Staff* కూడా ఎంచుకోవచ్చు."),
        )
        await show_treatment_categories(manager, clinic, phone, lang)
        return

    shown = query[:60]
    await manager.whatsapp.send_interactive_list(
        clinic,
        phone,
        header=_t(lang, "Treatments that may help", "उपयोगी उपचार", "ఉపయోగపడే చికిత్సలు")[:60],
        body=_t(lang,
                f"These treatments at our clinic may be relevant to *{shown}*. Tap one to learn more.\n\n"
                "_Only our specialist can confirm what suits you._",
                f"*{shown}* के लिए हमारे क्लिनिक के ये उपचार उपयोगी हो सकते हैं। अधिक जानने के लिए किसी एक पर टैप करें।\n\n"
                "_आपके लिए क्या सही है, यह केवल हमारे विशेषज्ञ बता सकते हैं।_",
                f"*{shown}* కోసం మా క్లినిక్‌లోని ఈ చికిత్సలు ఉపయోగపడవచ్చు. మరింత తెలుసుకోవడానికి ఒకదాన్ని నొక్కండి.\n\n"
                "_మీకు ఏది సరిపోతుందో మా నిపుణులు మాత్రమే నిర్ధారించగలరు._"),
        button_text=_t(lang, "View", "देखें", "చూడండి"),
        sections=[{
            "title": _t(lang, "Treatments", "उपचार", "చికిత్సలు")[:24],
            "rows": [{"id": f"trt_{t['id']}", "title": _title(t), "description": _row_description(t, lang)} for t in matches],
        }],
    )
    await manager.update_state(clinic, phone, "searching_treatments", {})


# ── Routing entry points used by conversation.py ────────────────────────────

async def handle_treatment_button(manager, clinic: dict, phone: str, button_id: str, session: dict, lang: str) -> None:
    if not specialty_enabled(clinic):
        # A list tapped after the clinic's plan changed.
        await return_to_main_menu(manager, clinic, phone, lang)
        return
    ctx = (session or {}).get("context") or {}

    if button_id == "menu_treatments":
        await show_treatment_categories(manager, clinic, phone, lang)
    elif button_id == "menu_concern":
        await prompt_concern(manager, clinic, phone, lang)
    elif button_id == "trtcat_more":
        await show_treatment_categories(manager, clinic, phone, lang, page=int(ctx.get("treatment_cat_page") or 0) + 1)
    elif button_id == "trt_more":
        category = ctx.get("treatment_category")
        if category:
            await show_treatments_in_category(manager, clinic, phone, category, lang,
                                              page=int(ctx.get("treatment_page") or 0) + 1)
        else:
            await show_treatment_categories(manager, clinic, phone, lang)
    elif button_id.startswith("trtcat_"):
        categories = ctx.get("treatment_categories") or []
        index = button_id[len("trtcat_"):]
        if index.isdigit() and int(index) < len(categories):
            await show_treatments_in_category(manager, clinic, phone, categories[int(index)], lang)
        else:
            await show_treatment_categories(manager, clinic, phone, lang)
    elif button_id.startswith("trtbook_"):
        await start_treatment_booking(manager, clinic, phone, button_id[len("trtbook_"):], lang)
    elif button_id.startswith("trtcall_"):
        await request_callback(manager, clinic, phone, button_id[len("trtcall_"):], lang)
    elif button_id.startswith("trt_"):
        await show_treatment_card(manager, clinic, phone, button_id[len("trt_"):], lang)
    else:
        await return_to_main_menu(manager, clinic, phone, lang)


async def handle_treatment_state(manager, clinic: dict, phone: str, lang: str) -> None:
    """Anything in a browsing/search state that no handler above claimed."""
    await return_to_main_menu(manager, clinic, phone, lang)


async def offer_treatment_browse(manager, clinic: dict, phone: str, lang: str) -> None:
    """After the clinical firewall answers, point specialty patients at the
    catalogue instead of leaving them at a dead end. Never raises."""
    try:
        if not await treatment_menu_active(clinic):
            return
        await manager.whatsapp.send_interactive_buttons(
            clinic,
            phone,
            body=_t(lang,
                    "You can also explore the treatments offered at our clinic.",
                    "आप हमारे क्लिनिक में उपलब्ध उपचार भी देख सकते हैं।",
                    "మా క్లినిక్‌లో లభించే చికిత్సలను కూడా చూడవచ్చు."),
            buttons=[{"id": "menu_treatments", "title": _t(lang, "Our Treatments", "हमारे उपचार", "మా చికిత్సలు")}],
        )
    except Exception as e:
        logger.warning(f"Could not offer treatment browse to {phone[:6]}***: {e}")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_specialty_whatsapp_flow.py -q
```
Expected: all PASS. Known adjustments:
- If `ConversationManager.__new__` plus `_page_rows` fails in `_manager()` because `_page_rows` needs instance state, instantiate a real `ConversationManager()` and replace its `whatsapp`, `update_state` and `_send_main_menu` with mocks instead.
- If `EMERGENCY_KEYWORDS` contains a word that is also a treatment word, pick a different keyword in the test.
- If `app.services.payment` import in the callback test fails because `payment_service` is created at import time with network access, patch `app.services.specialty_flow.request_callback`'s alert path by patching `app.services.payment.PaymentService._alert_admin` instead.

Run the orphan check.

- [ ] **Step 5: Commit**

```bash
git add app/services/specialty_flow.py tests/test_specialty_whatsapp_flow.py
git commit -m "feat(whatsapp): specialty treatment flow module (browse, card, concern search, callback, doctor pick)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
