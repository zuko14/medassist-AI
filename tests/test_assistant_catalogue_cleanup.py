"""KK-P4-09: PB-08 catalogue clean-up, auto-classify, and the hand-offs (owner decision 2026-10-15).

- `lab_tests.cleanup` (R0): GET /admin/lab-tests/cleanup-suggestions -> one card of counts per kind.
- `lab_tests.fix_prep` (R3, typed count, <= 200): POST /admin/lab-tests/cleanup-apply with ONLY the
  suggested fasting/prep fixes. Never a deletion or a price fix (CAT-05: the body matches
  LabCleanupApplyRequest; delete_duplicate_ids and price_fixes are always empty). Pinned staff only
  fix their branch's and the shared tests. Read-back: GET /admin/lab-tests.
- `lab_tests.auto_classify` (R2): the preview is the endpoint's own dry run (apply=false, writes
  nothing); confirm sends apply=true; read-back: the tests unfiled at the preview now have a heading.
  The registry spec called it an LLM feature; it is deterministic keyword rules (lab_classifier.py).
- PB-09 import, duplicate deletions, price fixes and AI details: a help card that opens the Test
  Catalogue page and names its button (no attachment path in Kriya Ketu: CAT-07).
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, rules
from app.assistant.registry import REGISTRY, read_only_post
from app.assistant.executor import Call
from app.main import app
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser
from tests.test_assistant_runs import CID, as_user, db  # noqa: F401  (fixtures)


@pytest.mark.parametrize("text, expected", [
    ("show catalogue clean up suggestions", ("action", "lab_tests.cleanup")),
    ("any duplicate tests in the catalogue", ("action", "lab_tests.cleanup")),
    ("catalogue lo problems chupinchu", ("action", "lab_tests.cleanup")),
    ("catalogue ki problems dikhao", ("action", "lab_tests.cleanup")),
    ("fix the fasting instructions", ("action", "lab_tests.fix_prep")),
    ("add missing fasting instructions", ("action", "lab_tests.fix_prep")),
    ("fasting instructions add cheyyi", ("action", "lab_tests.fix_prep")),
    ("fasting instructions jodo", ("action", "lab_tests.fix_prep")),
    ("auto classify the lab tests", ("action", "lab_tests.auto_classify")),
    ("file the unfiled tests", ("action", "lab_tests.auto_classify")),
    ("tests ko category mein daalo", ("action", "lab_tests.auto_classify")),
    ("tests ki category pettu", ("action", "lab_tests.auto_classify")),
])
def test_routes(text, expected):
    r = rules.route(text)
    assert (r.kind, r.action_id) == expected, r


@pytest.mark.parametrize("text, button", [
    ("import this price list", "Import Price List"),
    ("update prices from the new price list", "Import Price List"),
    ("how do I import a price list", "Import Price List"),
    ("delete the duplicate tests", "Clean-Up Suggestions"),
    ("remove duplicate lab tests", "Clean-Up Suggestions"),
    ("write a description for the CBC test", "Add Lab Test"),
])
def test_hand_offs_are_help_cards_that_open_the_test_catalogue(text, button):
    r = rules.route(text)
    assert (r.kind, r.page, r.help.button) == ("help", "labtests", button), r


def test_no_write_action_takes_a_file(db, cat):  # noqa: F811
    # CAT-07: Kriya Ketu has no attachment path at all; the request model refuses unknown fields.
    out = client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "import this price list", "input_mode": "text",
        "attachment": {"name": "prices.pdf"}})
    assert out.status_code == 422


def test_only_the_dry_run_of_auto_classify_is_a_read():
    assert read_only_post(Call("POST", "/admin/lab-tests/auto-classify", {}, {"apply": False}))
    assert not read_only_post(Call("POST", "/admin/lab-tests/auto-classify", {}, {"apply": True}))
    assert not read_only_post(Call("POST", "/admin/lab-tests/auto-classify", {}, {}))
    assert not read_only_post(Call("POST", "/admin/lab-tests/cleanup-apply", {}, {"prep_fixes": []}))


# ── through the API ──────────────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}
ID = [str(uuid.UUID(int=i)) for i in range(1, 400)]


def _t(i, name, branch=None, category=None, fasting=False, prep=None):
    return {"id": ID[i], "name": name, "branch_id": branch, "category": category, "fasting_required": fasting,
            "prep_instructions": prep, "price_paise": 30000, "is_active": True}


def _reset(tests=(), branch=None, missing=(), classify=None, apply_ok=True):
    S.clear()
    S.update(tests=[dict(t) for t in tests], branch=branch, writes=[], apply_ok=apply_ok,
             missing=list(missing), classify=classify or {})


@fake.get("/admin/me")
async def _me():
    return {"branch_id": S["branch"]}


@fake.get("/admin/lab-tests")
async def _list():
    return S["tests"]


@fake.get("/admin/lab-tests/cleanup-suggestions")
async def _suggestions():
    return {"summary": {"total_tests_analyzed": len(S["tests"]), "duplicate_groups_count": 2,
                        "suspicious_prices_count": 1, "missing_prep_count": len(S["missing"]),
                        "uncategorized_count": 4, "total_suggestions_count": 3 + len(S["missing"])},
            "duplicates": [{"name": "CBC"}], "suspicious_prices": [{"id": ID[300], "name": "ESR"}],
            "missing_prep": [{"id": S["tests"][i]["id"], "name": S["tests"][i]["name"],
                              "suggested_fasting_required": True, "suggested_prep_instructions": "Fast 10-12 hours."}
                             for i in S["missing"]], "uncategorized": []}


@fake.post("/admin/lab-tests/cleanup-apply")
async def _apply(request: Request):
    body = await request.json()
    S["writes"].append(("cleanup", body))
    if S["apply_ok"]:
        for fix in body["prep_fixes"]:
            row = next(t for t in S["tests"] if t["id"] == fix["test_id"])
            row.update(fasting_required=fix["fasting_required"], prep_instructions=fix["preparation"])
    return {"success": True}


@fake.post("/admin/lab-tests/auto-classify")
async def _classify(request: Request):
    body = await request.json()
    unfiled = [t for t in S["tests"] if t["category"] is None and t["name"] in S["classify"]]
    if body.get("apply"):
        S["writes"].append(("classify", body))
        for t in unfiled:
            t["category"] = S["classify"][t["name"]]
    groups = {}
    for t in unfiled:
        groups.setdefault(S["classify"][t["name"]], []).append(t["name"])
    return {"total": len(S["tests"]), "unfiled": len(unfiled), "applied": bool(body.get("apply")),
            "proposal": [{"category": c, "count": len(n), "examples": n[:6]} for c, n in groups.items()],
            "updated": len(unfiled) if body.get("apply") else 0}


client = TestClient(app)


def _lab_user(perms=("LAB_TESTS_MANAGE",), branch=None):
    return AdminUser(username="lab1", role="staff", clinic_id=CID, user_id=str(uuid.uuid4()), staff_role="LAB",
                     permissions=list(perms), branch_id=branch)


@pytest.fixture
def cat(as_user):  # noqa: F811
    as_user["user"] = _lab_user()
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake):
        yield as_user


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "labtests"}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def test_cleanup_suggestions_card_counts_each_kind(db, cat):  # noqa: F811
    _reset([_t(0, "Glucose Fasting")], missing=[0])
    out = _ask("show catalogue clean up suggestions")
    assert out["state"] == "COMPLETED_VERIFIED", out
    text = str(out["cards"])
    assert "Duplicate groups" in text and "Missing fasting" in text and S["writes"] == []


def test_cat05_prep_fixes_only_typed_count_and_read_back(db, cat):  # noqa: F811
    _reset([_t(0, "Glucose Fasting"), _t(1, "Lipid Profile"), _t(2, "CBC")], missing=[0, 1])
    out = _ask("fix the fasting instructions")
    card = out["cards"][0]
    assert (out["state"], card["type"], card["typed_count"]) == ("NEEDS_CONFIRMATION", "preview-typed", 2), out
    assert "Glucose Fasting" in str(card) and S["writes"] == []
    assert _confirm(out, typed_count=1).status_code == 422 and S["writes"] == []
    done = _confirm(out, typed_count=2).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    (kind, body), = S["writes"]
    assert kind == "cleanup" and body["delete_duplicate_ids"] == [] and body["price_fixes"] == []
    assert body["prep_fixes"] == [
        {"test_id": ID[0], "fasting_required": True, "preparation": "Fast 10-12 hours."},
        {"test_id": ID[1], "fasting_required": True, "preparation": "Fast 10-12 hours."}]


def test_a_fix_that_did_not_land_is_not_verified(db, cat):  # noqa: F811
    _reset([_t(0, "Glucose Fasting")], missing=[0], apply_ok=False)
    out = _ask("fix the fasting instructions")
    assert _confirm(out, typed_count=1).json()["state"] != "COMPLETED_VERIFIED"


def test_no_prep_suggestions_is_nothing_to_do(db, cat):  # noqa: F811
    _reset([_t(0, "CBC")])
    assert _ask("fix the fasting instructions")["error_code"] == "nothing_to_do" and S["writes"] == []


def test_pinned_staff_fix_only_their_branch_and_shared_tests(db, cat):  # noqa: F811
    cat["user"] = _lab_user(branch="b1")
    _reset([_t(0, "Glucose Fasting", branch="b2"), _t(1, "Lipid Profile")], branch="b1", missing=[0, 1])
    out = _ask("fix the fasting instructions")
    assert out["cards"][0]["typed_count"] == 1 and "Glucose Fasting" not in str(out["cards"])


def test_cap_200(db, cat):  # noqa: F811
    _reset([_t(i, f"Test {i}") for i in range(201)], missing=range(201))
    assert _ask("fix the fasting instructions")["error_code"] == "too_many" and S["writes"] == []


def test_auto_classify_previews_with_the_dry_run_and_verifies(db, cat):  # noqa: F811
    _reset([_t(0, "MRI Brain"), _t(1, "CBC"), _t(2, "Lipid Profile", category="Blood")],
           classify={"MRI Brain": "Scans (CT / MRI)", "CBC": "Lab Tests (Pathology)"})
    out = _ask("auto classify the lab tests")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "Scans (CT / MRI)" in str(out["cards"]) and S["writes"] == []  # the dry run wrote nothing
    done = _confirm(out).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    assert S["writes"] == [("classify", {"apply": True})]


def test_auto_classify_with_nothing_unfiled_is_nothing_to_do(db, cat):  # noqa: F811
    _reset([_t(0, "CBC", category="Blood")])
    assert _ask("auto classify the lab tests")["error_code"] == "nothing_to_do"


def test_auto_classify_warns_pinned_staff_it_is_clinic_wide(db, cat):  # noqa: F811
    cat["user"] = _lab_user(branch="b1")
    _reset([_t(0, "CBC")], branch="b1", classify={"CBC": "Lab Tests (Pathology)"})
    out = _ask("auto classify the lab tests")
    assert "every branch" in str(out["cards"])


def test_no_permission_is_refused(db, cat):  # noqa: F811
    cat["user"] = _lab_user(perms=())
    assert _ask("fix the fasting instructions")["state"] == "REJECTED"


def test_registered_with_the_route_permission():
    for aid, risk, method in (("lab_tests.cleanup", "R0", "GET"), ("lab_tests.fix_prep", "R3", "POST"),
                              ("lab_tests.auto_classify", "R2", "POST")):
        spec = REGISTRY[aid]
        assert (spec.risk, spec.endpoint[0], spec.permission, spec.perm_source) == (
            risk, method, "LAB_TESTS_MANAGE", "dependency"), aid
    assert REGISTRY["lab_tests.fix_prep"].max_items == 200
    assert playbooks.MAX_PREP_FIXES == 200
