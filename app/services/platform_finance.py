"""Platform owner financial tracking — the Kriya AI books.

OWNER-ONLY. Every function here answers a question only the platform owner may
ask: what each clinic is charged, what the platform pays to run, and what is
left over. None of it may ever reach a clinic-facing API — a clinic learning
what a different clinic pays is a commercial leak, and a clinic learning the
platform's margin on it is worse.

WHAT THIS MODULE IS FOR
The dashboard could already read what the platform COSTS (Meta per-message
spend, from outbound_message_ledger x meta_pricing_config) but had nowhere to
record what it EARNS. `plan_tiers.monthly_price_paise` existed but was
display-only and sat at 0, and the "Platform Revenue" tile was in fact reading
appointments.amount_paise — patients paying clinics, not clinics paying Kriya.
Migration 079 added the missing half; this module is the arithmetic over it.

MONEY IS ALWAYS INTEGER PAISE
Same convention as plan_tiers and meta_pricing_config. Division happens once,
at the presentation edge. A float rupee that round-trips through a sum is how
a books page starts disagreeing with itself by a paisa and stops being
trusted.

THE PURE CORE IS DELIBERATELY SEPARATE FROM THE QUERIES
resolve_rate / invoice_amount_paise / select_month_expenses / supersede_plan
take plain dicts and return plain values. That is what makes the money rules
testable without a database, and it is where every rule that could silently
cost the owner money lives.
"""

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Expense categories accepted by the platform_expenses CHECK constraint
# (migration 079). Kept here so the API can reject a bad value with a 400
# rather than letting Postgres raise a 500.
EXPENSE_CATEGORIES = (
    "hosting", "database", "ai", "messaging", "domain", "payment", "people", "other",
)

BILLING_MODES = ("per_location", "flat")

INVOICE_STATUSES = ("unpaid", "partial", "paid", "waived")

# Fallback when a plan has no plan_tiers row at all. Zero, never a guess: a
# made-up default would quietly inflate MRR and the owner would budget off it.
_DEFAULT_RATE_PAISE = 0


# ── Month helpers ────────────────────────────────────────────────────────────
# Months are 'YYYY-MM' strings throughout, in IST. The platform bills Indian
# clinics, so a bill dated "September" must mean September in Asia/Kolkata —
# using UTC would push roughly five and a half hours of every month-end into
# the wrong month's books.


def _ist_today() -> date:
    """Today in IST.

    Reuses the subscription service's clock so the books and the subscription
    lifecycle board can never disagree about what day it is. Falls back to a
    fixed +5:30 offset if that import is unavailable, rather than silently
    reverting to UTC.
    """
    try:
        from app.services.subscription import ist_today

        return ist_today()
    except Exception:  # pragma: no cover - defensive; the import is present in prod
        return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


def current_month() -> str:
    """The current billing month as 'YYYY-MM' (IST)."""
    return _ist_today().strftime("%Y-%m")


def is_valid_month(month: str) -> bool:
    """True for a well-formed 'YYYY-MM'.

    Validated rather than trusted because `month` reaches a CHECK constraint
    and an index; a malformed value would surface as an opaque 500.
    """
    if not isinstance(month, str) or len(month) != 7 or month[4] != "-":
        return False
    try:
        datetime.strptime(month, "%Y-%m")
        return True
    except ValueError:
        return False


def month_bounds(month: str) -> tuple[date, date]:
    """First and last calendar day of 'YYYY-MM'."""
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, 1), date(year, mon, monthrange(year, mon)[1])


def month_window_iso(month: str) -> tuple[str, str]:
    """UTC-ISO [start, end) covering the IST calendar month.

    The ledger stores `sent_at` in UTC, so an IST month runs from 18:30 UTC on
    the last day of the previous month to 18:30 UTC on its own last day.
    Comparing IST dates against UTC timestamps directly would misfile every
    message sent in India between midnight and 05:30.
    """
    first, last = month_bounds(month)
    start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc) - timedelta(
        hours=5, minutes=30
    )
    end = (
        datetime(last.year, last.month, last.day, tzinfo=timezone.utc)
        + timedelta(days=1)
        - timedelta(hours=5, minutes=30)
    )
    return start.isoformat(), end.isoformat()


