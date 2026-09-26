"""Dental treatment plans (migration 089): multi-sitting courses, reminders,
reviews, owner-set monthly message limits. Dental clinics only."""

import base64
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers.admin import AdminUser, verify_credentials
from app.services import dental_plans as dp
from app.services.tenant import dental_plans_enabled

CLINIC = "11111111-1111-1111-1111-111111111111"
APPT = "22222222-2222-2222-2222-222222222222"
PLAN_ID = "33333333-3333-3333-3333-333333333333"
DOC_ID = "44444444-4444-4444-4444-444444444444"
client = TestClient(app)

DENTAL = {"id": CLINIC, "name": "Smile Dental", "plan": "dental", "config": {}, "features": {}}
ADMIN = AdminUser(username="dental_admin", role="clinic_admin", clinic_id=CLINIC, user_id="u1")
STAFF = AdminUser(username="desk", role="staff", clinic_id=CLINIC, user_id="u2")
STAFF_OK = AdminUser(username="desk2", role="staff", clinic_id=CLINIC, user_id="u3",
                     permissions=["DENTAL_PLANS_MANAGE"])


def plan(**over):
    p = {"id": PLAN_ID, "clinic_id": CLINIC, "patient_phone": "+919876543210", "patient_name": "Ravi Kumar",
         "treatment_name": "Root Canal Treatment", "treatment_id": None, "planned_sittings": 3,
         "status": "active", "whatsapp_consent": False, "notify_patient": True, "notify_doctor": True,
         "quoted_amount_paise": 600000}
    p.update(over)
    return p


@pytest.fixture
def as_user():
    def _set(user):
        app.dependency_overrides[verify_credentials] = lambda: user
    yield _set
    app.dependency_overrides.pop(verify_credentials, None)


# ═══════ dental-only gate ═══════


@pytest.mark.parametrize("clinic, expected", [
    ({"plan": "dental"}, True),
    ({"plan": "dental", "features": {"dental_treatment_plans": False}}, False),  # owner switched it off
    ({"plan": "enterprise"}, False),       # the wildcard must not leak dental onto a hospital
    ({"plan": "multispecialty"}, False),
    ({"plan": "polyclinic"}, False),
    (None, False),
])
def test_dental_gate(clinic, expected):
    assert dental_plans_enabled(clinic) is expected


def test_non_dental_clinic_is_refused(as_user):
    as_user(ADMIN)
    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock,
               return_value={**DENTAL, "plan": "polyclinic"}):
        res = client.get("/admin/dental/overview")
    assert res.status_code == 403
    assert "dental clinics only" in res.json()["detail"]


def test_staff_needs_the_dental_permission(as_user):
    as_user(STAFF)
    assert client.get("/admin/dental/plans").status_code == 403
    as_user(STAFF_OK)
    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock,
               return_value={**DENTAL, "plan": "polyclinic"}):
        res = client.get("/admin/dental/plans")
    # the permission passed; the dental gate is what answers now
    assert res.status_code == 403 and "dental clinics only" in res.json()["detail"]


def test_staff_cannot_change_automation_settings(as_user):
    as_user(STAFF_OK)
    assert client.put("/admin/dental/settings", json={"dental_review_enabled": False}).status_code == 403


# ═══════ pure logic ═══════


@pytest.mark.parametrize("cfg, expected", [
    (None, dp.DEFAULT_MESSAGE_LIMITS),
    ({"dental_message_limits": {"patient": 50, "doctor": 0}}, {"patient": 50, "doctor": 0, "review": 500}),
    ({"dental_message_limits": {"patient": -1, "doctor": True, "review": "9"}}, dp.DEFAULT_MESSAGE_LIMITS),
])
def test_message_limits(cfg, expected):
    assert dp.message_limits(cfg) == expected


@pytest.mark.parametrize("used, limit, level", [(0, 100, "ok"), (89, 100, "ok"), (90, 100, "warning"),
                                                (100, 100, "full"), (0, 0, "full")])
def test_quota_level(used, limit, level):
    assert dp.quota_level(used, limit)["level"] == level


@pytest.mark.parametrize("payload", [
    "dentrev:not-a-uuid:3",
    f"dentrev:{APPT}:4",
    f"dentrev:{APPT}:0",
    f"xdentrev:{APPT}:3",
    f"dentrev:{APPT}:3;drop",
    "",
    None,
])
def test_review_payload_parser_rejects_anything_else(payload):
    assert dp.parse_review_payload(payload) is None


