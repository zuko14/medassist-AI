"""Insurance / TPA OS (G6, migration 124): payers, policies, cashless
pre-authorisation, the dual-debtor claim and its settlement.

The final bill stays the one GST tax invoice (G4). Filing the claim splits it:
total_bill = patient_payable + claimed (payer share), the payer share capped by
the latest approval letter. The patient pays their share at the counter
(deposits never pay the payer's share: ins_payer_hold); the payer settles by
NEFT ('insurance' receipt), TDS ('tds' receipt) and reason-coded disallowances,
which are later written off (a G4 credit note) or recovered from the patient.

Every state change is one SQL function (ins_*); the database refuses direct
writes and keeps the payer ledger append-only. This module validates, calls
them, maps their codes to HTTP errors and builds the read models.
Zero-LLM: imports nothing from the AI, voice or assistant packages.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.database import fetch_all, restrict_to_branch, sb, scoped_query, supabase
from app.services import ipd, orders
from app.services.opd_billing import issue_invoice
from app.utils.helpers import actor_uuid

logger = logging.getLogger(__name__)

PAYER_TYPES = ("tpa", "private_insurance", "government_scheme", "corporate")
DISALLOWANCE_REASONS = ("non_payable_item", "room_rent_proportionate", "copay", "policy_limit", "excess_over_tariff",
                        "documentation_incomplete", "not_medically_necessary", "repudiated", "other")
LIVE_PRE_AUTH = ("draft", "submitted", "query_raised", "approved", "enhancement_requested")
OPEN_CLAIM = ("filed", "in_process", "query_raised")

# error code (prefix before ':') -> (HTTP status, message shown to staff)
_ERRORS: dict[str, tuple[int, str]] = {
    "ins_pre_auth_exists": (409, "This stay already has a live pre-authorisation. Update it instead."),
    "ins_policy_not_found": (404, "Insurance policy not found."),
    "ins_policy_other_patient": (409, "That policy belongs to another patient."),
    "ins_policy_not_valid": (409, "The policy is inactive or not valid on the admission date."),
    "ins_payer_inactive": (409, "This payer is deactivated. Reactivate it in the payer master first."),
    "ins_pre_auth_not_found": (404, "Pre-authorisation not found."),
    "ins_pre_auth_claimed": (409, "A claim is filed on this pre-authorisation; it can no longer change."),
    "ins_bad_transition": (409, "The pre-authorisation or claim is not in a state that allows this step. Reload it."),
    "ins_note_required": (422, "A note / reason (3+ characters) is required for this step."),
    "ins_approval_ref_required": (422, "Enter the insurer's approval reference number."),
    "ins_amount_required": (422, "Enter a positive amount."),
    "ins_amount_too_large": (422, "The amount is too large."),
    "ins_approval_exceeds_request": (422, "The approved amount cannot exceed what was requested. Request an "
                                          "enhancement first."),
    "ins_enhancement_below_approved": (422, "An enhancement letter states the new total approved, which cannot be "
                                            "below the amount already approved."),
    "ins_no_pre_auth": (409, "This stay has no live pre-authorisation."),
    "ins_enhancement_pending": (409, "An enhancement is pending with the insurer. Record its decision before "
                                     "filing the claim."),
    "ins_pre_auth_not_approved": (409, "The pre-authorisation is not approved yet."),
    "ins_bill_not_issued": (409, "Issue the final bill before filing the claim."),
    "ins_nothing_to_claim": (422, "Non-payable items and co-pay cover the whole bill: nothing to claim."),
    "ins_patient_overpaid": (409, "The patient has already paid more than their share. Refund the excess at the "
                                  "billing desk, then file the claim."),
    "ins_claim_not_found": (404, "Claim not found."),
    "ins_claim_closed": (409, "This claim is already closed."),
    "ins_claim_has_settlement": (409, "Money or deductions are already recorded on this claim; it cannot be "
                                      "withdrawn."),
    "ins_key_reused": (409, "This request key was already used for another claim. Reload and try again."),
    "ins_utr_required": (422, "Enter the UTR / NEFT reference of the payment."),
    "ins_future_date": (422, "The settlement date cannot be in the future."),
    "ins_disallowance_reason_required": (422, "Every disallowance needs a positive amount and a reason code."),
    "ins_exceeds_outstanding": (422, "Received + TDS + disallowed exceeds what the payer still owes on this claim."),
    "ins_disallowance_not_found": (404, "Disallowance not found."),
    "ins_record_locked": (409, "This record is final and cannot be changed."),
    "ins_append_only": (409, "Insurance records cannot be deleted."),
    "ins_via_function_only": (409, "This record cannot be changed this way."),
    "ins_claim_open_on_bill": (409, "A live insurance claim is filed on this bill. Withdraw the claim first."),
    "ins_receipt_via_function_only": (409, "Insurance receipts are recorded from the claim's settlement."),
    "ins_receipt_exceeds_patient_share": (409, "This is a cashless bill: collect only the patient's share. The rest is "
                                               "the insurer's (reason = the most the patient owes, in paise)."),
}


def raise_for(exc: Exception) -> None:
    """ins_* codes here; ipd_* to the IPD mapper; opd_* (credit note, invoice)
    and constraint errors to the orders mapper. Never the raw database text."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code in sorted(_ERRORS, key=len, reverse=True):
        idx = msg.find(code)
        if idx >= 0:
            status, message = _ERRORS[code]
            tail = msg[idx + len(code):]
            reason = tail[1:].split()[0].strip("'\",}") if tail.startswith(":") else None
            raise HTTPException(status_code=status, detail={"code": code, "message": message, "reason": reason})
    if "ipd_" in msg:
        ipd.raise_for(exc)
    orders.raise_for(exc)


