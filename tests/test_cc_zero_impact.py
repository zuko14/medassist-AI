"""Continuous Care zero-impact matrix (P10; docs/continuous-care/TEST-PLAN.md §3, VERIFICATION §5).

A clinic that never enabled Continuous Care sends no cc_* query anywhere: every /admin/cc route refuses it before any
care read (valid bodies, so the refusal is the handler's, not FastAPI's), the existing OPD / IPD / RX / dashboard
routes, the conversation pipeline, the CC scheduler jobs, the voice outbound dispatch and DPDP erasure. With CC on,
the existing routes still send no cc_* query.
"""

import asyncio
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_cc_route_matrix import CC_ROUTES
from tests.test_ipd_api_e2e import _clinic, _patient, _q, api  # noqa: F401  (fixture)
from tests.test_rx_zero_impact import spy  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
U = lambda: str(uuid.uuid4())  # noqa: E731
BODIES = {
    ("PUT", "/admin/cc/settings"): {},
    ("PUT", "/admin/cc/cards/{card_key}/{lang}"): {"body": "A clinic approved care card text."},
    ("POST", "/admin/cc/measure-sources"): {"lab_test_id": U(), "measure_code": "hba1c"},
    ("POST", "/admin/cc/activations"): {"protocol_code": "t2dm"},
    ("POST", "/admin/cc/activations/{activation_id}/retire"): {"reason": "replaced"},
    ("POST", "/admin/cc/enrollments"): {"patient_id": U(), "protocol_code": "t2dm", "primary_doctor_id": U()},
    ("POST", "/admin/cc/enrollments/{enrollment_id}/optin"): {"method": "in_person"},
    ("POST", "/admin/cc/enrollments/{enrollment_id}/consent/counter"): {"notice_version": "cc-notice-1"},
    ("POST", "/admin/cc/enrollments/{enrollment_id}/pause"): {"reason": "travelling"},
    ("POST", "/admin/cc/enrollments/{enrollment_id}/end"): {"reason": "moved away"},
    ("POST", "/admin/cc/enrollments/{enrollment_id}/observations"): {"measure_code": "fbg", "value": 120},
    ("POST", "/admin/cc/observations/{observation_id}/correct"): {"value": 120, "reason": "typing error"},
    ("POST", "/admin/cc/discharge-followups"): {"admission_id": U(), "follow_up_on": "2026-12-01", "doctor_id": U()},
    ("POST", "/admin/cc/radar/{row_id}/dismiss"): {"reason": "not chronic"},
    ("POST", "/admin/cc/cases/{case_id}/actions"): {"action_code": "continue_plan"},
}


def _cc(paths):
    return [p for p in paths if p.rsplit("/", 1)[-1].startswith("cc_")]


def test_every_cc_route_refuses_a_clinic_without_care_before_any_cc_read(api, spy):  # noqa: F811
    clinic = _clinic(api.conn)
    # a doctor-linked clinic admin holds every CC permission, so the refusal below is the module gate's
    api.as_(api.U("admin", "clinic_admin", clinic, U(), doctor_id=U()))
    for method, path in sorted(CC_ROUTES):
        concrete = re.sub(r"\{lang\}", "en", re.sub(r"\{card_key\}", "hypo_now", path))
        concrete = re.sub(r"\{[a-z_]+\}", lambda m: U(), concrete)
        before = len(spy)
        body = BODIES.get((method, path), {}) if method in ("POST", "PUT", "PATCH") else None
        r = api.call(method, concrete, clinic, body)
        assert r.status_code == 403 and "not enabled" in r.text, (method, path, r.status_code, r.text)
        assert not _cc(spy[before:]), (method, path, _cc(spy[before:]))


@pytest.mark.parametrize("care_on", [False, True])
def test_existing_routes_send_no_cc_query(api, spy, care_on):  # noqa: F811
    clinic = _clinic(api.conn)
    if care_on:
        assert api.owner_toggle(clinic, "care_enabled", True).status_code == 200
    api.as_(api.U("admin", "clinic_admin", clinic, U()))
    del spy[:]
    for path in ("/admin/me", "/admin/opd/collections/summary", "/admin/opd/setup", "/admin/ipd/setup",
                 "/admin/billing/gstr1", "/admin/opd/consultations/today"):
        api.call("GET", path, clinic)
    assert not _cc(spy)


