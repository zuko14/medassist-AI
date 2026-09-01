"""Lab-test catalogue search.

A diagnostics client imported ~1000 tests. The interactive list shows 9 rows
plus a "More options" row, so reaching test #500 meant ~55 taps: browsing
alone made the catalogue unbookable. These cover the search path that replaces
it, and the routing that has to survive intent misclassification for it to
work at all.
"""

import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("WHATSAPP_TOKEN", "test_token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "000000000000")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_verify_token")
os.environ.setdefault("WABA_DISPLAY_NAME", "Test Diagnostics")
os.environ.setdefault("GROQ_API_KEY", "test_groq_key")
os.environ.setdefault("GROQ_MODEL", "llama-3.3-70b-versatile")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test_service_role_key")
os.environ.setdefault("HOSPITAL_NAME", "Accumax Diagnostics")
os.environ.setdefault("HOSPITAL_EMERGENCY_NUMBER", "108")
os.environ.setdefault("HOSPITAL_PHONE", "+919876543210")
os.environ.setdefault("HOSPITAL_MAPS_LINK", "https://maps.google.com")
os.environ.setdefault("HOSPITAL_WEBSITE", "https://test.hospital.com")
os.environ.setdefault("HOSPITAL_PRIVACY_POLICY_URL", "https://test.hospital.com/privacy")
os.environ.setdefault("HOSPITAL_ADDRESS", "Test Address")
os.environ.setdefault("HOSPITAL_LANDMARK", "Test Landmark")
os.environ.setdefault("BOOKING_REF_PREFIX", "AD")
os.environ.setdefault("APP_ENV", "testing")
os.environ.setdefault("APP_PORT", "8000")
os.environ.setdefault("LOG_LEVEL", "DEBUG")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "admin")

if "app.database" in sys.modules and not hasattr(sys.modules["app.database"], "__file__"):
    del sys.modules["app.database"]

from app.services.conversation import ConversationManager  # noqa: E402


CLINIC = {"id": "clinic-1", "whatsapp_number": "+911111111111"}
PHONE = "+919999999999"


def _test(name, tid=None, price=50000):
    return {
        "id": tid or name.lower().replace(" ", "-"),
        "name": name,
        "price_paise": price,
        "sample_type": "Blood",
        "is_active": True,
        "branch_id": None,
    }


# Shaped like the real Accumax catalogue: the "24 Hrs URINE ..." block that
# fills page 1 alphabetically, with the tests people actually search for
# buried hundreds of rows behind it.
CATALOGUE = (
    [_test(f"24 Hrs URINE {x}") for x in
     ("MAGNESIUM", "MICROALBUMIN", "OXALATE", "PHOSPHORUS", "POTASSIUM",
      "PROTEIN", "PROTEIN CRE", "SODIUM", "URIC ACID")]
    + [_test(f"FILLER TEST {i:03d}") for i in range(300)]
    + [
        _test("THYROID PROFILE T3 T4 TSH"),
        _test("TSH ULTRASENSITIVE"),
        _test("PROFILE - THYROID ANTIBODY"),
        _test("COMPLETE BLOOD COUNT (CBC)"),
    ]
)


class TestMatching:
    def test_single_term_finds_test_buried_past_page_one(self):
        hits = ConversationManager._match_lab_tests(CATALOGUE, "thyroid")
        names = [t["name"] for t in hits]
        assert "THYROID PROFILE T3 T4 TSH" in names
        assert "PROFILE - THYROID ANTIBODY" in names
        assert len(names) == 2

    def test_all_terms_must_match_not_the_raw_phrase(self):
        # "thyroid profile" must find "PROFILE - THYROID ...", where the two
        # words are reversed and separated -- a substring search would miss it.
        names = [t["name"] for t in ConversationManager._match_lab_tests(CATALOGUE, "thyroid profile")]
        assert "PROFILE - THYROID ANTIBODY" in names
        assert "THYROID PROFILE T3 T4 TSH" in names

    def test_multi_word_narrows_within_a_prefix_block(self):
        names = [t["name"] for t in ConversationManager._match_lab_tests(CATALOGUE, "urine sodium")]
        assert names == ["24 Hrs URINE SODIUM"]

    def test_case_insensitive(self):
        assert ConversationManager._match_lab_tests(CATALOGUE, "TsH UlTrA")[0]["name"] == "TSH ULTRASENSITIVE"

    def test_best_match_lands_on_page_one(self):
        # A prefix match must outrank a mid-string one, or the answer hides
        # behind "More options" and search buys the patient nothing.
        hits = ConversationManager._match_lab_tests(CATALOGUE, "thyroid")
        assert hits[0]["name"] == "THYROID PROFILE T3 T4 TSH"

    def test_no_match_returns_empty_not_everything(self):
        assert ConversationManager._match_lab_tests(CATALOGUE, "zzzznotatest") == []

    def test_blank_query_returns_full_catalogue(self):
        assert len(ConversationManager._match_lab_tests(CATALOGUE, "   ")) == len(CATALOGUE)