def test_review_payload_round_trip():
    assert dp.parse_review_payload(dp.review_payload(APPT, 2)) == (APPT, 2)


@pytest.mark.parametrize("key", list(dp.DENTAL_TEMPLATES))
def test_templates_meet_meta_rules(key):
    body = dp.meta_template_payload(key)
    text = body["components"][0]["text"]
    assert not text.startswith("{{") and not text.rstrip(". ").endswith("}}")  # Meta rejects both
    assert len(body["components"][0]["example"]["body_text"][0]) == text.count("{{")
    assert body["category"] == "UTILITY" and body["language"] == "en"
    assert body["name"].startswith("dental_") and body["name"].islower()


def test_review_template_has_three_quick_replies():
    btns = dp.meta_template_payload("review")["components"][1]["buttons"]
    assert [b["text"] for b in btns] == ["Excellent", "Good", "Needs improvement"]


def test_flat_strips_what_meta_rejects():
    assert dp.flat("a\n\tb     c") == "a b c"
    assert dp.flat("") == "-"


def test_summarize_progress_and_balance():
    sittings = [
        {"sitting_number": 1, "status": "completed", "amount_collected_paise": 200000, "review_rating": 3},
        {"sitting_number": 2, "status": "cancelled", "amount_collected_paise": None},
        {"sitting_number": 2, "status": "confirmed", "appointment_date": "2026-10-06", "appointment_time": "10:30"},
    ]
    s = dp.summarize(plan(), sittings)
    assert s["sittings_done"] == 1 and s["sittings_booked"] == 1
    assert s["next_sitting_number"] == 3  # 1 done, 2 booked (the cancelled 2 does not count)
    assert s["collected_paise"] == 200000 and s["balance_paise"] == 400000


# ═══════ booking a sitting ═══════


async def _schedule(p, messageable, slots=("10:30",), reason=None, existing=(), number=None):
    booked = {}

    async def fake_book(clinic_id, data):
        booked.update(data)
        return {"success": True, "appointment": {**data, "id": APPT}}

    with patch.object(dp, "available_slots", AsyncMock(return_value=(list(slots), reason))), \
         patch.object(dp, "plan_sittings", AsyncMock(return_value=list(existing))), \
         patch.object(dp, "patient_messageable", AsyncMock(return_value=messageable)), \
         patch("app.database.book_appointment", side_effect=fake_book):
        tomorrow = (dp.ist_now().date() + timedelta(days=1)).isoformat()
        appt = await dp.schedule_sitting(DENTAL, p, {"id": DOC_ID, "name": "Priya", "department": "Dental"},
                                         tomorrow, "10:30", number)
    return appt, booked


@pytest.mark.asyncio
async def test_sitting_for_unconsented_patient_can_never_be_messaged():
    _, booked = await _schedule(plan(), messageable=False)
    # every reminder flag pre-set: no job, dental or generic, will message them
    assert booked["reminder_24h_sent"] is True and booked["reminder_2h_sent"] is True
    assert booked["followup_sent"] is True
    assert booked["treatment_plan_id"] == PLAN_ID and booked["sitting_number"] == 1
    assert booked["booking_type"] == "consultation" and booked["doctor_id"] == DOC_ID


@pytest.mark.asyncio
async def test_sitting_for_consented_patient_keeps_reminders():
    _, booked = await _schedule(plan(whatsapp_consent=True), messageable=True,
                                existing=[{"sitting_number": 1, "status": "completed"}])
    assert "reminder_24h_sent" not in booked
    assert booked["sitting_number"] == 2  # next free number
    assert booked["followup_sent"] is True  # the review replaces the generic follow-up


@pytest.mark.asyncio
@pytest.mark.parametrize("slots, reason, fragment", [
    ((), "doctor_on_leave", "on leave"),
    ((), "hospital_closed", "holiday"),
    (("11:00",), None, "not free"),
])
async def test_sitting_only_at_a_free_time(slots, reason, fragment):
    with pytest.raises(dp.DentalError, match=fragment):
        await _schedule(plan(), messageable=True, slots=slots, reason=reason)


@pytest.mark.asyncio
async def test_closed_plan_cannot_book():
    with pytest.raises(dp.DentalError, match="closed"):
        await _schedule(plan(status="completed"), messageable=True)


@pytest.mark.asyncio
async def test_duplicate_sitting_number_refused():
    with pytest.raises(dp.DentalError, match="already booked"):
        await _schedule(plan(), messageable=True, existing=[{"sitting_number": 2, "status": "confirmed"}], number=2)


