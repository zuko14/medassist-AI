"""Diagnostics catalogue experience (session 12, migration 083).

From a live Accumax chat: "Exit", then "Hi" came back as
"73 test(s) matching 'Hi'" -- the patient could not get back to the menu.
Plus: service types (Health Packages, Radiology, Scans) on the WhatsApp main
menu, a one-click catalogue classifier for 1,392 unfiled tests, package
details, diagnostics Insights, and a "How to use" guide.

Load-bearing guarantees:
* "Hi" in the test search ALWAYS returns to the menu, whatever the LLM says.
* A catalogue with one heading (every unfiled catalogue) keeps "Book Lab Test".
* The classifier never overwrites a heading a person chose.
* Every command the help guide names is one the bot actually answers.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import conversation as conv
from app.services.ai_engine import INTENT_KEYWORDS, detect_intent, is_greeting
from app.services.conversation import (
    HELP_KEYWORDS,
    LAB_BOOKING_KEYWORDS,
    NAV_KEYWORDS,
    RESUBSCRIBE_KEYWORDS,
    ConversationManager,
    conversation_manager,
)
from app.services.lab_classifier import (
    CARDIAC, PACKAGES, PATHOLOGY, RADIOLOGY, SCANS, classify_unfiled, suggest_service_type,
)

REPO = Path(__file__).resolve().parent.parent
CLINIC = {"id": "11111111-2222-3333-4444-555555555555", "name": "Accumax Diagnostics",
          "plan": "diagstream", "features": {}}
PHONE = "+919000000083"


def _manager():
    m = ConversationManager()
    m.whatsapp = MagicMock()
    m.whatsapp.send_text = AsyncMock()
    m.whatsapp.send_interactive_list = AsyncMock()
    m.whatsapp.send_interactive_buttons = AsyncMock()
    m.update_state = AsyncMock()
    ConversationManager._lab_heading_cache.clear()
    return m


def _test(i, name, category=None, **extra):
    return {"id": f"{i:08d}-0000-0000-0000-000000000000", "name": name, "category": category,
            "price_paise": 50000, "is_active": True, **extra}


# ── the reported bug: "Hi" searched the catalogue ────────────────────────────


class TestGreetingLeavesTheSearch:
    @pytest.mark.parametrize("msg", ["Hi", "hi", "Hii", "hello!", "Hey 👋", "Good morning",
                                     "namaste", "नमस्ते", "హాయ్", "  HI  "])
    def test_greetings_are_recognised(self, msg):
        assert is_greeting(msg)

    @pytest.mark.parametrize("msg", ["thiamine", "hiv", "chikungunya", "hi thyroid", "high sugar", ""])
    def test_test_names_are_not_greetings(self, msg):
        assert not is_greeting(msg)

    @pytest.mark.asyncio
    async def test_hi_never_reaches_the_llm(self):
        with patch("app.services.ai_engine.call_openrouter_with_backoff",
                   AsyncMock(side_effect=AssertionError("LLM must not be called"))):
            assert await detect_intent("Hi", CLINIC) == "greeting"
            assert await detect_intent("Good morning!", CLINIC) == "greeting"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("msg", ["Hi", "Hii", "Good morning", "नमस्ते"])
    async def test_hi_in_the_search_goes_home_even_when_the_llm_says_unknown(self, msg):
        """The production sequence, through the real handle_message."""
        cm = conversation_manager
        with patch.object(conv, "get_or_create_conversation", AsyncMock(return_value={
                 "state": "browsing_lab_tests", "context": {"lab_test_query": "thyroid"}})), \
             patch.object(conv, "get_patient_by_phone", AsyncMock(return_value={
                 "name": "P", "language": "en", "opted_in": False, "data_consent": True})), \
             patch.object(conv, "get_lang", AsyncMock(return_value="en")), \
             patch.object(conv, "update_conversation", AsyncMock()), \
             patch.object(conv, "detect_intent", AsyncMock(return_value="unknown")), \
             patch.object(cm, "update_state", AsyncMock()), \
             patch.object(cm, "_send_main_menu", AsyncMock()) as menu, \
             patch.object(cm, "_show_lab_test_list", AsyncMock()) as search:
            await cm.handle_message(CLINIC, PHONE, msg, message_type="text")
        menu.assert_awaited_once()
        search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_real_test_name_is_still_searched(self):
        m = _manager()
        with patch.object(m, "_show_lab_test_list", AsyncMock()) as search, \
             patch.object(m, "_send_main_menu", AsyncMock()) as menu:
            await m._handle_browsing_lab_tests(CLINIC, PHONE, "thyroid", "unknown", {}, "en")
        assert search.await_args.kwargs["query"] == "thyroid"
        menu.assert_not_awaited()


# ── main menu: service types for a diagnostic centre ────────────────────────


def _menu_rows(m):
    return [r for s in m.whatsapp.send_interactive_list.await_args.kwargs["sections"] for r in s["rows"]]


async def _send_menu(catalogue):
    m = _manager()
    with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=True)), \
         patch("app.database.get_lab_tests", AsyncMock(return_value=catalogue)):
        await m._send_main_menu(CLINIC, PHONE, "en")
    return _menu_rows(m)


class TestDiagnosticsMainMenu:
    @pytest.mark.asyncio
    async def test_an_unfiled_catalogue_keeps_book_lab_test(self):
        rows = await _send_menu([_test(1, "CBC"), _test(2, "LFT")])
        assert [r["id"] for r in rows] == ["menu_book", "menu_emergency", "menu_human", "menu_help"]

    @pytest.mark.asyncio
    async def test_filed_service_types_become_menu_rows(self):
        cat = ([_test(i, f"T{i}", PATHOLOGY) for i in range(5)]
               + [_test(10, "Master Health Checkup", PACKAGES), _test(11, "MRI Brain", SCANS)])
        rows = await _send_menu(cat)
        ids = [r["id"] for r in rows]
        assert "menu_book" not in ids
        assert ids[:3] == ["labsvc_lab tests (pathology)", "labsvc_health packages", "labsvc_scans (ct / mri)"]
        assert rows[1]["title"].startswith("📦") and rows[2]["title"].startswith("🧲")
        assert rows[0]["description"] == "5 available"
        assert ids[-3:] == ["menu_emergency", "menu_human", "menu_help"]

    @pytest.mark.asyncio
    async def test_many_service_types_fit_meta_ten_rows(self):
        cat = [_test(i, f"T{i}", f"Service {i}") for i in range(12)]
        rows = await _send_menu(cat)
        assert len(rows) <= 10
        assert "menu_lab_tests" in [r["id"] for r in rows]  # "All services" catches the tail
        assert all(len(r["title"]) <= 24 and len(r.get("description") or "") <= 72 for r in rows)
        assert "menu_emergency" in [r["id"] for r in rows]

    @pytest.mark.asyncio
    async def test_a_catalogue_read_failure_never_breaks_the_menu(self):
        m = _manager()
        with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=True)), \
             patch("app.database.get_lab_tests", AsyncMock(side_effect=RuntimeError("db"))):
            await m._send_main_menu(CLINIC, PHONE, "en")
        assert [r["id"] for r in _menu_rows(m)][0] == "menu_book"

    @pytest.mark.asyncio
    async def test_hospitals_keep_their_menu_untouched_by_headings(self):
        m = _manager()
        with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
             patch("app.database.get_lab_tests", AsyncMock(side_effect=AssertionError("not read"))):
            await m._send_main_menu({"id": "c", "plan": "polyclinic"}, PHONE, "en")
        assert [r["id"] for r in _menu_rows(m)][0] == "menu_book"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key, expected", [
        ("health packages", PACKAGES), ("renamed since", None),
    ])
    async def test_tapping_a_service_type_opens_it(self, key, expected):
        m = _manager()
        cat = [_test(1, "A", PATHOLOGY), _test(2, "B", PACKAGES)]
        with patch("app.database.get_lab_tests", AsyncMock(return_value=cat)), \
             patch.object(conv, "log_analytics_event", AsyncMock()) as log, \
             patch.object(m, "_start_lab_booking", AsyncMock()) as start:
            await m._start_lab_booking_for_heading(CLINIC, PHONE, key, "en")
        assert start.await_args.kwargs["category"] == expected
        assert log.await_args.args[2] == "lab_category_viewed"

    @pytest.mark.asyncio
    async def test_a_chosen_service_type_survives_branch_selection(self):
        m = _manager()
        branches = [{"id": "b1", "is_active": True}, {"id": "b2", "is_active": True}]
        with patch("app.services.tenant.get_clinic_branches", AsyncMock(return_value=branches)), \
             patch.object(m, "_send_branch_selection", AsyncMock()):
            await m._start_lab_booking(CLINIC, PHONE, "en", category=PACKAGES)
        assert m.update_state.await_args.args[3] == {"lab_flow": True, "lab_category": PACKAGES}

    @pytest.mark.asyncio
    async def test_single_centre_opens_the_service_type_directly(self):
        m = _manager()
        with patch("app.services.tenant.get_clinic_branches", AsyncMock(return_value=[])), \
             patch.object(m, "_show_lab_test_list", AsyncMock()) as show:
            await m._start_lab_booking(CLINIC, PHONE, "en", category=SCANS)
        assert show.await_args.args[2] == {"lab_category": SCANS}

    @pytest.mark.asyncio
    async def test_no_category_is_the_old_behaviour(self):
        m = _manager()
        with patch("app.services.tenant.get_clinic_branches", AsyncMock(return_value=[])), \
             patch.object(m, "_show_lab_test_list", AsyncMock()) as show:
            await m._start_lab_booking(CLINIC, PHONE, "en")
        assert show.await_args.args[2] == {}

    @pytest.mark.parametrize("label", [PATHOLOGY, PACKAGES, RADIOLOGY, SCANS, CARDIAC, "A" * 60])
    def test_titles_never_cut_a_word_for_an_icon(self, label):
        title = ConversationManager._lab_heading_title(label)
        assert len(title) <= 24
        # Either the whole heading with its icon, or the heading's own words.
        assert title.endswith(label) or title == label[:24]

    def test_new_buttons_are_routed(self):
        src = (REPO / "app" / "services" / "conversation.py").read_text(encoding="utf-8")
        assert 'button_id.startswith("labsvc_")' in src and 'button_id == "menu_help"' in src


# ── help guide and re-subscribe ─────────────────────────────────────────────


class TestHelpGuide:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("lang", ["en", "hi", "te"])
    @pytest.mark.parametrize("diag_only", [True, False])
    async def test_guide_fits_whatsapp_and_matches_the_plan(self, lang, diag_only):
        m = _manager()
        clinic = {**CLINIC, "name": "A Very Long Hospital Name For Testing Purposes Ltd",
                  "plan": "diagstream" if diag_only else "polyclinic"}
        with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=diag_only)), \
             patch.object(conv, "log_analytics_event", AsyncMock()):
            await m._send_help_guide(clinic, PHONE, lang)
        body = m.whatsapp.send_interactive_buttons.await_args.kwargs["body"]
        assert len(body) <= 1024, len(body)
        assert "*book test*" in body
        assert ("*book* —" in body) is (not diag_only)
        assert ("*reschedule*" in body) is (not diag_only)
        for cmd in ("*menu*", "*stop*", "*start*", "*emergency*", "*change language*"):
            assert cmd in body

    def test_every_command_it_names_is_answered(self):
        assert "menu" in NAV_KEYWORDS
        assert "book test" in LAB_BOOKING_KEYWORDS
        assert "stop" in INTENT_KEYWORDS["opt_out"]
        assert "start" in RESUBSCRIBE_KEYWORDS
        assert "delete my data" in INTENT_KEYWORDS["data_deletion_request"]
        assert "staff" in INTENT_KEYWORDS["human_escalation"]
        assert "emergency" in INTENT_KEYWORDS["emergency"]
        assert "reschedule" in INTENT_KEYWORDS["reschedule_appointment"]
        assert "cancel" in INTENT_KEYWORDS["cancel_appointment"]
        src = (REPO / "app" / "services" / "conversation.py").read_text(encoding="utf-8")
        assert '"change language"' in src

    def test_the_invalid_input_reply_that_promised_help_now_has_it(self):
        from app.templates.whatsapp_templates import get_message

        assert "help" in get_message("invalid_input", "en")
        assert "help" in HELP_KEYWORDS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["browsing_lab_tests", "selecting_slot", "main_menu"])
    async def test_help_works_from_any_state_and_keeps_it(self, state):
        cm = conversation_manager
        with patch.object(conv, "get_or_create_conversation", AsyncMock(return_value={"state": state, "context": {}})), \
             patch.object(conv, "get_patient_by_phone", AsyncMock(return_value={
                 "name": "P", "language": "en", "opted_in": True, "data_consent": True})), \
             patch.object(conv, "get_lang", AsyncMock(return_value="en")), \
             patch.object(conv, "update_conversation", AsyncMock()), \
             patch.object(conv, "detect_intent", AsyncMock(return_value="unknown")), \
             patch.object(cm, "update_state", AsyncMock()) as upd, \
             patch.object(cm, "_send_help_guide", AsyncMock()) as guide:
            await cm.handle_message(CLINIC, PHONE, "help", message_type="text")
        guide.assert_awaited_once()
        upd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_menu_help_row_only_while_there_is_room(self):
        m = _manager()
        with patch.object(m, "_is_diagnostics_only", AsyncMock(return_value=False)), \
             patch.object(conv.specialty_flow, "treatment_menu_active", AsyncMock(return_value=True)), \
             patch.object(conv.specialty_flow, "has_entry_treatment", AsyncMock(return_value=True)):
            await m._send_main_menu({"id": "c", "plan": "womenchild"}, PHONE, "en")
        rows = _menu_rows(m)
        assert len(rows) == 10 and rows[-1]["id"] == "menu_help"
        assert "menu_emergency" in [r["id"] for r in rows]


class TestResubscribe:
    async def _run(self, opted_in):
        cm = conversation_manager
        with patch.object(conv, "get_or_create_conversation", AsyncMock(return_value={"state": "main_menu", "context": {}})), \
             patch.object(conv, "get_patient_by_phone", AsyncMock(return_value={
                 "name": "P", "language": "en", "opted_in": opted_in, "data_consent": True})), \
             patch.object(conv, "get_lang", AsyncMock(return_value="en")), \
             patch.object(conv, "update_conversation", AsyncMock()), \
             patch.object(conv, "detect_intent", AsyncMock(return_value="unknown")), \
             patch.object(cm, "update_state", AsyncMock()), \
             patch.object(cm, "_handle_main_menu", AsyncMock()), \
             patch.object(cm, "_handle_opt_in", AsyncMock()) as opt_in:
            await cm.handle_message(CLINIC, PHONE, "START", message_type="text")
        return opt_in

    @pytest.mark.asyncio
    async def test_start_after_stop_turns_follow_ups_back_on(self):
        (await self._run(False)).assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_for_a_subscribed_patient_means_what_it_always_did(self):
        (await self._run(True)).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opt_in_writes_the_patient_and_confirms(self):
        m = _manager()
        with patch.object(conv, "update_patient", AsyncMock()) as upd, \
             patch.object(conv, "log_analytics_event", AsyncMock()):
            await m._handle_opt_in(CLINIC, PHONE, "en")
        assert upd.await_args.args[2]["opted_in"] is True
        assert "*stop*" in m.whatsapp.send_text.await_args.args[2]


# ── classifier ──────────────────────────────────────────────────────────────


class TestClassifier:
    @pytest.mark.parametrize("name, expected", [
        ("MRI BRAIN (PLAIN)", SCANS), ("CT - PARA NASAL SINUSES WITH CONTRAST", SCANS), ("PET SCAN", SCANS),
        ("Master Health Checkup", PACKAGES), ("Executive Health Package", PACKAGES),
        ("X-RAY - CHEST PA VIEW", RADIOLOGY), ("hand AP/LAT view", RADIOLOGY),
        ("ULTRASONOGRAM - SCROTUM", RADIOLOGY), ("doppler arterial lower limb both", RADIOLOGY),
        ("BMD - SPINE AND HIP", RADIOLOGY), ("ECG", CARDIAC), ("2D ECHOCARDIOGRAM", CARDIAC),
        ("PULMONARY FUNCTION TEST - PFT", CARDIAC), ("COMPLETE BLOOD COUNT", PATHOLOGY),
        ("HEPATITIS B PROFILE", PATHOLOGY), ("PROTEIN, TOTAL", PATHOLOGY),
        ("ECHINOCOCCUS ANTIBODY", PATHOLOGY), ("CHIKUNGUNYA IGM", PATHOLOGY), ("", PATHOLOGY),
    ])
    def test_rules(self, name, expected):
        assert suggest_service_type(name) == expected

    def test_filed_tests_are_never_proposed(self):
        rows = [_test(1, "MRI Brain"), _test(2, "CBC", "My Own Heading"), _test(3, "ECG", "  ")]
        proposal = classify_unfiled(rows)
        names = {t["name"] for group in proposal.values() for t in group}
        assert names == {"MRI Brain", "ECG"}

    def test_labels_are_the_panels_own_starter_headings(self):
        html = (REPO / "admin" / "index.html").read_text(encoding="utf-8")
        block = html.split("const LAB_CATEGORY_SUGGESTIONS = [")[1].split("];")[0]
        for label in (PATHOLOGY, PACKAGES, RADIOLOGY, SCANS, CARDIAC):
            assert f"'{label}'" in block


class TestAutoClassifyEndpoint:
    def _patches(self, rows, update_result=None):
        fake = MagicMock()
        chain = fake.table.return_value.update.return_value.eq.return_value.in_.return_value
        return fake, chain, (
            patch("app.routers.admin.resolve_clinic_id_for_write", AsyncMock(return_value=CLINIC["id"])),
            patch("app.routers.admin._fetch_all_lab_tests", AsyncMock(return_value=rows)),
            patch("app.routers.admin.supabase", fake),
            patch("app.routers.admin.sb", AsyncMock(return_value=MagicMock(data=update_result or []))),
            patch("app.routers.admin.log_admin_action", AsyncMock()),
        )

    @pytest.mark.asyncio
    async def test_preview_writes_nothing(self):
        from app.routers.admin import AdminUser, LabAutoClassifyRequest, auto_classify_lab_tests

        rows = [_test(1, "MRI Brain"), _test(2, "CBC"), _test(3, "ECG", "Filed")]
        _, _, ps = self._patches(rows)
        with ps[0], ps[1], ps[2], ps[3] as sb, ps[4]:
            r = await auto_classify_lab_tests(LabAutoClassifyRequest(), request=None,
                                              clinic_id=CLINIC["id"], user=AdminUser("a", role="clinic_admin"))
        assert r["unfiled"] == 2 and r["applied"] is False and r["updated"] == 0
        sb.assert_not_awaited()
        assert {p["category"] for p in r["proposal"]} == {SCANS, PATHOLOGY}

    @pytest.mark.asyncio
    async def test_apply_is_clinic_scoped_and_only_touches_null_rows(self):
        from app.routers.admin import AdminUser, LabAutoClassifyRequest, auto_classify_lab_tests

        rows = [_test(1, "MRI Brain"), _test(2, "CBC")]
        fake, chain, ps = self._patches(rows, update_result=[{"id": "x"}])
        ConversationManager._lab_heading_cache[CLINIC["id"]] = (0, [])
        with ps[0], ps[1], ps[2], ps[3], ps[4]:
            r = await auto_classify_lab_tests(LabAutoClassifyRequest(apply=True), request=None,
                                              clinic_id=CLINIC["id"], user=AdminUser("a", role="clinic_admin"))
        assert r["applied"] is True and r["updated"] == 2
        fake.table.return_value.update.return_value.eq.assert_called_with("clinic_id", CLINIC["id"])
        chain.is_.assert_called_with("category", "null")
        assert CLINIC["id"] not in ConversationManager._lab_heading_cache


# ── details on a test (migration 083) ───────────────────────────────────────


class TestDetails:
    def test_model_validation(self):
        from pydantic import ValidationError

        from app.routers.admin import LabTestCreate, LabTestUpdate

        assert LabTestCreate(name="X", price_rupees=1).description is None
        assert LabTestCreate(name="X", price_rupees=1, description="  CBC, LFT  ").description == "CBC, LFT"
        assert LabTestUpdate(description="").description is None
        with pytest.raises(ValidationError):
            LabTestCreate(name="X", price_rupees=1, description="x" * 501)

    @pytest.mark.asyncio
    async def test_csv_details_column_and_no_wipe_rule(self):
        from app.routers import admin as admin_router

        async def run(csv_text):
            upload = MagicMock()
            upload.read = AsyncMock(return_value=csv_text.encode())
            fake = MagicMock()
            with patch.object(admin_router, "resolve_clinic_id_for_write", AsyncMock(return_value="c1")), \
                 patch.object(admin_router, "_fetch_all_lab_tests", AsyncMock(return_value=[])), \
                 patch.object(admin_router, "supabase", fake), \
                 patch.object(admin_router, "sb", AsyncMock(return_value=MagicMock(data=[{}]))), \
                 patch.object(admin_router, "log_admin_action", AsyncMock()):
                await admin_router.import_lab_tests_csv(file=upload, clinic_id="c1",
                                                        user=admin_router.AdminUser("a", role="clinic_admin"))
            return [c.args[0] for c in fake.table.return_value.insert.call_args_list]

        rows = await run('name,price_rupees,Tests Included\nFull Body,999,"CBC, LFT, KFT"\nCBC,300,\n')
        assert rows[0]["description"] == "CBC, LFT, KFT"
        assert "description" not in rows[1]
        rows = await run("name,price_rupees\nCBC,300\n")
        assert "description" not in rows[0]

    @pytest.mark.asyncio
    async def test_card_shows_details_and_records_interest(self):
        m = _manager()
        test = _test(1, "Full Body Checkup", PACKAGES, description="CBC, LFT, KFT, Lipid",
                     fasting_required=False, turnaround_hours=24)
        with patch("app.database.get_lab_test_by_id", AsyncMock(return_value=test)), \
             patch("app.database.get_lab_collection_window", AsyncMock(return_value={"days": "Mon,Tue,Wed,Thu,Fri,Sat,Sun"})), \
             patch("app.database.format_collection_window", MagicMock(return_value="7-11")), \
             patch.object(conv, "log_analytics_event", AsyncMock()) as log:
            await m._handle_browsing_lab_tests(CLINIC, PHONE, "", "button_click", {}, "en",
                                               interactive_data={"id": f"labtest_{test['id']}"})
        call = m.whatsapp.send_interactive_buttons.await_args
        body = call.kwargs.get("body") or " ".join(str(a) for a in call.args)
        assert "CBC, LFT, KFT, Lipid" in body
        event = next(c for c in log.await_args_list if c.args[2] == "lab_test_viewed")
        assert event.kwargs["metadata"]["category"] == PACKAGES

    def test_migration_083_is_additive(self):
        raw = (REPO / "migrations" / "083_lab_test_details.sql").read_text(encoding="utf-8")
        sql = "\n".join(line.split("--", 1)[0] for line in raw.splitlines())  # statements only
        assert "ADD COLUMN IF NOT EXISTS description TEXT" in sql
        assert "UPDATE lab_tests" not in sql and "CREATE INDEX" not in sql
        assert (REPO / "migrations" / "rollback" / "083_down.sql").exists()


# ── Insights ────────────────────────────────────────────────────────────────


class TestDiagnosticsInsights:
    def test_service_type_performance(self):
        from app.services.analytics import _diagnostics_insights

        catalogue = [_test(1, "Full Body", PACKAGES), _test(2, "MRI Brain", SCANS), _test(3, "CBC")]
        appts = [
            {"booking_type": "lab_test", "lab_test_id": catalogue[0]["id"], "lab_test_name": "Full Body",
             "status": "confirmed", "payment_id": "p1", "amount_paise": 99900, "patient_phone": "a"},
            {"booking_type": "lab_test", "lab_test_id": None, "lab_test_name": "mri brain",
             "status": "pending_payment", "amount_paise": 650000, "patient_phone": "b"},
            {"booking_type": "consultation", "patient_phone": "c"},
        ]
        events = [
            {"event_type": "lab_category_viewed", "phone": "a", "metadata": {"category": PACKAGES}},
            {"event_type": "lab_category_viewed", "phone": "d", "metadata": {"category": PACKAGES}},
            {"event_type": "lab_test_viewed", "phone": "d",
             "metadata": {"category": PACKAGES, "test_name": "Full Body"}},
            {"event_type": "lab_test_viewed", "phone": "e",
             "metadata": {"category": SCANS, "test_name": "CT Chest"}},
            {"event_type": "lab_test_viewed", "phone": "f", "metadata": "garbage"},
        ]
        d = _diagnostics_insights(appts, catalogue, events)
        types = {r["name"]: r for r in d["by_service_type"]}
        assert types[PACKAGES]["bookings"] == 1 and types[PACKAGES]["revenue_inr"] == 999.0
        assert types[PACKAGES]["interested_patients"] == 2 and types[PACKAGES]["conversion_pct"] == 50.0
        assert types[SCANS]["bookings"] == 1 and types[SCANS]["revenue_inr"] == 0  # unpaid
        assert d["totals"] == {"lab_bookings": 2, "revenue_inr": 999.0, "category_views": 2, "test_views": 2}
        assert {r["name"] for r in d["top_tests"]} == {"Full Body", "mri brain"}
        assert {r["name"] for r in d["most_viewed"]} == {"Full Body", "CT Chest"}

    def test_empty_period(self):
        from app.services.analytics import _diagnostics_insights

        d = _diagnostics_insights([], [], [])
        assert d["by_service_type"] == [] and d["totals"]["lab_bookings"] == 0

    def test_only_clinics_that_sell_tests_pay_for_it(self):
        src = (REPO / "app" / "routers" / "admin.py").read_text(encoding="utf-8")
        assert 'include_diagnostics=has_feature(clinic, "lab_test_booking")' in src

    def test_panel_renders_the_section(self):
        html = (REPO / "admin" / "index.html").read_text(encoding="utf-8")
        assert 'id="insightsDiagnostics"' in html and "renderDiagnosticsInsights(d.diagnostics)" in html
        assert 'id="labTypeTabs"' in html and 'id="labAutoClassifyCard"' in html
        assert 'id="f-ltDescription"' in html