def test_scheduler_jobs_skip_clinics_without_care(api, monkeypatch):  # noqa: F811
    from app.services import cc_channels, cc_radar
    from app.services.scheduler import SchedulerService
    plain = _clinic(api.conn)
    seen = []
    monkeypatch.setattr(cc_channels, "dispatch", lambda c, now=None: seen.append(c["id"]) or asyncio.sleep(0))
    monkeypatch.setattr(cc_radar, "rebuild", lambda c: seen.append(c["id"]) or asyncio.sleep(0))
    s = SchedulerService.__new__(SchedulerService)
    asyncio.run(s.cc_card_dispatch())
    asyncio.run(s.cc_radar())
    assert plain not in seen


def test_conversation_hook_makes_no_cc_call_for_a_clinic_without_care(monkeypatch):
    """The hook is behind care_access(clinic) on the clinic row already in memory: no import, no query."""
    from app.services import cc_hooks
    from app.services.conversation import ConversationManager
    tap, typed = AsyncMock(return_value=True), AsyncMock(return_value=True)
    monkeypatch.setattr(cc_hooks, "handle_tap", tap)
    monkeypatch.setattr(cc_hooks, "handle_typed", typed)
    clinic = {"id": "c1", "name": "H", "config": {}, "features": {"opd_enabled": True}, "plan": "polyclinic"}
    session = {"state": "main_menu", "context": {}, "last_processed_message_id": None, "booking_context_expires_at": None}
    m = ConversationManager()
    m.whatsapp = AsyncMock()
    with patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)), \
            patch("app.services.conversation.get_patient_by_phone", new=AsyncMock(return_value={"id": "p", "language": "en"})), \
            patch("app.services.conversation.update_conversation", new=AsyncMock()), \
            patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown")):
        for kind, msg, data in (("text", "142", None), ("interactive", "", {"id": "cc:adh:" + U() + ":taken"}),
                                ("button", "", {"id": "cc:read:" + U()})):
            try:
                asyncio.run(m._handle_message_locked(clinic=clinic, phone="+919800000001", message=msg,
                                                     message_type=kind, message_id="wamid." + U(),
                                                     interactive_data=data))
            except Exception:                              # the normal pipeline past the hook is not under test
                pass
    tap.assert_not_awaited()
    typed.assert_not_awaited()


def test_voice_outbound_counts_no_care_calls_for_a_clinic_without_care(monkeypatch):
    from app.voice import outbound
    called = []
    monkeypatch.setattr(outbound, "_care_today", lambda cid: called.append(cid) or asyncio.sleep(0, 0))
    assert asyncio.run(outbound._care_today_if_cc({"id": "c1", "cc_state": "OFF"})) == 0
    assert asyncio.run(outbound._care_today_if_cc({"id": "c1"})) == 0
    assert called == []
    asyncio.run(outbound._care_today_if_cc({"id": "c2", "cc_state": "WIND_DOWN"}))
    assert called == ["c2"]


def test_erasure_at_a_clinic_without_care_makes_no_cc_call(api, spy):  # noqa: F811
    from app.services.data_retention import DataRetentionService
    clinic = _clinic(api.conn)
    patient = _patient(api.conn, clinic)
    phone = _q(api.conn, "SELECT phone FROM patients WHERE id = %s", patient)
    del spy[:]
    asyncio.run(DataRetentionService().anonymize_clinical_records(clinic, phone))
    assert spy and not _cc(spy)


def test_no_trigger_on_existing_tables_and_ui_is_gated():
    import re as _re
    for n in ("145_cc_foundation", "146_cc_enrollments_plans", "147_cc_measure", "148_cc_act", "149_cc_verify_outcomes"):
        sql = (ROOT / "migrations" / f"{n}.sql").read_text(encoding="utf-8")
        assert all(t.startswith("cc_") for t in _re.findall(r"CREATE TRIGGER\s+\w+\s+BEFORE[A-Z ]+?ON\s+([a-z_]+)", sql)), n
        assert set(_re.findall(r"ALTER TABLE\s+([a-z_]+)", sql)) <= {"clinics", "voice_outbound_jobs",
                                                                     "clinic_message_quota_usage", "%I"}, n
    html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
    for page in ("ccsetup", "ccprotocols", "ccpatients", "ccradar", "ccqueue", "ccinbox", "ccoutcomes"):
        nav = _re.search(rf'<[^>]+data-page="{page}"[^>]*>', html)
        assert nav and "display:none" in nav.group(0) and "data-cc" in nav.group(0), page   # UI-G11 hidden by default
