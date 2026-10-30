"""A full large hospital must not put hundreds of UUIDs into one request URL:
the API gateway rejects request lines past ~8 KB (about 200 UUIDs)."""

import uuid

import pytest

from app.services import ipd


class _Q:
    def __init__(self, sizes):
        self.sizes = sizes

    def in_(self, column, values):
        self.sizes.append(len(values))
        self.values = values
        return self


@pytest.mark.asyncio
async def test_people_for_250_admissions_are_fetched_in_batches(monkeypatch):
    sizes: list[int] = []
    admissions = [{"id": str(uuid.uuid4()), "patient_id": str(uuid.uuid4())} for _ in range(250)]

    async def fake_rows(q):
        return [{"id": v, "name": "P"} for v in q.values]

    monkeypatch.setattr(ipd, "scoped_query", lambda *a, **k: _Q(sizes))
    monkeypatch.setattr(ipd, "_rows", fake_rows)
    people = await ipd._people("c1", admissions)
    assert len(people) == 250 and all(p["patient_name"] == "P" for p in people.values())
    assert sizes and max(sizes) <= 100
