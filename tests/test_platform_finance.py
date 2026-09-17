"""The platform owner's books: rates, expenses, invoices and the P&L.

Every assertion here guards a rule that, if it broke, would cost the owner
real money quietly rather than loudly — a rate that ignores a negotiated
discount, an expense counted twice in the month it was edited, an invoice
re-raised on a second click, or a Meta cost read from a truncated ledger.

The money rules are pure functions, so most of this file needs no database.
"""

import base64
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.platform_finance import (
    invoice_amount_paise,
    is_valid_month,
    margin_percent,
    month_bounds,
    month_window_iso,
    resolve_rate,
    select_month_expenses,
    supersede_plan,
)

client = TestClient(app)

PLAN_TIERS = {
    "polyclinic": {"monthly_price_paise": 800_000},   # Rs 8,000
    "diagstream": {"monthly_price_paise": 400_000},   # Rs 4,000
    "derma": {"monthly_price_paise": 600_000},        # Rs 6,000
    "enterprise": {},                                  # priced by negotiation only
}


def _auth() -> dict:
    creds = f"{settings.owner_username}:{settings.owner_password}"
    return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}


def _recurring(name, amount, start, end=None):
    return {
        "name": name, "amount_paise": amount, "is_recurring": True,
        "effective_from": start, "effective_to": end, "month": None,
    }


# -- 1. Rate resolution: the negotiated deal must survive a price rise --------


def test_plan_default_applies_when_clinic_has_no_override():
    rate = resolve_rate("polyclinic", None, PLAN_TIERS)
    assert rate["rate_paise"] == 800_000
    assert rate["source"] == "plan_default"
    assert rate["billing_mode"] == "per_location"


def test_override_beats_plan_default():
    """The whole point of the override table.

    An early customer was signed at Rs 5,000 while the polyclinic list price is
    Rs 8,000. Raising the list price later must not silently reprice them.
    """
    override = {"rate_paise": 500_000, "billing_mode": "per_location"}
    rate = resolve_rate("polyclinic", override, PLAN_TIERS)
    assert rate["rate_paise"] == 500_000
    assert rate["source"] == "override"


def test_override_survives_a_plan_price_rise():
    override = {"rate_paise": 500_000, "billing_mode": "per_location"}
    raised = {**PLAN_TIERS, "polyclinic": {"monthly_price_paise": 1_200_000}}
    assert resolve_rate("polyclinic", override, raised)["rate_paise"] == 500_000


def test_unpriced_plan_resolves_to_zero_not_a_guess():
    """A missing price must read as Rs 0, never as an invented default — the
    owner budgets off this number."""
    assert resolve_rate("enterprise", None, PLAN_TIERS)["rate_paise"] == 0
    assert resolve_rate("plan-that-does-not-exist", None, PLAN_TIERS)["rate_paise"] == 0
    assert resolve_rate(None, None, PLAN_TIERS)["rate_paise"] == 0


def test_malformed_override_falls_back_rather_than_crashing():
    for bad in ({"rate_paise": None}, {"rate_paise": -1}, {"rate_paise": "500000"}, {}):
        assert resolve_rate("polyclinic", bad, PLAN_TIERS)["source"] == "plan_default"


def test_unknown_billing_mode_is_coerced_to_per_location():
    rate = resolve_rate("polyclinic", {"rate_paise": 1, "billing_mode": "weekly"}, PLAN_TIERS)
    assert rate["billing_mode"] == "per_location"


# -- 2. What a clinic owes ----------------------------------------------------


def test_per_location_multiplies_by_branch_count():
    assert invoice_amount_paise(800_000, "per_location", 2) == 1_600_000


def test_flat_ignores_branch_count():
    """A flat deal must not double when the clinic opens a second branch."""
    assert invoice_amount_paise(800_000, "flat", 5) == 800_000


def test_single_site_clinic_is_billed_for_one_location():
    """A clinic with no branch rows is still one physical place. Billing it
    for zero locations would make every single-site clinic read as free."""
    assert invoice_amount_paise(800_000, "per_location", 0) == 800_000


def test_amounts_stay_integer_paise():
    amount = invoice_amount_paise(333_333, "per_location", 3)
    assert isinstance(amount, int)
    assert amount == 999_999


# -- 3. Expense windows: the double-count trap --------------------------------


def test_recurring_expense_applies_to_months_inside_its_window():
    rows = [_recurring("Render", 210_000, "2026-01-01")]
    recurring, one_off = select_month_expenses(rows, "2026-09")
    assert len(recurring) == 1
    assert one_off == []


