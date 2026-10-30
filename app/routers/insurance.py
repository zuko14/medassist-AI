"""Insurance / TPA OS API (G6, migration 124).

/admin/insurance/payers                     TPA / insurer master (12 common Indian payers seeded)
/admin/insurance/policies[/{patient_id}]    a patient's insurance cards
/admin/insurance/pre-auths                  pre-authorisation pipeline
/admin/insurance/claims[/{id}]              claims; .../settlement (UTR, TDS, disallowances), .../action
/admin/insurance/disallowances/{id}/resolve write off (GST credit note) or recover from the patient
/admin/insurance/ageing                     payer receivables: <30 / 30-60 / 60-90 / >90 days
/admin/ipd/admissions/{id}/insurance        the inpatient chart's cashless card
/admin/ipd/admissions/{id}/pre-auth         request pre-authorisation
/admin/ipd/pre-auth/{id}/decision           insurer's decision: approved / query_raised / rejected / enhancement_denied
/admin/ipd/pre-auth/{id}/action             submit / request_enhancement / cancel
/admin/ipd/admissions/{id}/cashless-bill    final bill issued + claim filed: the dual split

Permissions: INSURANCE_READ / INSURANCE_MANAGE (app.services.permissions.holds_insurance_permission);
writing off a disallowance also needs the credit-note grant. Entitlement: the IPD module.
Zero-LLM: nothing here imports the AI, voice or assistant packages.
"""

import logging
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.routers.admin import (
    AdminUser,
    enforce_branch_scope,
    enforce_clinic_access,
    log_admin_action,
    verify_credentials,
)
from app.routers.opd import _read_branch
from app.services import insurance, ipd
from app.services.permissions import holds_credit_note_permission, holds_insurance_permission
from app.services.tenant import get_clinic_by_id, ipd_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/insurance", tags=["Insurance / TPA"])
ipd_router = APIRouter(prefix="/admin/ipd", tags=["Insurance / TPA"])

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_MAX_PAISE = 100_000_000   # ₹10 lakh per amount, the bill-line ceiling elsewhere in the OS


# ── models ──────────────────────────────────────────────────────────────────

class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PayerIn(_In):
    name: str = Field(..., min_length=1, max_length=150)
    payer_type: Literal["tpa", "private_insurance", "government_scheme", "corporate"]
    tpa_code: Optional[str] = Field(None, max_length=50)
    portal_url: Optional[str] = Field(None, max_length=300, pattern=r"^https?://[^\s<>\"']+$")
    contact_email: Optional[str] = Field(None, max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    contact_phone: Optional[str] = Field(None, pattern=r"^[0-9+() -]{6,30}$")
    is_active: bool = True
    branch_id: Optional[str] = Field(None, pattern=_UUID)

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data):
        if isinstance(data, dict):
            for k in ("tpa_code", "portal_url", "contact_email", "contact_phone", "branch_id"):
                if isinstance(data.get(k), str) and not data[k].strip():
                    data[k] = None
        return data


class PolicyIn(_In):
    patient_id: str = Field(..., pattern=_UUID)
    family_member_id: Optional[str] = Field(None, pattern=_UUID)
    payer_id: str = Field(..., pattern=_UUID)
    policy_number: str = Field(..., min_length=1, max_length=80)
    member_id: str = Field(..., min_length=1, max_length=80)
    corporate_name: Optional[str] = Field(None, max_length=120)
    sum_insured_paise: Optional[int] = Field(None, ge=0, le=100_000_000_000)
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    is_active: bool = True

    @model_validator(mode="after")
    def _shape(self):
        if not self.policy_number.strip() or not self.member_id.strip():
            raise ValueError("Policy number and member / card id are required.")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not be before valid_from.")
        return self


class PreAuthIn(_In):
    policy_id: str = Field(..., pattern=_UUID)
    requested_paise: int = Field(..., ge=1, le=_MAX_PAISE)
    provisional_diagnosis: str = Field(..., min_length=3, max_length=500)
    treatment_plan: Optional[str] = Field(None, max_length=1000)
    expected_stay_days: Optional[int] = Field(None, ge=1, le=365)
    notes: Optional[str] = Field(None, max_length=1000)
    as_draft: bool = False