def _parse_date(value) -> Optional[date]:
    """Lenient date parse for a Postgres DATE arriving as a string."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# ── Pure money rules ─────────────────────────────────────────────────────────


def resolve_rate(
    plan: Optional[str],
    override: Optional[dict],
    plan_tiers: dict,
) -> dict:
    """What this clinic is charged per month, and why.

    ABSENCE OF AN OVERRIDE IS MEANINGFUL: no row in platform_billing_rates
    means "charge the plan default". A row means the owner struck a specific
    deal with this clinic — an early-adopter price, usually — and that deal
    must survive a later rise in the plan's list price. That is the whole
    reason the override is a row and not a recomputation.

    Returns the rate, the billing mode, and `source` so the dashboard can show
    the owner which of the two they are looking at before they edit it.
    """
    if override:
        rate = override.get("rate_paise")
        if isinstance(rate, int) and rate >= 0:
            mode = override.get("billing_mode") or "per_location"
            return {
                "rate_paise": rate,
                "billing_mode": mode if mode in BILLING_MODES else "per_location",
                "source": "override",
                "notes": override.get("notes"),
            }

    tier = plan_tiers.get(plan or "") or {}
    rate = tier.get("monthly_price_paise")
    return {
        "rate_paise": rate if isinstance(rate, int) and rate >= 0 else _DEFAULT_RATE_PAISE,
        "billing_mode": "per_location",
        "source": "plan_default",
        "notes": None,
    }


def invoice_amount_paise(rate_paise: int, billing_mode: str, locations: int) -> int:
    """What the clinic owes for one month.

    'flat' ignores the location count — some deals are struck as one all-in
    number and opening a second branch must not silently double that bill.

    The location floor of 1 mirrors _billable_locations() in the platform
    router: a clinic with no branch rows is still one physical place, and
    billing it for zero would make every single-site clinic read as free.
    """
    if billing_mode == "flat":
        return max(0, rate_paise)
    return max(0, rate_paise) * max(1, locations)


def select_month_expenses(rows: Iterable[dict], month: str) -> tuple[list, list]:
    """Split expense rows into (recurring, one_off) applicable to `month`.

    A recurring row applies when its window overlaps the month at all. Windows
    are month-aligned by supersede_plan() below, so an edited expense and its
    replacement can never both land in the same month — which would double-
    count it and understate profit.
    """
    first, last = month_bounds(month)
    recurring, one_off = [], []

    for row in rows:
        if row.get("is_recurring"):
            starts = _parse_date(row.get("effective_from"))
            ends = _parse_date(row.get("effective_to"))
            if starts and starts > last:
                continue  # not started yet in this month
            if ends and ends < first:
                continue  # already closed before this month
            recurring.append(row)
        elif row.get("month") == month:
            one_off.append(row)

    return recurring, one_off


def supersede_plan(existing: dict, month: str) -> dict:
    """How to apply an edit to a recurring expense without corrupting history.

    Two cases, and picking the wrong one is how a books page starts lying:

      update_in_place  — the row already starts inside the target month, so it
                         has no earlier month to preserve. Overwrite it.
      close_and_insert — the row has been running since an earlier month. Its
                         old amount is the truth for those months, so close it
                         on the last day BEFORE this month and start a new row
                         on the first day OF this month.

    Closing on the day before the month starts is what keeps the two rows from
    overlapping. If they overlapped, select_month_expenses() would return both
    and the month would be charged twice for one service.
    """
    first, _ = month_bounds(month)
    starts = _parse_date(existing.get("effective_from"))

    if starts is None or starts >= first:
        return {"action": "update_in_place"}

    return {
        "action": "close_and_insert",
        "close_old_at": (first - timedelta(days=1)).isoformat(),
        "open_new_at": first.isoformat(),
    }


def margin_percent(revenue_paise: int, cost_paise: int) -> float:
    """Margin as a percentage of revenue.

    Zero revenue returns 0.0 rather than dividing — an unpriced clinic is not
    infinitely unprofitable, it is simply not yet earning.
    """
    if revenue_paise <= 0:
        return 0.0
    return round(((revenue_paise - cost_paise) / revenue_paise) * 100.0, 2)


# ── Database reads ───────────────────────────────────────────────────────────


async def fetch_billing_rates() -> dict[str, dict]:
    """Per-clinic rate overrides, keyed by clinic_id.

    Returns {} on failure: a missing override table must degrade to "everyone
    is on their plan default", never take the whole finance panel down.
    """
    from app.database import sb, supabase

    try:
        res = await sb(
            # unscoped: platform super-admin reading owner-only billing rates
            supabase.table("platform_billing_rates").select("*")
        )
        return {r["clinic_id"]: r for r in (res.data or []) if r.get("clinic_id")}
    except Exception as e:
        logger.warning(f"Billing rate overrides unavailable: {e}")
        return {}


async def fetch_expenses(include_closed: bool = True) -> list[dict]:
    """All expense rows. Filtering to a month is select_month_expenses()'s job."""
    from app.database import sb, supabase

    try:
        res = await sb(
            # unscoped: platform super-admin reading owner-only expenses
            supabase.table("platform_expenses").select("*").order("category")
        )
        rows = res.data or []
    except Exception as e:
        logger.warning(f"Platform expenses unavailable: {e}")
        return []

    if include_closed:
        return rows

    today = _ist_today()
    live = []
    for r in rows:
        ends = _parse_date(r.get("effective_to"))
        if ends and ends < today:
            continue
        live.append(r)
    return live