def test_recurring_expense_excluded_before_it_starts_and_after_it_ends():
    rows = [
        _recurring("Future service", 100, "2026-11-01"),
        _recurring("Cancelled service", 100, "2026-01-01", "2026-06-30"),
    ]
    recurring, _ = select_month_expenses(rows, "2026-09")
    assert recurring == []


def test_one_off_expense_lands_only_in_its_own_month():
    rows = [{"name": "Razorpay setup", "amount_paise": 50_000,
             "is_recurring": False, "month": "2026-09"}]
    assert len(select_month_expenses(rows, "2026-09")[1]) == 1
    assert select_month_expenses(rows, "2026-08")[1] == []


def test_superseded_expense_and_its_replacement_never_share_a_month():
    """THE DOUBLE-COUNT GUARD.

    Render goes from Rs 2,100 to Rs 2,500 in September. The old row is closed
    on 31 August and the new one opens on 1 September. If both were returned
    for either month, that month would be charged twice for one service and the
    profit figure would be wrong by Rs 2,100.
    """
    old = _recurring("Render", 210_000, "2026-01-01", "2026-08-31")
    new = _recurring("Render", 250_000, "2026-09-01")

    aug, _ = select_month_expenses([old, new], "2026-08")
    sep, _ = select_month_expenses([old, new], "2026-09")

    assert [r["amount_paise"] for r in aug] == [210_000]  # August keeps the old price
    assert [r["amount_paise"] for r in sep] == [250_000]  # September gets the new one


# -- 4. Versioning decides which of those two rows exists ---------------------


def test_editing_an_expense_that_started_earlier_closes_and_reopens():
    existing = _recurring("Render", 210_000, "2026-01-01")
    plan = supersede_plan(existing, "2026-09")
    assert plan["action"] == "close_and_insert"
    assert plan["close_old_at"] == "2026-08-31"   # day before the new month
    assert plan["open_new_at"] == "2026-09-01"


def test_editing_an_expense_added_this_month_overwrites_it():
    """No earlier month depends on it, so versioning would only add clutter."""
    existing = _recurring("Render", 210_000, "2026-09-01")
    assert supersede_plan(existing, "2026-09")["action"] == "update_in_place"


def test_two_edits_in_the_same_month_do_not_stack_versions():
    """The second edit lands on a row that already starts this month, so it
    updates in place — otherwise every correction would leave a dead row."""
    after_first_edit = _recurring("Render", 250_000, "2026-09-01")
    assert supersede_plan(after_first_edit, "2026-09")["action"] == "update_in_place"


def test_supersede_boundaries_are_contiguous_with_no_gap():
    plan = supersede_plan(_recurring("X", 1, "2026-01-01"), "2026-03")
    assert plan["close_old_at"] == "2026-02-28"  # 2026 is not a leap year
    assert plan["open_new_at"] == "2026-03-01"


# -- 5. Month handling: an IST month, not a UTC one ---------------------------


def test_month_validation_rejects_malformed_input():
    assert is_valid_month("2026-09")
    for bad in ("2026-13", "2026-9", "26-09", "2026/09", "", None, 202609):
        assert not is_valid_month(bad)


def test_month_bounds_handles_month_lengths():
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))  # leap
    assert month_bounds("2026-09") == (date(2026, 9, 1), date(2026, 9, 30))


def test_month_window_is_shifted_for_ist():
    """A message sent at 01:00 IST on 1 September belongs to September.

    The ledger stores UTC, so the window must start at 18:30 UTC on 31 August.
    Comparing IST dates against UTC timestamps directly would misfile every
    message sent in India between midnight and 05:30.
    """
    start, end = month_window_iso("2026-09")
    assert start.startswith("2026-08-31T18:30")
    assert end.startswith("2026-09-30T18:30")


# -- 6. Margin ----------------------------------------------------------------


def test_margin_is_percentage_of_revenue():
    assert margin_percent(1_000_000, 250_000) == 75.0


def test_zero_revenue_does_not_divide_by_zero():
    """An unpriced clinic is not infinitely unprofitable, it is just not yet
    earning. This runs on every dashboard load before prices are set."""
    assert margin_percent(0, 500_000) == 0.0
    assert margin_percent(-5, 1) == 0.0


def test_margin_goes_negative_when_costs_exceed_revenue():
    assert margin_percent(100_000, 150_000) == -50.0


# -- 7. The ledger scan must not silently truncate ----------------------------