class DecisionIn(_In):
    decision: Literal["approved", "query_raised", "rejected", "enhancement_denied"]
    # approved: the TOTAL approved per the insurer's latest letter
    approved_amount_paise: Optional[int] = Field(None, ge=1, le=_MAX_PAISE)
    approval_ref: Optional[str] = Field(None, max_length=100)
    approval_letter_path: Optional[str] = Field(None, max_length=300)
    note: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _shape(self):
        if self.decision == "approved" and (not self.approved_amount_paise or not (self.approval_ref or "").strip()):
            raise ValueError("An approval needs the approved amount and the insurer's approval reference.")
        if self.decision in ("query_raised", "rejected", "enhancement_denied") and len((self.note or "").strip()) < 3:
            raise ValueError("Give the insurer's query / reason (3+ characters).")
        return self


class PreAuthActionIn(_In):
    action: Literal["submit", "request_enhancement", "cancel"]
    amount_paise: Optional[int] = Field(None, ge=1, le=_MAX_PAISE)   # request_enhancement: the extra amount
    note: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _shape(self):
        if self.action == "request_enhancement" and not self.amount_paise:
            raise ValueError("Enter the additional amount requested.")
        if self.action == "cancel" and len((self.note or "").strip()) < 3:
            raise ValueError("Give the reason for cancelling (3+ characters).")
        return self


class CashlessBillIn(_In):
    non_payable_paise: int = Field(0, ge=0, le=_MAX_PAISE)   # consumables, room-rent excess the policy excludes
    copay_paise: int = Field(0, ge=0, le=_MAX_PAISE)


class DisallowanceIn(_In):
    amount_paise: int = Field(..., ge=1, le=_MAX_PAISE)
    reason_code: Literal["non_payable_item", "room_rent_proportionate", "copay", "policy_limit", "excess_over_tariff",
                         "documentation_incomplete", "not_medically_necessary", "repudiated", "other"]
    note: Optional[str] = Field(None, max_length=300)

    @model_validator(mode="after")
    def _shape(self):
        if self.reason_code == "other" and len((self.note or "").strip()) < 3:
            raise ValueError("Describe the 'other' disallowance (3+ characters).")
        return self


class SettlementIn(_In):
    amount_received_paise: int = Field(0, ge=0, le=_MAX_PAISE)
    utr_number: Optional[str] = Field(None, min_length=4, max_length=100)
    tds_paise: int = Field(0, ge=0, le=_MAX_PAISE)
    settled_on: Optional[date] = None
    disallowances: list[DisallowanceIn] = Field(default_factory=list, max_length=20)
    idempotency_key: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9-]{8,72}$")

    @model_validator(mode="after")
    def _shape(self):
        if self.amount_received_paise and not (self.utr_number or "").strip():
            raise ValueError("Enter the UTR / NEFT reference of the payment.")
        if not (self.amount_received_paise or self.tds_paise or self.disallowances):
            raise ValueError("Enter the amount received, TDS or a disallowance.")
        return self


class ClaimActionIn(_In):
    action: Literal["query", "in_process", "withdraw"]
    note: Optional[str] = Field(None, max_length=1000)


class ResolveIn(_In):
    resolution: Literal["written_off", "recovered_from_patient"]
    note: str = Field(..., min_length=3, max_length=300)


# ── guards ──────────────────────────────────────────────────────────────────

async def _scope(user: AdminUser, clinic_id: str, perm: str) -> str:
    """Permission, tenant and the IPD module."""
    if not holds_insurance_permission(user, perm):
        raise HTTPException(status_code=403, detail=f"Missing permission: {perm}")
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not clinic or ipd_access(clinic) is None:
        raise HTTPException(status_code=403, detail="Insurance / TPA needs the IPD module.")
    return scope


async def _admission(user: AdminUser, scope: str, admission_id: str) -> dict:
    a = await ipd.get_admission_row(scope, admission_id)
    enforce_branch_scope(user, a.get("branch_id"))
    return a


async def _audit(user: AdminUser, request: Request, action: str, rtype: str, rid: Optional[str], details=None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid,
                           details=details or {}, ip_address=request.client.host if request.client else None)


# ── payers & policies ───────────────────────────────────────────────────────

