"""voice.settings_update (registry §4.10, PB-05; released after D-13 / migration 110): one AI receptionist
setting per request, R2, VOICE_MANAGE (handler) on a clinic with the receptionist on.

Settings Kriya changes: receptionist name, language, speaking pace, reception hours (and days), emergency
number, lead-call window (09:00-21:00 only), daily lead-call limit, attempts per lead. The sales pitch,
voice, concurrency and the monthly budget (platform owner only) stay on the page / platform.
The PUT sends the settings read at preview with that one field changed; the whole read is in the plan hash
(an edit in between means a new preview) and the server writes only the changed key (D-13).
"""

import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.voice.dates import IST
from tests.test_assistant_runs import CID, _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=IST)
SETTINGS = {"primary_language": "te-IN", "assistant_name": "Kriya", "speaker": None, "pace": 1.0,
            "reception_hours": {"start": "09:00", "end": "18:00", "days": "Mon-Sat"}, "emergency_number": "108",
            "monthly_budget_paise": 100000,
            "outbound": {"auto_leads": False, "window_start": "10:00", "window_end": "19:00", "max_attempts": 2,
                         "daily_cap": 30, "concurrent": 1, "pitch": {"te": "", "hi": "", "en": ""}}}


# ── resolver ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ('rename the receptionist to "Asha"', ["assistant_name", "Asha"]),
    ("receptionist name to Asha Devi", ["assistant_name", "Asha Devi"]),
    ("receptionist language hindi", ["primary_language", "hi-IN"]),
    ("receptionist should speak in english", ["primary_language", "en-IN"]),
    ("receptionist speak slower", ["pace", "-0.1"]),
    ("receptionist speak faster", ["pace", "+0.1"]),
    ("receptionist pace 1.2", ["pace", "1.2"]),
    ("receptionist hours 9 to 6", ["reception_hours", {"window": "09:00-18:00", "days": None}]),
    ("reception hours mon-fri 8 am to 8 pm", ["reception_hours", {"window": "08:00-20:00", "days": "Mon,Tue,Wed,Thu,Fri"}]),
    ("receptionist emergency number 9876543210", ["emergency_number", "9876543210"]),
    ("lead calls between 10 and 7", ["outbound_window", "10:00-19:00"]),
    ("lead call window 11 to 5", ["outbound_window", "11:00-17:00"]),
    ("lead call limit 50", ["daily_cap", 50]),
    ("only 40 lead calls a day", ["daily_cap", 40]),
    ("try each lead 3 times", ["max_attempts", 3]),
    ("receptionist settings", None),
])
def test_resolve_voice_setting(text, expected):
    assert resolve.resolve_voice_setting(text) == expected


@pytest.mark.parametrize("text", ["receptionist hours 9 to 6", "lead call limit 50", 'rename the receptionist to "Asha"',
                                  "receptionist ka naam Asha rakho", "receptionist peru Asha pettu"])
def test_routes(text):
    assert rules.route(text, now=NOW).action_id == "voice.settings_update"


def test_other_voice_phrases_are_unchanged():
    assert rules.route("turn on automatic lead calls", now=NOW).action_id == "voice.auto_leads"
    assert rules.route("call the open leads from this week", now=NOW).action_id == "leads.call"
    assert rules.route("what is the receptionist's name?", now=NOW).kind == "help"


# ── API against a fake panel ─────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}


def _reset(settings=None, save_ok=True):
    S.clear()
    S.update(settings=json.loads(json.dumps(settings or SETTINGS)), writes=[], save_ok=save_ok)


@fake.get("/admin/voice/settings")
async def _get():
    return {"settings": S["settings"], "budget_paise": 100000}


@fake.put("/admin/voice/settings")
async def _put(request: Request):
    body = await request.json()
    S["writes"].append(body)
    if S["save_ok"]:
        S["settings"] = body
    return {"success": True}


client = TestClient(app)


@pytest.fixture
def vs(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    clinic = {"id": CID, "plan": "polyclinic", "account_type": "tenant",
              "features": {"kriya_ketu": True, "ai_receptionist": True}}
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=clinic)):
        yield as_user


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "voice"}}).json()


def _confirm(out):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"]}).json()


def _changed(want_path, value):
    want = json.loads(json.dumps(SETTINGS))
    node = want
    for k in want_path[:-1]:
        node = node[k]
    node[want_path[-1]] = value
    return want


@pytest.mark.parametrize("text, path, value, shown", [
    ("receptionist hours 9 to 7", ["reception_hours", "end"], "19:00", "19:00"),
    ('rename the receptionist to "Asha"', ["assistant_name"], "Asha", "Asha"),
    ("receptionist language hindi", ["primary_language"], "hi-IN", "Hindi"),
    ("receptionist speak slower", ["pace"], 0.9, "0.9"),
    ("lead call limit 50", ["outbound", "daily_cap"], 50, "50"),
    ("lead calls between 11 and 6", ["outbound", "window_start"], "11:00", "11:00"),
    ("try each lead 3 times", ["outbound", "max_attempts"], 3, "3"),
    ("receptionist emergency number 9876543210", ["emergency_number"], "9876543210", "3210"),
])
def test_one_setting_changes_and_everything_else_is_sent_back(db, vs, text, path, value, shown):  # noqa: F811
    out = _ask(text)
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert shown in json.dumps(out["cards"][0], ensure_ascii=False) and S["writes"] == []
    assert _confirm(out)["state"] == "COMPLETED_VERIFIED"
    (body,) = S["writes"]
    want = _changed(path, value)
    if path == ["outbound", "window_start"]:
        want["outbound"]["window_end"] = "18:00"
    assert body == want