async def _rpc(name: str, params: dict) -> Any:
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:  # postgrest APIError carries the ins_* code
        raise_for(e)
    return res.data


def _actor(user: Any) -> dict:
    return {"p_actor_id": actor_uuid(user), "p_actor_name": str(getattr(user, "username", None) or "system")[:100]}


async def _one(table: str, clinic_id: str, row_id: Optional[str], cols: str = "*") -> dict:
    if not row_id:
        return {}
    res = await sb(scoped_query(table, clinic_id, cols).eq("id", row_id).limit(1))
    return res.data[0] if res.data else {}


# ── payers ──────────────────────────────────────────────────────────────────

_PAYER_FIELDS = ("name", "payer_type", "tpa_code", "portal_url", "contact_email", "contact_phone", "is_active",
                 "branch_id")


async def list_payers(clinic_id: str, include_inactive: bool = True) -> list[dict]:
    """The clinic's payer master. A clinic created after migration 124 gets the
    12 common Indian payers on its first read (idempotent)."""
    def build():
        q = scoped_query("insurance_payers", clinic_id)
        return (q if include_inactive else q.eq("is_active", True)).order("name").order("id")
    rows = await fetch_all(build, label="insurance:payers")
    if not rows:
        await _rpc("insurance_seed_payers", {"p_clinic_id": clinic_id})
        rows = await fetch_all(build, label="insurance:payers")
    return rows


async def create_or_update_payer(clinic_id: str, data: dict, payer_id: Optional[str] = None) -> dict:
    row = {k: data[k] for k in _PAYER_FIELDS if k in data}
    if "name" in row:
        row["name"] = row["name"].strip()
    try:
        if payer_id:
            res = await sb(supabase.table("insurance_payers").update(row)
                           .eq("clinic_id", clinic_id).eq("id", payer_id))
        else:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("insurance_payers").insert({**row, "clinic_id": clinic_id}))
    except Exception as e:
        if "insurance_payers_clinic_name_key" in str(e):
            raise HTTPException(status_code=409, detail="A payer with this name already exists.")
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Payer not found.")
    return res.data[0]


# ── policies ────────────────────────────────────────────────────────────────

_POLICY_FIELDS = ("payer_id", "family_member_id", "policy_number", "member_id", "corporate_name", "sum_insured_paise",
                  "valid_from", "valid_to", "is_active")


