"""Tests for Phase 1 review fixes.

Covers:
- Prep fix applied against a fake table that rejects unknown columns (verifying prep_instructions, not preparation).
- Clinic admin cannot change the budget (POST/PATCH /admin/ai/budget is removed).
- Platform owner can change clinic budget (POST /platform/clinics/{clinic_id}/ai-budget).
- Panel generateAiDetailsDraft references f-labTestId and not editLabTestId.
- Updates run before deletes in cleanup-apply; if update fails, deletes are aborted.
- Price validation between 1.0 and 100,000.0 rupees.
- UUID validation for test_ids.
"""

import re
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.routers.admin import (
    AdminUser,
    LabCleanupApplyRequest,
    LabPrepFixItem,
    LabPriceFixItem,
    apply_lab_tests_cleanup,
)


class StrictLabTestsTable:
    """A fake PostgREST table that raises ValueError if any unknown column is updated or queried."""

    ALLOWED_COLUMNS = {
        "id",
        "clinic_id",
        "branch_id",
        "name",
        "category",
        "sample_type",
        "price_paise",
        "turnaround_hours",
        "fasting_required",
        "prep_instructions",  # The REAL column!
        "description",
        "created_at",
        "updated_at",
    }

    def __init__(self, rows):
        self.rows = rows
        self._filters = []
        self._selected_cols = None
        self._update_payload = None

    def select(self, cols):
        self._selected_cols = cols
        return self

    def eq(self, col, val):
        if col not in self.ALLOWED_COLUMNS:
            raise ValueError(f"Unknown column filter: {col}")
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        if col not in self.ALLOWED_COLUMNS:
            raise ValueError(f"Unknown column filter in in_: {col}")
        return self

    def update(self, payload):
        for k in payload.keys():
            if k not in self.ALLOWED_COLUMNS:
                raise ValueError(f"REJECTED: Unknown column '{k}' cannot be written to lab_tests!")
        self._update_payload = payload
        return self

    def execute(self):
        # Filter matching rows
        res = []
        for r in self.rows:
            match = True
            for col, val in self._filters:
                if r.get(col) != val:
                    match = False
                    break
            if match:
                if self._update_payload:
                    r.update(self._update_payload)
                res.append(dict(r))
        return MagicMock(data=res)


@pytest.mark.asyncio
async def test_apply_prep_fix_against_table_rejecting_unknown_columns():
    """Apply a prep fix against a fake table that strictly rejects unknown columns.

    If code writes to 'preparation', this test will FAIL with ValueError.
    It MUST write to 'prep_instructions'.
    """
    valid_id = "11111111-1111-1111-1111-111111111111"
    clinic_id = "22222222-2222-2222-2222-222222222222"

    table_rows = [
        {
            "id": valid_id,
            "clinic_id": clinic_id,
            "branch_id": None,
            "name": "Fasting Blood Sugar",
            "fasting_required": False,
            "prep_instructions": None,
        }
    ]

    fake_table = StrictLabTestsTable(table_rows)

    user = AdminUser("testadmin")
    user.username = "testadmin"
    user.clinic_id = clinic_id
    user.role = "clinic_admin"
    user.permissions = ["LAB_TESTS_MANAGE"]

    req = LabCleanupApplyRequest(
        prep_fixes=[
            LabPrepFixItem(
                test_id=valid_id,
                fasting_required=True,
                preparation="10-12 hours overnight fasting required.",
            )
        ]
    )

    with patch("app.routers.admin.supabase.table", side_effect=lambda name: StrictLabTestsTable(table_rows)), \
         patch("app.database.supabase.table", side_effect=lambda name: StrictLabTestsTable(table_rows)), \
         patch("app.routers.admin.sb", side_effect=lambda q: q.execute()), \
         patch("app.routers.admin.log_admin_action", new_callable=AsyncMock):
        res = await apply_lab_tests_cleanup(
            body=req,
            clinic_id=clinic_id,
            user=user,
        )
        assert res["updated_count"] == 1
        assert table_rows[0]["fasting_required"] is True
        assert table_rows[0]["prep_instructions"] == "10-12 hours overnight fasting required."
        assert "preparation" not in table_rows[0]


def test_panel_generate_ai_details_draft_references_f_lab_test_id():
    """The admin panel's generateAiDetailsDraft must reference f-labTestId and not editLabTestId."""
    index_path = Path(__file__).resolve().parent.parent / "admin" / "index.html"
    content = index_path.read_text(encoding="utf-8")

    # Extract the function body
    match = re.search(r"window\.generateAiDetailsDraft\s*=\s*async\s*function\s*\(\)\s*\{(.*?)\};", content, re.DOTALL)
    assert match, "generateAiDetailsDraft function not found in admin/index.html"
    fn_body = match.group(1)

    assert "f-labTestId" in fn_body, "generateAiDetailsDraft must reference 'f-labTestId'"
    assert "editLabTestId" not in fn_body, "generateAiDetailsDraft must not reference nonexistent 'editLabTestId'"
    assert "Save the test first" in fn_body, "generateAiDetailsDraft must show 'Save the test first' for new test"
    assert "AI unavailable — write details manually" in fn_body, "generateAiDetailsDraft must show error message when AI returns template"