def test_reception_days_change_with_the_hours(db, vs):  # noqa: F811
    _confirm(_ask("reception hours mon-fri 8 am to 8 pm"))
    assert S["writes"][0]["reception_hours"] == {"start": "08:00", "end": "20:00", "days": "Mon,Tue,Wed,Thu,Fri"}


@pytest.mark.parametrize("text, why", [
    ("lead calls between 8 and 10", "09:00"),                 # outside the 09:00-21:00 calling hours
    ("lead call limit 900", "500"),
    ("try each lead 5 times", "3"),
    ("receptionist pace 3", "0.5"),
    ('rename the receptionist to "X"', "2"),
])
def test_values_outside_the_allowed_range_are_refused_with_the_range(db, vs, text, why):  # noqa: F811
    out = _ask(text)
    assert out["error_code"] == "nothing_to_do" and why in out["message"] and S["writes"] == []


def test_no_change_is_refused(db, vs):  # noqa: F811
    out = _ask("lead call limit 30")
    assert out["error_code"] == "nothing_to_do" and "already" in out["message"] and S["writes"] == []


def test_an_edit_after_the_preview_means_a_new_preview(db, vs):  # noqa: F811
    out = _ask("lead call limit 50")
    S["settings"]["assistant_name"] = "Someone else's edit"
    again = _confirm(out)
    assert (again["state"], again["error_code"]) == ("NEEDS_CONFIRMATION", "plan_changed") and S["writes"] == []


def test_a_save_the_read_back_cannot_see_is_unknown(db, vs):  # noqa: F811
    _reset(save_ok=False)
    out = _ask("lead call limit 50")
    assert _confirm(out)["state"] == "UNKNOWN_OUTCOME" and len(S["writes"]) == 1


def test_an_unclear_setting_asks_on_the_form(db, vs):  # noqa: F811
    out = _ask("change the receptionist settings")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["cards"][0]["missing"] == ["setting"], out
    done = client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": out["run_id"],
        "slot_answers": {"setting": "lead call limit 45"}, "ui_context": {"page": "voice"}}).json()
    assert done["state"] == "NEEDS_CONFIRMATION" and "45" in json.dumps(done["cards"][0]), done


def test_staff_without_voice_manage_are_refused(db, vs):  # noqa: F811
    vs["user"] = _user()
    out = _ask("lead call limit 50")
    assert out["state"] == "REJECTED" and "Voice Manage" in out["message"] and S["writes"] == []


def test_registry_shape():
    spec = REGISTRY["voice.settings_update"]
    assert (spec.risk, spec.permission, spec.perm_source, spec.endpoint) == (
        "R2", "VOICE_MANAGE", "handler", ("PUT", "/admin/voice/settings"))


def test_corpus_covers_the_action():
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    aid = "voice.settings_update"
    pos = [e for e in corpus if e["expect"] == f"action:{aid}" or e.get("action") == aid]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}
    assert len([e for e in corpus if e.get("near") == aid and e["expect"] != f"action:{aid}"]) >= 2


def test_the_real_route_writes_only_the_changed_key_and_never_the_budget():
    """What Ketu sends (the settings it read, one field changed, budget included) through the real
    PUT /admin/voice/settings: only outbound.daily_cap is written; the platform-set budget is untouched."""
    from unittest.mock import MagicMock
    from tests.config_writes import captured_config_writes
    from tests.voice.test_voice_routes import ADMIN, CID as VCID, client as voice_client
    stored = {"voice": {**SETTINGS, "monthly_budget_paise": 100000}, "address": "Old Road"}
    sent = json.loads(json.dumps(SETTINGS))
    sent["outbound"]["daily_cap"] = 50
    sent["monthly_budget_paise"] = 999999          # a stale or tampered budget in the body is ignored
    c, p = voice_client(ADMIN)
    try:
        with captured_config_writes() as writes, \
             patch("app.routers.voice_admin.sb", new=AsyncMock(return_value=MagicMock(data=[{"config": stored}]))), \
             patch("app.routers.voice_admin.invalidate_tenant_cache"), \
             patch("app.routers.voice_admin.log_admin_action", new=AsyncMock()):
            r = c.put(f"/admin/voice/settings?clinic_id={VCID}", json=sent)
    finally:
        p.stop()
    assert r.status_code == 200, r.text
    assert writes[-1]["changed"] == [["voice", "outbound", "daily_cap"]]
    assert writes[-1]["config"]["voice"]["monthly_budget_paise"] == 100000 and writes[-1]["config"]["address"] == "Old Road"