async def attach_policy(clinic_id: str, patient_id: str, data: dict) -> dict:
    """Attach (or, for the same payer + policy number, update) an insurance card."""
    if not await _one("patients", clinic_id, patient_id, "id"):
        raise HTTPException(status_code=404, detail="Patient not found.")
    payer = await _one("insurance_payers", clinic_id, data["payer_id"], "id, is_active")
    if not payer:
        raise HTTPException(status_code=404, detail="Payer not found.")
    if data.get("family_member_id") and not (await _one("family_members", clinic_id, data["family_member_id"],
                                                        "id, patient_id")).get("patient_id") == patient_id:
        raise HTTPException(status_code=404, detail="Family member not found for this patient.")
    row = {k: (str(data[k]) if isinstance(data.get(k), date) else data.get(k)) for k in _POLICY_FIELDS if k in data}
    row["policy_number"] = row["policy_number"].strip()
    row["member_id"] = row["member_id"].strip()
    existing = await sb(scoped_query("patient_insurance_policies", clinic_id, "id").eq("patient_id", patient_id)
                        .eq("payer_id", row["payer_id"]).eq("policy_number", row["policy_number"]).limit(1))
    try:
        if existing.data:
            res = await sb(supabase.table("patient_insurance_policies").update(row)
                           .eq("clinic_id", clinic_id).eq("id", existing.data[0]["id"]))
        else:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("patient_insurance_policies").insert(
                {**row, "clinic_id": clinic_id, "patient_id": patient_id}))
    except Exception as e:
        raise_for(e)
    return res.data[0]


async def list_policies(clinic_id: str, patient_id: str) -> list[dict]:
    rows = await fetch_all(lambda: scoped_query("patient_insurance_policies", clinic_id).eq("patient_id", patient_id)
                           .order("is_active", desc=True).order("created_at", desc=True).order("id"),
                           label="insurance:policies")
    payers = {p["id"]: p for p in await orders._rows_in("insurance_payers", clinic_id, "id, name, payer_type, tpa_code",
                                                        "id", [r["payer_id"] for r in rows])}
    today = date.today().isoformat()
    return [{**r, "payer": payers.get(r["payer_id"]),
             "is_valid_today": bool(r["is_active"] and (not r.get("valid_from") or str(r["valid_from"]) <= today)
                                    and (not r.get("valid_to") or str(r["valid_to"]) >= today))} for r in rows]


# ── pre-authorisation ───────────────────────────────────────────────────────

async def create_pre_auth(clinic_id: str, admission_id: str, data: dict, user: Any) -> dict:
    return await _rpc("ins_create_pre_auth", {"p_clinic_id": clinic_id, "p_admission_id": admission_id,
                                              "p_row": data, **_actor(user)})


async def get_pre_auth(clinic_id: str, pre_auth_id: str) -> dict:
    row = await _one("ipd_pre_authorizations", clinic_id, pre_auth_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "ins_pre_auth_not_found",
                                                     "message": _ERRORS["ins_pre_auth_not_found"][1]})
    return row


async def record_pre_auth_decision(clinic_id: str, pre_auth_id: str, action: str, data: dict, user: Any) -> dict:
    """One step of the state machine (submit / query / approve / reject /
    request_enhancement / deny_enhancement / cancel). An approval states the
    TOTAL approved per the insurer's latest letter."""
    return await _rpc("ins_pre_auth_transition", {"p_clinic_id": clinic_id, "p_pre_auth_id": pre_auth_id,
                                                  "p_action": action, "p_row": data, **_actor(user)})


# ── the cashless bill and the claim ─────────────────────────────────────────

async def generate_cashless_final_bill(clinic_id: str, admission_id: str, non_payable_paise: int, copay_paise: int,
                                       user: Any) -> dict:
    """Generate the stay's final bill (G5) if it has none, issue it (G4: GST
    invoice for every service), then file the claim that splits it between the
    payer and the patient. Each step is idempotent, so a retry continues where
    a failure stopped."""
    bill = await sb(scoped_query("opd_invoices", clinic_id, "id, status").eq("admission_id", admission_id)
                    .neq("status", "void").limit(1))
    if not bill.data:
        out = await ipd.generate_final_bill(clinic_id, admission_id, user)
        invoice_id = out["invoice_id"]
        status = "draft"
    else:
        invoice_id, status = bill.data[0]["id"], bill.data[0]["status"]
    if status == "draft":
        await issue_invoice(clinic_id, invoice_id, user)
    claim = await _rpc("ins_file_claim", {"p_clinic_id": clinic_id, "p_admission_id": admission_id,
                                          "p_non_payable_paise": non_payable_paise, "p_copay_paise": copay_paise,
                                          **_actor(user)})
    return {**claim, "split": split_of(claim)}


