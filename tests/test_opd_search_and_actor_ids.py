"""Regressions from production on 2026-10-10.

1. OPD name search 500'd: it filtered patient_records.name, a column that table
   never had (it is full_name).
2. The env / super-admin login ("super_admin_env") was written into, and used to
   filter, uuid-typed audit columns: billing polled a 400 every 5 s, and opening
   a patient in Consultation or creating an invoice failed for super-admins.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services import opd_billing
from app.services.opd import search_patients
from app.utils.helpers import actor_uuid, as_uuid

CLINIC = "f13ea1b8-ec12-4d15-82a8-82668b74bd29"
STAFF_ID = "7a1c3b52-0d7e-4c8b-9a51-2f3e4d5c6b7a"


def _path(q):
    return str(getattr(getattr(q, "request", None), "path", "") or getattr(q, "path", "")) + str(
        getattr(getattr(q, "request", None), "params", "") or getattr(q, "params", ""))


@pytest.mark.asyncio
async def test_name_search_filters_patient_records_on_full_name():
    seen = []

    async def fake_sb(q):
        p = _path(q)
        seen.append(p)
        if "patient_records" in p:
            return MagicMock(data=[
                {"id": "r1", "full_name": "Umesh Kumar", "phone": "+919000000002", "age_years": 40, "gender": "M"},
                {"id": "r2", "full_name": "Umesh", "phone": "+919493386498", "age_years": 16},  # already a patient
            ])
        if "family_members" in p:
            return MagicMock(data=[])
        return MagicMock(data=[{"id": "p1", "name": "Umesh", "phone": "+919493386498", "mrn": "MRN-000001"}])

    with patch("app.services.opd.sb", side_effect=fake_sb):
        hits = await search_patients(CLINIC, "umesh")

    legacy_q = next(p for p in seen if "patient_records" in p)
    assert "full_name" in legacy_q and "name=ilike" not in legacy_q.replace("full_name=ilike", "")
    assert [h["name"] for h in hits] == ["Umesh", "Umesh Kumar"]       # the registered one is not repeated
    legacy = hits[1]
    assert legacy["match_reason"] == "legacy_record" and legacy["age_years"] == 40


def test_actor_uuid_only_passes_real_staff_ids():
    assert actor_uuid(SimpleNamespace(user_id=STAFF_ID)) == STAFF_ID
    assert actor_uuid({"user_id": STAFF_ID}) == STAFF_ID
    for env_id in ("super_admin_env", "platform_owner_env", None, ""):
        assert actor_uuid(SimpleNamespace(user_id=env_id)) is None
        assert actor_uuid({"user_id": env_id}) is None
    assert as_uuid(STAFF_ID.upper()) == STAFF_ID


@pytest.mark.asyncio
async def test_super_admin_billing_poll_never_sends_a_non_uuid_filter():
    sb = AsyncMock()
    with patch("app.services.opd_billing.sb", sb):
        shift = await opd_billing.get_current_shift(CLINIC, SimpleNamespace(user_id="super_admin_env"))
    assert shift is None and sb.await_count == 0


@pytest.mark.asyncio
async def test_super_admin_cannot_open_a_cash_drawer_and_is_told_why():
    with pytest.raises(HTTPException) as e:
        await opd_billing.open_shift(CLINIC, 0, actor=SimpleNamespace(user_id="super_admin_env", username="sa"))
    assert e.value.status_code == 403 and "staff login" in e.value.detail
