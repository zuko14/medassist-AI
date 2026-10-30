"""IPD finance (G5, migration 116): deposits, running bill, packages, discount
approval, credit / corporate bills, receivables.

Every money or state change is one ipd_* SQL function (or one ledger insert
whose trigger checks it) under the admission row lock; this module validates,
calls them, maps their ipd_* codes (app.services.ipd.raise_for) and builds the
read models. Spec: docs/hospital-os/G5-IPD-FINANCE-SPEC.md.

Zero-LLM: imports nothing from the AI, voice or assistant packages.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services.ipd import DEFAULT_SETTINGS, _actor, _people, raise_for, rpc
from app.services.opd_billing import resolve_counter_shift
from app.utils.helpers import actor_uuid

logger = logging.getLogger(__name__)

APPROVER_PERMISSION = "IPD_ADMIN"


def _p(clinic_id: str, user: Any, **params) -> dict:
    aid, name = _actor(user)
    return {"p_clinic_id": clinic_id, **{f"p_{k}": v for k, v in params.items()},
            "p_actor_id": aid, "p_actor_name": name}


# ── running bill ────────────────────────────────────────────────────────────

async def running_bill(clinic_id: str, admission_id: str) -> dict:
    """Charges so far, package cover, approved discount, deposits and what is
    payable - the same line rule the final bill uses (ipd_bill_lines)."""
    return await rpc("ipd_running_bill", {"p_clinic_id": clinic_id, "p_admission_id": admission_id})


# ── deposits ────────────────────────────────────────────────────────────────

async def list_deposits(clinic_id: str, admission_id: str) -> list[dict]:
    return await fetch_all(lambda: scoped_query("ipd_deposits", clinic_id).eq("admission_id", admission_id)
                           .order("created_at").order("id"), label="ipd_finance:deposits")


async def _insert_deposit_row(clinic_id: str, row: dict) -> dict:
    """Insert one ledger row; a retried click (same key) returns the first row."""
    async def existing() -> Optional[dict]:
        res = await sb(scoped_query("ipd_deposits", clinic_id).eq("idempotency_key", row["idempotency_key"]).limit(1))
        return res.data[0] if res.data else None

    first = await existing()
    if first:
        if first["admission_id"] != row["admission_id"] or first["kind"] != row["kind"]:
            raise HTTPException(status_code=409, detail="This request key was already used for another entry.")
        return {**first, "replayed": True}
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("ipd_deposits").insert(row))
    except Exception as e:
        if "uq_ipd_deposits_idempotency" in str(e):   # a concurrent retry won the race
            first = await existing()
            if first:
                return {**first, "replayed": True}
        if "violates check constraint" in str(e) and "ipd_" not in str(getattr(e, "message", "") or ""):
            raise HTTPException(status_code=422, detail="Some values are outside the allowed range or incomplete.")
        raise_for(e)
    return res.data[0]


async def take_deposit(clinic_id: str, admission_id: str, body: dict, user: Any) -> dict:
    """Money in against the stay, into the receiver's cashier shift (DEP- number)."""
    shift_id = await resolve_counter_shift(clinic_id, user, body.get("shift_id"), require_drawer=True)
    aid, name = _actor(user)
    return await _insert_deposit_row(clinic_id, {
        "clinic_id": clinic_id, "admission_id": admission_id, "kind": "deposit", "mode": body["mode"],
        "amount_paise": body["amount_paise"], "reference": body.get("reference"), "shift_id": shift_id,
        "received_by_admin_id": aid, "received_by_name": name, "idempotency_key": body["idempotency_key"],
    })


async def refund_deposit(clinic_id: str, admission_id: str, body: dict, user: Any) -> dict:
    """Unused deposit back to the patient, out of the refunder's shift (DRF- number).
    The ledger trigger refuses more than the unused balance, under the stay lock."""
    shift_id = await resolve_counter_shift(clinic_id, user, body.get("shift_id"), require_drawer=True)
    aid, name = _actor(user)
    return await _insert_deposit_row(clinic_id, {
        "clinic_id": clinic_id, "admission_id": admission_id, "kind": "refund", "mode": body["mode"],
        "amount_paise": body["amount_paise"], "reference": body.get("reference"), "reason": body["reason"],
        "shift_id": shift_id, "received_by_admin_id": aid, "received_by_name": name,
        "idempotency_key": body["idempotency_key"],
    })


async def apply_deposits(clinic_id: str, admission_id: str, user: Any) -> dict:
    return await rpc("ipd_apply_deposits", _p(clinic_id, user, admission_id=admission_id))


# ── packages ────────────────────────────────────────────────────────────────