def split_of(claim: dict) -> dict:
    """The dual-debtor view of a claim, in paise."""
    hold = (claim["claimed_amount_paise"] - claim["settled_amount_paise"] - claim["tds_deducted_paise"]
            - claim["written_off_paise"] - claim["recovered_from_patient_paise"])
    return {"total_bill_paise": claim["total_bill_paise"], "payer_payable_paise": claim["claimed_amount_paise"],
            "patient_payable_paise": claim["patient_payable_paise"] + claim["recovered_from_patient_paise"],
            "non_payable_paise": claim["non_payable_paise"], "copay_paise": claim["copay_paise"],
            "payer_received_paise": claim["settled_amount_paise"], "tds_paise": claim["tds_deducted_paise"],
            "disallowed_paise": claim["disallowed_amount_paise"], "written_off_paise": claim["written_off_paise"],
            "recovered_from_patient_paise": claim["recovered_from_patient_paise"],
            "payer_outstanding_paise": claim["claimed_amount_paise"] - claim["settled_amount_paise"]
            - claim["tds_deducted_paise"] - claim["disallowed_amount_paise"],
            "payer_hold_paise": hold}


async def get_claim(clinic_id: str, claim_id: str) -> dict:
    row = await _one("ipd_insurance_claims", clinic_id, claim_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "ins_claim_not_found",
                                                     "message": _ERRORS["ins_claim_not_found"][1]})
    return row


async def claim_detail(clinic_id: str, claim_id: str) -> dict:
    claim = await get_claim(clinic_id, claim_id)
    txns = await fetch_all(lambda: scoped_query("ipd_claim_transactions", clinic_id).eq("claim_id", claim_id)
                           .order("created_at").order("id"), label="insurance:txns")
    # one settlement writes its rows in one transaction (same timestamp): money first, then deductions
    txns.sort(key=lambda r: (r["created_at"], {"payment": 0, "tds": 1, "disallowance": 2}[r["kind"]]))
    invoice = await _one("opd_invoices", clinic_id, claim["invoice_id"],
                         "id, invoice_number, status, total_paise, credited_paise, paid_paise")
    pre_auth = await _one("ipd_pre_authorizations", clinic_id, claim["pre_auth_id"],
                          "id, pre_auth_number, approval_ref_number, total_approved_paise")
    payer = await _one("insurance_payers", clinic_id, claim["payer_id"], "id, name, payer_type")
    return {**claim, "split": split_of(claim), "transactions": txns, "invoice": invoice, "pre_auth": pre_auth,
            "payer": payer}


async def record_claim_settlement(clinic_id: str, claim_id: str, data: dict, idempotency_key: Optional[str],
                                  user: Any) -> dict:
    """NEFT received (UTR), TDS withheld and reason-coded disallowances, in one
    transaction: payer_payable = received + TDS + disallowed + still outstanding."""
    row = {**data, "settled_on": str(data["settled_on"]) if data.get("settled_on") else None}
    claim = await _rpc("ins_record_settlement", {"p_clinic_id": clinic_id, "p_claim_id": claim_id, "p_row": row,
                                                 "p_idempotency_key": idempotency_key, **_actor(user)})
    return {**claim, "split": split_of(claim)}


async def resolve_disallowance(clinic_id: str, txn_id: str, resolution: str, note: str, user: Any) -> dict:
    return await _rpc("ins_resolve_disallowance", {"p_clinic_id": clinic_id, "p_txn_id": txn_id,
                                                   "p_resolution": resolution, "p_note": note, **_actor(user)})


