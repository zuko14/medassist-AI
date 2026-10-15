"""KK-P1-08: Kriya Ketu policy pre-check, preview, plan hash and confirmation (architecture §3 steps 7-11, §5).

Unit: role x permission-source x feature matrix, R3 role rule, plan_hash stability.
Flow (through /admin/assistant with synthetic actions dispatching into the fake
/admin app): R0 runs at once; R2 previews, writes nothing until confirmed, then
executes and verifies; stale plan -> re-preview; wrong hash, expiry, typed count,
parallel confirm and revoked permission never execute.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.assistant import policy
from app.assistant.executor import Call, Step
from app.assistant.registry import ADMIN_ROLES, REGISTRY, ActionSpec, validate
from app.main import app
from app.routers import assistant as assistant_router
from tests.test_assistant_executor import STATE, _reset, fake  # noqa: F401  (autouse reset)
from tests.test_assistant_runs import _user, as_user, db  # noqa: F401  (fixtures)

CLINIC = {"id": "c1", "plan": "polyclinic", "features": {"kriya_ketu": True}}


def _noop(*a, **k):
    return None


def _spec(**kw):
    base = dict(id="t.x", module="t", summary="Do the thing", risk="R2", call=_noop, verify=_noop,
                endpoint=("POST", "/admin/leaves"))
    return ActionSpec(**{**base, **kw})


# ── pre-check matrix (KK-FR-020) ──────────────────────────────────────────────

@pytest.mark.parametrize("role, perms, spec_kw, allowed, reason", [
    ("clinic_admin", [], dict(permission="LEADS_MANAGE", perm_source="dependency"), True, None),
    ("super_admin", [], dict(permission="LEADS_MANAGE", perm_source="dependency"), True, None),
    ("staff", ["LEADS_MANAGE"], dict(permission="LEADS_MANAGE", perm_source="dependency"), True, None),
    ("staff", [], dict(permission="LEADS_MANAGE", perm_source="dependency"), False, "forbidden"),
    ("staff", [], dict(permission="VOICE_MANAGE", perm_source="handler"), False, "forbidden"),
    ("staff", ["VOICE_MANAGE"], dict(permission="VOICE_MANAGE", perm_source="handler"), True, None),
    ("staff", ["LEADS_MANAGE"], dict(perm_source="role", roles=ADMIN_ROLES), False, "forbidden"),
    ("clinic_admin", [], dict(perm_source="role", roles=ADMIN_ROLES), True, None),
    ("staff", [], dict(), True, None),                                          # perm_source none
    ("staff", [], dict(risk="R3", roles=ADMIN_ROLES, max_items=25), False, "forbidden"),
    ("clinic_admin", [], dict(risk="R3", roles=ADMIN_ROLES, max_items=25), True, None),
    ("staff", ["LAB_TESTS_MANAGE"], dict(risk="R3", permission="LAB_TESTS_MANAGE", perm_source="dependency",
                                         max_items=200), True, None),
    ("clinic_admin", [], dict(feature=lambda c: False), False, "feature_disabled"),
    ("clinic_admin", [], dict(feature=lambda c: c.get("plan") == "polyclinic"), True, None),
])
def test_precheck_matrix(role, perms, spec_kw, allowed, reason):
    spec = _spec(**spec_kw)
    validate(spec)
    user = _user(role=role, staff_role="FRONT_DESK" if role == "staff" else None)
    user.permissions = perms
    d = policy.precheck(spec, user, CLINIC)
    assert (d.allowed, d.reason) == (allowed, reason)


def test_refusal_names_the_missing_permission():
    user = _user()
    d = policy.precheck(_spec(permission="LEADS_MANAGE", perm_source="dependency"), user, CLINIC)
    assert "Leads Manage" in d.message


def test_admin_only_refusal_says_so():
    d = policy.precheck(_spec(perm_source="role", roles=ADMIN_ROLES), _user(), CLINIC)
    assert "admin" in d.message.lower()


def test_permitted_actions_is_the_llm_enum():
    allowed = _spec(id="a.ok")
    blocked = _spec(id="a.no", permission="LEADS_MANAGE", perm_source="dependency")
    with patch.dict(REGISTRY, {"a.ok": allowed, "a.no": blocked}):
        ids = policy.permitted_actions(_user(), CLINIC)
    assert "a.ok" in ids and "a.no" not in ids and "nav.open" in ids


# ── plan hash (KK-FR-023) ─────────────────────────────────────────────────────

def _steps(name="a", query=None):
    return [Step("Create", Call("POST", "/admin/thing", query=query or {}, json={"name": name, "n": 1}), _noop)]


def test_plan_hash_is_stable_and_order_independent():
    a = policy.plan_hash("t.x", {"b": 2, "a": 1}, _steps(), {"x": [1, 2]}, 3)
    b = policy.plan_hash("t.x", {"a": 1, "b": 2}, _steps(), {"x": [1, 2]}, 3)
    assert a == b and len(a) == 64


@pytest.mark.parametrize("change", [
    dict(action_id="t.y"), dict(params={"a": 2}), dict(steps=_steps("b")), dict(snapshot={"x": [2, 1]}),
    dict(affected=4), dict(steps=_steps(query={"days": 7})),
])
def test_plan_hash_covers_every_write_and_the_snapshot(change):
    base = dict(action_id="t.x", params={"a": 1}, steps=_steps(), snapshot={"x": [1, 2]}, affected=3)
    assert policy.plan_hash(**base) != policy.plan_hash(**{**base, **change})


# ── flow through the API ──────────────────────────────────────────────────────

async def _verify_written(read, data, call):
    out = await read(Call("GET", f"/admin/thing/{call.json['name']}"))
    return (out.data["exists"] if out.status == "ok" else None), "read back from the thing list"


async def _preview(read, params):
    out = await read(Call("GET", "/admin/thing/a"))
    return {"title": "Create thing a", "bullets": ["One thing will be created."], "rows": [], "warnings": [],
            "affected": 1, "snapshot": {"exists": out.data["exists"], "mode": STATE["mode"]}}


async def _preview_bulk(read, params):
    return {"title": "Create 3 things", "bullets": [], "rows": [], "warnings": [], "affected": 3,
            "snapshot": None}


READ = ActionSpec(id="thing.read", module="t", summary="Read a thing", risk="R0", triggers=("thing status",),
                  endpoint=("GET", "/admin/thing/{name}"),
                  call=lambda p: [Step("Read", Call("GET", "/admin/thing/a"))],
                  present=lambda outs, p: {"message": f"exists={outs[0].data['exists']}",
                                           "cards": [{"type": "text", "text": "ok"}]})
CREATE = ActionSpec(id="thing.create", module="t", summary="Create a thing", risk="R2", triggers=("make thing",),
                    endpoint=("POST", "/admin/thing"), preview=_preview, verify=_verify_written,
                    call=lambda p: [Step("Create a", Call("POST", "/admin/thing", json={"name": "a"}),
                                         _verify_written)])
BULK = ActionSpec(id="thing.bulk", module="t", summary="Create things", risk="R3", roles=ADMIN_ROLES, max_items=3,
                  triggers=("make many things",), endpoint=("POST", "/admin/thing"), preview=_preview_bulk,
                  verify=_verify_written,
                  call=lambda p: [Step(f"Create {n}", Call("POST", "/admin/thing", json={"name": n}), _verify_written)
                                  for n in ("x", "y", "z")])
SECRET = ActionSpec(id="thing.secret", module="t", summary="Secret", risk="R2", triggers=("secret thing",),
                    permission="LEADS_MANAGE", perm_source="dependency", endpoint=("POST", "/admin/thing"),
                    preview=_preview, verify=_verify_written, call=CREATE.call)
OFF = ActionSpec(id="thing.off", module="t", summary="Off", risk="R0", triggers=("disabled thing",),
                 feature=lambda c: False, endpoint=("GET", "/admin/thing/{name}"), call=READ.call)


@pytest.fixture
def actions(as_user):  # noqa: F811
    for s in (READ, CREATE, BULK, SECRET, OFF):
        validate(s)
    with patch.dict(REGISTRY, {s.id: s for s in (READ, CREATE, BULK, SECRET, OFF)}), \
         patch.object(assistant_router, "DISPATCH_APP", fake):
        yield as_user


client = TestClient(app, cookies={"kriya_admin_session": "sess-123"})


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "dashboard"}}).json()


def _confirm(run_id, plan_hash, **extra):
    return client.post(f"/admin/assistant/runs/{run_id}/confirm", json={"plan_hash": plan_hash, **extra})


def test_r0_executes_at_once(db, actions):  # noqa: F811
    out = _ask("thing status")
    assert (out["state"], out["action_id"], out["message"]) == ("COMPLETED_VERIFIED", "thing.read", "exists=False")
    assert out["steps"][0]["status"] == "done"


def test_r2_previews_and_writes_nothing_until_confirmed(db, actions):  # noqa: F811
    out = _ask("make thing")
    assert out["state"] == "NEEDS_CONFIRMATION" and out["plan_hash"] and out["expires_at"]
    assert out["cards"][0]["type"] == "preview" and out["cards"][0]["title"] == "Create thing a"
    assert STATE["writes"] == 0
    done = _confirm(out["run_id"], out["plan_hash"]).json()
    assert done["state"] == "COMPLETED_VERIFIED" and STATE["writes"] == 1
    assert done["steps"][0]["evidence"] == "read back from the thing list"
    assert done["cards"][0]["type"] == "result-ok"


def test_second_confirm_never_executes_again(db, actions):  # noqa: F811
    out = _ask("make thing")
    _confirm(out["run_id"], out["plan_hash"])
    again = _confirm(out["run_id"], out["plan_hash"])
    assert again.status_code == 409 and STATE["writes"] == 1


def test_wrong_hash_is_refused(db, actions):  # noqa: F811
    out = _ask("make thing")
    r = _confirm(out["run_id"], "0" * 64)
    assert r.status_code == 409 and r.json()["detail"]["error_code"] == "stale_plan"
    assert STATE["writes"] == 0


def test_changed_data_re_previews_instead_of_executing(db, actions):  # noqa: F811
    out = _ask("make thing")
    STATE["written"].add("a")  # someone else created it between preview and confirm
    again = _confirm(out["run_id"], out["plan_hash"]).json()
    assert (again["state"], again["error_code"]) == ("NEEDS_CONFIRMATION", "plan_changed")
    assert again["plan_hash"] != out["plan_hash"] and STATE["writes"] == 0


def test_expired_preview_never_executes(db, actions):  # noqa: F811
    out = _ask("make thing")
    db.tables["assistant_runs"][0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert _confirm(out["run_id"], out["plan_hash"]).status_code == 409
    assert db.tables["assistant_runs"][0]["state"] == "EXPIRED" and STATE["writes"] == 0


@pytest.mark.parametrize("typed, ok", [(None, False), (2, False), (3, True)])
def test_r3_needs_the_typed_count(db, actions, typed, ok):  # noqa: F811
    actions["user"] = _user(role="clinic_admin", staff_role=None)
    out = _ask("make many things")
    assert out["cards"][0]["type"] == "preview-typed" and out["cards"][0]["typed_count"] == 3
    extra = {} if typed is None else {"typed_count": typed}
    r = _confirm(out["run_id"], out["plan_hash"], **extra)
    if ok:
        assert r.json()["state"] == "COMPLETED_VERIFIED" and STATE["writes"] == 3
    else:
        assert r.status_code == 422 and r.json()["detail"]["error_code"] == "typed_count_mismatch"
        assert STATE["writes"] == 0


def test_r3_is_refused_for_staff(db, actions):  # noqa: F811
    out = _ask("make many things")
    assert (out["state"], out["error_code"]) == ("REJECTED", "forbidden")


def test_forbidden_action_is_refused_by_name(db, actions):  # noqa: F811
    out = _ask("secret thing")
    assert (out["state"], out["error_code"]) == ("REJECTED", "forbidden")
    assert "Leads Manage" in out["message"] and STATE["writes"] == 0


def test_feature_off_is_refused(db, actions):  # noqa: F811
    assert _ask("disabled thing")["error_code"] == "feature_disabled"


def test_permission_lost_before_confirm_is_refused(db, actions):  # noqa: F811
    actions["user"] = _user(role="clinic_admin", staff_role=None)
    out = _ask("secret thing")
    assert out["state"] == "NEEDS_CONFIRMATION"
    actions["user"] = _user(role="staff")
    r = _confirm(out["run_id"], out["plan_hash"]).json()
    assert (r["state"], r["error_code"]) == ("REJECTED", "forbidden") and STATE["writes"] == 0


def test_cancelled_preview_cannot_be_confirmed(db, actions):  # noqa: F811
    out = _ask("make thing")
    client.post(f"/admin/assistant/runs/{out['run_id']}/cancel")
    assert _confirm(out["run_id"], out["plan_hash"]).status_code == 409 and STATE["writes"] == 0


async def _preview_dup(read, params):
    return {"title": "Resend", "affected": 1, "snapshot": None, "requires_ack": True}


@pytest.mark.parametrize("ack, ok", [(False, False), (True, True)])
def test_duplicate_risk_needs_an_explicit_ack(db, actions, ack, ok):  # noqa: F811
    dup = ActionSpec(id="thing.resend", module="t", summary="Resend", risk="R2", triggers=("resend thing",),
                     endpoint=("POST", "/admin/thing"), preview=_preview_dup, verify=_verify_written,
                     call=CREATE.call)
    validate(dup)
    with patch.dict(REGISTRY, {dup.id: dup}):
        out = _ask("resend thing")
        assert out["cards"][0]["requires_ack"] is True
        r = _confirm(out["run_id"], out["plan_hash"], ack_duplicate=ack)
    if ok:
        assert r.json()["state"] == "COMPLETED_VERIFIED" and STATE["writes"] == 1
    else:
        assert r.status_code == 422 and r.json()["detail"]["error_code"] == "ack_required" and STATE["writes"] == 0


def test_llm_enum_holds_only_permitted_actions(db, actions):  # noqa: F811
    spy = AsyncMock(return_value=assistant_router.llm.LlmOutcome("disabled"))
    with patch.object(assistant_router.settings, "assistant_llm_enabled", True), \
         patch.object(assistant_router.llm, "classify", spy):
        _ask("something the rules will not match at all")
    ids = spy.await_args.kwargs["action_ids"]
    assert "thing.create" in ids and "thing.secret" not in ids and "thing.bulk" not in ids


# ── clarify replies (KK-FR-012: form completion, no LLM; guard NEEDS_CLARIFICATION -> RECEIVED) ──

from app.assistant.registry import SlotSpec  # noqa: E402

DATED = ActionSpec(id="thing.dated", module="t", summary="Create a dated thing", risk="R2",
                   triggers=("make dated thing",), endpoint=("POST", "/admin/thing"), preview=_preview,
                   verify=_verify_written, slots=(SlotSpec("date", "date"),), call=CREATE.call)
TIE_A = ActionSpec(id="board.alpha", module="t", summary="Alpha board", risk="R0", triggers=("board",),
                   endpoint=("GET", "/admin/thing/{name}"), call=READ.call, present=READ.present)
TIE_B = ActionSpec(id="board.beta", module="t", summary="Beta board", risk="R0", triggers=("board",),
                   endpoint=("GET", "/admin/thing/{name}"), call=READ.call, present=READ.present)


@pytest.fixture
def more(actions):  # noqa: F811
    with patch.dict(REGISTRY, {s.id: s for s in (DATED, TIE_A, TIE_B)}):
        yield actions


def _reply(run_id, answers, text="continue"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "dashboard"}, "reply_to_run_id": run_id, "slot_answers": answers})


def test_missing_slot_form_is_answered_and_the_same_run_continues(db, more):  # noqa: F811
    out = _ask("make dated thing")
    assert out["state"] == "NEEDS_CLARIFICATION" and out["route_layer"] == "form"
    assert out["cards"][0]["fields"] == [{"name": "date", "kind": "date"}]
    r = _reply(out["run_id"], {"date": "2026-10-20"}).json()
    assert (r["run_id"], r["state"], r["route_layer"]) == (out["run_id"], "NEEDS_CONFIRMATION", "form")
    assert db.tables["assistant_runs"][0]["params"] == {"date": "2026-10-20"}
    assert len(db.tables["assistant_runs"]) == 1 and STATE["writes"] == 0


@pytest.mark.parametrize("answers", [{"date": "tomorrow-ish"}, {"date": None}, {}, {"date": "2026-02-30"}])
def test_bad_answers_are_refused_and_the_form_stays(db, more, answers):  # noqa: F811
    out = _ask("make dated thing")
    r = _reply(out["run_id"], answers)
    assert r.status_code == 422 and r.json()["detail"]["error_code"] == "invalid_answer"
    assert db.tables["assistant_runs"][0]["state"] == "NEEDS_CLARIFICATION"


def test_a_retried_reply_is_harmless(db, more):  # noqa: F811
    out = _ask("make dated thing")
    first = _reply(out["run_id"], {"date": "2026-10-20"}).json()
    again = _reply(out["run_id"], {"date": "2026-10-21"}).json()
    assert again["state"] == first["state"] == "NEEDS_CONFIRMATION"
    assert db.tables["assistant_runs"][0]["params"] == {"date": "2026-10-20"}


def test_an_expired_form_cannot_continue(db, more):  # noqa: F811
    out = _ask("make dated thing")
    db.tables["assistant_runs"][0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    r = _reply(out["run_id"], {"date": "2026-10-20"}).json()
    assert r["state"] == "EXPIRED" and STATE["writes"] == 0


def test_a_tie_is_resolved_by_choosing_one_action(db, more):  # noqa: F811
    out = _ask("board")
    card = out["cards"][0]
    assert out["state"] == "NEEDS_CLARIFICATION"
    assert {a["id"]: a["summary"] for a in card["actions"]} == {"board.alpha": "Alpha board", "board.beta": "Beta board"}
    assert _reply(out["run_id"], {"action": "staff.delete"}).status_code == 422
    r = _reply(out["run_id"], {"action": "board.beta"}).json()
    assert (r["state"], r["action_id"]) == ("COMPLETED_VERIFIED", "board.beta")


def test_answers_to_params_maps_doctor_candidates_by_id():
    spec = ActionSpec(id="x.leave", module="t", summary="Leave", risk="R2", call=_noop, verify=_noop,
                      endpoint=("POST", "/admin/leaves"), slots=(SlotSpec("doctor", "doctor"), SlotSpec("date", "date")))
    card = {"missing": ["doctor"], "candidates": [{"id": "d1", "name": "Dr. Srinivas Rao"},
                                                  {"id": "d3", "name": "Dr. Srinivas Kumar"}]}
    got = assistant_router._answers_to_params(spec, card, {"doctor": "d3"}, {"date": "2026-10-20"})
    assert got == {"date": "2026-10-20", "doctor": "Dr. Srinivas Kumar"}
    for bad in ({"doctor": "d9"}, {"doctor": "Dr. Anyone"}, {}):
        with pytest.raises(ValueError):
            assistant_router._answers_to_params(spec, card, bad, {"date": "2026-10-20"})
