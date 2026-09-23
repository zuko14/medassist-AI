"""Session 21: "Weekly operational summary generated successfully" over an
empty card.

Two bugs, both hidden by tests that mocked the database:

1. The placeholder row claimed for the daily limit had source="generating",
   which migration 085's CHECK (source IN ('ai','template')) rejects. The
   insert failed, the error was swallowed, the final UPDATE matched no row --
   and the endpoint still answered "ready". Nothing was ever saved.
2. The panel rendered only status "available"; the API has always sent "ready".

The real-PostgreSQL tests insert the rows the code actually builds, so the
schema is the judge.
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import weekly_summary as ws
from tests.conftest_db import get_default_clinic_id

NOW = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)
FACTS = {"bookings": {"last_week": 3}, "top_services": []}


class _Q:
    def __init__(self, data=None, exc=None):
        self.data, self.exc = data, exc

    def __getattr__(self, _name):
        return lambda *a, **k: self


class _Table:
    """Records what generate_weekly_summary writes. UPDATE matches nothing
    (the pre-fix production outcome), SELECT finds nothing."""

    def __init__(self, log, insert_fails):
        self.log, self.insert_fails = log, insert_fails

    def select(self, *_):
        return _Q([])

    def update(self, payload):
        self.log.append(("update", payload))
        return _Q([])

    def insert(self, payload):
        self.log.append(("insert", payload))
        return _Q(exc=RuntimeError("insert failed")) if self.insert_fails else _Q([payload])


async def _run(insert_fails=False):
    log = []

    async def fake_sb(q):
        if q.exc:
            raise q.exc
        return MagicMock(data=q.data)

    fake = MagicMock()
    fake.table.side_effect = lambda _n: _Table(log, insert_fails)
    with patch.object(ws, "supabase", fake), patch.object(ws, "sb", side_effect=fake_sb), \
         patch.object(ws, "build_weekly_fact_sheet", AsyncMock(return_value=FACTS)), \
         patch.object(ws, "call_ai_gateway", AsyncMock(side_effect=RuntimeError("no ai"))):
        result = await ws.generate_weekly_summary("11111111-1111-1111-1111-111111111111", now=NOW)
    return result, log


@pytest.mark.asyncio
async def test_placeholder_uses_a_source_the_schema_accepts():
    _, log = await _run()
    placeholder = log[0][1]
    assert log[0][0] == "insert"
    assert placeholder["source"] in ("ai", "template")
    assert placeholder["summary_text"] == ""


@pytest.mark.asyncio
async def test_summary_is_inserted_when_the_update_matched_no_row():
    result, log = await _run()
    final = log[-1]
    assert final[0] == "insert" and final[1]["summary_text"] == result["summary_text"]
    assert final[1]["summary_text"].startswith("### What happened")
    assert result["status"] == "ready"


@pytest.mark.asyncio
async def test_unsaved_summary_is_an_error_not_a_success():
    with pytest.raises(HTTPException) as exc:
        await _run(insert_fails=True)
    assert exc.value.status_code == 503


def test_get_treats_an_unfinished_placeholder_as_not_generated():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import admin as admin_module
    from app.routers.admin import router, verify_credentials
    from tests.test_weekly_insights_summary import _make_admin_user

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_credentials] = lambda: _make_admin_user(clinic_id="clinic-1")
    row = {"summary_text": "", "fact_sheet": {}, "source": "template"}
    with patch.object(admin_module, "sb", AsyncMock(return_value=MagicMock(data=[row]))):
        resp = TestClient(app).get("/admin/insights/summary?clinic_id=clinic-1")
    assert resp.json()["status"] == "not_generated"


def test_panel_renders_the_status_the_api_sends():
    path = os.path.join(os.path.dirname(__file__), "..", "admin", "index.html")
    html = open(path, encoding="utf-8").read()
    fn = html[html.find("async function loadWeeklySummary"):html.find("async function generateWeeklySummary")]
    assert "data.status === 'ready'" in fn
    assert "'available'" not in fn


# ── Real PostgreSQL ──────────────────────────────────────────────────────────


def _insert(cur, row):
    cols = list(row)
    cur.execute(
        f"INSERT INTO weekly_insights_summaries ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [json.dumps(row[c]) if isinstance(row[c], dict) else row[c] for c in cols],
    )


@pytest.mark.asyncio
async def test_rows_the_code_writes_are_accepted_by_the_real_schema(real_pg_conn):
    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)
    _, log = await _run()
    placeholder, final = log[0][1], log[-1][1]
    cur.execute("DELETE FROM weekly_insights_summaries WHERE clinic_id = %s", (clinic_id,))
    try:
        _insert(cur, {**placeholder, "clinic_id": clinic_id})  # raised before the fix
        cur.execute("DELETE FROM weekly_insights_summaries WHERE clinic_id = %s", (clinic_id,))
        _insert(cur, {**final, "clinic_id": clinic_id})
        cur.execute(
            "SELECT source, length(summary_text) > 0 FROM weekly_insights_summaries WHERE clinic_id = %s",
            (clinic_id,),
        )
        assert cur.fetchone() == ("template", True)
    finally:
        cur.execute("DELETE FROM weekly_insights_summaries WHERE clinic_id = %s", (clinic_id,))


def test_the_pre_fix_placeholder_is_what_the_schema_rejected(real_pg_conn):
    import psycopg2

    cur = real_pg_conn.cursor()
    clinic_id = get_default_clinic_id(cur)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(cur, {"clinic_id": clinic_id, "iso_year": 2026, "iso_week": 38,
                      "summary_text": "", "source": "generating"})