async def get_transaction(clinic_id: str, txn_id: str) -> dict:
    row = await _one("ipd_claim_transactions", clinic_id, txn_id)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "ins_disallowance_not_found",
                                                     "message": _ERRORS["ins_disallowance_not_found"][1]})
    return row


async def claim_transition(clinic_id: str, claim_id: str, action: str, note: Optional[str], user: Any) -> dict:
    return await _rpc("ins_claim_transition", {"p_clinic_id": clinic_id, "p_claim_id": claim_id, "p_action": action,
                                               "p_note": note, **_actor(user)})


# ── read models: stay card, pipeline, claims, ageing ────────────────────────

async def admission_insurance(clinic_id: str, admission_id: str) -> dict:
    """The cashless card (inpatient chart and the TPA desk's stay view): the
    patient's policies, the live pre-authorisation (with history from
    ipd_events), the claim, and the running bill's figures."""
    a = await ipd.get_admission_row(clinic_id, admission_id)
    pre = await fetch_all(lambda: scoped_query("ipd_pre_authorizations", clinic_id).eq("admission_id", admission_id)
                          .order("created_at", desc=True).order("id"), label="insurance:stay_preauth")
    live = next((p for p in pre if p["status"] in LIVE_PRE_AUTH), None)
    claims = await fetch_all(lambda: scoped_query("ipd_insurance_claims", clinic_id).eq("admission_id", admission_id)
                             .order("created_at", desc=True).order("id"), label="insurance:stay_claims")
    claim = next((c for c in claims if c["status"] != "withdrawn"), None)
    history = await fetch_all(lambda: scoped_query("ipd_events", clinic_id, "event_type, actor_name, details, occurred_at")
                              .eq("admission_id", admission_id).like("event_type", "preauth_%")
                              .order("occurred_at").order("id"), label="insurance:stay_history")
    payers = {p["id"]: p for p in await orders._rows_in("insurance_payers", clinic_id, "id, name, payer_type", "id",
                                                        [p["payer_id"] for p in pre])}
    who = (await ipd._people(clinic_id, [a])).get(a["id"], {})
    # the desk works the stay from here, without IPD billing rights: identity and the bill's figures only
    running = await _rpc("ipd_running_bill", {"p_clinic_id": clinic_id, "p_admission_id": admission_id})
    return {"admission_id": admission_id, "admission_number": a.get("admission_number"), "status": a.get("status"),
            "admission": {**{k: a.get(k) for k in ("id", "admission_number", "status", "patient_id", "family_member_id",
                                                  "provisional_diagnosis", "branch_id")},
                          "patient_name": who.get("patient_name"), "mrn": who.get("mrn")},
            "running": {k: running.get(k) for k in ("charged_paise", "tax_paise", "payable_paise", "bill", "insurance",
                                                    "deposits", "refundable_paise", "due_after_deposits_paise")},
            "policies": await list_policies(clinic_id, a["patient_id"]),
            "pre_auth": {**live, "payer": payers.get(live["payer_id"])} if live else None,
            "past_pre_auths": [{k: p[k] for k in ("id", "pre_auth_number", "status", "denial_reason", "cancel_reason")}
                               for p in pre if p is not live],
            "claim": {**claim, "split": split_of(claim)} if claim else None,
            "history": history}


async def _decorate(clinic_id: str, rows: list[dict]) -> list[dict]:
    stays = {s["id"]: s for s in await orders._rows_in("ipd_admissions", clinic_id,
                                                       "id, admission_number, patient_id, family_member_id, status, "
                                                       "admitted_at, branch_id", "id",
                                                       [r["admission_id"] for r in rows])}
    people = await ipd._people(clinic_id, list(stays.values()))
    payers = {p["id"]: p for p in await orders._rows_in("insurance_payers", clinic_id, "id, name, payer_type", "id",
                                                        [r["payer_id"] for r in rows])}
    out = []
    for r in rows:
        s = stays.get(r["admission_id"], {})
        who = people.get(r["admission_id"], {})
        out.append({**r, "admission_number": s.get("admission_number"), "admission_status": s.get("status"),
                    "patient_name": who.get("patient_name"), "mrn": who.get("mrn"),
                    "payer_name": (payers.get(r["payer_id"]) or {}).get("name"),
                    "payer_type": (payers.get(r["payer_id"]) or {}).get("payer_type")})
    return out


