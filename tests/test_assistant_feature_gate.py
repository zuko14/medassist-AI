"""KK-P1-02: Kriya Ketu configuration and feature gate (KK-FR-001, KK-FR-009).

The assistant is visible only when the global switch is on, the platform owner
opted the clinic in (clinics.features.kriya_ketu, never the enterprise
wildcard), and the user is not a corporate viewer or phlebotomist.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings, settings
from app.routers import admin
from app.routers.admin import AdminUser
from app.services.tenant import OPT_IN_FEATURES, kriya_ketu_enabled

CID = str(uuid.uuid4())


def test_kriya_ketu_is_an_owner_opt_in_feature():
    assert OPT_IN_FEATURES["kriya_ketu"] == "Kriya Ketu assistant"


@pytest.mark.parametrize("clinic, expected", [
    ({"plan": "polyclinic", "features": {"kriya_ketu": True}}, True),
    ({"plan": "polyclinic", "account_type": "tenant", "features": {"kriya_ketu": True}}, True),
    ({"plan": "enterprise", "features": {"*": True}}, False),           # wildcard never grants it
    ({"plan": "enterprise", "features": {}}, False),
    ({"plan": "polyclinic", "features": {"kriya_ketu": "true"}}, False),  # only a real bool
    ({"plan": "polyclinic", "features": {"kriya_ketu": False}}, False),
    ({"plan": "polyclinic", "account_type": "corporate_partner", "features": {"kriya_ketu": True}}, False),
    ({"plan": "polyclinic", "features": None}, False),
    (None, False),
])
def test_kriya_ketu_enabled(clinic, expected):
    assert kriya_ketu_enabled(clinic) is expected


def test_settings_defaults_are_off_and_match_the_spec():
    s = Settings()
    assert s.assistant_enabled is False
    assert s.assistant_stt_enabled is False
    assert s.assistant_llm_enabled is True
    assert s.assistant_llm_model == "google/gemini-3.1-flash-lite"
    assert s.assistant_llm_fallback_model == "openai/gpt-4.1-mini"
    assert s.assistant_llm_timeout_seconds == 5.0
    assert s.assistant_llm_max_tokens == 200
    assert s.assistant_llm_rate_per_hour == 20
    assert s.assistant_stt_model == "saaras:v3"
    assert s.assistant_stt_timeout_seconds == 10.0


def _user(staff_role=None, clinic_id=CID, role="staff"):
    return AdminUser(username="desk1", role=role, clinic_id=clinic_id, user_id="u1",
                     staff_role=staff_role, permissions=[])


async def _me(user, features, *, on=True, stt=True, clinic_id=None):
    clinic = {"id": CID, "plan": "polyclinic", "features": features}
    with patch.object(settings, "assistant_enabled", on), \
         patch.object(settings, "assistant_stt_enabled", stt), \
         patch("app.routers.admin.get_clinic_by_id", new_callable=AsyncMock, return_value=clinic):
        return await admin.get_current_admin(clinic_id=clinic_id, user=user)


@pytest.mark.asyncio
async def test_me_shows_the_assistant_and_mic_when_everything_is_on():
    res = await _me(_user("FRONT_DESK"), {"kriya_ketu": True})
    assert res["assistant_enabled"] is True
    assert res["assistant_stt_enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("staff_role, features, on, stt, exp_assistant, exp_stt", [
    ("FRONT_DESK", {"kriya_ketu": True}, False, True, False, False),   # global kill switch
    ("FRONT_DESK", {}, True, True, False, False),                      # clinic not opted in
    ("FRONT_DESK", {"kriya_ketu": True}, True, False, True, False),    # mic off only
    ("CORPORATE_VIEWER", {"kriya_ketu": True}, True, True, False, False),
    ("PHLEBOTOMIST", {"kriya_ketu": True}, True, True, False, False),
])
async def test_me_hides_the_assistant(staff_role, features, on, stt, exp_assistant, exp_stt):
    res = await _me(_user(staff_role), features, on=on, stt=stt)
    assert res["assistant_enabled"] is exp_assistant
    assert res["assistant_stt_enabled"] is exp_stt


@pytest.mark.asyncio
async def test_unscoped_super_admin_gets_the_assistant_hidden():
    res = await _me(_user(role="super_admin", clinic_id=None), {"kriya_ketu": True})
    assert res["assistant_enabled"] is False
    assert res["assistant_stt_enabled"] is False


@pytest.mark.asyncio
async def test_scoped_super_admin_sees_it_for_an_opted_in_clinic():
    user = _user(role="super_admin", clinic_id=None)
    with patch.object(admin, "enforce_clinic_access", return_value=CID):
        res = await _me(user, {"kriya_ketu": True}, clinic_id=CID)
    assert res["assistant_enabled"] is True


def test_owner_can_switch_kriya_ketu_on_through_the_existing_features_route():
    import base64
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import platform

    app.dependency_overrides[platform.verify_owner_credentials] = lambda: AdminUser(
        "owner", role="platform_owner", user_id="platform_owner_env")
    auth = {"Authorization": "Basic " + base64.b64encode(
        f"{settings.owner_username}:{settings.owner_password}".encode()).decode()}
    clinic = MagicMock(data=[{"account_type": "tenant", "plan": "polyclinic", "features": {}}])
    try:
        with patch.object(platform, "sb", AsyncMock(side_effect=[clinic, MagicMock(data=[{}])])) as sb_mock, \
             patch.object(platform, "log_admin_action", AsyncMock()):
            r = TestClient(app).patch(f"/platform/clinics/{CID}/features",
                                      json={"feature": "kriya_ketu", "enabled": True}, headers=auth)
    finally:
        app.dependency_overrides.pop(platform.verify_owner_credentials, None)
    assert r.status_code == 200
    assert sb_mock.call_args_list[-1].args[0].request.json["features"] == {"kriya_ketu": True}


def test_owner_dashboard_has_a_one_click_kriya_ketu_toggle():
    """§5 #10 resolved (owner, 2026-10-15): the clinic modal in admin/platform.html switches the
    opt-in through the same PATCH route as the other per-clinic features, and shows the saved state
    exactly as kriya_ketu_enabled() reads it (only an explicit True is on)."""
    import re
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "admin" / "platform.html").read_text(encoding="utf-8")
    assert re.search(r'<input type="checkbox" id="modalKetuToggle"\s+'
                     r'onchange="toggleClinicFeature\(\'kriya_ketu\', this\.checked\)">', html)
    assert html.count("document.getElementById('modalKetuToggle').checked = overrides.kriya_ketu === true;") == 1
    assert "ASSISTANT_ENABLED" in html  # the per-clinic switch alone does nothing while the global switch is off