@router.get("/payers")
async def list_payers(include_inactive: bool = True, clinic_id: str = "default",
                      user: AdminUser = Depends(verify_credentials)):
    return {"payers": await insurance.list_payers(await _scope(user, clinic_id, "INSURANCE_READ"), include_inactive)}


@router.post("/payers", status_code=201)
async def create_payer(body: PayerIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    row = await insurance.create_or_update_payer(scope, body.model_dump())
    await _audit(user, request, "INSURANCE_PAYER_CREATE", "insurance_payer", row["id"], {"name": row["name"]})
    return row


@router.put("/payers/{payer_id}")
async def update_payer(payer_id: str, body: PayerIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    row = await insurance.create_or_update_payer(scope, body.model_dump(), payer_id)
    await _audit(user, request, "INSURANCE_PAYER_UPDATE", "insurance_payer", payer_id,
                 {"name": row["name"], "is_active": row["is_active"]})
    return row


@router.post("/policies", status_code=201)
async def attach_policy(body: PolicyIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    row = await insurance.attach_policy(scope, body.patient_id, body.model_dump(exclude={"patient_id"}))
    await _audit(user, request, "INSURANCE_POLICY_SAVE", "patient_insurance_policy", row["id"],
                 {"patient_id": body.patient_id, "payer_id": body.payer_id})
    return row


@router.get("/policies/{patient_id}")
async def list_policies(patient_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_READ")
    return {"policies": await insurance.list_policies(scope, patient_id)}


# ── pipeline, claims, ageing ────────────────────────────────────────────────

@router.get("/pre-auths")
async def pre_auth_pipeline(status_filter: Optional[Literal["draft", "submitted", "query_raised", "approved",
                                                            "enhancement_requested"]] = Query(None, alias="status"),
                            branch_id: Optional[str] = None, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_READ")
    return await insurance.pre_auth_pipeline(scope, _read_branch(user, branch_id), status_filter)


@router.get("/claims")
async def list_claims(view: Literal["open", "all"] = "open", branch_id: Optional[str] = None,
                      clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_READ")
    return await insurance.list_claims(scope, _read_branch(user, branch_id), view)


@router.get("/claims/{claim_id}")
async def get_claim(claim_id: str, clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_READ")
    claim = await insurance.get_claim(scope, claim_id)
    enforce_branch_scope(user, claim.get("branch_id"))
    return await insurance.claim_detail(scope, claim_id)


@router.post("/claims/{claim_id}/settlement")
async def record_settlement(claim_id: str, body: SettlementIn, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    claim = await insurance.get_claim(scope, claim_id)
    enforce_branch_scope(user, claim.get("branch_id"))
    data = body.model_dump(exclude={"idempotency_key"})
    out = await insurance.record_claim_settlement(scope, claim_id, data, body.idempotency_key, user)
    if not out.get("replayed"):
        await _audit(user, request, "INSURANCE_CLAIM_SETTLEMENT", "ipd_insurance_claim", claim_id,
                     {"received_paise": body.amount_received_paise, "tds_paise": body.tds_paise,
                      "disallowed_paise": sum(d.amount_paise for d in body.disallowances),
                      "utr": body.utr_number, "status": out["status"]})
    return out


@router.post("/claims/{claim_id}/action")
async def claim_action(claim_id: str, body: ClaimActionIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    claim = await insurance.get_claim(scope, claim_id)
    enforce_branch_scope(user, claim.get("branch_id"))
    out = await insurance.claim_transition(scope, claim_id, body.action, body.note, user)
    await _audit(user, request, f"INSURANCE_CLAIM_{body.action.upper()}", "ipd_insurance_claim", claim_id,
                 {"status": out["status"]})
    return out


@router.post("/disallowances/{txn_id}/resolve")
async def resolve_disallowance(txn_id: str, body: ResolveIn, request: Request, clinic_id: str = "default",
                               user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    if body.resolution == "written_off" and not holds_credit_note_permission(user):
        # a write-off is a GST credit note on the tax invoice: the billing authority approves it
        raise HTTPException(status_code=403, detail="Writing off needs the credit-note permission (billing approval).")
    txn = await insurance.get_transaction(scope, txn_id)
    claim = await insurance.get_claim(scope, txn["claim_id"])
    enforce_branch_scope(user, claim.get("branch_id"))
    out = await insurance.resolve_disallowance(scope, txn_id, body.resolution, body.note, user)
    if not out.get("replayed"):
        await _audit(user, request, "INSURANCE_DISALLOWANCE_" + body.resolution.upper(), "ipd_claim_transaction", txn_id,
                     {"claim_id": txn["claim_id"], "amount_paise": txn["amount_paise"],
                      "credit_note": (out.get("credit_note") or {}).get("credit_note_number")})
    return out


@router.get("/ageing")
async def ageing(branch_id: Optional[str] = None, clinic_id: str = "default",
                 user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_READ")
    return await insurance.get_tpa_ageing_report(scope, _read_branch(user, branch_id))


# ── stay-level: chart card, pre-authorisation, cashless bill ────────────────

@ipd_router.get("/admissions/{admission_id}/insurance")
async def admission_insurance(admission_id: str, clinic_id: str = "default",
                              user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_READ")
    await _admission(user, scope, admission_id)
    return await insurance.admission_insurance(scope, admission_id)


@ipd_router.post("/admissions/{admission_id}/pre-auth", status_code=201)
async def request_pre_auth(admission_id: str, body: PreAuthIn, request: Request, clinic_id: str = "default",
                           user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    await _admission(user, scope, admission_id)
    out = await insurance.create_pre_auth(scope, admission_id, body.model_dump(), user)
    await _audit(user, request, "INSURANCE_PRE_AUTH_CREATE", "ipd_pre_authorization", out["id"],
                 {"admission_id": admission_id, "number": out["pre_auth_number"],
                  "requested_paise": body.requested_paise, "status": out["status"]})
    return out


_DECISION_ACTION = {"approved": "approve", "query_raised": "query", "rejected": "reject",
                    "enhancement_denied": "deny_enhancement"}


@ipd_router.patch("/pre-auth/{pre_auth_id}/decision")
async def pre_auth_decision(pre_auth_id: str, body: DecisionIn, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    pa = await insurance.get_pre_auth(scope, pre_auth_id)
    enforce_branch_scope(user, pa.get("branch_id"))
    out = await insurance.record_pre_auth_decision(scope, pre_auth_id, _DECISION_ACTION[body.decision], {
        "amount_paise": body.approved_amount_paise, "approval_ref": body.approval_ref,
        "approval_letter_path": body.approval_letter_path, "note": body.note}, user)
    await _audit(user, request, "INSURANCE_PRE_AUTH_" + body.decision.upper(), "ipd_pre_authorization", pre_auth_id,
                 {"from": out.get("from"), "to": out["status"], "total_approved_paise": out["total_approved_paise"],
                  "approval_ref": out.get("approval_ref_number")})
    return out


@ipd_router.post("/pre-auth/{pre_auth_id}/action")
async def pre_auth_action(pre_auth_id: str, body: PreAuthActionIn, request: Request, clinic_id: str = "default",
                          user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    pa = await insurance.get_pre_auth(scope, pre_auth_id)
    enforce_branch_scope(user, pa.get("branch_id"))
    out = await insurance.record_pre_auth_decision(scope, pre_auth_id, body.action,
                                                   {"amount_paise": body.amount_paise, "note": body.note}, user)
    await _audit(user, request, "INSURANCE_PRE_AUTH_" + body.action.upper(), "ipd_pre_authorization", pre_auth_id,
                 {"from": out.get("from"), "to": out["status"], "amount_paise": body.amount_paise})
    return out


@ipd_router.post("/admissions/{admission_id}/cashless-bill")
async def cashless_bill(admission_id: str, body: CashlessBillIn, request: Request, clinic_id: str = "default",
                        user: AdminUser = Depends(verify_credentials)):
    scope = await _scope(user, clinic_id, "INSURANCE_MANAGE")
    await _admission(user, scope, admission_id)
    out = await insurance.generate_cashless_final_bill(scope, admission_id, body.non_payable_paise, body.copay_paise,
                                                       user)
    if not out.get("replayed"):
        await _audit(user, request, "INSURANCE_CLAIM_FILE", "ipd_insurance_claim", out["id"],
                     {"admission_id": admission_id, "number": out["claim_number"], **out["split"]})
    return out

