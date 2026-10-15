"""KK-P4-12: H-tier hand-off framework (KK-FR-025, SEC-18, UI-19, T-29).

H actions are things a person must click themselves: staff accounts and passwords, payment settings,
cash / refunds / voids, signing a visit, deletions, data import/export, corporate, live-call takeover.
Kriya answers with a "handoff" card: the page, the page's own button, and the form fields it will
pre-fill. Nothing is executed: no plan is built, the executor is never reached, and no request is
sent to the panel API (SEC-18). Password / secret / key fields are refused at registration and never
filled by the widget. A page the user can't see is refused, not opened (T-29).
"""

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import policy, resolve, rules
from app.assistant.registry import REGISTRY, ActionSpec, Handoff, validate
from app.main import app
from app.routers import assistant as assistant_router
from tests.test_assistant_runs import _user, as_user, db  # noqa: F401  (fixtures)

HTML = (Path(__file__).resolve().parents[1] / "admin" / "index.html").read_text(encoding="utf-8")
H_ACTIONS = sorted(a for a, s in REGISTRY.items() if s.risk == "H")


def _section(page: str) -> str:
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'<div id="pg-([a-z]+)" class="sec', HTML)]
    i = next(i for i, (_, p) in enumerate(starts) if p == page)
    return HTML[starts[i][0]:starts[i + 1][0] if i + 1 < len(starts) else len(HTML)]


# ── registration ─────────────────────────────────────────────────────────────

def test_the_hand_offs_of_the_plan_are_registered():
    assert {"staff.create", "staff.manage", "payment.settings", "opd.record_payment", "opd.refund", "opd.void",
            "visit.sign", "doctor.delete", "lab_test.delete", "treatment.delete", "branch.manage", "data.export",
            "data.import", "corporate.manage", "voice.takeover"} <= set(H_ACTIONS)


@pytest.mark.parametrize("aid", H_ACTIONS)
def test_an_h_action_is_never_executable(aid):
    spec = REGISTRY[aid]
    assert spec.handoff and not spec.call and not spec.verify and not spec.endpoint and not spec.preview


@pytest.mark.parametrize("aid", H_ACTIONS)
def test_its_button_and_fields_exist_on_its_page_and_no_field_is_a_password(aid):
    h = REGISTRY[aid].handoff
    sec = _section(h.page)
    if h.button:
        assert re.search(r">[^<]*" + re.escape(h.button), sec), f"{h.button!r} not on {h.page}"
    for element_id, _ in h.fields:
        tag = re.search(rf'<[^>]*\bid="{re.escape(element_id)}"[^>]*>', sec)
        assert tag, f"#{element_id} not on {h.page}"
        assert 'type="password"' not in tag.group(0)


@pytest.mark.parametrize("element_id, param", [("f-staffPassword", "x"), ("f-key", "api_key"), ("f-x", "secret"),
                                                ("f-y", "upi_pin"), ("f-z", "token")])
def test_registration_refuses_a_secret_field(element_id, param):
    spec = ActionSpec(id="x.h", module="x", summary="x", risk="H",
                      handoff=Handoff(page="staff", button="Create Staff Login", fields=((element_id, param),)))
    with pytest.raises(ValueError, match="secret"):
        validate(spec)


def test_sec18_policy_never_builds_a_plan_for_h():
    import asyncio

    async def read(call):
        raise AssertionError("no read")

    with pytest.raises(policy.PlanError):
        asyncio.run(policy.build_plan(REGISTRY["staff.create"], {}, read))


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("add staff Ramesh as receptionist", "staff.create"),
    ("create a login for Priya as front desk", "staff.create"),
    ("staff login create cheyyi", "staff.create"),
    ("naya staff login banao", "staff.create"),
    ("reset Ramesh's password", "staff.manage"),
    ("change the razorpay key", "payment.settings"),
    ("update payment settings", "payment.settings"),
    ("record cash payment for token 5", "opd.record_payment"),
    ("refund the invoice for Ravi", "opd.refund"),
    ("void the invoice for Ravi", "opd.void"),
    ("sign the visit", "visit.sign"),  # "prescription" is refused by the clinical screen (kept strict)
    ("delete doctor Rao", "doctor.delete"),
    ("delete the CBC test", "lab_test.delete"),
    ("delete the PRP treatment", "treatment.delete"),
    ("add a new branch", "branch.manage"),
    ("export patient data", "data.export"),
    ("import patients from excel", "data.import"),
    ("add a corporate company", "corporate.manage"),
    ("take over the live call", "voice.takeover"),
])
def test_routes(text, action):
    r = rules.route(text)
    assert (r.kind, r.action_id) == ("action", action), r


@pytest.mark.parametrize("text, name, role", [
    ("add staff Ramesh as receptionist", "Ramesh", "RECEPTIONIST"),
    ("create a login for Priya Sharma as front desk", "Priya Sharma", "FRONT_DESK"),
    ("new staff login for Anil, lab", "Anil", "LAB_OPERATOR"),
    ("add staff", None, None),
])
def test_staff_name_and_role(text, name, role):
    assert resolve.resolve_staff_name(text) == name
    assert resolve.resolve_staff_role(text) == role


# ── through the API ──────────────────────────────────────────────────────────

fake = FastAPI()
CALLS: list = []


@fake.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def _any(path: str, request: Request):
    CALLS.append((request.method, path))
    return {}


client = TestClient(app)