async def list_packages(clinic_id: str, active_only: bool = False) -> list[dict]:
    def build():
        q = scoped_query("ipd_packages", clinic_id)
        return (q.eq("is_active", True) if active_only else q).order("code").order("id")
    return await fetch_all(build, label="ipd_finance:packages")


async def create_package(clinic_id: str, body: dict, user: Any) -> dict:
    row = {**body, "clinic_id": clinic_id, "created_by": actor_uuid(user)}
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("ipd_packages").insert(row))
    except Exception as e:
        if "ipd_packages_code_key" in str(e):
            raise HTTPException(status_code=409, detail="A package with this code already exists.")
        if "violates check constraint" in str(e):
            raise HTTPException(status_code=422, detail="Some values are outside the allowed range or incomplete.")
        raise_for(e)
    return res.data[0]


async def update_package(clinic_id: str, package_id: str, patch: dict) -> dict:
    """Master edits never re-price a stay: each stay keeps the snapshot it was given."""
    try:
        res = await sb(supabase.table("ipd_packages").update(patch).eq("clinic_id", clinic_id).eq("id", package_id))
    except Exception as e:
        if "violates check constraint" in str(e):
            raise HTTPException(status_code=422, detail="Some values are outside the allowed range or incomplete.")
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Package not found.")
    return res.data[0]


async def apply_package(clinic_id: str, admission_id: str, package_id: str, user: Any) -> dict:
    return await rpc("ipd_apply_package", _p(clinic_id, user, admission_id=admission_id, package_id=package_id))


async def remove_package(clinic_id: str, admission_id: str, reason: str, user: Any) -> dict:
    return await rpc("ipd_remove_package", _p(clinic_id, user, admission_id=admission_id, reason=reason))


# ── discounts ───────────────────────────────────────────────────────────────

def _is_clinic_admin(user: Any) -> bool:
    return getattr(user, "role", None) in ("clinic_admin", "super_admin")


def approval_limit(user: Any, settings: dict) -> Optional[int]:
    """The most this login may grant without a second person. None = no cap.

    Clinic admins: no cap. IPD_ADMIN: discount_approver_limit_paise (None = no cap).
    Other billing staff: discount_auto_approve_paise (default 0 = every discount
    needs an approver)."""
    s = {**DEFAULT_SETTINGS, **(settings or {})}
    if _is_clinic_admin(user):
        return None
    if APPROVER_PERMISSION in (getattr(user, "permissions", None) or []):
        return s.get("discount_approver_limit_paise")
    return int(s.get("discount_auto_approve_paise") or 0)


def _within(limit: Optional[int], amount: int) -> bool:
    return limit is None or amount <= limit


async def request_discount(clinic_id: str, admission_id: str, body: dict, settings: dict, user: Any) -> dict:
    """Recorded as approved at once when within the requester's own limit,
    otherwise as a request an approver decides (requester != approver)."""
    auto = _within(approval_limit(user, settings), body["amount_paise"])
    return await rpc("ipd_request_discount", _p(
        clinic_id, user, admission_id=admission_id, amount_paise=body["amount_paise"], reason=body["reason"],
        auto_approve=auto, idempotency_key=body["idempotency_key"]))