@pytest.mark.asyncio
async def test_ledger_scan_pages_past_the_postgrest_row_cap():
    """THE BUG THIS REPLACED.

    PostgREST caps an unbounded select at 1000 rows and says nothing. The old
    platform sweep read that floor as a total, so the dashboard reported
    exactly 1,000 messages sent and a Meta cost to match. Profit is now
    computed off the same figure, so a truncated scan would flatter every
    margin on the page.
    """
    from app.services.message_accounting import scan_outbound_ledger

    all_rows = [{"clinic_id": "c1", "category": "utility"} for _ in range(2_350)]

    def _range(start, end):
        page = MagicMock()
        page.execute.return_value.data = all_rows[start: end + 1]
        return page

    builder = MagicMock()
    builder.select.return_value.eq.return_value.neq.return_value.gte.return_value.range.side_effect = _range

    fake_supabase = MagicMock()
    fake_supabase.table.return_value = builder

    with patch("app.database.supabase", fake_supabase):
        rows = await scan_outbound_ledger(start_iso="2026-09-01T00:00:00+00:00")

    assert len(rows) == 2_350, "scan stopped at the row cap instead of paging"


@pytest.mark.asyncio
async def test_ledger_scan_returns_partial_results_on_failure():
    """A dashboard showing most of the month beats one showing an error."""
    from app.services.message_accounting import scan_outbound_ledger

    builder = MagicMock()
    builder.select.return_value.eq.return_value.neq.return_value.gte.return_value.range.side_effect = (
        RuntimeError("connection reset")
    )
    fake_supabase = MagicMock()
    fake_supabase.table.return_value = builder

    with patch("app.database.supabase", fake_supabase):
        rows = await scan_outbound_ledger(start_iso="2026-09-01T00:00:00+00:00")

    assert rows == []


# -- 8. The rollup assembles the arithmetic correctly -------------------------


@pytest.mark.asyncio
async def test_finance_summary_computes_mrr_expenses_and_both_profit_lines():
    """End-to-end arithmetic on known inputs.

    Visakha  polyclinic, override Rs 5,000/loc x 2 locations = Rs 10,000
    Accumx   diagstream, plan default Rs 4,000 x 1 location  = Rs 4,000
    Aura     derma, plan default Rs 6,000      x 2 locations = Rs 12,000
                                                  MRR = Rs 26,000
    Meta costs: 624 + 8,952 + 2,424 paise         = Rs 120.00
    Fixed expenses: Render Rs 2,100 + Supabase Rs 2,080 = Rs 4,180
    """
    import app.services.platform_finance as pf

    clinics = [
        {"id": "visakha", "name": "Visakha", "plan": "polyclinic", "is_active": True},
        {"id": "accumx", "name": "Accumx", "plan": "diagstream", "is_active": True},
        {"id": "aura", "name": "Aura", "plan": "derma", "is_active": True},
    ]
    fake_supabase = MagicMock()
    fake_supabase.table.return_value.select.return_value.execute.return_value.data = clinics

    with patch("app.database.supabase", fake_supabase), \
         patch("app.services.message_accounting._get_plan_tiers", AsyncMock(return_value=PLAN_TIERS)), \
         patch.object(pf, "fetch_billing_rates", AsyncMock(return_value={
             "visakha": {"rate_paise": 500_000, "billing_mode": "per_location"},
         })), \
         patch("app.routers.platform._fetch_clinic_branch_counts", AsyncMock(return_value={
             "visakha": {"active": 2}, "accumx": {"active": 1}, "aura": {"active": 2},
         })), \
         patch.object(pf, "meta_cost_by_clinic", AsyncMock(return_value={
             "visakha": 624, "accumx": 8_952, "aura": 2_424,
         })), \
         patch.object(pf, "fetch_invoices", AsyncMock(return_value=[
             {"clinic_id": "visakha", "id": "i1", "status": "paid",
              "amount_paise": 1_000_000, "amount_paid_paise": 1_000_000},
             {"clinic_id": "accumx", "id": "i2", "status": "unpaid",
              "amount_paise": 400_000, "amount_paid_paise": 0},
         ])), \
         patch.object(pf, "fetch_expenses", AsyncMock(return_value=[
             _recurring("Render", 210_000, "2026-01-01"),
             _recurring("Supabase", 208_000, "2026-01-01"),
         ])):
        result = await pf.finance_summary("2026-09")

    t = result["totals"]
    assert result["success"] is True
    assert t["mrr_paise"] == 2_600_000              # Rs 26,000
    assert t["meta_cost_paise"] == 12_000           # Rs 120.00
    assert t["fixed_expense_paise"] == 418_000      # Rs 4,180
    assert t["total_expense_paise"] == 430_000
    assert t["net_profit_paise"] == 2_600_000 - 430_000    # accrual
    assert t["collected_paise"] == 1_000_000
    assert t["cash_profit_paise"] == 1_000_000 - 430_000   # cash
    assert t["outstanding_paise"] == 400_000               # Accumx still owes

    by_id = {r["clinic_id"]: r for r in result["clinics"]}
    # The negotiated rate is used, not the Rs 8,000 list price.
    assert by_id["visakha"]["revenue_paise"] == 1_000_000
    assert by_id["visakha"]["rate_source"] == "override"
    # Per-clinic profit deducts that clinic's own Meta cost and nothing else —
    # no share of Render or Supabase is pushed onto a clinic row.
    assert by_id["accumx"]["net_paise"] == 400_000 - 8_952


