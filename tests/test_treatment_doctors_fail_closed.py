"""Treatment-to-doctor mapping fails closed (Kriya Ketu bug register K-01, owner decision DP-12).

get_treatment_doctor_ids() returned an empty set on a database error, and an empty
set means "no mapping: any active doctor". A failed read therefore offered every
doctor for a treatment only some of them perform. A failed read now yields None,
and _treatment_doctors() offers no doctor, so the patient is told none is
available for online booking right now instead of being sent to the wrong doctor.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app import database
from app.services import specialty_flow

TREATMENT = "11111111-1111-1111-1111-111111111111"
DOCTORS = [{"id": "d1", "name": "Dr. A", "is_active": True}, {"id": "d2", "name": "Dr. B", "is_active": True}]


@pytest.mark.asyncio
async def test_read_error_is_not_reported_as_no_mapping():
    with patch.object(database, "sb", new=AsyncMock(side_effect=RuntimeError("timeout"))):
        assert await database.get_treatment_doctor_ids("c-1", TREATMENT) is None


@pytest.mark.asyncio
async def test_no_mapping_still_means_any_doctor():
    with patch.object(specialty_flow, "get_doctors", new=AsyncMock(return_value=DOCTORS)), \
         patch.object(specialty_flow, "get_treatment_doctor_ids", new=AsyncMock(return_value=set())):
        assert [d["id"] for d in await specialty_flow._treatment_doctors("c-1", TREATMENT)] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_mapping_limits_the_doctors():
    with patch.object(specialty_flow, "get_doctors", new=AsyncMock(return_value=DOCTORS)), \
         patch.object(specialty_flow, "get_treatment_doctor_ids", new=AsyncMock(return_value={"d2"})):
        assert [d["id"] for d in await specialty_flow._treatment_doctors("c-1", TREATMENT)] == ["d2"]


@pytest.mark.asyncio
async def test_failed_mapping_read_offers_no_doctor():
    with patch.object(specialty_flow, "get_doctors", new=AsyncMock(return_value=DOCTORS)), \
         patch.object(specialty_flow, "get_treatment_doctor_ids", new=AsyncMock(return_value=None)):
        assert await specialty_flow._treatment_doctors("c-1", TREATMENT) == []
