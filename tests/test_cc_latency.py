"""Seeded p95 latency gate (docs/continuous-care/TEST-PLAN.md §5, P10) on real PostgreSQL.

1,000 enrolled patients at a Continuous Care clinic. Three paths are timed with CC OFF and with CC ON for the same
clinic row, interleaved so drift hits both sides equally:
  * POST /webhook (signature, parse, tenant resolve, then the message through `_handle_message_locked`),
  * `_handle_message_locked` alone with a typed number in the main menu (the one input the CC hook inspects with a
    DB read),
  * the OPD consultation surfaces (`/admin/opd/queue`, `/admin/opd/workspace` — the repo has no
    `/admin/opd/consultations/*` path; these are the consultation screens).
Gate: p95(ON) ≤ p95(OFF) × 1.05 + 3 ms. The 3 ms absolute allowance absorbs timer and scheduler noise on a laptop
for paths that take a few ms; a real regression (an unindexed scan over 1,000 enrollments) costs far more.
"""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from statistics import quantiles
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.tenant import get_clinic_by_id, invalidate_tenant_cache
from tests.cc_world import care, wa  # noqa: F401  (fixtures)
from tests.test_ipd_api_e2e import _q, api  # noqa: F401  (fixture)

N, WARM, ALLOW_S = 60, 8, 0.003


def _p95(xs):
    return quantiles(xs, n=20)[18]


def _seed(care):
    """1,000 active enrollments cloned from one real enrollment (its consent row copied per patient)."""
    c = care.conn
    if _q(c, "SELECT count(*) FROM cc_enrollments WHERE clinic_id = %s", care.clinic) >= 1000:
        return
    e, _ = care.enroll()
    cols = lambda t, skip: [r for r in _q(c, "SELECT array_agg(column_name::text) FROM information_schema.columns "  # noqa: E731
                                          "WHERE table_name = %s AND is_generated = 'NEVER'", t) if r not in skip]
    base = 100_000_000 + uuid.uuid4().int % 800_000_000
    ccols = cols("cc_consents", {"id", "patient_id"})
    ecols = cols("cc_enrollments", {"id", "patient_id", "consent_id"})
    _q(c, f"""WITH p AS (
                INSERT INTO patients (clinic_id, phone, name, opted_in)
                SELECT %s, '+917' || (%s + g)::text, 'Seed ' || g, true
                FROM generate_series(1, 1000) g RETURNING id),
              k AS (
                INSERT INTO cc_consents (patient_id, {', '.join(ccols)})
                SELECT p.id, {', '.join('t.' + x for x in ccols)} FROM p,
                       (SELECT * FROM cc_consents WHERE id = (SELECT consent_id FROM cc_enrollments WHERE id = %s)) t
                RETURNING id, patient_id)
              INSERT INTO cc_enrollments (patient_id, consent_id, {', '.join(ecols)})
              SELECT k.patient_id, k.id, {', '.join('t.' + x for x in ecols)} FROM k,
                     (SELECT * FROM cc_enrollments WHERE id = %s) t""", care.clinic, base, e["id"], e["id"])


def _pair(on: dict):
    off = {**on, "cc_state": "OFF", "features": {**(on.get("features") or {}), "care_enabled": False}}
    return {"off": off, "on": on}


def _run(fn_off, fn_on):
    t = {"off": [], "on": []}
    for i in range(WARM + N):
        for k, fn in (("off", fn_off), ("on", fn_on)) if i % 2 else (("on", fn_on), ("off", fn_off)):
            s = time.perf_counter()
            fn()
            if i >= WARM:
                t[k].append(time.perf_counter() - s)
    return _p95(t["off"]), _p95(t["on"])


def _gate(name, off, on):
    assert on <= off * 1.05 + ALLOW_S, f"{name}: p95 CC ON {on * 1000:.2f} ms vs OFF {off * 1000:.2f} ms"


