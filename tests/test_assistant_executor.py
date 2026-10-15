"""KK-P1-05: Kriya Ketu executor — in-process dispatch + read-back verification (ADR-004).

A fake /admin app stands in for the real routes, so status mapping, credential
forwarding, timeouts and the no-retry rule are tested without touching admin.py.
Covers EXE-01/03/04/07/08 and the "never retry a write" / "never forward raw
detail" rules.
"""

import asyncio
import uuid

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from app.assistant import executor, store
from app.assistant.executor import Call, CallerAuth, Step
from tests.test_assistant_runs import _DB

CID = str(uuid.uuid4())
AUTH = CallerAuth(cookie="sess-123", authorization=None)

# ── fake /admin app ──────────────────────────────────────────────────────────

fake = FastAPI()
STATE = {"mode": "ok", "writes": 0, "written": set(), "slow": 0.0}


def _need_session(request: Request):
    if request.cookies.get("kriya_admin_session") != "sess-123" and \
            request.headers.get("authorization") != "Basic dXNlcjpwdw==":
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")


@fake.get("/admin/echo", dependencies=[Depends(_need_session)])
async def echo(request: Request):
    return {"cookie": request.cookies.get("kriya_admin_session"),
            "authorization": request.headers.get("authorization"),
            "correlation": request.headers.get("x-correlation-id"),
            "run": request.headers.get("x-kriya-assistant-run"),
            "query": dict(request.query_params),
            "other": request.headers.get("x-forwarded-for")}


@fake.post("/admin/thing", dependencies=[Depends(_need_session)])
async def write_thing(body: dict):
    mode = STATE["mode"]
    if mode.isdigit() and int(mode) < 500:
        raise HTTPException(status_code=int(mode), detail="raw: duplicate key value violates constraint uq_secret")
    STATE["writes"] += 1
    if STATE["slow"]:
        await asyncio.sleep(STATE["slow"])
    STATE["written"].add(body["name"])
    if mode == "500":
        raise RuntimeError("boom after commit: postgrest internals")
    return {"id": body["name"]}


@fake.get("/admin/thing/{name}", dependencies=[Depends(_need_session)])
async def read_thing(name: str):
    if STATE["mode"] == "read500":
        raise HTTPException(status_code=500, detail="Traceback (most recent call last)")
    return {"exists": name in STATE["written"]}


@fake.get("/admin/forbidden", dependencies=[Depends(_need_session)])
async def forbidden():
    raise HTTPException(status_code=403, detail="Missing permission: LEADS_MANAGE")


@pytest.fixture(autouse=True)
def _reset():
    STATE.update(mode="ok", writes=0, written=set(), slow=0.0)


@pytest.fixture
def db():
    d = _DB()

    async def _sb(q):
        return q.execute()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store, "supabase", d)
        mp.setattr(store, "sb", _sb)
        yield d


def _run(db, state="EXECUTING"):
    row = {"id": str(uuid.uuid4()), "clinic_id": CID, "username": "desk1", "role": "staff",
           "client_request_id": str(uuid.uuid4()), "input_mode": "text", "input_text": "x", "state": state,
           "params": {}, "steps": [], "result": None, "updated_at": "2026-10-14T00:00:00+00:00"}
    db.tables.setdefault("assistant_runs", []).append(row)
    return dict(row)


async def _verify_written(read, data, call):
    out = await read(Call("GET", f"/admin/thing/{call.json['name']}"))
    if out.status != "ok":
        return None, "could not read back"
    return out.data["exists"], "read back from the thing list"


def _write(name="a"):
    return Step(label=f"Create {name}", call=Call("POST", "/admin/thing", json={"name": name}), verify=_verify_written)


async def _dispatch(call, **kw):
    return await executor.dispatch(call, AUTH, CID, "run-1", app=fake, **kw)


# ── dispatch ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_forwards_only_the_callers_credentials_scope_and_ids():
    from app.utils.correlation import set_correlation_id
    set_correlation_id("corr-9")
    out = await _dispatch(Call("GET", "/admin/echo", query={"days": 7}))
    assert out.status == "ok"
    assert out.data == {"cookie": "sess-123", "authorization": None, "correlation": "corr-9", "run": "run-1",
                        "query": {"days": "7", "clinic_id": CID}, "other": None}


@pytest.mark.asyncio
async def test_basic_auth_callers_are_forwarded_too():
    auth = CallerAuth(cookie=None, authorization="Basic dXNlcjpwdw==")
    out = await executor.dispatch(Call("GET", "/admin/echo"), auth, CID, "r", app=fake)
    assert out.status == "ok" and out.data["authorization"] == "Basic dXNlcjpwdw=="