def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_interactive_list = AsyncMock(return_value=True)
    m.whatsapp.send_text = AsyncMock(return_value=True)
    m.update_state = AsyncMock()
    return m


def _sent_rows(manager):
    kwargs = manager.whatsapp.send_interactive_list.await_args.kwargs
    return kwargs["sections"][0]["rows"], kwargs["body"]


class TestShowList:
    @pytest.mark.asyncio
    async def test_search_shows_only_matches(self):
        m = _manager()
        ctx = {}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=CATALOGUE):
            await m._show_lab_test_list(CLINIC, PHONE, ctx, "en", query="thyroid")

        rows, body = _sent_rows(m)
        assert [r["title"] for r in rows] == [
            "THYROID PROFILE T3 T4 TSH"[:24],
            "PROFILE - THYROID ANTIBODY"[:24],
        ]
        assert "More options" not in [r["title"] for r in rows]
        assert "thyroid" in body
        # Persisted so "More options" pages the search, not the full catalogue.
        assert ctx["lab_test_query"] == "thyroid"

    @pytest.mark.asyncio
    async def test_unfiltered_large_catalogue_advertises_search(self):
        m = _manager()
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=CATALOGUE):
            await m._show_lab_test_list(CLINIC, PHONE, {}, "en")

        rows, body = _sent_rows(m)
        assert "Type the test name to search" in body
        assert str(len(CATALOGUE)) in body
        assert rows[-1]["title"] == "More options"  # browsing still works

    @pytest.mark.asyncio
    async def test_small_catalogue_gets_no_search_nag(self):
        m = _manager()
        small = CATALOGUE[:5]
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=small):
            await m._show_lab_test_list(CLINIC, PHONE, {}, "en")

        _, body = _sent_rows(m)
        assert "search" not in body.lower()

    @pytest.mark.asyncio
    async def test_no_match_falls_back_to_full_list_instead_of_dead_ending(self):
        m = _manager()
        ctx = {}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=CATALOGUE):
            await m._show_lab_test_list(CLINIC, PHONE, ctx, "en", query="zzzznotatest")

        assert "No test matched" in m.whatsapp.send_text.await_args.args[2]
        rows, _ = _sent_rows(m)
        assert rows[0]["title"] == "24 Hrs URINE MAGNESIUM"[:24]
        assert ctx["lab_test_query"] is None
        assert ctx["lab_test_page"] == 0

    @pytest.mark.asyncio
    async def test_pasted_paragraph_cannot_blow_the_1024_char_body_cap(self):
        m = _manager()
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=CATALOGUE):
            await m._show_lab_test_list(CLINIC, PHONE, {}, "en", query="thyroid " + "x" * 5000)

        # No match, so it falls back to the full list -- but neither the
        # apology nor the body may carry the whole paste.
        assert len(m.whatsapp.send_text.await_args.args[2]) < 400
        _, body = _sent_rows(m)
        assert len(body) < 1024

    @pytest.mark.asyncio
    async def test_more_options_pages_within_the_search_results(self):
        m = _manager()
        # 30 matches: page 0 shows 9 + More, page 1 shows the next 9.
        many = [_test(f"VITAMIN D {i:03d}") for i in range(30)]
        ctx = {"lab_test_page": 0, "lab_test_query": "vitamin"}
        with patch("app.database.get_lab_tests", new_callable=AsyncMock, return_value=many + CATALOGUE):
            await m._handle_browsing_lab_tests(
                CLINIC, PHONE, "", "unknown", ctx, "en", {"id": "labtest_more"}
            )

        rows, _ = _sent_rows(m)
        titles = [r["title"] for r in rows]
        assert all(t.startswith("VITAMIN D") for t in titles[:-1]), titles
        assert titles[-1] == "More options"
        assert ctx["lab_test_query"] == "vitamin"