def _handler(clinic):
    from app.services.conversation import ConversationManager
    m = ConversationManager()
    m.whatsapp = AsyncMock()
    session = {"state": "main_menu", "context": {}, "last_processed_message_id": None, "booking_context_expires_at": None}
    patches = (patch("app.services.conversation.get_or_create_conversation", new=AsyncMock(return_value=session)),
               patch("app.services.conversation.get_patient_by_phone",
                     new=AsyncMock(return_value={"id": str(uuid.uuid4()), "language": "en"})),
               patch("app.services.conversation.update_conversation", new=AsyncMock()),
               patch("app.services.conversation.detect_intent", new=AsyncMock(return_value="unknown")))

    def go():
        for p in patches:
            p.start()
        try:
            asyncio.run(m._handle_message_locked(clinic=clinic, phone="+919800000099", message="142", message_type="text",
                                                 message_id="wamid." + uuid.uuid4().hex))
        finally:
            for p in reversed(patches):
                p.stop()
    return go


@pytest.fixture
def seeded(care):  # noqa: F811
    _seed(care)
    invalidate_tenant_cache()
    return SimpleNamespace(care=care, clinics=_pair(asyncio.run(get_clinic_by_id(care.clinic))))


def test_seed_has_1000_enrolled_patients(seeded):
    assert _q(seeded.care.conn, "SELECT count(*) FROM cc_enrollments WHERE clinic_id = %s AND status = 'active'",
              seeded.care.clinic) >= 1000


def test_handle_message_locked_p95(seeded):
    off, on = _run(_handler(seeded.clinics["off"]), _handler(seeded.clinics["on"]))
    _gate("_handle_message_locked", off, on)


def test_webhook_p95(seeded, monkeypatch):
    from app.config import settings
    from app.routers import webhook
    care = seeded.care
    secret = "latency-secret"
    monkeypatch.setattr(settings, "meta_app_secret", secret, raising=False)
    pnid = "pn" + uuid.uuid4().hex[:10]
    _q(care.conn, "UPDATE clinics SET phone_number_id = %s WHERE id = %s", pnid, care.clinic)
    mode = {"clinic": seeded.clinics["off"]}

    # the background task is timed in-line below (TestClient would otherwise run it with the real WhatsApp client)
    monkeypatch.setattr(webhook, "process_message_safe", AsyncMock(side_effect=lambda *a, **k: asyncio.sleep(0)))

    def post(which):
        def go():
            mode["clinic"] = seeded.clinics[which]
            body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "e", "changes": [{"field": "messages",
                "value": {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "1", "phone_number_id": pnid},
                          "contacts": [{"wa_id": "919800000099", "profile": {"name": "P"}}],
                          "messages": [{"from": "919800000099", "id": "wamid." + uuid.uuid4().hex, "timestamp": "1",
                                        "type": "text", "text": {"body": "142"}}]}}]}]}).encode()
            sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            r = care.api.client.post("/webhook", content=body, headers={"Content-Type": "application/json",
                                                                         "X-Hub-Signature-256": sig})
            assert r.status_code == 200
            _handler(mode["clinic"])()
        return go
    off, on = _run(post("off"), post("on"))
    _gate("/webhook", off, on)


@pytest.mark.parametrize("path", ["/admin/opd/queue", "/admin/opd/workspace"])
def test_opd_consultation_surfaces_p95(seeded, path):
    care = seeded.care
    _q(care.conn, "UPDATE clinics SET features = coalesce(features, '{}') || '{\"opd_enabled\": true}' WHERE id = %s",
       care.clinic)
    care.api.as_(care.u.admin)

    def get(flag):
        def go():
            r = care.api.call("GET", path, care.clinic)
            assert r.status_code in (200, 404, 422), r.text                 # timed either way; never a 5xx
        return go

    def toggled(flag):
        state = {"v": None}

        def go():
            if state["v"] != flag:
                _q(care.conn, "UPDATE clinics SET features = jsonb_set(coalesce(features, '{}'), '{care_enabled}', %s::jsonb) "
                              "WHERE id = %s", json.dumps(flag), care.clinic)
                invalidate_tenant_cache()
                state["v"] = flag
            get(flag)()
        return go
    off, on = _run(toggled(False), toggled(True))
    _q(care.conn, "UPDATE clinics SET features = jsonb_set(features, '{care_enabled}', 'true') WHERE id = %s", care.clinic)
    invalidate_tenant_cache()
    _gate(path, off, on)