@pytest.mark.asyncio
async def test_a_call_cannot_override_the_clinic_scope():
    out = await _dispatch(Call("GET", "/admin/echo", query={"clinic_id": "someone-else"}))
    assert out.data["query"]["clinic_id"] == CID


@pytest.mark.asyncio
async def test_no_credentials_means_401_not_privilege():
    out = await executor.dispatch(Call("GET", "/admin/echo"), CallerAuth(None, None), CID, "r", app=fake)
    assert (out.status, out.error_code) == ("failed", "session_expired")


@pytest.mark.asyncio
async def test_forbidden_stays_forbidden():
    out = await _dispatch(Call("GET", "/admin/forbidden"))
    assert (out.status, out.http_status, out.error_code) == ("failed", 403, "forbidden")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, code", [("400", "invalid"), ("404", "not_found"), ("409", "conflict"),
                                        ("422", "invalid"), ("429", "rate_limited"), ("418", "failed")])
async def test_4xx_is_failed_with_a_fixed_message_and_no_write(mode, code):
    STATE["mode"] = mode
    out = await _dispatch(Call("POST", "/admin/thing", json={"name": "a"}))
    assert (out.status, out.error_code) == ("failed", code)
    assert "raw" not in out.message and "constraint" not in out.message
    assert STATE["writes"] == 0


@pytest.mark.asyncio
async def test_5xx_on_a_write_is_unknown_and_never_retried():
    STATE["mode"] = "500"
    out = await _dispatch(Call("POST", "/admin/thing", json={"name": "a"}))
    assert (out.status, out.error_code) == ("unknown", "server_error")
    assert "boom" not in out.message and "postgrest" not in out.message
    assert STATE["writes"] == 1


@pytest.mark.asyncio
async def test_5xx_on_a_read_is_failed_not_unknown():
    STATE["mode"] = "read500"
    out = await _dispatch(Call("GET", "/admin/thing/a"))
    assert (out.status, out.error_code) == ("failed", "server_error")
    assert "Traceback" not in out.message


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_lets_the_endpoint_finish():
    STATE["slow"] = 0.3
    out = await _dispatch(Call("POST", "/admin/thing", json={"name": "late"}), timeout=0.05)
    assert (out.status, out.error_code) == ("unknown", "timeout")
    await asyncio.sleep(0.5)
    assert STATE["writes"] == 1 and "late" in STATE["written"]  # finished, not cancelled; not retried


@pytest.mark.asyncio
async def test_only_admin_paths_are_dispatched():
    for path in ("/platform/clinics", "/admin/assistant/requests", "/webhook", "admin/thing"):
        with pytest.raises(ValueError):
            await _dispatch(Call("GET", path))


# ── execute / reverify (state machine) ───────────────────────────────────────

async def _execute(db, run, steps, **kw):
    return await executor.execute(run, steps, AUTH, app=fake, **kw)


@pytest.mark.asyncio
async def test_r0_read_completes_verified(db):
    run = _run(db)
    row, outcomes = await _execute(db, run, [Step("Read thing", Call("GET", "/admin/thing/x"))])
    assert row["state"] == "COMPLETED_VERIFIED"
    assert outcomes[0].data == {"exists": False}
    assert row["steps"][0]["status"] == "done"


@pytest.mark.asyncio
async def test_write_verified_by_read_back(db):
    run = _run(db)
    row, _ = await _execute(db, run, [_write("a")])
    assert row["state"] == "COMPLETED_VERIFIED"
    assert row["steps"][0] == {"label": "Create a", "method": "POST", "path": "/admin/thing", "status": "done",
                               "http_status": 200, "error_code": None, "evidence": "read back from the thing list",
                               "reverify_attempts": 0}
    assert STATE["writes"] == 1


@pytest.mark.asyncio
async def test_steps_never_store_response_data(db):
    run = _run(db)
    row, _ = await _execute(db, run, [Step("Read", Call("GET", "/admin/echo"))])
    assert "sess-123" not in str(row)