def test_clinic_admin_cannot_change_budget():
    """A clinic_admin cannot change the AI budget. POST/PATCH /admin/ai/budget is removed."""
    from app.routers.admin import router as admin_router
    from app.routers.platform import router as platform_router

    app = FastAPI()
    app.include_router(admin_router, prefix="/admin")
    app.include_router(platform_router, prefix="/platform")

    client = TestClient(app)

    # Attempt to POST to /admin/ai/budget -> 405 Method Not Allowed (since route is removed)
    res = client.post("/admin/ai/budget", json={"budget_rupees": 5000})
    assert res.status_code in (404, 405), f"Expected 404/405, got {res.status_code}"

    # Attempt to PATCH to /admin/ai/budget -> 405 Method Not Allowed
    res = client.patch("/admin/ai/budget", json={"budget_rupees": 5000})
    assert res.status_code in (404, 405), f"Expected 404/405, got {res.status_code}"


@pytest.mark.asyncio
async def test_cleanup_apply_runs_updates_before_deletes_and_aborts_on_failure():
    """If an update fails, deletes must not run."""
    valid_id = "11111111-1111-1111-1111-111111111111"
    clinic_id = "22222222-2222-2222-2222-222222222222"

    user = AdminUser("testadmin")
    user.username = "testadmin"
    user.clinic_id = clinic_id
    user.role = "clinic_admin"
    user.permissions = ["LAB_TESTS_MANAGE"]

    req = LabCleanupApplyRequest(
        delete_duplicate_ids=["33333333-3333-3333-3333-333333333333"],
        price_fixes=[
            LabPriceFixItem(test_id=valid_id, price_rupees=200.0)
        ],
    )

    with patch("app.routers.admin.bulk_delete_lab_tests", new_callable=AsyncMock) as mock_bulk_del, \
         patch("app.routers.admin.sb", side_effect=Exception("Database update error")):
        mock_bulk_del.return_value = {"deleted": 1}
        with pytest.raises(HTTPException) as exc:
            await apply_lab_tests_cleanup(body=req, clinic_id=clinic_id, user=user)
        assert exc.value.status_code == 500
        # bulk_delete_lab_tests must NOT have been called!
        mock_bulk_del.assert_not_called()


def test_validation_rules_reject_invalid_inputs():
    """Verify UUID validation, price bounds (₹1-₹1,00,000), prep max length (500), and list max length (1000)."""
    valid_uuid = "11111111-1111-1111-1111-111111111111"

    # 1. Invalid UUID
    with pytest.raises(ValueError, match="Invalid UUID"):
        LabPrepFixItem(test_id="not-a-uuid", fasting_required=True)

    with pytest.raises(ValueError, match="Invalid UUID"):
        LabPriceFixItem(test_id="invalid", price_rupees=100.0)

    with pytest.raises(ValueError, match="Invalid UUID"):
        LabCleanupApplyRequest(delete_duplicate_ids=["not-a-uuid"])

    # 2. Price bounds: ₹1 to ₹1,00,000
    with pytest.raises(ValueError):
        LabPriceFixItem(test_id=valid_uuid, price_rupees=0.5)  # < 1.0

    with pytest.raises(ValueError):
        LabPriceFixItem(test_id=valid_uuid, price_rupees=100000.5)  # > 100,000.0

    # Valid prices pass
    assert LabPriceFixItem(test_id=valid_uuid, price_rupees=1.0).price_rupees == 1.0
    assert LabPriceFixItem(test_id=valid_uuid, price_rupees=100000.0).price_rupees == 100000.0

    # 3. Preparation cap at 500 characters
    with pytest.raises(ValueError):
        LabPrepFixItem(test_id=valid_uuid, fasting_required=True, preparation="X" * 501)

    assert len(LabPrepFixItem(test_id=valid_uuid, fasting_required=True, preparation="X" * 500).preparation) == 500

    # 4. List cap at 1,000 items
    with pytest.raises(ValueError):
        LabCleanupApplyRequest(delete_duplicate_ids=[valid_uuid] * 1001)


@pytest.mark.asyncio
async def test_platform_owner_can_set_clinic_ai_budget():
    """Platform owner can update a clinic's AI budget via /platform/clinics/{clinic_id}/ai-budget."""
    from app.routers.platform import platform_update_clinic_ai_budget, PlatformAIBudgetRequest
    clinic_id = "11111111-1111-1111-1111-111111111111"

    owner = AdminUser("platform_owner")
    owner.role = "platform_owner"

    fake_clinic_data = [{"id": clinic_id, "name": "Apollo Lab", "config": {"ai_budget_paise": 50000}}]

    updates = []
    class FakeTable:
        def select(self, *a, **k):
            return self
        def eq(self, *a, **k):
            return self
        def limit(self, *a, **k):
            return self
        def update(self, payload):
            updates.append(payload)
            return self

    with patch("app.routers.platform.supabase.table", side_effect=lambda name: FakeTable()), \
         patch("app.database.supabase.table", side_effect=lambda name: FakeTable()), \
         patch("app.routers.platform.sb", new_callable=AsyncMock) as mock_sb, \
         patch("app.routers.platform.log_admin_action", new_callable=AsyncMock):
        mock_sb.side_effect = [
            MagicMock(data=fake_clinic_data),
            MagicMock(data=[]),
        ]
        res = await platform_update_clinic_ai_budget(
            clinic_id=clinic_id,
            body=PlatformAIBudgetRequest(budget_rupees=1200.0),
            request=MagicMock(client=MagicMock(host="127.0.0.1")),
            owner=owner,
        )
        assert res["success"] is True
        assert res["budget_paise"] == 120000
        assert res["budget_rupees"] == 1200.0
        assert len(updates) == 1
        assert updates[0]["config"]["ai_budget_paise"] == 120000