@pytest.mark.asyncio
async def test_past_date_refused():
    yesterday = (dp.ist_now().date() - timedelta(days=1)).isoformat()
    with pytest.raises(dp.DentalError, match="past"):
        await dp.schedule_sitting(DENTAL, plan(), {"id": DOC_ID, "name": "Priya"}, yesterday, "10:30")


# ═══════ quota + sending ═══════


@pytest.mark.asyncio
async def test_quota_exhausted_refuses_and_warns():
    async def fake_sb(q):
        return MagicMock(data=-1)

    with patch.object(dp, "supabase", MagicMock()), patch.object(dp, "sb", side_effect=fake_sb), \
         patch.object(dp, "_warn_threshold", AsyncMock()) as warn:
        assert await dp.reserve_quota(DENTAL, "doctor") is False
    warn.assert_awaited_once()
    assert warn.await_args.kwargs["full"] is True


@pytest.mark.asyncio
async def test_quota_granted_under_the_limit_without_warning():
    async def fake_sb(q):
        return MagicMock(data=5)

    with patch.object(dp, "supabase", MagicMock()), patch.object(dp, "sb", side_effect=fake_sb), \
         patch.object(dp, "_warn_threshold", AsyncMock()) as warn:
        assert await dp.reserve_quota(DENTAL, "doctor") is True
    warn.assert_not_awaited()


@pytest.mark.asyncio
async def test_quota_service_error_fails_closed():
    sup = MagicMock()
    sup.rpc.side_effect = RuntimeError("db down")
    with patch.object(dp, "supabase", sup):
        assert await dp.reserve_quota(DENTAL, "patient") is False


@pytest.mark.asyncio
async def test_failed_send_gives_the_message_back():
    with patch.object(dp, "reserve_quota", AsyncMock(return_value=True)), \
         patch.object(dp, "release_quota", AsyncMock()) as release, \
         patch("app.services.whatsapp.whatsapp_service.send_template", AsyncMock(return_value=False)):
        ok = await dp._send(DENTAL, "doctor", "+919000000000", "doctor_sitting", [], "dental_doctor")
    assert ok is False
    release.assert_awaited_once_with(DENTAL, "doctor")


@pytest.mark.asyncio
async def test_confirmation_falls_back_to_the_approved_generic_template():
    sends = []

    async def fake_send(clinic, phone, name, components=None, _source=None):
        sends.append(name)
        return name == "appointment_confirmation"

    appt = {"id": APPT, "sitting_number": 2, "doctor_name": "Priya", "appointment_date": "2026-10-06",
            "appointment_time": "10:30:00"}
    with patch.object(dp, "patient_messageable", AsyncMock(return_value=True)), \
         patch.object(dp, "reserve_quota", AsyncMock(return_value=True)), \
         patch.object(dp, "release_quota", AsyncMock()) as release, \
         patch("app.services.whatsapp.whatsapp_service.send_template", side_effect=fake_send):
        assert await dp.send_patient_confirmation(DENTAL, plan(), appt) is True
    assert sends == ["dental_sitting_confirmation", "appointment_confirmation"]
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_consent_no_patient_message():
    with patch.object(dp, "patient_messageable", AsyncMock(return_value=False)), \
         patch.object(dp, "_send", AsyncMock()) as send:
        assert await dp.send_patient_reminder(DENTAL, plan(), {"id": APPT}) is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_doctor_without_whatsapp_number_is_not_messaged():
    with patch.object(dp, "_send", AsyncMock()) as send:
        assert await dp.send_doctor_sitting(DENTAL, plan(), {"id": APPT}, {"name": "Priya"}) is False
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_request_carries_per_sitting_payloads():
    captured = {}

    async def fake_send(clinic, kind, phone, key, comps, source, fallback=None):
        captured.update(kind=kind, key=key, comps=comps)
        return True

    with patch.object(dp, "_send", side_effect=fake_send):
        await dp.send_review_request(DENTAL, plan(), {"id": APPT})
    buttons = [c for c in captured["comps"] if c["type"] == "button"]
    assert [b["parameters"][0]["payload"] for b in buttons] == [
        f"dentrev:{APPT}:3", f"dentrev:{APPT}:2", f"dentrev:{APPT}:1"]
    assert captured["kind"] == "review"


# ═══════ patient reminder job ═══════