async def fetch_invoices(month: Optional[str] = None) -> list[dict]:
    """Invoices, optionally for one month."""
    from app.database import sb, supabase

    try:
        query = (
            # unscoped: platform super-admin reading owner-only invoices
            supabase.table("platform_invoices").select("*")
        )
        if month:
            query = query.eq("period_month", month)
        res = await sb(query.order("period_month", desc=True))
        return res.data or []
    except Exception as e:
        logger.warning(f"Platform invoices unavailable: {e}")
        return []


async def meta_cost_by_clinic(month: str) -> dict[str, int]:
    """Actual Meta message spend per clinic for a calendar month, in paise.

    Reuses message_accounting's pricing cache and its paginated ledger scan so
    the books and the WhatsApp Usage card cannot disagree about either the
    rates or the row count. Scoped to the IST calendar month rather than the
    rolling 30-day window that card uses, because an invoice covers a month.

    Returns {} on failure — profit then reads as revenue with no message cost
    deducted, which is visibly wrong on the dashboard rather than quietly low.
    """
    from app.services.message_accounting import _get_pricing, scan_outbound_ledger

    start_iso, end_iso = month_window_iso(month)

    try:
        pricing = await _get_pricing()
        rows = await scan_outbound_ledger(
            start_iso=start_iso,
            end_iso=end_iso,
            columns="clinic_id, category",
        )
    except Exception as e:
        logger.warning(f"Meta cost per clinic unavailable for {month}: {e}")
        return {}

    rate = {
        "utility": pricing.get("utility_paise", 12),
        "marketing": pricing.get("marketing_paise", 75),
        "authentication": pricing.get("authentication_paise", 10),
        "service": pricing.get("service_paise", 0),
    }

    cost: dict[str, int] = {}
    for row in rows:
        cid = row.get("clinic_id")
        if not cid:
            continue
        cost[cid] = cost.get(cid, 0) + rate.get(row.get("category") or "utility", 0)
    return cost


# ── The rollup ───────────────────────────────────────────────────────────────


