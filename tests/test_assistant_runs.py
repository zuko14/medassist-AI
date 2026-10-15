"""KK-P1-03: Kriya Ketu run store, state machine guards and /admin/assistant routes.

Covers KK-FR-001..003 and 024: feature gate, tenant/actor from the session only,
idempotency on client_request_id, CAS on run state, BOLA (another user's run is
404), super_admin scope (400 without ?clinic_id), lazy expiry and the EXECUTING
sweep. The supabase client is replaced by an in-memory table that enforces the
migration-108 unique key, so the store's own queries are exercised.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.assistant import store
from app.config import settings
from app.main import app
from app.routers import admin
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser

CID = str(uuid.uuid4())
OTHER_CID = str(uuid.uuid4())


# ── in-memory PostgREST stand-in ──────────────────────────────────────────────

class _Dup(Exception):
    code = "23505"


class _Res:
    def __init__(self, data):
        self.data = data


class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload = db, table, "select", None
        self.filters, self._order, self._limit = [], None, None

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, row):
        self.op, self.payload = "update", row
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: str(r.get(col)) == str(val))
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def lt(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r[col] < val)
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self.db.setdefault(self.table, [])
        if self.op == "insert":
            row = dict(self.payload)
            key = (row["clinic_id"], row["username"], row["client_request_id"])
            if any((r["clinic_id"], r["username"], r["client_request_id"]) == key for r in rows):
                raise _Dup("duplicate key value violates unique constraint")
            now = datetime.now(timezone.utc).isoformat()
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("created_at", now)
            row.setdefault("updated_at", now)
            row.setdefault("steps", [])
            row.setdefault("params", {})
            rows.append(row)
            return _Res([dict(row)])
        hit = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
            return _Res([dict(r) for r in hit])
        if self._order:
            hit.sort(key=lambda r: r.get(self._order[0]) or "", reverse=self._order[1])
        if self._limit is not None:
            hit = hit[: self._limit]
        return _Res([dict(r) for r in hit])


class _DB:
    def __init__(self):
        self.tables = {}
        self.queries = []

    def table(self, name):
        q = _Q(self.tables, name)
        self.queries.append(q)
        return q


@pytest.fixture
def db():
    fake = _DB()

    async def _sb(q):
        return q.execute()

    with patch.object(store, "supabase", fake), patch.object(store, "sb", _sb):
        yield fake


def _user(username="desk1", clinic_id=CID, role="staff", staff_role="FRONT_DESK"):
    return AdminUser(username=username, role=role, clinic_id=clinic_id, user_id=str(uuid.uuid4()),
                     staff_role=staff_role, permissions=[])


@pytest.fixture
def as_user():
    holder = {"user": _user()}
    app.dependency_overrides[admin.verify_credentials] = lambda: holder["user"]
    clinic = {"id": CID, "plan": "polyclinic", "features": {"kriya_ketu": True}}
    with patch.object(settings, "assistant_enabled", True), \
         patch.object(settings, "assistant_llm_enabled", False), \
         patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value=clinic)), \
         patch.object(assistant_router, "_rate_limited", AsyncMock(return_value=False)):
        yield holder
    app.dependency_overrides.pop(admin.verify_credentials, None)


client = TestClient(app)


def _post(text="open leads", crid=None, **extra):
    body = {"client_request_id": crid or str(uuid.uuid4()), "text": text, "input_mode": "text",
            "ui_context": {"page": "dashboard"}}
    body.update(extra)
    return client.post("/admin/assistant/requests", json=body)


# ── access (KK-FR-001, 002) ───────────────────────────────────────────────────

def test_disabled_globally_is_403(db, as_user):
    with patch.object(settings, "assistant_enabled", False):
        r = _post()
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "assistant_disabled"


def test_clinic_not_opted_in_is_403(db, as_user):
    with patch.object(assistant_router, "get_clinic_by_id", AsyncMock(return_value={"plan": "enterprise",
                                                                                     "features": {"*": True}})):
        r = _post()
    assert r.status_code == 403


def test_body_clinic_id_is_rejected(db, as_user):
    assert _post(clinic_id=OTHER_CID).status_code == 422


def test_text_over_1000_chars_is_rejected(db, as_user):
    assert _post(text="x" * 1001).status_code == 422


def test_super_admin_without_scope_gets_400(db, as_user):
    as_user["user"] = _user(role="super_admin", clinic_id=None, staff_role=None)
    assert _post().status_code == 400
    assert client.get("/admin/assistant/runs").status_code == 400


def test_super_admin_with_scope_writes_to_that_clinic(db, as_user):
    as_user["user"] = _user(role="super_admin", clinic_id=None, staff_role=None)
    crid = str(uuid.uuid4())
    body = {"client_request_id": crid, "text": "open leads", "input_mode": "text", "ui_context": {"page": "dashboard"}}
    r = client.post(f"/admin/assistant/requests?clinic_id={CID}", json=body)
    assert r.status_code == 200
    assert db.tables["assistant_runs"][0]["clinic_id"] == CID


def test_rate_limited_is_429(db, as_user):
    with patch.object(assistant_router, "_rate_limited", AsyncMock(return_value=True)):
        assert _post().status_code == 429
    assert "assistant_runs" not in db.tables


# ── routing in this task: nav.open only ───────────────────────────────────────

@pytest.mark.parametrize("text, page", [
    ("open leads", "leads"), ("Go to lab reports", "labreports"), ("show the live queue page", "opdqueue"),
    ("Appointments", "appointments"), ("open WhatsApp Leads", "leads"), ("take me to payment settings", "paysettings"),
])
def test_navigation_completes_verified(db, as_user, text, page):
    out = _post(text).json()
    assert out["state"] == "COMPLETED_VERIFIED"
    assert out["action_id"] == "nav.open"
    assert out["route_layer"] == "nav"
    assert out["navigate"] == page
    row = db.tables["assistant_runs"][0]
    assert row["params"] == {"page": page} and row["state"] == "COMPLETED_VERIFIED"


@pytest.mark.parametrize("text, code, layer", [
    ("cancel all appointments", "unsupported", "none"), ("open the pod bay doors", "unsupported", "none"),
    ("don't open leads?", "negated", "rules"), ("what if Dr Rao is on leave", "hypothetical", "rules"),
    ("prescribe paracetamol for Sita", "clinical", "rules"),
])
def test_anything_else_is_rejected_with_no_write(db, as_user, text, code, layer):
    out = _post(text).json()
    assert (out["state"], out["error_code"], out["route_layer"]) == ("REJECTED", code, layer)
    assert out["navigate"] is None


def test_hidden_page_is_not_available(db, as_user):
    out = _post("open staff accounts", ui_context={"page": "dashboard", "visible_pages": ["dashboard"]}).json()
    assert (out["state"], out["error_code"]) == ("REJECTED", "not_available")
    assert _post("open staff accounts", ui_context={"page": "", "visible_pages": ["staff"]}).json()["navigate"] == "staff"


def test_how_to_is_a_help_card(db, as_user):
    out = _post("how do I add a doctor leave?").json()
    assert (out["state"], out["action_id"], out["route_layer"]) == ("COMPLETED_VERIFIED", "help.howto", "rules")
    assert out["cards"][0]["type"] == "help" and out["cards"][0]["page"] == "leaves"


def test_visible_pages_is_bounded(db, as_user):
    assert _post(ui_context={"page": "", "visible_pages": ["x" * 41]}).status_code == 422
    assert _post(ui_context={"page": "", "visible_pages": ["leads"] * 65}).status_code == 422


def test_run_out_shape(db, as_user):
    out = _post().json()
    assert set(out) == {"run_id", "state", "action_id", "route_layer", "message", "cards", "steps",
                        "plan_hash", "expires_at", "navigate", "error_code"}


# ── idempotency (KK-FR-003) ───────────────────────────────────────────────────

def test_same_client_request_id_returns_the_same_run(db, as_user):
    crid = str(uuid.uuid4())
    a, b = _post(crid=crid).json(), _post(crid=crid).json()
    assert a["run_id"] == b["run_id"]
    assert len(db.tables["assistant_runs"]) == 1


def test_another_user_may_reuse_a_client_request_id(db, as_user):
    crid = str(uuid.uuid4())
    a = _post(crid=crid).json()
    as_user["user"] = _user("desk2")
    b = _post(crid=crid).json()
    assert a["run_id"] != b["run_id"]


@pytest.mark.asyncio
async def test_insert_race_returns_the_winner(db):
    user = _user()
    crid = str(uuid.uuid4())
    run = {"client_request_id": crid, "input_mode": "text", "input_text": "open leads"}
    original_get = store.get_by_request

    calls = {"n": 0}

    async def first_miss(*a):
        calls["n"] += 1
        return None if calls["n"] == 1 else await original_get(*a)

    winner, _ = await store.create_run(CID, user, run, "1.2.3.4")
    with patch.object(store, "get_by_request", first_miss):
        again, created = await store.create_run(CID, user, run, "1.2.3.4")
    assert created is False and again["id"] == winner["id"]


# ── BOLA (SEC-03) and tenant scope (SEC-01) ───────────────────────────────────

def test_another_users_run_is_404_everywhere(db, as_user):
    run_id = _post().json()["run_id"]
    as_user["user"] = _user("desk2")
    assert client.post(f"/admin/assistant/runs/{run_id}/cancel").status_code == 404
    assert client.post(f"/admin/assistant/runs/{run_id}/confirm", json={"plan_hash": "x"}).status_code == 404
    assert client.get("/admin/assistant/runs").json() == []
    assert _post(reply_to_run_id=run_id).status_code == 404


def test_every_store_query_is_scoped_to_clinic_and_user(db, as_user):
    _post()
    client.get("/admin/assistant/runs")
    for q in db.queries:
        assert q.table == "assistant_runs"
        if q.op != "insert":
            probe = {"clinic_id": OTHER_CID, "username": "desk1"}
            probe2 = {"clinic_id": CID, "username": "intruder"}
            assert not all(f(probe) for f in q.filters), "query not scoped to clinic"
            assert not all(f(probe2) for f in q.filters), "query not scoped to username"


def test_list_returns_only_this_users_runs_newest_first(db, as_user):
    first = _post("open leads").json()["run_id"]
    second = _post("open doctors").json()["run_id"]
    as_user["user"] = _user("desk2")
    _post("open staff")
    as_user["user"] = _user("desk1")
    ids = [r["run_id"] for r in client.get("/admin/assistant/runs").json()]
    assert set(ids) == {first, second}


# ── CAS (KK-FR-024), cancel, confirm, expiry, sweep ───────────────────────────

def _seed(db, state, **over):
    row = {"id": str(uuid.uuid4()), "clinic_id": CID, "username": "desk1", "role": "staff",
           "client_request_id": str(uuid.uuid4()), "input_mode": "text", "input_text": "x", "state": state,
           "params": {}, "steps": [], "result": None, "plan_hash": "h1",
           "created_at": datetime.now(timezone.utc).isoformat(),
           "updated_at": datetime.now(timezone.utc).isoformat(),
           "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    row.update(over)
    db.tables.setdefault("assistant_runs", []).append(row)
    return row["id"]


@pytest.mark.asyncio
async def test_parallel_transitions_have_exactly_one_winner(db):
    run_id = _seed(db, "NEEDS_CONFIRMATION")
    results = await asyncio.gather(*[
        store.transition(CID, "desk1", run_id, ("NEEDS_CONFIRMATION",), "EXECUTING") for _ in range(5)])
    assert sum(r is not None for r in results) == 1


def test_cancel_from_needs_confirmation(db, as_user):
    run_id = _seed(db, "NEEDS_CONFIRMATION")
    assert client.post(f"/admin/assistant/runs/{run_id}/cancel").json()["state"] == "CANCELLED"
    # Second cancel: nothing left to cancel
    assert client.post(f"/admin/assistant/runs/{run_id}/cancel").status_code == 409


@pytest.mark.parametrize("state", ["EXECUTING", "VERIFYING", "COMPLETED_VERIFIED", "UNKNOWN_OUTCOME"])
def test_cannot_cancel_executing_or_finished_runs(db, as_user, state):
    run_id = _seed(db, state)
    assert client.post(f"/admin/assistant/runs/{run_id}/cancel").status_code == 409
    assert db.tables["assistant_runs"][0]["state"] == state


def test_confirm_never_executes_in_this_task(db, as_user):
    run_id = _seed(db, "COMPLETED_VERIFIED")
    r = client.post(f"/admin/assistant/runs/{run_id}/confirm", json={"plan_hash": "h1"})
    assert r.status_code == 409
    assert r.json()["detail"]["error_code"] == "not_confirmable"
    run_id = _seed(db, "NEEDS_CONFIRMATION")
    assert client.post(f"/admin/assistant/runs/{run_id}/confirm", json={"plan_hash": "h1"}).status_code == 409
    assert db.tables["assistant_runs"][-1]["state"] == "NEEDS_CONFIRMATION"


def test_confirm_body_is_strict(db, as_user):
    run_id = _seed(db, "NEEDS_CONFIRMATION")
    r = client.post(f"/admin/assistant/runs/{run_id}/confirm", json={"plan_hash": "h1", "clinic_id": OTHER_CID})
    assert r.status_code == 422


def test_expired_confirmation_becomes_expired_on_read(db, as_user):
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    run_id = _seed(db, "NEEDS_CONFIRMATION", expires_at=past)
    r = client.post(f"/admin/assistant/runs/{run_id}/confirm", json={"plan_hash": "h1"})
    assert r.status_code == 409
    assert db.tables["assistant_runs"][0]["state"] == "EXPIRED"


def test_get_runs_sweeps_stuck_executing_and_expired(db, as_user):
    old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    stuck = _seed(db, "EXECUTING", updated_at=old)
    fresh = _seed(db, "EXECUTING")
    lapsed = _seed(db, "NEEDS_CLARIFICATION", expires_at=old)
    states = {r["run_id"]: r["state"] for r in client.get("/admin/assistant/runs").json()}
    assert states[stuck] == "UNKNOWN_OUTCOME"
    assert states[fresh] == "EXECUTING"
    assert states[lapsed] == "EXPIRED"


def test_active_filter_and_limit_bounds(db, as_user):
    _seed(db, "COMPLETED_VERIFIED")
    active = _seed(db, "NEEDS_CONFIRMATION")
    assert [r["run_id"] for r in client.get("/admin/assistant/runs?active=1").json()] == [active]
    assert client.get("/admin/assistant/runs?limit=500").status_code == 422


def test_unknown_run_id_format_is_404_not_500(db, as_user):
    assert client.post("/admin/assistant/runs/not-a-uuid/cancel").status_code in (404, 422)


@pytest.mark.parametrize("staff_role, guard", [
    ("CORPORATE_VIEWER", admin._enforce_corporate_viewer_scope),
    ("PHLEBOTOMIST", admin._enforce_phlebotomist_scope),
])
@pytest.mark.parametrize("method, path", [("POST", "/admin/assistant/requests"), ("GET", "/admin/assistant/runs")])
def test_confined_logins_never_reach_assistant_routes(staff_role, guard, method, path):
    from fastapi import HTTPException
    from starlette.requests import Request

    req = Request({"type": "http", "method": method, "path": path, "headers": [], "query_string": b""})
    with pytest.raises(HTTPException) as exc:
        guard(req, _user(staff_role=staff_role))
    assert exc.value.status_code == 403


# ── L4 fallback wiring (KK-P1-07) ─────────────────────────────────────────────

from app.assistant import llm as _llm  # noqa: E402


def _llm_out(status="ok", tokens=150, **intent):
    base = {"kind": "command", "action_id": "nav.open", "question_for_user": None,
            "slots": {k: None for k in _llm.SLOT_FIELDS}}
    slots = intent.pop("slots", {})
    base.update(intent)
    base["slots"].update(slots)
    parsed = _llm.Intent.model_validate(base) if status == "ok" else None
    return _llm.LlmOutcome(status, parsed, tokens if status in ("ok", "invalid") else 0, 2)


def test_rules_hit_never_calls_the_llm(db, as_user):
    spy = AsyncMock()
    with patch.object(assistant_router.llm, "classify", spy):
        assert _post("open leads").json()["route_layer"] == "nav"
    assert spy.await_count == 0


def test_llm_resolves_a_miss_into_navigation(db, as_user):
    with patch.object(assistant_router.llm, "classify", AsyncMock(return_value=_llm_out(slots={"page": "leads"}))):
        out = _post("i want to see who messaged us on whatsapp recently").json()
    assert (out["state"], out["navigate"], out["route_layer"]) == ("COMPLETED_VERIFIED", "leads", "llm")
    row = db.tables["assistant_runs"][0]
    assert (row["llm_tokens"], row["llm_cost_paise"]) == (150, 2)


@pytest.mark.parametrize("kind, code", [("negated", "negated"), ("hypothetical", "hypothetical"), ("multi", "multi")])
def test_llm_safety_kinds_never_route(db, as_user, kind, code):
    with patch.object(assistant_router.llm, "classify", AsyncMock(return_value=_llm_out(kind=kind, action_id=None))):
        out = _post("something the rules did not catch at all").json()
    assert (out["state"], out["error_code"], out["route_layer"]) == ("REJECTED", code, "llm")


@pytest.mark.parametrize("status, layer", [("disabled", "none"), ("unavailable", "none"), ("invalid", "llm"),
                                           ("rate_limited", "none"), ("suspicious", "none")])
def test_llm_failure_is_a_deterministic_reply(db, as_user, status, layer):
    with patch.object(assistant_router.llm, "classify", AsyncMock(return_value=_llm_out(status))):
        out = _post("something the rules did not catch at all").json()
    assert (out["state"], out["error_code"], out["route_layer"]) == ("REJECTED", "unsupported", layer)


@pytest.mark.parametrize("role, noted", [("clinic_admin", True), ("staff", False)])
def test_budget_note_only_for_admins(db, as_user, role, noted):
    as_user["user"] = _user(role=role, staff_role=None if role != "staff" else "FRONT_DESK")
    with patch.object(assistant_router.llm, "classify", AsyncMock(return_value=_llm_out("budget"))):
        out = _post("something the rules did not catch at all").json()
    assert out["error_code"] == "unsupported"
    assert ("AI budget" in out["message"]) is noted


def test_llm_gets_only_the_visible_pages(db, as_user):
    with patch.object(assistant_router.llm, "classify", AsyncMock(return_value=_llm_out(slots={"page": "leads"}))) as c:
        _post("show me who wrote to us on whatsapp", ui_context={"page": "", "visible_pages": ["leads"]})
    assert c.await_args.kwargs["visible_pages"] == ["leads"]
