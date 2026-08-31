"""Multi-branch diagnostic chains, and the billing visibility that goes with them.

A diagnostic chain (Vijaya, Lucid, Apollo Diagnostics) runs several collection
centres. The bot must ask WHICH centre before showing a catalogue, because
catalogues differ per centre and the booking needs a branch_id to route the
sample. Single-centre labs must NOT gain that extra step.

The platform bills per active location, so opening a branch changes what the
clinic owes. The owner panel has to show that without being asked.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _branch(bid, name, active=True, diagnostic=True):
    return {
        "id": bid,
        "name": name,
        "short_name": name.split()[0],
        "address": f"{name} Road",
        "landmark": "Near Metro",
        "maps_link": "",
        "is_active": active,
        "is_diagnostic": diagnostic,
    }


def _cm():
    from app.services.conversation import conversation_manager

    return conversation_manager


# -- Patient flow -------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_centre_lab_skips_branch_selection():
    """The common case must not gain an extra tap."""
    cm = _cm()
    clinic = {"id": "clinic-1", "name": "Solo Diagnostics"}

    with patch(
        "app.services.tenant.get_clinic_branches",
        new_callable=AsyncMock,
        return_value=[_branch("b1", "Madhurwada Centre")],
    ), patch.object(cm, "_show_lab_test_list", new_callable=AsyncMock) as show, patch.object(
        cm, "_send_branch_selection", new_callable=AsyncMock
    ) as pick:
        await cm._start_lab_booking(clinic, "+919876543210", "en")

    pick.assert_not_awaited()
    show.assert_awaited_once()
    context = show.await_args.args[2]
    assert context["branch_id"] == "b1", "single branch must be auto-selected"
    assert context["branch_name"] == "Madhurwada"


@pytest.mark.asyncio
async def test_lab_with_no_branches_at_all_still_shows_the_catalogue():
    cm = _cm()
    clinic = {"id": "clinic-1", "name": "Solo Diagnostics"}

    with patch(
        "app.services.tenant.get_clinic_branches", new_callable=AsyncMock, return_value=[]
    ), patch.object(cm, "_show_lab_test_list", new_callable=AsyncMock) as show:
        await cm._start_lab_booking(clinic, "+919876543210", "en")

    show.assert_awaited_once()
    assert show.await_args.args[2] == {}


@pytest.mark.asyncio
async def test_multi_centre_chain_asks_for_the_location_first():
    cm = _cm()
    clinic = {"id": "clinic-1", "name": "Vijaya Diagnostics"}
    branches = [
        _branch("b1", "Madhurwada Centre"),
        _branch("b2", "Gajuwaka Centre"),
        _branch("b3", "Dwaraka Centre"),
    ]

    with patch(
        "app.services.tenant.get_clinic_branches",
        new_callable=AsyncMock,
        return_value=branches,
    ), patch.object(cm, "_show_lab_test_list", new_callable=AsyncMock) as show, patch.object(
        cm, "_send_branch_selection", new_callable=AsyncMock
    ) as pick, patch.object(cm, "update_state", new_callable=AsyncMock) as upd:
        await cm._start_lab_booking(clinic, "+919876543210", "en")

    show.assert_not_awaited()
    pick.assert_awaited_once()
    offered = pick.await_args.args[2]
    assert [b["id"] for b in offered] == ["b1", "b2", "b3"]
    assert upd.await_args.args[2] == "selecting_branch"
    assert upd.await_args.args[3] == {"lab_flow": True}


@pytest.mark.asyncio
async def test_inactive_centres_are_not_offered():
    cm = _cm()
    clinic = {"id": "clinic-1", "name": "Vijaya Diagnostics"}
    branches = [
        _branch("b1", "Madhurwada Centre"),
        _branch("b2", "Closed Centre", active=False),
    ]

    with patch(
        "app.services.tenant.get_clinic_branches",
        new_callable=AsyncMock,
        return_value=branches,
    ), patch.object(cm, "_show_lab_test_list", new_callable=AsyncMock) as show, patch.object(
        cm, "_send_branch_selection", new_callable=AsyncMock
    ) as pick:
        await cm._start_lab_booking(clinic, "+919876543210", "en")

    # Only one active branch left -> auto-select, no picker.
    pick.assert_not_awaited()
    assert show.await_args.args[2]["branch_id"] == "b1"


@pytest.mark.asyncio
async def test_choosing_a_centre_shows_its_catalogue_not_the_reports_dead_end():
    """Regression: every branch of a diagnostics chain has is_diagnostic=True.

    The old handler bounced any is_diagnostic branch to the main menu with a
    "you can view reports from the menu" message, which for a diagnostics-only
    chain meant the patient could never book at all.
    """
    cm = _cm()
    clinic = {"id": "clinic-1", "name": "Vijaya Diagnostics"}

    with patch(
        "app.services.tenant.get_branch_by_id",
        new_callable=AsyncMock,
        return_value=_branch("b2", "Gajuwaka Centre"),
    ), patch.object(cm, "_show_lab_test_list", new_callable=AsyncMock) as show, patch.object(
        cm.whatsapp, "send_text", new_callable=AsyncMock
    ) as send_text, patch.object(cm, "_send_main_menu", new_callable=AsyncMock) as menu:
        await cm._handle_selecting_branch(
            clinic=clinic,
            phone="+919876543210",
            message="",
            intent="",
            context={"lab_flow": True},
            patient={},
            lang="en",
            interactive_data={"id": "branch_b2"},
        )

    menu.assert_not_awaited()
    send_text.assert_not_awaited()
    show.assert_awaited_once()
    context = show.await_args.args[2]
    assert context["branch_id"] == "b2"
    assert context["branch_name"] == "Gajuwaka"
    assert "lab_flow" not in context, "flow marker must not leak into booking context"


@pytest.mark.asyncio
async def test_consultation_branch_selection_is_unchanged():
    """A normal clinic's diagnostic branch must still bounce to the menu."""
    cm = _cm()
    clinic = {"id": "clinic-1", "name": "City Polyclinic"}

    with patch(
        "app.services.tenant.get_branch_by_id",
        new_callable=AsyncMock,
        return_value=_branch("b9", "Lab Annexe", diagnostic=True),
    ), patch.object(
        cm, "_is_diagnostics_only", new_callable=AsyncMock, return_value=False
    ), patch.object(
        cm.whatsapp, "send_text", new_callable=AsyncMock
    ) as send_text, patch.object(
        cm, "_send_main_menu", new_callable=AsyncMock
    ) as menu, patch.object(
        cm, "update_state", new_callable=AsyncMock
    ), patch.object(cm, "_show_lab_test_list", new_callable=AsyncMock) as show:
        await cm._handle_selecting_branch(
            clinic=clinic,
            phone="+919876543210",
            message="",
            intent="",
            context={},
            patient={},
            lang="en",
            interactive_data={"id": "branch_b9"},
        )

    show.assert_not_awaited()
    send_text.assert_awaited_once()
    menu.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_chain_with_more_than_ten_centres_can_reach_them_all():
    """Branch lists paginate too - the 11th centre must not vanish."""
    cm = _cm()
    clinic = {"id": "clinic-1", "name": "Big Chain Diagnostics"}
    branches = [_branch(f"b{i}", f"Centre {i}") for i in range(14)]

    with patch(
        "app.services.tenant.get_clinic_branches",
        new_callable=AsyncMock,
        return_value=branches,
    ), patch.object(cm, "_send_branch_selection", new_callable=AsyncMock) as pick, patch.object(
        cm, "update_state", new_callable=AsyncMock
    ):
        await cm._start_lab_booking(clinic, "+919876543210", "en")
        offered = pick.await_args.args[2]

    # The picker receives all 14; _page_rows splits them into reachable pages.
    assert len(offered) == 14
    rows_p0, _ = cm._page_rows(
        [
            {"id": f"branch_{b['id']}", "title": b["name"], "description": ""}
            for b in offered
        ],
        0,
        "branch_more",
        "en",
    )
    assert rows_p0[-1]["id"] == "branch_more"
    assert len(rows_p0) == 10