@pytest.mark.asyncio
async def test_verify_mismatch_is_unknown_outcome(db):
    async def says_missing(read, data, call):
        return False, "not found on read-back"

    run = _run(db)
    row, _ = await _execute(db, run, [Step("Create", Call("POST", "/admin/thing", json={"name": "a"}), says_missing)])
    assert row["state"] == "UNKNOWN_OUTCOME"
    assert row["steps"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_4xx_write_is_failed_and_nothing_else_runs(db):
    STATE["mode"] = "409"
    run = _run(db)
    row, _ = await _execute(db, run, [_write("a"), _write("b")])
    assert row["state"] == "FAILED" and row["error_code"] == "conflict"
    assert [s["status"] for s in row["steps"]] == ["failed", "skipped"]


@pytest.mark.asyncio
async def test_playbook_stops_at_the_first_failed_write(db):
    run = _run(db)
    bad = Step("Forbidden", Call("GET", "/admin/forbidden"))
    row, _ = await _execute(db, run, [_write("a"), bad, _write("c")])
    assert row["state"] == "PARTIALLY_COMPLETED"
    assert [s["status"] for s in row["steps"]] == ["done", "failed", "skipped"]
    assert STATE["written"] == {"a"}


@pytest.mark.asyncio
async def test_execute_requires_the_run_to_be_claimed(db):
    run = _run(db, state="NEEDS_CONFIRMATION")
    with pytest.raises(ValueError):
        await _execute(db, run, [_write("a")])
    assert STATE["writes"] == 0


@pytest.mark.asyncio
async def test_timeout_then_reverify_completes_with_one_write(db):
    STATE["slow"] = 0.2
    run = _run(db)
    row, _ = await _execute(db, run, [_write("a")], timeout=0.02)
    assert row["state"] == "UNKNOWN_OUTCOME" and row["error_code"] == "timeout"
    await asyncio.sleep(0.4)
    row = await executor.reverify(row, [_write("a")], AUTH, app=fake)
    assert row["state"] == "COMPLETED_VERIFIED"
    assert STATE["writes"] == 1


@pytest.mark.asyncio
async def test_reverify_finds_nothing_written_is_failed(db):
    async def not_there(read, data, call):
        return (False, "not written") if data is None else (None, "can't tell")

    run = _run(db)
    step = Step("Create", Call("POST", "/admin/thing", json={"name": "a"}), not_there)
    row, _ = await _execute(db, run, [step])
    assert row["state"] == "UNKNOWN_OUTCOME"
    row = await executor.reverify(row, [step], AUTH, app=fake)
    assert row["state"] == "FAILED"


@pytest.mark.asyncio
async def test_three_undecided_reverifies_need_a_human(db):
    async def unsure(read, data, call):
        return None, "can't tell"

    run = _run(db)
    step = Step("Create", Call("POST", "/admin/thing", json={"name": "a"}), unsure)
    row, _ = await _execute(db, run, [step])
    for expected in ("UNKNOWN_OUTCOME", "UNKNOWN_OUTCOME", "NEEDS_HUMAN"):
        row = await executor.reverify(row, [step], AUTH, app=fake)
        assert row["state"] == expected
    assert STATE["writes"] == 1


@pytest.mark.asyncio
async def test_reverify_only_from_unknown_outcome(db):
    run = _run(db, state="COMPLETED_VERIFIED")
    with pytest.raises(ValueError):
        await executor.reverify(run, [_write("a")], AUTH, app=fake)


# ── through the REAL app: real verify_credentials, middlewares, scope ────────

def test_session_cookie_name_matches_the_admin_router():
    from app.routers.admin import ADMIN_SESSION_COOKIE
    assert executor.SESSION_COOKIE == ADMIN_SESSION_COOKIE


@pytest.mark.asyncio
@pytest.mark.parametrize("password, expected", [("admin", ("ok", 200)), ("wrong", ("failed", 401))])
async def test_real_app_runs_its_own_auth_for_the_dispatched_request(password, expected):
    import base64
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.routers import admin

    limiter = MagicMock(is_rate_limited=MagicMock(return_value=False))
    clinic = {"id": CID, "plan": "polyclinic", "features": {}}
    basic = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    with patch.object(admin, "sb", AsyncMock(return_value=MagicMock(data=[]))), \
         patch.object(admin, "login_rate_limiter", limiter), \
         patch.object(admin, "get_clinic_by_id", AsyncMock(return_value=clinic)):
        out = await executor.dispatch(Call("GET", "/admin/me"), CallerAuth(None, basic), CID, "r")
    assert (out.status, out.http_status) == expected
    if out.status == "ok":
        assert out.data["username"] == "admin" and out.data["role"] == "super_admin"
        assert out.data["clinic_id"] is None and out.data["plan"] == "polyclinic"  # scoped via ?clinic_id
    else:
        assert out.error_code == "session_expired" and "admin" not in out.message