@pytest.mark.asyncio
async def test_inactive_clinic_is_shown_but_not_billed():
    import app.services.platform_finance as pf

    clinics = [
        {"id": "live", "name": "Live", "plan": "derma", "is_active": True},
        {"id": "churned", "name": "Churned", "plan": "polyclinic", "is_active": False},
    ]
    fake_supabase = MagicMock()
    fake_supabase.table.return_value.select.return_value.execute.return_value.data = clinics

    with patch("app.database.supabase", fake_supabase), \
         patch("app.services.message_accounting._get_plan_tiers", AsyncMock(return_value=PLAN_TIERS)), \
         patch.object(pf, "fetch_billing_rates", AsyncMock(return_value={})), \
         patch("app.routers.platform._fetch_clinic_branch_counts", AsyncMock(return_value={})), \
         patch.object(pf, "meta_cost_by_clinic", AsyncMock(return_value={})), \
         patch.object(pf, "fetch_invoices", AsyncMock(return_value=[])), \
         patch.object(pf, "fetch_expenses", AsyncMock(return_value=[])):
        result = await pf.finance_summary("2026-09")

    by_id = {r["clinic_id"]: r for r in result["clinics"]}
    assert by_id["churned"]["revenue_paise"] == 0
    assert by_id["churned"]["is_active"] is False
    assert result["totals"]["mrr_paise"] == 600_000  # only the live clinic
    assert result["totals"]["active_clinics"] == 1


# -- 9. The endpoints are owner-only ------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("get", "/platform/finance"),
    ("get", "/platform/finance/rates"),
    ("get", "/platform/finance/expenses"),
    ("get", "/platform/finance/invoices"),
    ("post", "/platform/finance/invoices/generate"),
])
def test_finance_endpoints_reject_anonymous_callers(method, path):
    """These carry every clinic's negotiated price and the platform's margin.
    An unauthenticated 200 here is a commercial data leak."""
    res = getattr(client, method)(path)
    assert res.status_code in (401, 403), f"{path} answered {res.status_code} without credentials"


def test_month_parameter_is_validated():
    res = client.get("/platform/finance?month=2026-13", headers=_auth())
    assert res.status_code == 422


# -- 10. Regression: the endpoint this feature rewired still answers -----------


@patch("app.routers.platform.log_admin_action")
@patch("app.services.message_accounting.scan_outbound_ledger", new_callable=AsyncMock)
@patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock)
@patch("app.services.message_accounting._get_pricing", new_callable=AsyncMock)
@patch("app.database.supabase")
def test_messaging_usage_payload_shape_is_unchanged(
    mock_supabase, mock_pricing, mock_tiers, mock_scan, _log
):
    """get_platform_usage was rewired onto the paginated scanner. Its response
    contract is consumed by the existing WhatsApp Usage card and must not have
    shifted."""
    mock_pricing.return_value = {
        "utility_paise": 12, "marketing_paise": 75,
        "authentication_paise": 10, "service_paise": 0,
    }
    mock_tiers.return_value = PLAN_TIERS
    mock_scan.return_value = [
        {"clinic_id": "c1", "category": "utility"},
        {"clinic_id": "c1", "category": "marketing"},
    ]
    mock_supabase.table.return_value.select.return_value.execute.return_value.data = [
        {"id": "c1", "name": "Alpha", "plan": "polyclinic", "is_active": True},
    ]

    res = client.get("/platform/messaging-usage", headers=_auth())
    assert res.status_code == 200
    body = res.json()
    for key in (
        "success", "period_days", "total_outbound", "total_utility",
        "total_marketing", "total_authentication", "total_service",
        "total_estimated_cost_inr", "pricing", "clinics",
    ):
        assert key in body, f"messaging-usage lost its `{key}` field"

    assert body["total_outbound"] == 2
    assert body["total_estimated_cost_inr"] == 0.87  # (12 + 75) paise
    assert set(body["clinics"][0]) >= {
        "clinic_id", "clinic_name", "plan", "outbound_total",
        "utility_count", "marketing_count", "estimated_cost_inr",
    }