@pytest.mark.asyncio
async def test_reminder_flag_set_only_when_dental_reminder_sent():
    rows = [{"id": "a1", "treatment_plan_id": PLAN_ID}, {"id": "a2", "treatment_plan_id": PLAN_ID}]
    flagged = []

    def table(name):
        q = MagicMock()
        q.update.return_value = q

        def _eq(col, val):
            if col == "id":
                flagged.append(val)
            return q
        q.eq.side_effect = _eq
        return q

    sup = MagicMock()
    sup.table.side_effect = table
    with patch.object(dp, "_dental_clinics", AsyncMock(return_value=[DENTAL])), \
         patch("app.services.subscription.automated_outbound_allowed", return_value=True), \
         patch.object(dp, "_sittings_on", AsyncMock(return_value=rows)), \
         patch.object(dp, "_plans_by_id", AsyncMock(return_value={PLAN_ID: plan()})), \
         patch.object(dp, "send_patient_reminder", AsyncMock(side_effect=[True, False])), \
         patch.object(dp, "supabase", sup), patch.object(dp, "sb", AsyncMock()):
        result = await dp.run_patient_reminders()
    assert result == {"sent": 1, "skipped": 1}
    # only a1 got the flag; a2 is left for the generic 09:00 reminder
    assert flagged == ["a1"]


@pytest.mark.asyncio
async def test_review_job_is_silent_outside_daytime():
    with patch.object(dp, "ist_now", return_value=dp.ist_now().replace(hour=23)), \
         patch.object(dp, "_dental_clinics", AsyncMock()) as clinics:
        assert await dp.run_review_requests() == {"sent": 0, "skipped": 0}
    clinics.assert_not_awaited()


# ═══════ review reply (inbound button) ═══════


class _Mgr:
    def __init__(self):
        self.whatsapp = MagicMock()
        self.whatsapp.send_text = AsyncMock()


@pytest.mark.asyncio
async def test_review_from_another_phone_is_rejected():
    mgr = _Mgr()
    with patch.object(dp, "get_sitting", AsyncMock(return_value={"id": APPT, "patient_phone": "+919999999999"})), \
         patch.object(dp, "sb", AsyncMock()) as write:
        handled = await dp.handle_review_reply(mgr, DENTAL, "+919876543210", f"dentrev:{APPT}:3")
    assert handled is True
    write.assert_not_awaited()  # nothing recorded


@pytest.mark.asyncio
async def test_not_our_payload_is_left_alone():
    assert await dp.handle_review_reply(_Mgr(), DENTAL, "+91", "book_lab_test") is False


@pytest.mark.asyncio
async def test_excellent_rating_sends_google_link_and_poor_alerts_admin():
    clinic = {**DENTAL, "config": {"dental_google_review_link": "https://g.page/r/smile"}}
    appt = {"id": APPT, "patient_phone": "+919876543210", "patient_name": "Ravi", "sitting_number": 2,
            "treatment_name": "Root Canal", "doctor_name": "Priya"}
    mgr = _Mgr()
    with patch.object(dp, "get_sitting", AsyncMock(side_effect=lambda *a: dict(appt))), \
         patch.object(dp, "supabase", MagicMock()), patch.object(dp, "sb", AsyncMock()) as write:
        await dp.handle_review_reply(mgr, clinic, "+919876543210", f"dentrev:{APPT}:3")
        assert "https://g.page/r/smile" in mgr.whatsapp.send_text.await_args.args[2]
        assert write.await_count == 1  # rating only

        mgr2 = _Mgr()
        await dp.handle_review_reply(mgr2, clinic, "+919876543210", f"dentrev:{APPT}:1")
        assert "sorry" in mgr2.whatsapp.send_text.await_args.args[2].lower()
        assert write.await_count == 3  # rating + admin notification


@pytest.mark.asyncio
async def test_conversation_routes_only_dental_review_buttons():
    """A template quick-reply with our prefix is consumed; any other button is not."""
    from app.services import conversation as conv

    src = open(conv.__file__, encoding="utf-8").read()
    assert 'if message_type == "button" and interactive_data:' in src
    assert "payload.startswith(dental_plans.REVIEW_PAYLOAD_PREFIX)" in src


# ═══════ owner: limits + invoice ═══════


def owner_auth():
    creds = f"{settings.owner_username}:{settings.owner_password}"
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


