"""KK-P4-05 (held item, released after D-27 / migration 109): holiday.create (R3, HOLIDAYS_CREATE).

- R3: typed count of the confirmed bookings that day (registry §4.5), even when it is 0.
- POST /admin/holidays only blocks NEW bookings (database.get_available_slots); nothing cancels the
  bookings already made for that day. The preview says so and names the count.
- A past date, or a date that is already a holiday, is refused before any write.
- The name is the staff member's ("for Diwali", quotes, after a colon), else "Clinic closed"; <= 100 chars.
- Read-back: GET /admin/holidays lists the date.
"""

import json
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.assistant import playbooks, resolve, rules
from app.assistant.registry import REGISTRY
from app.main import app
from app.routers import assistant as assistant_router
from app.voice.dates import IST
from tests.test_assistant_runs import _user, as_user, db  # noqa: F401  (fixtures)

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=IST)
DAY = "2026-11-02"


@pytest.mark.parametrize("text, expected", [
    ("close the clinic on 2 Nov for Diwali", "Diwali"),
    ('add a holiday on 2 nov "Diwali holiday"', "Diwali holiday"),
    ("holiday on 2 nov: Annual maintenance", "Annual maintenance"),
    ("add a holiday on 2 nov", None),
])
def test_resolve_holiday_name(text, expected):
    assert resolve.resolve_holiday_name(text) == expected


@pytest.mark.parametrize("text", ["close the clinic on 2 Nov for Diwali", "add a holiday on 2 nov",
                                  "2 nov clinic close cheyyi", "2 nov ko clinic band rahega"])
def test_routes_to_holiday_create(text):
    r = rules.route(text, now=NOW)
    assert (r.kind, r.action_id, r.params.get("date")) == ("action", "holiday.create", DAY), r


fake = FastAPI()
S: dict = {}


def _reset(holidays=(), bookings=0, branch=None, write_ok=True):
    S.clear()
    S.update(holidays=[dict(h) for h in holidays], bookings=bookings, branch=branch, write_ok=write_ok, writes=[])


@fake.get("/admin/me")
async def _me():
    return {"branch_id": S["branch"]}


@fake.get("/admin/holidays")
async def _holidays():
    return S["holidays"]


@fake.get("/admin/appointments")
async def _appts(request: Request):
    p = request.query_params
    assert p["date_from"] == p["date_to"] and p["status"] == "confirmed"
    return {"appointments": [], "total": S["bookings"] if p["date_from"] == DAY else 0}


@fake.post("/admin/holidays")
async def _add(holiday_date: str, name: str):
    S["writes"].append((holiday_date, name))
    if S["write_ok"]:
        S["holidays"].append({"holiday_date": holiday_date, "name": name})
    return {"holiday_date": holiday_date, "name": name}


client = TestClient(app)


@pytest.fixture
def hol(as_user):  # noqa: F811
    as_user["user"] = _user(role="clinic_admin")
    _reset()
    with patch.object(assistant_router, "DISPATCH_APP", fake), patch.object(playbooks, "_now", lambda: NOW), \
         patch.object(rules, "_now", lambda: NOW):
        yield as_user


def _ask(text):
    return client.post("/admin/assistant/requests", json={
        "client_request_id": str(uuid.uuid4()), "text": text, "input_mode": "text",
        "ui_context": {"page": "holidays"}}).json()


def _confirm(out, **extra):
    return client.post(f"/admin/assistant/runs/{out['run_id']}/confirm", json={"plan_hash": out["plan_hash"], **extra})


def test_holiday_needs_the_typed_booking_count_and_warns_they_stay_booked(db, hol):  # noqa: F811
    _reset(bookings=3)
    out = _ask("close the clinic on 2 Nov for Diwali")
    card = out["cards"][0]
    assert (out["state"], card["type"], card["typed_count"]) == ("NEEDS_CONFIRMATION", "preview-typed", 3), out
    assert any("not cancelled" in w for w in card["warnings"]) and "Diwali" in card["title"]
    assert _confirm(out, typed_count=2).status_code == 422 and S["writes"] == []
    done = _confirm(out, typed_count=3).json()
    assert done["state"] == "COMPLETED_VERIFIED" and S["writes"] == [(DAY, "Diwali")]


def test_no_bookings_means_typing_0(db, hol):  # noqa: F811
    out = _ask("add a holiday on 2 nov")
    assert out["cards"][0]["typed_count"] == 0
    assert _confirm(out, typed_count=0).json()["state"] == "COMPLETED_VERIFIED"
    assert S["writes"] == [(DAY, "Clinic closed")]


def test_an_existing_holiday_is_refused(db, hol):  # noqa: F811
    _reset(holidays=[{"holiday_date": DAY, "name": "Diwali"}])
    out = _ask("add a holiday on 2 nov")
    assert out["error_code"] == "nothing_to_do" and "Diwali" in out["message"] and S["writes"] == []


def test_a_past_date_is_refused_before_any_read(hol):
    # Typed text rolls a past day forward (dates.parse_date); a past ISO date can still come from the form.
    import asyncio

    async def read(call):
        raise AssertionError("no read for a past date")

    with pytest.raises(playbooks.EmptyPlan, match="passed"):
        asyncio.run(playbooks.holiday_create_preview(read, {"date": "2026-10-01"}))


def test_a_pinned_user_is_told_the_count_is_their_branch_and_the_closure_is_clinic_wide(db, hol):  # noqa: F811
    _reset(branch="b1")
    out = _ask("add a holiday on 2 nov")
    assert any("branch" in w for w in out["cards"][0]["warnings"])


def test_a_write_the_read_back_cannot_see_is_unknown(db, hol):  # noqa: F811
    _reset(write_ok=False)
    out = _ask("add a holiday on 2 nov")
    assert _confirm(out, typed_count=0).json()["state"] == "UNKNOWN_OUTCOME"


def test_staff_without_the_permission_is_refused(db, hol):  # noqa: F811
    hol["user"] = _user()
    out = _ask("add a holiday on 2 nov")
    assert out["state"] == "REJECTED" and "Holidays Create" in out["message"] and S["writes"] == []


def test_registry_shape():
    spec = REGISTRY["holiday.create"]
    assert (spec.risk, spec.permission, spec.perm_source, spec.endpoint) == (
        "R3", "HOLIDAYS_CREATE", "dependency", ("POST", "/admin/holidays"))


def test_corpus_covers_holiday_create():
    corpus = json.loads((__import__("pathlib").Path(__file__).parent / "fixtures" / "assistant_corpus.json")
                        .read_text(encoding="utf-8"))["phrases"]
    pos = [e for e in corpus if e["expect"] == "action:holiday.create"]
    assert len(pos) >= 5 and {"te-Latin", "hi-Latin"} <= {e.get("lang") for e in pos}
    assert len([e for e in corpus if e.get("near") == "holiday.create" and e["expect"] != "action:holiday.create"]) >= 2
