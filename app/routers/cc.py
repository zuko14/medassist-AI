"""Kriya Continuous Care API (`/admin/cc/*`; docs/continuous-care/API.md).

Every route: permission -> enforce_clinic_access -> care_access(clinic) -> (new work) wind-down -> branch pin ->
service. Bodies are Pydantic with extra="forbid" (no drug / dose field can reach a care action, ADR-CC-04).
Zero-LLM: nothing here imports AI, voice NLU or assistant code (tests/test_cc_no_llm.py).
"""

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services import cc, cc_cases, cc_channels, cc_radar, cc_verify
from app.services.permissions import holds_cc_permission
from app.services.tenant import care_access, get_clinic_by_id
from app.utils.helpers import today_ist

router = APIRouter(prefix="/admin/cc", tags=["Continuous Care"])
UUID36 = Field(..., min_length=36, max_length=36)
Lang = Literal["en", "hi", "te"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SettingsIn(_In):
    updated_at: Optional[str] = None                      # CAS: the value the editor loaded
    reply_hours: Optional[dict] = None
    med_times: Optional[dict] = None
    card_window: Optional[dict] = None
    sla_hours: Optional[dict] = None
    emergency_number: Optional[str] = Field(None, max_length=20)
    template_lang_default: Optional[Lang] = None
    card_show_medicine_names: Optional[bool] = None
    care_tier_share: Optional[int] = None
    radar_threshold: Optional[int] = None
    glucose_unit: Optional[Literal["mg/dL", "mmol/L"]] = None
    voice_fallback: Optional[bool] = None
    care_daily_cap: Optional[int] = None


class CardIn(_In):
    body: str = Field(..., min_length=10, max_length=1000)


class MeasureSourceIn(_In):
    lab_test_id: str = UUID36
    parameter_id: Optional[str] = Field(None, min_length=36, max_length=36)
    parameter_name: Optional[str] = Field(None, max_length=120)
    measure_code: Literal["hba1c", "fbg", "ppbg", "rbg", "tsh", "ldl", "creatinine", "alt", "wbc", "hb", "beta_hcg", "egfr",
                          "lipid_total"]
    unit_factor: float = Field(1, gt=0, le=1000)


class ActivationIn(_In):
    protocol_code: str = Field(..., pattern=r"^[a-z0-9_]{2,40}$")
    params: dict = Field(default_factory=dict)
    step_protocol_text: Optional[str] = Field(None, max_length=1000)


class ReasonIn(_In):
    reason: str = Field(..., min_length=3, max_length=300)


class EnrollIn(_In):
    patient_id: str = UUID36
    family_member_id: Optional[str] = Field(None, min_length=36, max_length=36)
    protocol_code: str = Field(..., pattern=r"^[a-z0-9_]{2,40}$")
    primary_doctor_id: str = UUID36
    channel_pref: Literal["whatsapp", "voice", "both"] = "whatsapp"
    language: Optional[Lang] = None
    branch_id: Optional[str] = Field(None, min_length=36, max_length=36)
    wa_optin_method: Optional[Literal["in_person", "phone_call"]] = None   # W2 step 0, ticked by staff
    source: Optional[Literal["doctor_enrolled", "radar_candidate", "import", "discharge"]] = None
    facts: dict = Field(default_factory=dict)            # doctor-entered pack facts (EDD, transfer date, surgery date…)


class OptinIn(_In):
    method: Literal["in_person", "phone_call"]


class CounterConsentIn(_In):
    notice_version: str = Field(..., pattern=r"^cc-notice-[0-9]+$")
    guardian_relation: Optional[str] = Field(None, min_length=2, max_length=40)


class ObservationIn(_In):
    measure_code: Literal["fbg", "ppbg", "rbg", "bp", "weight", "hba1c", "phq9", "gad7", "phq2", "adherence", "symptoms"]
    value: Optional[float] = None
    value2: Optional[float] = None
    code: Optional[str] = Field(None, max_length=40)
    observed_at: Optional[str] = None


class CorrectionIn(_In):
    value: Optional[float] = None
    value2: Optional[float] = None
    code: Optional[str] = Field(None, max_length=40)
    reason: str = Field(..., min_length=5, max_length=300)


class CaseActionIn(_In):
    expected_updated_at: Optional[str] = None
    note: Optional[str] = Field(None, max_length=1000)
    closed_reason: Optional[Literal["patient_declined", "unreachable", "transferred", "duplicate", "clinical_decision"]] = None


class ActionParams(_In):
    """Closed per-action parameters. There is deliberately no field for a medicine, dose or frequency."""
    days: Optional[int] = Field(None, ge=1, le=90)
    within_days: Optional[int] = Field(None, ge=1, le=60)
    doctor_id: Optional[str] = Field(None, min_length=36, max_length=36)
    date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    time: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    lab_test_id: Optional[str] = Field(None, min_length=36, max_length=36)
    refer_to: Optional[str] = Field(None, max_length=120)
    reason: Optional[str] = Field(None, max_length=300)


class TakeActionIn(_In):
    action_code: Literal["book_review_visit", "order_lab", "book_home_sample", "recheck_in_days", "continue_plan",
                         "call_patient_now", "refer", "pause", "end"]
    params: ActionParams = Field(default_factory=ActionParams)


class DischargeFollowupIn(_In):
    admission_id: str = UUID36
    follow_up_on: date
    doctor_id: str = UUID36
    note: Optional[str] = Field(None, max_length=300)
    enroll_post_discharge: bool = False


# ── scope ────────────────────────────────────────────────────────────────────

async def _scope(user: AdminUser, clinic_id: Optional[str], *perms: str, new_work: bool = False) -> tuple[str, dict]:
    if perms and not any(holds_cc_permission(user, p) for p in perms):
        raise HTTPException(status_code=403, detail={"code": "missing_permission", "message":
                            f"Missing permission: {' or '.join(perms)}"})
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    access = care_access(clinic)
    if access is None:
        raise HTTPException(status_code=403, detail={"code": "care_not_enabled",
                                                     "message": "Continuous Care is not enabled for this clinic."})
    if new_work and access == "WIND_DOWN":
        raise HTTPException(status_code=409, detail={"code": "care_wind_down", "message":
                            "Continuous Care is being switched off: no new enrollments or messages."})
    return scope, clinic


def _branch(user: AdminUser) -> Optional[str]:
    return str(user.branch_id) if getattr(user, "branch_id", None) else None


def _clinic_wide(user: AdminUser) -> None:
    if getattr(user, "branch_id", None):
        raise HTTPException(status_code=403, detail="Only a clinic-wide login can change care settings.")


async def _audit(user, request: Request, action: str, rtype: str, rid: Optional[str], details: Optional[dict] = None):
    await log_admin_action(user=user, action=action, resource_type=rtype, resource_id=rid, details=details or {},
                           ip_address=request.client.host if request.client else None)


# ── setup & settings (CC_ADMIN) ──────────────────────────────────────────────

@router.get("/setup")
async def get_setup(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_ADMIN")
    return {"checklist": await cc.setup_checklist(clinic), "settings": cc.get_settings(clinic),
            "state": clinic.get("cc_state"), "access": care_access(clinic),
            "budget": {"wa_health": cc.get_settings(clinic).get("wa_health"),
                       "templates_24h": await cc_channels.templates_sent_24h(scope),
                       "template_budget": cc_channels.template_budget(clinic)},
            "notices": {lang: cc.notice(clinic, lang) for lang in cc.LANGS}}


@router.get("/settings")
async def get_settings(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "CC_ADMIN")
    return {"settings": cc.get_settings(clinic), "updated_at": (clinic.get("cc_settings") or {}).get("updated_at")}


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request, clinic_id: Optional[str] = None,
                       user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_ADMIN")
    _clinic_wide(user)
    changes = body.model_dump(exclude_none=True, exclude={"updated_at"})
    out = await cc.save_settings(clinic, changes, body.updated_at)
    await _audit(user, request, "cc_settings", "clinic", scope, {"keys": sorted(changes)})
    return {"settings": out}


@router.post("/templates/submit")
async def submit_templates(request: Request, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    from app.services.dental_plans import DentalError
    scope, clinic = await _scope(user, clinic_id, "CC_ADMIN", new_work=True)
    try:
        out = await cc_channels.submit_templates(clinic)
    except DentalError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await _audit(user, request, "cc_templates_submit", "clinic", scope)
    return {"results": out}


@router.get("/templates")
async def get_templates(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    from app.services.dental_plans import DentalError
    _, clinic = await _scope(user, clinic_id, "CC_ADMIN")
    try:
        statuses = await cc_channels.refresh_template_statuses(clinic)
    except DentalError:
        statuses = (clinic.get("cc_settings") or {}).get("templates") or {n: "NOT_SUBMITTED" for n in cc_channels.TEMPLATES}
    return {"templates": [{"name": n, "status": statuses.get(n, "NOT_SUBMITTED"), "body": t["body"]}
                          for n, t in cc_channels.TEMPLATES.items()]}


@router.get("/measure-sources")
async def get_measure_sources(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN", "CC_VIEW")
    return {"sources": await cc.measure_sources(scope)}


@router.post("/measure-sources", status_code=201)
async def post_measure_source(body: MeasureSourceIn, clinic_id: Optional[str] = None,
                              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN")
    return await cc.add_measure_source(scope, body.model_dump(exclude_none=True))


@router.post("/measure-sources/seed")
async def seed_measure_sources(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN")
    return {"added": await cc.seed_measure_sources(scope)}


@router.delete("/measure-sources/{source_id}")
async def delete_measure_source(source_id: str, clinic_id: Optional[str] = None,
                                user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN")
    await cc.delete_measure_source(scope, source_id)
    return {"deleted": True}


@router.get("/cards")
async def get_cards(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN")
    return {"cards": await cc.list_cards(scope)}


@router.put("/cards/{card_key}/{lang}")
async def put_card(card_key: str, lang: Lang, body: CardIn, request: Request, clinic_id: Optional[str] = None,
                   user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN")
    row = await cc.approve_card(scope, card_key, lang, body.body, user)
    await _audit(user, request, "cc_card_approve", "cc_card", row["id"], {"card_key": card_key, "language": lang,
                                                                           "version": row["version"]})
    return row


@router.get("/notice")
async def get_notice(lang: Lang = "en", clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "CC_VIEW")
    return cc.notice(clinic, lang)


# ── protocols (ADR-CC-06) ────────────────────────────────────────────────────

@router.get("/protocols")
async def get_protocols(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_VIEW", "CC_ADMIN", "CC_APPROVE")
    return {"packs": cc.pack_catalogue(), "activations": await cc.list_activations(scope),
            "can_sign": holds_cc_permission(user, "CC_APPROVE")}


@router.post("/activations", status_code=201)
async def post_activation(body: ActivationIn, request: Request, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN", new_work=True)
    row = await cc.create_activation(scope, body.protocol_code, body.params, body.step_protocol_text, user)
    await _audit(user, request, "cc_activation_draft", "cc_activation", row["id"], {"protocol": body.protocol_code})
    return row


@router.post("/activations/{activation_id}/sign")
async def sign_activation(activation_id: str, request: Request, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "CC_APPROVE", new_work=True)
    row = await cc.sign_activation(clinic, activation_id, user)
    await _audit(user, request, "cc_activation_sign", "cc_activation", activation_id,
                 {"protocol": row["protocol_code"], "version": row["version"]})
    return row


@router.post("/activations/{activation_id}/retire")
async def retire_activation(activation_id: str, body: ReasonIn, request: Request, clinic_id: Optional[str] = None,
                            user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN")
    row = await cc.retire_activation(scope, activation_id, body.reason)
    await _audit(user, request, "cc_activation_retire", "cc_activation", activation_id)
    return row


# ── enrollments, consent, plans ──────────────────────────────────────────────

@router.get("/enrollments")
async def get_enrollments(clinic_id: Optional[str] = None, status: Optional[str] = None, protocol: Optional[str] = None,
                          doctor_id: Optional[str] = None, q: Optional[str] = Query(None, max_length=80),
                          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    return await cc.list_enrollments(scope, status, protocol, doctor_id, q, _branch(user), limit, offset)


@router.post("/enrollments", status_code=201)
async def post_enrollment(body: EnrollIn, request: Request, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    is_doctor = holds_cc_permission(user, "CC_APPROVE")
    if not is_doctor and not holds_cc_permission(user, "CC_COORDINATE"):
        raise HTTPException(status_code=403, detail={"code": "missing_permission", "message":
                            "Missing permission: CC_APPROVE or CC_COORDINATE"})
    _, clinic = await _scope(user, clinic_id, new_work=True)
    data = body.model_dump()
    if _branch(user):
        data["branch_id"] = _branch(user)
    enr = await cc.create_enrollment(clinic, data, user, is_doctor)
    if body.facts:
        await cc_store_facts(clinic["id"], enr, body.facts)
    if enr["status"] == "invited" and body.wa_optin_method:
        doc = getattr(user, "username", "")
        from app.database import sb, scoped_query
        d = (await sb(scoped_query("doctors", clinic["id"], "name").eq("id", body.primary_doctor_id).limit(1))).data or []
        await cc_channels.send_consent_invite(clinic, enr, (d[0]["name"] if d else doc).replace("Dr. ", ""))
    await _audit(user, request, "cc_enroll", "cc_enrollment", enr["id"], {"protocol": body.protocol_code,
                                                                           "status": enr["status"]})
    return enr


async def cc_store_facts(clinic_id: str, enrollment: dict, facts: dict) -> None:
    """Doctor-entered pack facts (dates only, or booleans / short text) kept in the enrollment baseline."""
    import re
    from app.database import sb, supabase
    clean = {}
    for k, v in list(facts.items())[:20]:
        if not re.fullmatch(r"[a-z_]{2,40}", str(k)):
            continue
        if isinstance(v, bool) or (isinstance(v, str) and len(v) <= 120) or isinstance(v, (int, float)):
            clean[k] = v
    base = {**(enrollment.get("baseline") or {}), "facts": {**((enrollment.get("baseline") or {}).get("facts") or {}), **clean}}
    await sb(supabase.table("cc_enrollments").update({"baseline": base}).eq("clinic_id", clinic_id).eq("id", enrollment["id"]))


@router.get("/enrollments/{enrollment_id}")
async def get_enrollment(enrollment_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    e = await cc.get_enrollment(scope, enrollment_id, _branch(user))
    from app.database import sb, scoped_query
    latest = (await sb(scoped_query("cc_evaluations", scope).eq("enrollment_id", enrollment_id)
                       .order("evaluated_at", desc=True).limit(1))).data or []
    cases = await cc_cases.list_cases(scope, None, None, None, _branch(user))
    return {**e, "plan": await cc.current_plan(scope, enrollment_id), "latest_evaluation": latest[0] if latest else None,
            "open_cases": [c for c in cases if c.get("enrollment_id") == enrollment_id],
            "observations": await cc.observations(scope, enrollment_id, None, None, None)}


@router.post("/enrollments/{enrollment_id}/approve")
async def approve_enrollment(enrollment_id: str, request: Request, clinic_id: Optional[str] = None,
                             user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_APPROVE", new_work=True)
    e = await cc.get_enrollment(scope, enrollment_id, _branch(user))
    out = await cc.approve_enrollment(scope, e["id"], user)
    await _audit(user, request, "cc_enroll_approve", "cc_enrollment", enrollment_id)
    return out


@router.post("/enrollments/{enrollment_id}/optin")
async def post_optin(enrollment_id: str, body: OptinIn, request: Request, clinic_id: Optional[str] = None,
                     user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_COORDINATE", "CC_APPROVE", new_work=True)
    e = await cc.get_enrollment(scope, enrollment_id, _branch(user))
    row = await cc.record_wa_optin(scope, e["patient_id"], e.get("family_member_id"), body.method, user, e["language"])
    await _audit(user, request, "cc_wa_optin", "cc_enrollment", enrollment_id, {"method": body.method})
    return row


@router.post("/enrollments/{enrollment_id}/invite")
async def post_invite(enrollment_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_COORDINATE", "CC_APPROVE", new_work=True)
    e = await cc.get_enrollment(scope, enrollment_id, _branch(user))
    if e["status"] != "invited":
        raise HTTPException(status_code=409, detail="Only an invited enrollment can be sent the invitation.")
    return await cc_channels.send_consent_invite(clinic, e, getattr(user, "username", "") or "")


@router.post("/enrollments/{enrollment_id}/consent/counter")
async def counter_consent(enrollment_id: str, body: CounterConsentIn, request: Request, clinic_id: Optional[str] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_COORDINATE", new_work=True)
    await cc.get_enrollment(scope, enrollment_id, _branch(user))
    out = await cc.counter_consent(clinic, enrollment_id, body.notice_version, body.guardian_relation, user)
    await _audit(user, request, "cc_consent_counter", "cc_enrollment", enrollment_id,
                 {"notice_version": body.notice_version})
    return out


@router.post("/enrollments/{enrollment_id}/pause")
async def pause(enrollment_id: str, body: ReasonIn, clinic_id: Optional[str] = None,
                user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_COORDINATE")
    await cc.get_enrollment(scope, enrollment_id, _branch(user))
    return await cc.set_status(scope, enrollment_id, "paused", body.reason)


@router.post("/enrollments/{enrollment_id}/resume")
async def resume(enrollment_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_COORDINATE", new_work=True)
    await cc.get_enrollment(scope, enrollment_id, _branch(user))
    return await cc.set_status(scope, enrollment_id, "active", "resumed")


@router.post("/enrollments/{enrollment_id}/end")
async def end(enrollment_id: str, body: ReasonIn, clinic_id: Optional[str] = None,
              user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_COORDINATE")
    await cc.get_enrollment(scope, enrollment_id, _branch(user))
    return await cc.set_status(scope, enrollment_id, "ended_by_clinic", body.reason)


@router.get("/enrollments/{enrollment_id}/plan/preview")
async def plan_preview(enrollment_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_VIEW")
    return await cc.plan_preview(clinic, await cc.get_enrollment(scope, enrollment_id, _branch(user)))


@router.post("/enrollments/{enrollment_id}/plan/rebuild")
async def plan_rebuild(enrollment_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_COORDINATE", new_work=True)
    return await cc.rebuild_plan(clinic, await cc.get_enrollment(scope, enrollment_id, _branch(user)),
                                 built_by=getattr(user, "username", "staff") or "staff")


@router.get("/enrollments/{enrollment_id}/observations")
async def get_observations(enrollment_id: str, clinic_id: Optional[str] = None, measure: Optional[str] = None,
                           from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                           user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    await cc.get_enrollment(scope, enrollment_id, _branch(user))
    return {"observations": await cc.observations(scope, enrollment_id, measure, from_, to)}


@router.post("/enrollments/{enrollment_id}/observations", status_code=201)
async def post_observation(enrollment_id: str, body: ObservationIn, request: Request, clinic_id: Optional[str] = None,
                           user: AdminUser = Depends(verify_credentials)):
    scope, clinic = await _scope(user, clinic_id, "CC_COORDINATE")
    e = await cc.get_enrollment(scope, enrollment_id, _branch(user))
    row = await cc.record_observation(scope, e["id"], body.measure_code, value=body.value, value2=body.value2,
                                      code=body.code, source="coordinator_entered", observed_at=body.observed_at, user=user)
    out = await cc.evaluate_enrollment(clinic, e) if row and e["status"] == "active" else {}
    await _audit(user, request, "cc_observation_staff", "cc_enrollment", enrollment_id, {"measure": body.measure_code})
    return {"observation": row, "evaluation": out}


@router.post("/observations/{observation_id}/correct", status_code=201)
async def correct(observation_id: str, body: CorrectionIn, request: Request, clinic_id: Optional[str] = None,
                  user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_COORDINATE")
    row = await cc.correct_observation(scope, observation_id, body.value, body.value2, body.code, body.reason, user)
    await _audit(user, request, "cc_observation_correct", "cc_observation", observation_id)
    return row


@router.post("/discharge-followups", status_code=201)
async def post_discharge_followup(body: DischargeFollowupIn, request: Request, clinic_id: Optional[str] = None,
                                  user: AdminUser = Depends(verify_credentials)):
    """IPD discharge screen: a structured follow-up date (latent L5) and, optionally, 30-day post-discharge care."""
    from app.database import sb, scoped_query, supabase
    scope, clinic = await _scope(user, clinic_id, "CC_APPROVE", "CC_COORDINATE", new_work=True)
    adm = (await sb(scoped_query("ipd_admissions", scope, "id, patient_id").eq("id", body.admission_id).limit(1))).data
    if not adm:
        raise HTTPException(status_code=404, detail="Admission not found.")
    try:
        # unscoped: insert_scoped_by_payload
        row = (await sb(supabase.table("cc_discharge_followups").upsert({
            "clinic_id": scope, "admission_id": body.admission_id, "patient_id": adm[0]["patient_id"],
            "follow_up_on": body.follow_up_on.isoformat(), "doctor_id": body.doctor_id, "note": body.note,
            "created_by_name": getattr(user, "username", None)}, on_conflict="clinic_id,admission_id"))).data[0]
    except Exception as e:
        cc.raise_for(e)
    if body.enroll_post_discharge:
        await cc.create_enrollment(clinic, {"patient_id": adm[0]["patient_id"], "protocol_code": "post_discharge",
                                            "primary_doctor_id": body.doctor_id, "source": "discharge"}, user,
                                   holds_cc_permission(user, "CC_APPROVE"))
    await _audit(user, request, "cc_discharge_followup", "ipd_admission", body.admission_id)
    return row


# ── radar, cases, inbox, actions ─────────────────────────────────────────────

@router.get("/radar")
async def get_radar(clinic_id: Optional[str] = None, reason: Optional[str] = None,
                    min_score: Optional[int] = Query(None, ge=0, le=1000), user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    return {"rows": await cc_radar.list_rows(scope, reason, min_score), "labels": cc_radar.REASON_LABELS}


@router.post("/radar/rebuild")
async def rebuild_radar(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    _, clinic = await _scope(user, clinic_id, "CC_COORDINATE", new_work=True)
    return await cc_radar.rebuild(clinic)


@router.post("/radar/{row_id}/dismiss")
async def dismiss_radar(row_id: str, body: ReasonIn, request: Request, clinic_id: Optional[str] = None,
                        user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_COORDINATE")
    row = await cc_radar.dismiss(scope, row_id, body.reason, user)
    await _audit(user, request, "cc_radar_dismiss", "cc_radar_row", row_id, {"reason": body.reason})
    return row


@router.get("/cases")
async def get_cases(clinic_id: Optional[str] = None, status: Optional[str] = None, tier: Optional[str] = None,
                    assigned: Optional[Literal["me"]] = None, user: AdminUser = Depends(verify_credentials)):
    from app.services.cc import actor_id
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    return {"cases": await cc_cases.list_cases(scope, status, tier, actor_id(user) if assigned else None, _branch(user))}


@router.get("/cases/{case_id}")
async def get_case(case_id: str, clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    return await cc_cases.case_detail(scope, case_id, _branch(user))


async def _case_action(action: str, case_id: str, body: CaseActionIn, request: Request, clinic_id: Optional[str],
                       user: AdminUser) -> dict:
    scope, _ = await _scope(user, clinic_id, "CC_COORDINATE")
    out = await cc_cases.transition(scope, case_id, action, user, expected_updated_at=body.expected_updated_at,
                                    note=body.note, closed_reason=body.closed_reason, branch_id=_branch(user))
    await _audit(user, request, f"cc_case_{action}", "cc_case", case_id)
    return out


@router.post("/cases/{case_id}/acknowledge")
async def case_ack(case_id: str, body: CaseActionIn, request: Request, clinic_id: Optional[str] = None,
                   user: AdminUser = Depends(verify_credentials)):
    return await _case_action("acknowledge", case_id, body, request, clinic_id, user)


@router.post("/cases/{case_id}/assign")
async def case_assign(case_id: str, body: CaseActionIn, request: Request, clinic_id: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    return await _case_action("assign", case_id, body, request, clinic_id, user)


@router.post("/cases/{case_id}/note")
async def case_note(case_id: str, body: CaseActionIn, request: Request, clinic_id: Optional[str] = None,
                    user: AdminUser = Depends(verify_credentials)):
    return await _case_action("note", case_id, body, request, clinic_id, user)


@router.post("/cases/{case_id}/escalate")
async def case_escalate(case_id: str, body: CaseActionIn, request: Request, clinic_id: Optional[str] = None,
                        user: AdminUser = Depends(verify_credentials)):
    return await _case_action("escalate", case_id, body, request, clinic_id, user)


@router.post("/cases/{case_id}/close")
async def case_close(case_id: str, body: CaseActionIn, request: Request, clinic_id: Optional[str] = None,
                     user: AdminUser = Depends(verify_credentials)):
    return await _case_action("close", case_id, body, request, clinic_id, user)


@router.post("/cases/{case_id}/request-doctor")
async def case_request_doctor(case_id: str, body: CaseActionIn, request: Request, clinic_id: Optional[str] = None,
                              user: AdminUser = Depends(verify_credentials)):
    return await _case_action("request_doctor", case_id, body, request, clinic_id, user)


@router.get("/inbox")
async def get_inbox(clinic_id: Optional[str] = None, user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_APPROVE")
    return {"cases": await cc_cases.inbox(scope, getattr(user, "doctor_id", None), _branch(user))}


@router.post("/cases/{case_id}/actions", status_code=201)
async def post_action(case_id: str, body: TakeActionIn, request: Request, clinic_id: Optional[str] = None,
                      idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", min_length=8, max_length=80),
                      user: AdminUser = Depends(verify_credentials)):
    doctor = holds_cc_permission(user, "CC_APPROVE")
    if not doctor and not holds_cc_permission(user, "CC_COORDINATE"):
        raise HTTPException(status_code=403, detail={"code": "missing_permission", "message":
                            "Missing permission: CC_APPROVE or CC_COORDINATE"})
    _, clinic = await _scope(user, clinic_id, new_work=True)
    out = await cc_cases.take_action(clinic, case_id, body.action_code, body.params.model_dump(exclude_none=True), user,
                                     role="doctor" if doctor else "coordinator", idempotency_key=idempotency_key,
                                     branch_id=_branch(user))
    await _audit(user, request, "cc_action", "cc_case", case_id, {"action": body.action_code})
    return out


@router.get("/actions")
async def get_actions(clinic_id: Optional[str] = None, status: Optional[str] = None,
                      user: AdminUser = Depends(verify_credentials)):
    from app.database import sb, scoped_query
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    q = scoped_query("cc_actions", scope)
    if status:
        q = q.eq("status", status)
    return {"actions": (await sb(q.order("created_at", desc=True).limit(500))).data or []}


# ── outcomes ─────────────────────────────────────────────────────────────────

def _range(from_: Optional[date], to: Optional[date]) -> tuple:
    until = to or today_ist()
    since = from_ or until.replace(day=1)
    if since > until or (until - since).days > 731:
        raise HTTPException(status_code=422, detail="Pick a date range of at most two years.")
    return since, until


@router.get("/outcomes")
async def get_outcomes(clinic_id: Optional[str] = None, protocol: Optional[str] = None,
                       from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_VIEW")
    since, until = _range(from_, to)
    return await cc_verify.report(scope, protocol, since, until)


@router.get("/outcomes/export.csv")
async def export_outcomes(request: Request, clinic_id: Optional[str] = None, protocol: Optional[str] = None,
                          from_: Optional[date] = Query(None, alias="from"), to: Optional[date] = None,
                          user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _scope(user, clinic_id, "CC_ADMIN")
    since, until = _range(from_, to)
    r = await cc_verify.report(scope, protocol, since, until)
    await _audit(user, request, "cc_outcomes_export", "clinic", scope, {"protocol": protocol})
    return Response(cc_verify.report_csv(r), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="care-outcomes.csv"'})
