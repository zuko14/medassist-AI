"""KK-P4-08: PB-07 lab price (single R2 / bulk R3), lab test create / deactivate, collection window.

All LAB_TESTS_MANAGE route dependencies (registry §4.6). Every write is a PUT/POST of the Test
Catalogue page's own endpoint, read back from GET /admin/lab-tests (or GET /admin/lab-collection-window).

- CAT-01: a new price <= 0 (or above the INTEGER-paise ceiling, D-31) is refused before any write.
- CAT-02: percent + rounding (Rs 600 +10 % -> Rs 660; round to Rs 1/5/10, stated in the preview).
- CAT-03: bulk = typed count; per-row read-back; a failure part-way -> PARTIALLY_COMPLETED.
- CAT-04: cap 200 tests in one bulk change.
- A test is named by words of its name; anything but exactly one editable match is refused with the
  matches listed (never a guess). Branch-pinned staff only touch their branch's and shared tests (D-31).
- A new test's name is taken only from quotes (stored exactly as typed), else the form asks.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.routers.admin import AdminUser
from app.voice.dates import IST
from tests.test_assistant_runs import CID, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=IST)


# ── resolvers ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("change CBC price to ₹350", ["amount", "350"]),
    ("set price of vitamin d to 1200", ["amount", "1200"]),
    ("CBC rate 400 rupees", ["amount", "400"]),
    ("increase CBC price by 10%", ["percent", "10"]),
    ("reduce lipid profile price by 5 percent", ["percent", "-5"]),
    ("increase CBC price by ₹50", ["delta", "50"]),
    ("CBC price tagginchu 100 rupees", ["delta", "-100"]),
    ("change CBC price", None),
    ("change CBC price by 10%", None),           # a percent needs a direction
    ("set CBC price to 0", None),
])
def test_resolve_price(text, expected):
    assert resolve.resolve_price(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("increase all prices by 10%", "10"), ("reduce all lab prices by 5%", "-5"),
    ("all test rates 8 percent penchu", "8"), ("sab tests ke daam 10% badhao", "10"),
    ("change all prices by 10%", None), ("increase all prices by ₹50", None),
])
def test_resolve_percent_change(text, expected):
    assert resolve.resolve_percent_change(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("increase all prices by 10% round to 10", 10), ("... rounded to the nearest 5", 5), ("by 10%", 1),
])
def test_resolve_round(text, expected):
    assert resolve.resolve_round(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("change CBC price to ₹350", "cbc"), ("set price of Vitamin D test to 1200 rupees", "vitamin d"),
    ("deactivate the lipid profile test", "lipid profile"), ("increase thyroid profile price by 10%", "thyroid profile"),
    ("change price to 350", None),
])
def test_resolve_lab_test_term(text, expected):
    assert resolve.resolve_lab_test(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("increase all prices by 10%", "all"), ("increase all imaging prices by 10%", "imaging"),
    ("reduce all radiology test prices by 5%", "radiology"),
])
def test_resolve_lab_scope(text, expected):
    assert resolve.resolve_lab_scope(text) == expected


@pytest.mark.parametrize("text, expected", [
    ('add lab test "Vitamin D3" at ₹1200', "Vitamin D3"), ("add lab test Vitamin D at ₹1200", None),
])
def test_resolve_new_test_name_only_from_quotes(text, expected):
    assert resolve.resolve_quoted(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("set collection hours 7 to 11", "07:00-11:00"), ("collection window 6:30 am to 10:30 am", "06:30-10:30"),
    ("sample collection 9 am to 1 pm", "09:00-13:00"), ("collection time 7 to 1", "07:00-13:00"),
    ("collection hours 07:00-11:30", "07:00-11:30"), ("collection hours 11 to 2 pm", "11:00-14:00"),
    ("collection hours 7", None), ("collection hours 25 to 26", None), ("collection hours 11 to 9 am", None),
])
def test_resolve_time_window(text, expected):
    assert resolve.resolve_time_window(text) == expected


# ── routing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [
    ("change CBC price to ₹350", "lab_test.price"),
    ("increase thyroid profile price by 10%", "lab_test.price"),
    ("CBC rate 400 rupees ki marchu", "lab_test.price"),
    ("CBC ka rate 400 rupees badlo", "lab_test.price"),
    ("increase all prices by 10%", "lab_test.price_bulk"),
    ("reduce all lab test prices by 5% round to 10", "lab_test.price_bulk"),
    ("anni tests prices 10% penchu", "lab_test.price_bulk"),
    ("sab tests ke daam 10% badhao", "lab_test.price_bulk"),
    ('add lab test "Vitamin D3" at ₹1200', "lab_test.create"),
    ('new test "HbA1c" 450 rupees', "lab_test.create"),
    ("deactivate the lipid profile test", "lab_test.deactivate"),
    ("stop offering lipid profile test", "lab_test.deactivate"),
    ("lipid profile test band karo", "lab_test.deactivate"),
    ("set collection hours 7 to 11", "lab_collection_window.update"),
    ("change sample collection time to 6:30 am to 10:30 am", "lab_collection_window.update"),
    ("collection timings 7 to 11 ki marchu", "lab_collection_window.update"),
])
def test_routes(text, action):
    # On the Test Catalogue page: elsewhere a price change may be a lab test or a treatment (KK-P4-10; the
    # router then decides by the clinic's features, see test_assistant_treatments.py).
    r = rules.route(text, now=NOW, ui_page="labtests")
    assert (r.kind, r.action_id) in (("action", action), ("clarify", action)), r


def test_reads_do_not_answer_price_changes():
    assert rules.route("change lab test prices", now=NOW).action_id != "lab_tests.list"


# ── price math (CAT-01, CAT-02) ──────────────────────────────────────────────

@pytest.mark.parametrize("old, change, step, new", [
    (600, ["percent", "10"], 1, 660), (333, ["percent", "10"], 10, 370), (333, ["percent", "10"], 5, 365),
    (333, ["percent", "10"], 1, 366), (500, ["percent", "-5"], 1, 475), (500, ["amount", "350"], 1, 350),
    (500, ["delta", "50"], 1, 550), (500, ["delta", "-100"], 1, 400), (2, ["percent", "-90"], 1, 0),
])
def test_new_price(old, change, step, new):
    assert playbooks.new_price(Decimal(old), change, step) == new


# ── through the API ──────────────────────────────────────────────────────────

fake = FastAPI()
S: dict = {}
B1, B2 = "b1", "b2"


def _t(name, price, branch=None, active=True, category=None):
    return {"id": f"t-{name.lower().replace(' ', '-')}-{branch or 'all'}", "name": name, "price_paise": price * 100,
            "branch_id": branch, "is_active": active, "category": category}


def _reset(tests=(), branch=None, fail_put_at=None, window=None):
    S.clear()
    S.update(tests=[dict(t) for t in tests], branch=branch, fail_put_at=fail_put_at, writes=[],
             window=window or {"start": "07:00", "end": "11:00", "days": "Mon,Tue,Wed,Thu,Fri,Sat"})


@fake.get("/admin/me")
async def _me():
    return {"branch_id": S["branch"]}


@fake.get("/admin/lab-tests")
async def _list():
    return S["tests"]


@fake.put("/admin/lab-tests/{tid}")
async def _put(tid: str, request: Request):
    body = await request.json()
    S["writes"].append(("PUT", tid, body))
    if S["fail_put_at"] == len(S["writes"]):
        from fastapi import HTTPException
        raise HTTPException(500, "boom")
    row = next(t for t in S["tests"] if t["id"] == tid)
    if "price_rupees" in body:
        row["price_paise"] = body["price_rupees"] * 100
    if "is_active" in body:
        row["is_active"] = body["is_active"]
    return row


@fake.post("/admin/lab-tests")
async def _create(request: Request):
    body = await request.json()
    S["writes"].append(("POST", body))
    row = {"id": "t-new", "name": body["name"], "price_paise": body["price_rupees"] * 100, "branch_id": None,
           "is_active": True}
    S["tests"].append(row)
    return row


@fake.get("/admin/lab-collection-window")
async def _window(branch_id: str = None):
    return {"lab_collection": S["window"], "source": "clinic", "branch_id": branch_id}


@fake.put("/admin/lab-collection-window")
async def _set_window(request: Request, branch_id: str = None):
    body = await request.json()
    S["writes"].append(("WINDOW", branch_id, body))
    S["window"] = body
    return {"success": True, "lab_collection": body}


client = TestClient(app)


def _lab_user(role="staff", perms=("LAB_TESTS_MANAGE",), branch=None):
    return AdminUser(username="lab1", role=role, clinic_id=CID, user_id=str(uuid.uuid4()), staff_role="LAB",
                     permissions=list(perms), branch_id=branch)


@pytest.fixture
def cat(as_user):  # noqa: F811
    as_user["user"] = _lab_user()
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text, page="labtests"):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": page}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def test_single_price_change_previews_old_to_new_and_verifies(db, cat):  # noqa: F811
    _reset([_t("CBC", 300), _t("Lipid Profile", 800)])
    out = _ask("change CBC price to ₹350")
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert "₹300" in str(out["cards"]) and "₹350" in str(out["cards"])
    assert S["writes"] == []
    done = _confirm(out).json()
    assert done["state"] == "COMPLETED_VERIFIED", done
    assert S["writes"] == [("PUT", "t-cbc-all", {"price_rupees": 350})]


def test_cat01_a_price_at_or_below_zero_is_refused_before_any_write(db, cat):  # noqa: F811
    _reset([_t("CBC", 2)])
    out = _ask("reduce CBC price by 90%")
    assert out["error_code"] == "nothing_to_do" and "₹0" in out["message"] and S["writes"] == []


def test_cat01_a_price_above_the_ceiling_is_refused(db, cat):  # noqa: F811
    _reset([_t("CBC", 21_000_000)])
    out = _ask("increase CBC price by 10%")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_the_same_price_is_nothing_to_do(db, cat):  # noqa: F811
    _reset([_t("CBC", 350)])
    assert _ask("change CBC price to ₹350")["error_code"] == "nothing_to_do"


def test_several_matches_are_listed_never_guessed(db, cat):  # noqa: F811
    _reset([_t("Thyroid Profile T3", 500), _t("Thyroid Profile Free", 700)])
    out = _ask("change thyroid profile price to ₹600")
    assert out["error_code"] == "nothing_to_do" and "Thyroid Profile Free" in out["message"] and S["writes"] == []


def test_an_exact_name_wins_over_longer_names(db, cat):  # noqa: F811
    _reset([_t("CBC", 300), _t("CBC with ESR", 400)])
    out = _ask("change CBC price to ₹350")
    assert out["state"] == "NEEDS_CONFIRMATION" and "CBC with ESR" not in str(out["cards"])


def test_an_unknown_test_suggests_close_names(db, cat):  # noqa: F811
    _reset([_t("Lipid Profile", 800)])
    out = _ask("change lipd profile price to ₹900")
    assert out["error_code"] == "nothing_to_do" and "Lipid Profile" in out["message"]


def test_pinned_staff_only_see_their_branch_and_shared_tests(db, cat):  # noqa: F811
    cat["user"] = _lab_user(branch=B1)
    _reset([_t("CBC", 300, branch=B2), _t("ESR", 200)], branch=B1)
    out = _ask("change CBC price to ₹350")
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []
    out = _ask("increase all prices by 10%")
    assert out["cards"][0]["typed_count"] == 1 and "t-cbc-b2" not in str(out)


def test_no_permission_is_refused_before_any_read(db, cat):  # noqa: F811
    cat["user"] = _lab_user(perms=())
    out = _ask("change CBC price to ₹350")
    assert out["state"] == "REJECTED" and "Lab Tests Manage" in out["message"]


def test_cat02_cat03_bulk_typed_count_rounding_and_per_row_verify(db, cat):  # noqa: F811
    _reset([_t(f"Test {i}", 333 + i) for i in range(23)])
    out = _ask("increase all prices by 10% round to 10")
    card = out["cards"][0]
    assert (out["state"], card["type"], card["typed_count"]) == ("NEEDS_CONFIRMATION", "preview-typed", 23), out
    assert "₹10" in str(card)  # the rounding rule is stated
    assert _confirm(out, typed_count=22).status_code == 422 and S["writes"] == []
    done = _confirm(out, typed_count=23).json()
    assert done["state"] == "COMPLETED_VERIFIED" and len(S["writes"]) == 23
    assert S["writes"][0] == ("PUT", "t-test-0-all", {"price_rupees": 370})


def test_cat03_a_failure_part_way_is_partially_completed(db, cat):  # noqa: F811
    _reset([_t(f"Test {i}", 100 + i) for i in range(5)], fail_put_at=3)
    out = _ask("increase all prices by 10%")
    done = _confirm(out, typed_count=5).json()
    assert done["state"] in ("PARTIALLY_COMPLETED", "UNKNOWN_OUTCOME"), done
    assert len(S["writes"]) == 3  # stops at the failure; never retried


def test_bulk_by_category(db, cat):  # noqa: F811
    _reset([_t("MRI Brain", 5000, category="Imaging"), _t("CBC", 300, category="Blood")])
    out = _ask("increase all imaging prices by 10%")
    assert out["cards"][0]["typed_count"] == 1 and "MRI Brain" in str(out["cards"])


def test_bulk_unknown_category_lists_the_categories(db, cat):  # noqa: F811
    _reset([_t("CBC", 300, category="Blood")])
    out = _ask("increase all imaging prices by 10%")
    assert out["error_code"] == "nothing_to_do" and "Blood" in out["message"]


def test_cat04_cap_200(db, cat):  # noqa: F811
    _reset([_t(f"T{i}", 100) for i in range(201)])
    assert _ask("increase all prices by 10%")["error_code"] == "too_many" and S["writes"] == []
    _reset([_t(f"T{i}", 100) for i in range(200)])
    assert _ask("increase all prices by 10%")["cards"][0]["typed_count"] == 200


def test_create_takes_the_quoted_name_exactly(db, cat):  # noqa: F811
    _reset([_t("CBC", 300)])
    out = _ask('add lab test "Vitamin D3" at ₹1200')
    assert out["state"] == "NEEDS_CONFIRMATION", out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("POST", {"name": "Vitamin D3", "price_rupees": 1200})]


def test_create_without_quotes_asks_for_the_name(db, cat):  # noqa: F811
    out = _ask("add lab test Vitamin D at ₹1200")
    assert out["state"] == "NEEDS_CLARIFICATION" and "name" in str(out["cards"]) and S["writes"] == []


def test_create_an_existing_name_is_refused(db, cat):  # noqa: F811
    _reset([_t("CBC", 300)])
    out = _ask('add lab test "cbc" at ₹300')
    assert out["error_code"] == "nothing_to_do" and S["writes"] == []


def test_deactivate(db, cat):  # noqa: F811
    _reset([_t("Lipid Profile", 800)])
    out = _ask("deactivate the lipid profile test")
    assert out["state"] == "NEEDS_CONFIRMATION"
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("PUT", "t-lipid-profile-all", {"is_active": False})]


def test_deactivate_an_inactive_test_is_nothing_to_do(db, cat):  # noqa: F811
    _reset([_t("Lipid Profile", 800, active=False)])
    assert _ask("deactivate the lipid profile test")["error_code"] == "nothing_to_do"


def test_collection_window_keeps_the_days_and_verifies(db, cat):  # noqa: F811
    _reset(window={"start": "07:00", "end": "11:00", "days": "Mon,Wed", "sunday_start": "08:00",
                   "sunday_end": "10:00"})
    out = _ask("set collection hours 6:30 am to 10:30 am")
    assert out["state"] == "NEEDS_CONFIRMATION" and "07:00" in str(out["cards"]), out
    assert _confirm(out).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [("WINDOW", None, {"start": "06:30", "end": "10:30", "days": "Mon,Wed",
                                             "sunday_start": "08:00", "sunday_end": "10:00"})]


def test_collection_window_for_pinned_staff_is_their_branch(db, cat):  # noqa: F811
    cat["user"] = _lab_user(branch=B1)
    _reset(branch=B1)
    out = _ask("set collection hours 6 to 10")
    _confirm(out)
    assert S["writes"][0][:2] == ("WINDOW", B1)


def test_collection_window_unchanged_is_nothing_to_do(db, cat):  # noqa: F811
    assert _ask("set collection hours 7 to 11")["error_code"] == "nothing_to_do"


def test_registered_with_the_route_permission():
    for aid, risk, method in (("lab_test.price", "R2", "PUT"), ("lab_test.price_bulk", "R3", "PUT"),
                              ("lab_test.create", "R2", "POST"), ("lab_test.deactivate", "R2", "PUT"),
                              ("lab_collection_window.update", "R2", "PUT")):
        spec = REGISTRY[aid]
        assert (spec.risk, spec.endpoint[0], spec.permission, spec.perm_source) == (
            risk, method, "LAB_TESTS_MANAGE", "dependency"), aid
    assert REGISTRY["lab_test.price_bulk"].max_items == 200


# ── clarify-form answers ─────────────────────────────────────────────────────

def _answer(out, **answers):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": "x", "reply_to_run_id": out["run_id"],
        "slot_answers": answers})


def test_form_name_and_bare_number_price_create_the_test_as_typed(db, cat):  # noqa: F811
    out = _ask("add a new lab test")
    assert out["state"] == "NEEDS_CLARIFICATION"
    out = _answer(out, name="  Vitamin   D3 ", price="1,200").json()
    assert out["state"] == "NEEDS_CONFIRMATION", out
    _confirm(out)
    assert S["writes"] == [("POST", {"name": "Vitamin D3", "price_rupees": 1200})]


@pytest.mark.parametrize("answer, status", [("0", 422), ("abc", 422), ("₹0", 422), ("x" * 101, 422)])
def test_form_bad_answers_are_422(db, cat, answer, status):  # noqa: F811
    out = _ask("add a new lab test")
    assert _answer(out, name=answer if len(answer) > 100 else "Vitamin D3",
                   price="450" if len(answer) > 100 else answer).status_code == status
    assert S["writes"] == []


def test_form_percent_needs_a_sign(db, cat):  # noqa: F811
    _reset([_t("CBC", 100)])
    out = _ask("change all prices")
    assert out["state"] == "NEEDS_CLARIFICATION", out
    assert _answer(out, price="10").status_code == 422
    out = _answer(out, price="-10").json()
    assert out["state"] == "NEEDS_CONFIRMATION" and "₹90" in str(out["cards"])