async def get_discount(clinic_id: str, discount_id: str) -> dict:
    res = await sb(scoped_query("ipd_discounts", clinic_id).eq("id", discount_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Discount not found.")
    return res.data[0]


async def decide_discount(clinic_id: str, discount: dict, decision: str, note: Optional[str], settings: dict,
                          user: Any) -> dict:
    if decision == "approved":
        if not (_is_clinic_admin(user) or APPROVER_PERMISSION in (getattr(user, "permissions", None) or [])):
            raise HTTPException(status_code=403, detail="Missing permission: IPD_ADMIN")
        if not _within(approval_limit(user, settings), discount["amount_paise"]):
            raise HTTPException(status_code=403, detail="This discount is above your approval limit. "
                                                        "A clinic administrator must approve it.")
    return await rpc("ipd_decide_discount", _p(clinic_id, user, discount_id=discount["id"], decision=decision,
                                               note=note))


async def list_pending_discounts(clinic_id: str, branch_id: Optional[str] = None) -> list[dict]:
    rows = await fetch_all(lambda: scoped_query("ipd_discounts", clinic_id).eq("status", "requested")
                           .order("requested_at").order("id"), label="ipd_finance:pending_discounts")
    return await _with_stay(clinic_id, rows, branch_id)


# ── credit / corporate ─────────────────────────────────────────────────────

async def authorize_credit(clinic_id: str, admission_id: str, body: dict, user: Any) -> dict:
    return await rpc("ipd_authorize_credit", _p(
        clinic_id, user, admission_id=admission_id, payer_name=body["payer_name"], reference=body["reference"],
        amount_paise=body["amount_paise"], notes=body.get("notes")))


async def revoke_credit(clinic_id: str, credit_id: str, reason: str, user: Any) -> dict:
    return await rpc("ipd_revoke_credit", _p(clinic_id, user, credit_id=credit_id, reason=reason))


async def get_credit(clinic_id: str, credit_id: str) -> dict:
    res = await sb(scoped_query("ipd_credit_authorizations", clinic_id).eq("id", credit_id).limit(1))
    if not res.data:
        raise HTTPException(status_code=404, detail="Credit authorisation not found.")
    return res.data[0]


# ── cross-stay read models: receivables, deposits held ─────────────────────

async def _rows_in(table: str, clinic_id: str, cols: str, column: str, ids) -> list[dict]:
    ids = sorted({str(i) for i in ids if i})
    out: list[dict] = []
    for i in range(0, len(ids), 100):   # batches keep the request line short
        chunk = ids[i:i + 100]
        out += await fetch_all(lambda c=chunk: scoped_query(table, clinic_id, cols).in_(column, c).order("id"),
                               label=f"ipd_finance:{table}")
    return out


async def _with_stay(clinic_id: str, rows: list[dict], branch_id: Optional[str]) -> list[dict]:
    """Attach admission number, patient and payer; drop other branches' stays."""
    stays = {a["id"]: a for a in await _rows_in("ipd_admissions", clinic_id,
                                                "id, admission_number, branch_id, patient_id, family_member_id, "
                                                "payer_type, payer_name, status", "id",
                                                [r["admission_id"] for r in rows])}
    people = await _people(clinic_id, list(stays.values()))
    out = []
    for r in rows:
        a = stays.get(r["admission_id"])
        if not a or (branch_id and a.get("branch_id") not in (None, branch_id)):
            continue
        out.append({**r, "admission_number": a.get("admission_number"), "admission_status": a.get("status"),
                    "payer_type": a.get("payer_type"), "payer_name": a.get("payer_name"),
                    **{k: v for k, v in people.get(a["id"], {}).items() if k in ("patient_name", "mrn")}})
    return out


def _bucket(days: int) -> str:
    return "0-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"


async def receivables(clinic_id: str, branch_id: Optional[str] = None) -> dict:
    """Issued inpatient bills with money still due, aged from the issue date, with the
    payer that authorised credit; plus stays holding unused deposits."""
    def bills_q():
        q = scoped_query("opd_invoices", clinic_id, "id, admission_id, branch_id, invoice_number, status, total_paise, "
                                                   "paid_paise, issued_at").not_.is_("admission_id", "null") \
            .in_("status", ["issued", "partially_paid"])
        return (q.eq("branch_id", branch_id) if branch_id else q).order("issued_at").order("id")

    bills = await fetch_all(bills_q, label="ipd_finance:receivables")
    credits = {c["admission_id"]: c for c in await _rows_in(
        "ipd_credit_authorizations", clinic_id, "id, admission_id, payer_name, reference, amount_paise, status",
        "admission_id", [b["admission_id"] for b in bills]) if c["status"] == "active"}
    now = datetime.now(timezone.utc)
    rows, buckets = [], {"0-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    for b in await _with_stay(clinic_id, bills, None):
        due = (b.get("total_paise") or 0) - (b.get("paid_paise") or 0)
        issued = datetime.fromisoformat(str(b["issued_at"]).replace("Z", "+00:00")) if b.get("issued_at") else now
        days = max(0, (now - issued).days)
        buckets[_bucket(days)] += due
        credit = credits.get(b["admission_id"])
        rows.append({**b, "due_paise": due, "age_days": days, "bucket": _bucket(days),
                     "credit": {k: credit[k] for k in ("payer_name", "reference", "amount_paise")} if credit else None})

    def dep_q():
        q = scoped_query("ipd_deposits", clinic_id, "id, admission_id, kind, amount_paise, branch_id")
        return (q.eq("branch_id", branch_id) if branch_id else q).order("id")

    balance: dict[str, int] = {}
    for d in await fetch_all(dep_q, label="ipd_finance:deposits_held"):
        sign = 1 if d["kind"] in ("deposit", "unapplied") else -1
        balance[d["admission_id"]] = balance.get(d["admission_id"], 0) + sign * d["amount_paise"]
    held = await _with_stay(clinic_id, [{"admission_id": k, "balance_paise": v} for k, v in balance.items() if v],
                            None)
    return {"bills": rows, "ageing_paise": buckets, "total_due_paise": sum(buckets.values()),
            "deposits_held": held, "deposits_held_paise": sum(h["balance_paise"] for h in held)}