@pytest.mark.parametrize("method, path", [
    ("GET", "/platform/dental-messaging"),
    ("PATCH", f"/platform/clinics/{CLINIC}/dental-messaging"),
    ("POST", f"/platform/clinics/{CLINIC}/dental-templates/submit"),
    ("GET", f"/platform/clinics/{CLINIC}/dental-templates/status"),
])
def test_owner_dental_routes_need_owner_auth(method, path):
    assert client.request(method, path, json={}).status_code == 401


def _owner_clinic_patch(clinic_row, written):
    def table(name):
        obj = MagicMock()
        for m in ("select", "eq", "limit"):
            getattr(obj, m).return_value = obj
        obj._rows = [clinic_row]

        def _update(payload):
            written.update(payload)
            up = MagicMock()
            up.eq.return_value = up
            up._rows = [{}]
            return up
        obj.update.side_effect = _update
        return obj

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))
    return table, fake_sb


def test_owner_limits_only_for_dental_clinics():
    written = {}
    table, fake_sb = _owner_clinic_patch({"id": CLINIC, "name": "Poly", "plan": "polyclinic", "config": {}}, written)
    with patch("app.routers.platform.supabase") as sup, patch("app.routers.platform.sb", side_effect=fake_sb):
        sup.table.side_effect = table
        res = client.patch(f"/platform/clinics/{CLINIC}/dental-messaging", headers=owner_auth(),
                           json={"doctor_limit": 10})
    assert res.status_code == 400 and not written


def test_owner_sets_limits_and_addon_preserving_config():
    written = {}
    table, fake_sb = _owner_clinic_patch(
        {"id": CLINIC, "name": "Smile", "plan": "dental", "config": {"meta_waba_id": "w"}}, written)
    with patch("app.routers.platform.supabase") as sup, patch("app.routers.platform.sb", side_effect=fake_sb), \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        sup.table.side_effect = table
        res = client.patch(f"/platform/clinics/{CLINIC}/dental-messaging", headers=owner_auth(),
                           json={"doctor_limit": 50, "review_limit": 0, "addon_rupees": 299})
    assert res.status_code == 200, res.text
    assert written["config"]["meta_waba_id"] == "w"
    assert written["config"]["dental_message_limits"] == {"patient": 1000, "doctor": 50, "review": 0}
    assert written["config"]["dental_messaging_addon_paise"] == 29900


@patch("app.routers.platform.log_admin_action", new_callable=AsyncMock)
@patch("app.routers.platform._fetch_clinic_branch_counts", new_callable=AsyncMock)
@patch("app.services.platform_finance.fetch_invoices", new_callable=AsyncMock)
@patch("app.services.platform_finance.fetch_billing_rates", new_callable=AsyncMock)
@patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock)
@patch("app.routers.platform.supabase")
def test_invoice_adds_messaging_addon_for_dental_only(mock_supabase, mock_tiers, mock_rates, mock_invoices,
                                                      mock_branches, _log):
    inserted = {}

    def table_router(name):
        m = MagicMock()
        if name == "clinics":
            m.select.return_value.execute.return_value.data = [
                {"id": "dent", "name": "Smile", "plan": "dental", "is_active": True,
                 "config": {"dental_messaging_addon_paise": 29_900}},
                # a stray key on a non-dental clinic must NOT be billed
                {"id": "poly", "name": "Poly", "plan": "polyclinic", "is_active": True,
                 "config": {"dental_messaging_addon_paise": 29_900}},
            ]
        elif name == "platform_invoices":
            def _insert(rows):
                inserted["rows"] = rows
                r = MagicMock()
                r.execute.return_value.data = rows
                return r
            m.insert.side_effect = _insert
        return m

    mock_supabase.table.side_effect = table_router
    mock_tiers.return_value = {"dental": {"monthly_price_paise": 500_000},
                               "polyclinic": {"monthly_price_paise": 500_000}}
    mock_rates.return_value = {}
    mock_branches.return_value = {}
    mock_invoices.return_value = []
    res = client.post("/platform/finance/invoices/generate", headers=owner_auth(), json={"month": "2026-11"})
    assert res.status_code == 200, res.text
    rows = {r["clinic_id"]: r for r in inserted["rows"]}
    assert rows["dent"]["amount_paise"] == 529_900 and rows["dent"]["messaging_addon_paise"] == 29_900
    assert rows["poly"]["amount_paise"] == 500_000 and "messaging_addon_paise" not in rows["poly"]


# ═══════ branches (migration 090) ═══════

BRANCH_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BRANCH_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PINNED_A = AdminUser(username="desk_a", role="staff", clinic_id=CLINIC, user_id="u4",
                     permissions=["DENTAL_PLANS_MANAGE"], branch_id=BRANCH_A)