# -- 11. Endpoint validation: the branches that decide what gets billed --------


def test_recurring_expense_rejects_a_month():
    """A recurring cost applies every month; naming one is a contradiction the
    CHECK constraint would otherwise reject as an opaque 500."""
    res = client.post(
        "/platform/finance/expenses",
        headers=_auth(),
        json={"name": "Render", "amount_paise": 210_000, "is_recurring": True, "month": "2026-09"},
    )
    assert res.status_code == 422
    assert "recurring" in res.json()["detail"].lower()


def test_one_off_expense_requires_a_month():
    res = client.post(
        "/platform/finance/expenses",
        headers=_auth(),
        json={"name": "Razorpay setup", "amount_paise": 50_000, "is_recurring": False},
    )
    assert res.status_code == 422


def test_negative_amounts_are_rejected():
    """Pydantic ge=0. A negative expense would read as income."""
    res = client.post(
        "/platform/finance/expenses",
        headers=_auth(),
        json={"name": "Refund", "amount_paise": -100, "is_recurring": True},
    )
    assert res.status_code == 422


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_partial_payment_covering_the_full_invoice_is_refused(mock_supabase, _log):
    """Recording a 'partial' payment that in fact clears the invoice would
    leave it reading as outstanding forever."""
    mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "inv1", "amount_paise": 400_000, "amount_paid_paise": 0, "status": "unpaid"}
    ]
    res = client.patch(
        "/platform/finance/invoices/inv1",
        headers=_auth(),
        json={"status": "partial", "amount_paid_paise": 400_000},
    )
    assert res.status_code == 422
    assert "mark it paid" in res.json()["detail"].lower()


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_partial_payment_without_an_amount_is_refused(mock_supabase, _log):
    mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {"id": "inv1", "amount_paise": 400_000, "amount_paid_paise": 0, "status": "unpaid"}
    ]
    res = client.patch(
        "/platform/finance/invoices/inv1", headers=_auth(), json={"status": "partial"}
    )
    assert res.status_code == 422


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform.supabase")
def test_setting_a_rate_on_an_unknown_clinic_is_a_404_not_a_500(mock_supabase, _log):
    mock_supabase.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
    res = client.put(
        "/platform/finance/rates/not-a-real-clinic",
        headers=_auth(),
        json={"rate_paise": 800_000, "billing_mode": "per_location"},
    )
    assert res.status_code == 404


@patch("app.routers.platform.log_admin_action")
@patch("app.routers.platform._fetch_clinic_branch_counts", new_callable=AsyncMock)
@patch("app.services.platform_finance.fetch_invoices", new_callable=AsyncMock)
@patch("app.services.platform_finance.fetch_billing_rates", new_callable=AsyncMock)
@patch("app.services.message_accounting._get_plan_tiers", new_callable=AsyncMock)
@patch("app.routers.platform.supabase")
def test_invoice_generation_is_idempotent_and_skips_inactive_clinics(
    mock_supabase, mock_tiers, mock_rates, mock_invoices, mock_branches, _log
):
    """Two clicks must not double-bill, and a churned clinic must not be billed."""
    inserted = {}

    def table_router(name):
        m = MagicMock()
        if name == "clinics":
            m.select.return_value.execute.return_value.data = [
                {"id": "already", "name": "Already Billed", "plan": "derma", "is_active": True},
                {"id": "fresh", "name": "Fresh", "plan": "derma", "is_active": True},
                {"id": "churned", "name": "Churned", "plan": "derma", "is_active": False},
            ]
        elif name == "platform_invoices":
            def _insert(rows):
                inserted["rows"] = rows
                r = MagicMock()
                r.execute.return_value.data = rows
                return r
            m.insert.side_effect = _insert
        return m

    mock_supabase.table.side_effect = table_router
    mock_tiers.return_value = PLAN_TIERS
    mock_rates.return_value = {}
    mock_branches.return_value = {"fresh": {"active": 2}}
    # "already" has an invoice for the month; it must not be raised again.
    mock_invoices.return_value = [{"clinic_id": "already", "id": "i0"}]

    res = client.post(
        "/platform/finance/invoices/generate", headers=_auth(), json={"month": "2026-09"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["created"] == 1
    assert body["skipped"] == 2  # one already billed, one inactive

    billed = {r["clinic_id"] for r in inserted["rows"]}
    assert billed == {"fresh"}, "generation billed a clinic it should have skipped"
    # derma list price Rs 6,000 x 2 active branches
    assert inserted["rows"][0]["amount_paise"] == 1_200_000
    assert inserted["rows"][0]["locations_billed"] == 2