@pytest.fixture
def h(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    CALLS.clear()
    with patch.object(assistant_router, "DISPATCH_APP", fake):
        yield as_user


def _ask(text, visible=None):
    ctx = {"page": "dashboard", **({"visible_pages": visible} if visible is not None else {})}
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text", "ui_context": ctx}).json()


def test_staff_hand_off_card_lists_the_prefill_and_sends_nothing(db, h):  # noqa: F811
    out = _ask("add staff Ramesh as receptionist")
    card = out["cards"][0]
    assert (out["action_id"], card["type"], card["page"], card["button"]) == (
        "staff.create", "handoff", "staff", "Create Staff Login"), out
    assert card["fields"] == [{"id": "f-staffRole", "label": "Role", "value": "RECEPTIONIST"},
                              {"id": "f-staffFullName", "label": "Name", "value": "Ramesh"}]
    assert "password" in card["text"].lower()  # the person sets it
    assert CALLS == []  # SEC-18: nothing reached the panel API
    stored = json.dumps(db.tables["assistant_runs"])
    assert "f-staffPassword" not in stored


def test_a_hand_off_without_fields_points_to_the_row_buttons(db, h):  # noqa: F811
    card = _ask("delete doctor Rao")["cards"][0]
    assert (card["type"], card["page"], card["fields"], card["button"]) == ("handoff", "doctors", [], "")
    assert "row" in card["text"] and CALLS == []


def test_the_prescription_wording_stays_with_the_clinical_screen():
    assert rules.route("sign the prescription").error_code == "clinical"


def test_t29_a_page_the_user_cannot_see_is_refused(db, h):  # noqa: F811
    out = _ask("add staff Ramesh as receptionist", visible=["dashboard", "appointments"])
    assert out["state"] == "REJECTED" and out["error_code"] == "not_available" and CALLS == []


def test_the_page_feature_gate_still_applies(db, h):  # noqa: F811
    out = _ask("refund the invoice for Ravi")  # the test clinic has no live OPD
    assert out["state"] in ("COMPLETED_VERIFIED", "REJECTED")
    assert CALLS == []


# ── widget (node): the card and the fill, UI-19 ──────────────────────────────

pytestmark_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def _fn(name):
    m = re.search(rf"function {name}\(.*?\n\}}", HTML, re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _node(src):
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytestmark_node
def test_handoff_card_html_escapes_and_offers_open_and_fill():
    card = {"type": "handoff", "page": "staff", "button": "Create Staff Login", "text": "Press <b>Create</b>",
            "fields": [{"id": "f-staffFullName", "label": "Name", "value": "<img src=x>"}]}
    src = "\n".join(_fn(f) for f in ("esc", "ketuCardHtml")) + \
        f"\nconsole.log(JSON.stringify(ketuCardHtml({json.dumps(card)}, p => p)));"
    html = _node(src)
    assert "&lt;b&gt;Create" in html and "&lt;img" in html and "<img" not in html
    assert 'data-ketu-fill' in html and "Open &amp; fill" in html


FAKE_DOC = """
const events = [];
function el(id, tag, extra) {
  return Object.assign({id, tagName: tag, type: tag === 'INPUT' ? 'text' : undefined, value: '', disabled: false,
    options: [], dispatchEvent(e) { events.push(id + ':' + e.type); return true; },
    submit() { throw new Error('submitted'); }, click() { throw new Error('clicked'); }}, extra || {});
}
const els = {
  'f-name': el('f-name', 'INPUT'),
  'f-pass': el('f-pass', 'INPUT', {type: 'password'}),
  'f-role': el('f-role', 'SELECT', {options: [{value: 'RECEPTIONIST', hidden: false}, {value: 'DOCTOR', hidden: true}]}),
  'f-off': el('f-off', 'INPUT', {disabled: true}),
};
const doc = {getElementById: id => els[id] || null};
class Event { constructor(t) { this.type = t; } }
"""


@pytestmark_node
def test_fill_sets_only_listed_fields_never_a_password_never_submits():
    fields = [{"id": "f-name", "value": "Ramesh"}, {"id": "f-pass", "value": "hunter2"},
              {"id": "f-role", "value": "RECEPTIONIST"}, {"id": "f-missing", "value": "x"},
              {"id": "f-off", "value": "y"}]
    src = FAKE_DOC + _fn("ketuFillFields") + f"""
const filled = ketuFillFields(doc, {json.dumps(fields)});
console.log(JSON.stringify({{filled, name: els['f-name'].value, pass: els['f-pass'].value,
  role: els['f-role'].value, off: els['f-off'].value, events}}));"""
    out = _node(src)
    assert out["filled"] == ["f-name", "f-role"]
    assert (out["name"], out["pass"], out["role"], out["off"]) == ("Ramesh", "", "RECEPTIONIST", "")
    assert "f-name:input" in out["events"] and "f-role:change" in out["events"]


@pytestmark_node
def test_fill_skips_a_field_the_page_hides():
    src = FAKE_DOC + _fn("ketuFillFields") + """
els['f-name'].getClientRects = () => [];
els['f-role'].getClientRects = () => [{}];
const filled = ketuFillFields(doc, [{id: 'f-name', value: 'Ramesh'}, {id: 'f-role', value: 'RECEPTIONIST'}]);
console.log(JSON.stringify({filled, name: els['f-name'].value}));"""
    out = _node(src)
    assert out == {"filled": ["f-role"], "name": ""}


@pytestmark_node
def test_fill_refuses_a_hidden_option_and_secret_like_ids():
    fields = [{"id": "f-role", "value": "DOCTOR"}, {"id": "f-name", "value": "x", "_": 1}]
    src = FAKE_DOC + _fn("ketuFillFields") + f"""
els['f-secretKey'] = el('f-secretKey', 'INPUT');
const filled = ketuFillFields(doc, {json.dumps(fields)}.concat([{{id: 'f-secretKey', value: 'k'}}]));
console.log(JSON.stringify({{filled, role: els['f-role'].value, key: els['f-secretKey'].value}}));"""
    out = _node(src)
    assert out["filled"] == ["f-name"] and out["role"] == "" and out["key"] == ""