async def pre_auth_pipeline(clinic_id: str, branch_id: Optional[str] = None, status: Optional[str] = None) -> dict:
    """Live pre-authorisations, oldest waiting first (the desk chases insurers)."""
    statuses = [status] if status else list(LIVE_PRE_AUTH)

    def build():
        q = scoped_query("ipd_pre_authorizations", clinic_id).in_("status", statuses)
        return restrict_to_branch(q, branch_id).order("updated_at").order("id")
    rows = await _decorate(clinic_id, await fetch_all(build, label="insurance:pipeline"))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"items": rows, "counts": counts, "total": len(rows)}


async def list_claims(clinic_id: str, branch_id: Optional[str] = None, scope: str = "open") -> dict:
    """open: filed / in process / query raised (and closed claims with an
    unresolved disallowance); all: everything, newest first."""
    def build():
        q = scoped_query("ipd_insurance_claims", clinic_id)
        q = q.neq("status", "withdrawn") if scope == "all" else q
        return restrict_to_branch(q, branch_id).order("filed_at", desc=True).order("id")
    rows = await fetch_all(build, label="insurance:claims")
    if scope != "all":
        rows = [r for r in rows if r["status"] in OPEN_CLAIM
                or (r["status"] != "withdrawn"
                    and r["disallowed_amount_paise"] > r["written_off_paise"] + r["recovered_from_patient_paise"])]
    rows = await _decorate(clinic_id, rows)
    return {"items": [{**r, "split": split_of(r)} for r in rows], "total": len(rows)}


def ageing_bucket(days: int) -> str:
    return "lt_30" if days < 30 else "d30_60" if days < 60 else "d60_90" if days < 90 else "gt_90"


async def get_tpa_ageing_report(clinic_id: str, branch_id: Optional[str] = None,
                                now: Optional[datetime] = None) -> dict:
    """Outstanding payer receivables (claimed - received - TDS - disallowed),
    aged from the filing date, by payer: <30, 30-60, 60-90, >90 days."""
    now = now or datetime.now(timezone.utc)

    def build():
        q = scoped_query("ipd_insurance_claims", clinic_id, "id, payer_id, admission_id, claim_number, filed_at, "
                                                            "outstanding_paise, status, branch_id").gt("outstanding_paise", 0)
        return restrict_to_branch(q, branch_id).order("filed_at").order("id")
    claims = [c for c in await fetch_all(build, label="insurance:ageing") if c["status"] != "withdrawn"]
    payers = {p["id"]: p for p in await orders._rows_in("insurance_payers", clinic_id, "id, name, payer_type", "id",
                                                        [c["payer_id"] for c in claims])}
    empty = {"lt_30": 0, "d30_60": 0, "d60_90": 0, "gt_90": 0}
    by_payer: dict[str, dict] = {}
    totals = dict(empty)
    for c in claims:
        filed = datetime.fromisoformat(str(c["filed_at"]).replace("Z", "+00:00"))
        bucket = ageing_bucket(max(0, (now - filed).days))
        p = payers.get(c["payer_id"], {})
        row = by_payer.setdefault(c["payer_id"], {"payer_id": c["payer_id"], "payer_name": p.get("name"),
                                                  "payer_type": p.get("payer_type"), **empty, "total_paise": 0,
                                                  "claims": 0, "oldest_days": 0})
        row[bucket] += c["outstanding_paise"]
        row["total_paise"] += c["outstanding_paise"]
        row["claims"] += 1
        row["oldest_days"] = max(row["oldest_days"], max(0, (now - filed).days))
        totals[bucket] += c["outstanding_paise"]
    payers_out = sorted(by_payer.values(), key=lambda r: (-r["total_paise"], r["payer_name"] or ""))
    return {"as_of": now.isoformat(), "payers": payers_out, "totals": {**totals, "total_paise": sum(totals.values())},
            "claims": len(claims)}
