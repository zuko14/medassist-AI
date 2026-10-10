"""Check-in must read the day's max token with NULLS LAST.

Postgres sorts NULLs FIRST under DESC. Every appointment not yet checked in
(always including the walk-in being checked in) has token_number NULL, so the
old query read max=0 and could only reach tokens 1-5 through its retries: the
6th check-in of the day for a doctor failed.
"""
from unittest.mock import patch

import pytest
from postgrest import SyncPostgrestClient

import app.database as db


@pytest.mark.asyncio
async def test_max_token_query_sorts_nulls_last():
    client = SyncPostgrestClient("http://pg.invalid")
    sent = []

    class Res:
        def __init__(self, data):
            self.data = data

    async def fake_sb(builder):
        params = dict(builder.request.params)
        sent.append((builder.request.http_method, params))
        if builder.request.http_method == "GET" and "order" not in params:
            return Res([{"id": "a1", "doctor_name": "Dr. Rao", "branch_id": None,
                         "appointment_date": "2026-10-09", "token_number": None,
                         "queue_status": None, "status": "confirmed", "queue_timeline": {}}])
        if builder.request.http_method == "GET":
            return Res([{"token_number": 7}])
        return Res([{"id": "a1", "token_number": 8}])

    with patch.object(db, "supabase", client), patch.object(db, "sb", fake_sb):
        row = await db.check_in_appointment("clinic-1", "a1")

    max_q = next(p for m, p in sent if m == "GET" and "order" in p)
    assert max_q["order"] == "token_number.desc.nullslast"
    assert row["token_number"] == 8