async def finance_summary(month: Optional[str] = None) -> dict:
    """The whole P&L for one month: per clinic, and platform-wide.

    PER-CLINIC PROFIT DEDUCTS ONLY THAT CLINIC'S MESSAGE COST. Render and
    Supabase are genuinely shared and any split of them across clinics is an
    accounting choice, not a fact; allocating them would make a small clinic
    look unprofitable because of a bill it did not cause. They are subtracted
    once, platform-wide, where they actually belong.

    TWO PROFIT LINES, BOTH REAL:
      net_profit  = expected MRR      - expenses  (accrual: what the month earned)
      cash_profit = actually collected - expenses  (cash: what reached the bank)
    They differ by exactly what clinics owe but have not paid. Showing only the
    first is how a business feels profitable while running out of money.
    """
    from app.database import sb, supabase
    from app.routers.platform import _billable_locations, _fetch_clinic_branch_counts
    from app.services.message_accounting import _get_plan_tiers

    month = month or current_month()
    if not is_valid_month(month):
        raise ValueError(f"Invalid month '{month}' — expected YYYY-MM")

    try:
        clinics_res = await sb(
            # unscoped: platform_admin
            supabase.table("clinics").select("id, name, plan, is_active")
        )
        clinics = clinics_res.data or []
    except Exception as e:
        logger.error(f"Finance summary could not read clinics: {e}")
        return {"success": False, "error": "Could not read clinics", "month": month}

    plan_tiers = await _get_plan_tiers()
    overrides = await fetch_billing_rates()
    branch_census = await _fetch_clinic_branch_counts()
    meta_costs = await meta_cost_by_clinic(month)
    invoices = {i["clinic_id"]: i for i in await fetch_invoices(month) if i.get("clinic_id")}
    expense_rows = await fetch_expenses()

    recurring, one_off = select_month_expenses(expense_rows, month)
    fixed_expense_paise = sum(int(r.get("amount_paise") or 0) for r in recurring + one_off)

    rows = []
    mrr_paise = 0
    total_meta_paise = 0

    for c in clinics:
        cid = c.get("id")
        if not cid:
            continue
        plan = c.get("plan")
        active = c.get("is_active", True) is not False

        rate = resolve_rate(plan, overrides.get(cid), plan_tiers)
        locations = _billable_locations((branch_census.get(cid) or {}).get("active", 0))

        # An inactive clinic is not billed. Its row still appears — the owner
        # needs to see that it stopped earning, not have it vanish.
        revenue = (
            invoice_amount_paise(rate["rate_paise"], rate["billing_mode"], locations)
            if active else 0
        )
        meta = meta_costs.get(cid, 0)

        mrr_paise += revenue
        total_meta_paise += meta

        inv = invoices.get(cid)
        rows.append({
            "clinic_id": cid,
            "clinic_name": c.get("name"),
            "plan": plan,
            "is_active": active,
            "locations_billed": locations,
            "rate_paise": rate["rate_paise"],
            "rate_source": rate["source"],
            "billing_mode": rate["billing_mode"],
            "rate_notes": rate["notes"],
            "revenue_paise": revenue,
            "meta_cost_paise": meta,
            "net_paise": revenue - meta,
            "margin_percent": margin_percent(revenue, meta),
            "invoice": {
                "id": inv.get("id"),
                "status": inv.get("status"),
                "amount_paise": inv.get("amount_paise"),
                "amount_paid_paise": inv.get("amount_paid_paise", 0),
                "paid_at": inv.get("paid_at"),
            } if inv else None,
        })

    rows.sort(key=lambda r: r["revenue_paise"], reverse=True)

    # Collections come from the invoice table, never from the rate table: a
    # rate is what was asked for, an invoice payment is what arrived.
    billed_paise = sum(int(i.get("amount_paise") or 0) for i in invoices.values())
    collected_paise = sum(int(i.get("amount_paid_paise") or 0) for i in invoices.values())
    outstanding_paise = sum(
        max(0, int(i.get("amount_paise") or 0) - int(i.get("amount_paid_paise") or 0))
        for i in invoices.values()
        if i.get("status") != "waived"
    )

    total_expense_paise = fixed_expense_paise + total_meta_paise

    status_counts = {s: 0 for s in INVOICE_STATUSES}
    for i in invoices.values():
        st = i.get("status")
        if st in status_counts:
            status_counts[st] += 1

    return {
        "success": True,
        "month": month,
        "is_current_month": month == current_month(),
        "totals": {
            "mrr_paise": mrr_paise,
            "billed_paise": billed_paise,
            "collected_paise": collected_paise,
            "outstanding_paise": outstanding_paise,
            "meta_cost_paise": total_meta_paise,
            "fixed_expense_paise": fixed_expense_paise,
            "total_expense_paise": total_expense_paise,
            "net_profit_paise": mrr_paise - total_expense_paise,
            "cash_profit_paise": collected_paise - total_expense_paise,
            "margin_percent": margin_percent(mrr_paise, total_expense_paise),
            "active_clinics": sum(1 for r in rows if r["is_active"]),
        },
        "clinics": rows,
        "expenses": {
            "recurring": recurring,
            "one_off": one_off,
            "fixed_total_paise": fixed_expense_paise,
            "meta_total_paise": total_meta_paise,
        },
        "invoice_summary": {
            "count": len(invoices),
            **status_counts,
        },
    }