# -- Platform owner billing visibility ----------------------------------------


def test_a_clinic_with_no_branch_rows_is_still_one_billable_location():
    from app.routers.platform import _billable_locations

    assert _billable_locations(0) == 1
    assert _billable_locations(1) == 1
    assert _billable_locations(4) == 4


@pytest.mark.asyncio
async def test_branch_census_counts_active_diagnostic_and_recent_additions():
    from app.routers.platform import _fetch_clinic_branch_counts

    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    rows = [
        {"clinic_id": "c1", "is_active": True, "is_diagnostic": True, "created_at": old},
        {"clinic_id": "c1", "is_active": True, "is_diagnostic": True, "created_at": recent},
        {"clinic_id": "c1", "is_active": False, "is_diagnostic": True, "created_at": old},
        {"clinic_id": "c2", "is_active": True, "is_diagnostic": False, "created_at": old},
    ]

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.range.return_value.execute.return_value = MagicMock(
        data=rows
    )

    with patch("app.routers.platform.supabase", mock_sb):
        census = await _fetch_clinic_branch_counts(window_days=30)

    assert census["c1"]["total"] == 3
    assert census["c1"]["active"] == 2, "an explicitly inactive branch is not billed"
    assert census["c1"]["diagnostic"] == 3
    assert census["c1"]["added_recently"] == 1
    assert census["c1"]["newest_at"] == recent
    assert census["c2"]["active"] == 1
    assert census["c2"]["diagnostic"] == 0


@pytest.mark.asyncio
async def test_branch_census_fails_soft_so_the_leaderboard_still_renders():
    from app.routers.platform import _fetch_clinic_branch_counts

    mock_sb = MagicMock()
    mock_sb.table.return_value.select.return_value.range.return_value.execute.side_effect = Exception(
        "PostgREST down"
    )

    with patch("app.routers.platform.supabase", mock_sb):
        assert await _fetch_clinic_branch_counts() == {}