@pytest.mark.parametrize("plan_branch, status", [(BRANCH_B, 403), (BRANCH_A, 200), (None, 200)])
def test_pinned_staff_reach_only_their_branch_or_clinic_wide_plans(as_user, plan_branch, status):
    as_user(PINNED_A)
    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=DENTAL), \
         patch.object(dp, "get_plan", AsyncMock(return_value=plan(branch_id=plan_branch))), \
         patch.object(dp, "plan_sittings", AsyncMock(return_value=[])), \
         patch.object(dp, "patient_messageable", AsyncMock(return_value=True)):
        res = client.get(f"/admin/dental/plans/{PLAN_ID}")
    assert res.status_code == status


def _branch_rows(rows):
    sup = MagicMock()
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    sup.table.return_value = q

    async def fake_sb(builder):
        return MagicMock(data=rows)
    return sup, fake_sb


@pytest.mark.asyncio
async def test_doctor_of_another_branch_is_refused():
    sup, fake_sb = _branch_rows([{"branch_id": BRANCH_B, "session": "both"}])
    with patch.object(dp, "supabase", sup), patch.object(dp, "sb", side_effect=fake_sb):
        with pytest.raises(dp.DentalError, match="does not work at this plan's branch"):
            await dp.doctor_branch_session({"id": DOC_ID}, BRANCH_A)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows, expected", [
    ([], None),                                               # unassigned doctor works everywhere
    ([{"branch_id": BRANCH_A, "session": "morning"}], "morning"),
    ([{"branch_id": BRANCH_A, "session": None}], "both"),
])
async def test_doctor_branch_session(rows, expected):
    sup, fake_sb = _branch_rows(rows)
    with patch.object(dp, "supabase", sup), patch.object(dp, "sb", side_effect=fake_sb):
        assert await dp.doctor_branch_session({"id": DOC_ID}, BRANCH_A) == expected
    assert await dp.doctor_branch_session({"id": DOC_ID}, None) is None


@pytest.mark.asyncio
async def test_branch_plan_sitting_uses_branch_session_and_is_tagged():
    booked = {}

    async def fake_book(clinic_id, data):
        booked.update(data)
        return {"success": True, "appointment": {**data, "id": APPT}}

    slots = AsyncMock(return_value=(["10:30"], None))
    sup, fake_sb = _branch_rows([{"name": "Gajuwaka"}])
    with patch.object(dp, "doctor_branch_session", AsyncMock(return_value="morning")), \
         patch("app.database.get_available_slots", slots), \
         patch.object(dp, "plan_sittings", AsyncMock(return_value=[])), \
         patch.object(dp, "patient_messageable", AsyncMock(return_value=True)), \
         patch.object(dp, "supabase", sup), patch.object(dp, "sb", side_effect=fake_sb), \
         patch("app.database.book_appointment", side_effect=fake_book):
        tomorrow = (dp.ist_now().date() + timedelta(days=1)).isoformat()
        await dp.schedule_sitting(DENTAL, plan(branch_id=BRANCH_A), {"id": DOC_ID, "name": "Priya"},
                                  tomorrow, "10:30")
    assert slots.await_args.kwargs == {"branch_id": BRANCH_A, "branch_session": "morning"}
    assert booked["branch_id"] == BRANCH_A and booked["branch_name"] == "Gajuwaka"


def test_pinned_staff_plan_is_forced_into_their_branch(as_user):
    as_user(PINNED_A)
    inserted = {}

    def table(name):
        q = MagicMock()
        for m in ("select", "eq", "limit"):
            getattr(q, m).return_value = q

        def _insert(row):
            inserted.update(row)
            q._rows = [{**row, "id": PLAN_ID}]
            return q
        q.insert.side_effect = _insert
        return q

    async def fake_sb(builder):
        return MagicMock(data=getattr(builder, "_rows", []))

    with patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=DENTAL), \
         patch("app.routers.admin.supabase") as sup, patch("app.routers.admin.sb", side_effect=fake_sb), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock):
        sup.table.side_effect = table
        res = client.post("/admin/dental/plans", json={
            "patient_phone": "9876543210", "patient_name": "Ravi", "treatment_name": "Root Canal",
            "branch_id": BRANCH_B,  # ignored: a pinned account cannot file into another branch
        })
    assert res.status_code == 200, res.text
    assert inserted["branch_id"] == BRANCH_A