class TestTypedInputBecomesSearch:
    @pytest.mark.asyncio
    async def test_typed_text_searches_rather_than_re_showing_page_one(self):
        m = _manager()
        m._show_lab_test_list = AsyncMock()
        await m._handle_browsing_lab_tests(CLINIC, PHONE, "  Thyroid  ", "unknown", {}, "en", None)
        assert m._show_lab_test_list.await_args.kwargs["query"] == "Thyroid"

    @pytest.mark.asyncio
    async def test_all_resets_to_the_full_catalogue(self):
        m = _manager()
        m._show_lab_test_list = AsyncMock()
        await m._handle_browsing_lab_tests(
            CLINIC, PHONE, "all", "unknown", {"lab_test_query": "thyroid"}, "en", None
        )
        assert "query" not in m._show_lab_test_list.await_args.kwargs


class TestRouting:
    """A bare test name classifies as view_services/doctor_availability. At a
    diagnostics-only clinic both call _start_lab_booking, which rebuilds the
    context from scratch -- so without the guard the search would reset to
    page 1 of the unfiltered catalogue and never work in production.
    """

    async def _route(self, intent, message, state="browsing_lab_tests"):
        m = _manager()
        m._handle_browsing_lab_tests = AsyncMock()
        m._start_lab_booking = AsyncMock()
        m._send_main_menu = AsyncMock()
        m._show_doctors = AsyncMock()
        m._show_services = AsyncMock()
        m._is_diagnostics_only = AsyncMock(return_value=True)
        session = {"state": state, "context": {}}
        with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"):
            await m._process_state(
                CLINIC, PHONE, message, intent, session,
                {"id": "p1", "language": "en"}, "en", None,
            )
        return m

    @pytest.mark.asyncio
    @pytest.mark.parametrize("intent", ["view_services", "doctor_availability", "unknown"])
    async def test_test_name_reaches_the_search_handler(self, intent):
        m = await self._route(intent, "thyroid")
        m._handle_browsing_lab_tests.assert_awaited_once()
        m._start_lab_booking.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("word", ["menu", "MENU", " home ", "cancel", "back", "exit"])
    async def test_escape_hatches_reach_the_main_menu_not_the_search(self, word):
        # These arrive through the state machine as ordinary free text, so the
        # handler -- not the router -- is what has to let the patient out.
        m = _manager()
        m._send_main_menu = AsyncMock()
        m._show_lab_test_list = AsyncMock()
        await m._handle_browsing_lab_tests(CLINIC, PHONE, word, "unknown", {}, "en", None)
        m._send_main_menu.assert_awaited_once()
        m._show_lab_test_list.assert_not_awaited()
        assert m.update_state.await_args.args[2] == "main_menu"

    @pytest.mark.asyncio
    async def test_emergency_still_wins_over_search(self):
        m = _manager()
        m._handle_browsing_lab_tests = AsyncMock()
        m._handle_emergency = AsyncMock()
        session = {"state": "browsing_lab_tests", "context": {}}
        with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"):
            await m._process_state(
                CLINIC, PHONE, "chest pain", "emergency", session,
                {"id": "p1", "language": "en"}, "en", None,
            )
        m._handle_emergency.assert_awaited_once()
        m._handle_browsing_lab_tests.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guard_does_not_leak_into_other_states(self):
        m = await self._route("view_services", "thyroid", state="main_menu")
        m._handle_browsing_lab_tests.assert_not_awaited()
        m._start_lab_booking.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tapping_a_row_is_not_treated_as_a_search(self):
        m = _manager()
        m._handle_browsing_lab_tests = AsyncMock()
        m._start_lab_booking = AsyncMock()
        m._is_diagnostics_only = AsyncMock(return_value=True)
        session = {"state": "browsing_lab_tests", "context": {}}
        with patch("app.services.conversation.get_lang", new_callable=AsyncMock, return_value="en"):
            await m._process_state(
                CLINIC, PHONE, "THYROID PROFILE T3 T4 TSH", "unknown", session,
                {"id": "p1", "language": "en"}, "en", {"id": "labtest_abc"},
            )
        # Falls through to the state machine, which routes to the same handler
        # -- but exactly once, with the interactive payload intact.
        m._handle_browsing_lab_tests.assert_awaited_once()
        assert m._handle_browsing_lab_tests.await_args.args[-1] == {"id": "labtest_abc"}
