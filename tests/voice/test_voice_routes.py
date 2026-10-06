"""/admin/voice and /platform/voice: permission + opt-in gating, takeover CAS,
outbound window bounds, test console, branch ownership on number mapping."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import voice_admin, voice_platform
from app.routers.admin import AdminUser, verify_credentials
from app.routers.platform import verify_owner_credentials

from tests.voice.test_dialog import DOCS
from tests.voice.test_io_layer import CLINIC

CID = CLINIC["id"]
OFF = {**CLINIC, "features": {}}


def client(user, clinic=CLINIC):
    app = FastAPI()
    app.include_router(voice_admin.router)
    app.include_router(voice_platform.router)
    app.dependency_overrides[verify_credentials] = lambda: user
    app.dependency_overrides[verify_owner_credentials] = lambda: AdminUser("owner", role="platform_owner",
                                                                           user_id="platform_owner_env")
    p = patch("app.routers.voice_admin.get_clinic_by_id", new=AsyncMock(return_value=clinic))
    p.start()
    return TestClient(app), p


ADMIN = AdminUser("a", role="clinic_admin", clinic_id=CID, user_id="u1")
STAFF = AdminUser("s", role="staff", clinic_id=CID, user_id="u2", permissions=[])
VIEWER = AdminUser("v", role="staff", clinic_id=CID, user_id="u3", permissions=["VOICE_VIEW"])


def test_staff_without_grant_is_refused():
    c, p = client(STAFF)
    try:
        assert c.get(f"/admin/voice/calls?clinic_id={CID}").status_code == 403
    finally:
        p.stop()


def test_clinic_not_enabled_by_owner_is_refused():
    c, p = client(ADMIN, clinic=OFF)
    try:
        r = c.get(f"/admin/voice/calls?clinic_id={CID}")
        assert r.status_code == 403 and "not enabled" in r.json()["detail"]
    finally:
        p.stop()


def test_viewer_cannot_take_over():
    c, p = client(VIEWER)
    try:
        assert c.post(f"/admin/voice/calls/x/takeover?clinic_id={CID}").status_code == 403
    finally:
        p.stop()


def test_takeover_of_a_finished_call_is_409():
    c, p = client(ADMIN)
    try:
        with patch("app.routers.voice_admin.sb", new=AsyncMock(return_value=MagicMock(data=[]))):
            assert c.post(f"/admin/voice/calls/x/takeover?clinic_id={CID}").status_code == 409
    finally:
        p.stop()


@pytest.mark.parametrize("start,end", [("07:00", "19:00"), ("10:00", "22:00"), ("19:00", "10:00")])
def test_outbound_window_bounds(start, end):
    c, p = client(ADMIN)
    body = {"primary_language": "te-IN", "assistant_name": "Kriya", "pace": 1.0,
            "reception_hours": {"start": "09:00", "end": "20:00", "days": "Mon,Tue"},
            "outbound": {"auto_leads": True, "window_start": start, "window_end": end, "max_attempts": 2,
                         "daily_cap": 10}}
    try:
        assert c.put(f"/admin/voice/settings?clinic_id={CID}", json=body).status_code == 422
    finally:
        p.stop()


def test_test_console_starts_a_test_call_and_greets():
    c, p = client(ADMIN)
    created = {"id": "call-t", "call_ref": "CALL-T", "caller_phone": "+910000000000", "mode": "test",
               "language": "te-IN", "dialog": {}}
    try:
        with patch("app.routers.voice_admin.store.create_call", new=AsyncMock(return_value=created)) as cc, \
             patch("app.voice.session.store.update_call", new=AsyncMock()), \
             patch("app.voice.session.store.add_event", new=AsyncMock()):
            r = c.post(f"/admin/voice/test-turn?clinic_id={CID}", json={})
        assert r.status_code == 200 and "ABC Hospitals" in r.json()["say"][0]
        assert cc.await_args.args[1]["mode"] == "test"
    finally:
        p.stop()


def test_owner_cannot_map_a_number_to_another_clinics_branch():
    c, p = client(ADMIN)
    responses = iter([MagicMock(data=[{"id": CID, "account_type": "tenant"}]), MagicMock(data=[])])
    try:
        with patch("app.routers.voice_platform.sb", new=AsyncMock(side_effect=lambda q: next(responses))):
            r = c.post("/platform/voice/numbers", json={"clinic_id": CID, "branch_id": "b-other",
                                                        "exophone": "+914071234567"})
        assert r.status_code == 422
    finally:
        p.stop()
