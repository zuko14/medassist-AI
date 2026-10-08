"""Home sample collection — /admin/home-collection (migration 097).

Two audiences on one tenant-scoped surface:

* the centre (clinic_admin / super_admin, or staff holding
  HOME_COLLECTION_MANAGE): settings, the day's visits, phlebotomist roster,
  manual (re)assignment;
* a phlebotomist login (staff_role PHLEBOTOMIST): GET /my and status updates
  on visits assigned to it. Nothing else -- verify_credentials confines that
  login to those two routes, and both re-read the account from the database
  on every request, so a deactivation takes effect immediately.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import is_uuid, restrict_to_branch, sb, supabase
from app.routers.admin import AdminUser, enforce_clinic_access, log_admin_action, verify_credentials
from app.services import home_collection as hc
from app.services.permissions import PHLEBOTOMIST, enforce_branch_scope, resolve_owned_branch
from app.services.tenant import (
    get_clinic_by_id,
    home_collection_available,
    invalidate_tenant_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/home-collection", tags=["home-collection"])

#: The columns a visit card needs. Never the payment ids or symptoms.
VISIT_COLUMNS = (
    "id, booking_ref, status, patient_name, patient_phone, lab_test_id, lab_test_name, "
    "appointment_date, branch_id, branch_name, amount_paise, payment_id, "
    "collection_slot, collection_address, collection_landmark, collection_lat, collection_lng, "
    "collection_contact_phone, home_collection_fee_paise, phlebotomist_id, "
    "collection_status, collection_status_at, collection_notes, created_at"
)


def _ip(request: Optional[Request]) -> str:
    return request.client.host if (request and request.client) else "unknown"


def _is_phleb(user: AdminUser) -> bool:
    return getattr(user, "staff_role", None) == PHLEBOTOMIST


def _is_manager(user: AdminUser) -> bool:
    if _is_phleb(user):
        return False
    return user.role in ("super_admin", "clinic_admin") or "HOME_COLLECTION_MANAGE" in (user.permissions or [])


async def _scope(user: AdminUser, clinic_id: str) -> tuple[str, dict]:
    scope = enforce_clinic_access(user, clinic_id)
    clinic = await get_clinic_by_id(scope)
    if not home_collection_available(clinic):
        raise HTTPException(status_code=403, detail="Home sample collection is not part of this clinic's plan.")
    return scope, clinic


async def _manager_scope(user: AdminUser, clinic_id: str) -> tuple[str, dict]:
    if not _is_manager(user):
        raise HTTPException(status_code=403, detail="Missing permission: HOME_COLLECTION_MANAGE")
    return await _scope(user, clinic_id)


def _parse_date(value: Optional[str]) -> str:
    if not value:
        return datetime.now(hc.IST).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")


async def _fasting_by_test(scope: str, rows: list[dict]) -> dict:
    ids = sorted({str(r["lab_test_id"]) for r in rows if r.get("lab_test_id")})
    if not ids:
        return {}
    res = await sb(
        supabase.table("lab_tests").select("id, fasting_required, sample_type")
        .eq("clinic_id", scope).in_("id", ids)
    )
    return {str(t["id"]): t for t in (res.data or [])}


def _card(row: dict, phlebs: dict, tests: dict) -> dict:
    test = tests.get(str(row.get("lab_test_id")), {})
    paid = bool(row.get("payment_id"))
    p = phlebs.get(str(row.get("phlebotomist_id") or ""))
    return {
        **row,
        "maps_link": hc.maps_link(row.get("collection_lat"), row.get("collection_lng")),
        "fasting_required": bool(test.get("fasting_required")),
        "sample_type": test.get("sample_type"),
        "paid_online": paid,
        "collect_paise": 0 if paid else int(row.get("amount_paise") or 0),
        "phlebotomist_name": hc.display_name(p) if p else None,
        "phlebotomist_phone": (p or {}).get("phone"),
        # Lifecycle order, so the buttons read "Start trip, Sample collected, ...".
        "next_statuses": [st for st in hc.STATUSES if st in hc.TRANSITIONS.get(row.get("collection_status") or "", ())],
    }


# ═══════ settings ═══════


class SettingsIn(BaseModel):
    enabled: bool = False
    fee_rupees: int = Field(0, ge=0, le=5000)
    free_above_rupees: int = Field(0, ge=0, le=500000)
    slot_minutes: int = Field(60)
    slot_capacity: int = Field(0, ge=0, le=500)
    lead_minutes: int = Field(60, ge=0, le=24 * 60)
    max_distance_km: int = Field(0, ge=0, le=200)
    centre_lat: Optional[float] = Field(None, ge=-90, le=90)
    centre_lng: Optional[float] = Field(None, ge=-180, le=180)
    # Empty = visits follow the lab's sample collection window.
    windows: list[dict] = Field(default_factory=list, max_length=hc.MAX_WINDOWS)


def _settings_out(s: dict) -> dict:
    return {
        **s,
        "fee_rupees": s["fee_paise"] // 100,
        "free_above_rupees": s["free_above_paise"] // 100,
    }


@router.get("/settings")
async def get_settings(
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    scope, clinic = await _manager_scope(user, clinic_id)
    source, raw = "default", None
    if branch_id:
        branch = await resolve_owned_branch(user, branch_id, scope)
        raw = (branch.get("config") or {}).get("home_collection")
        if isinstance(raw, dict):
            source = "branch"
    if not isinstance(raw, dict):
        raw = (clinic.get("config") or {}).get("home_collection")
        if isinstance(raw, dict):
            source = "clinic"
    return {"settings": _settings_out(hc.normalize_settings(raw)), "source": source, "branch_id": branch_id}


@router.put("/settings")
async def put_settings(
    body: SettingsIn,
    request: Request,
    clinic_id: str = "default",
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    scope, _ = await _manager_scope(user, clinic_id)
    if body.slot_minutes not in hc.SLOT_MINUTES_ALLOWED:
        raise HTTPException(status_code=422, detail=f"slot_minutes must be one of {hc.SLOT_MINUTES_ALLOWED}")
    if body.max_distance_km and (body.centre_lat is None or body.centre_lng is None):
        raise HTTPException(status_code=422, detail="A service radius needs the centre's location (latitude and longitude).")
    windows = hc.normalize_windows(body.windows)
    if len(windows) != len(body.windows):
        raise HTTPException(
            status_code=422,
            detail="Each visit window needs a start before its end (HH:MM), and windows must not overlap.",
        )
    settings = hc.normalize_settings({
        "enabled": body.enabled,
        "fee_paise": body.fee_rupees * 100,
        "free_above_paise": body.free_above_rupees * 100,
        "slot_minutes": body.slot_minutes,
        "slot_capacity": body.slot_capacity,
        "lead_minutes": body.lead_minutes,
        "max_distance_km": body.max_distance_km,
        "centre_lat": body.centre_lat,
        "centre_lng": body.centre_lng,
        "windows": windows,
    })
    if branch_id:
        branch = await resolve_owned_branch(user, branch_id, scope)
        config = branch.get("config") or {}
        config["home_collection"] = settings
        await sb(supabase.table("branches").update({"config": config}).eq("clinic_id", scope).eq("id", branch["id"]))
    else:
        if user.role == "staff" and getattr(user, "branch_id", None):
            raise HTTPException(status_code=403, detail="Branch staff can only change their own branch's settings.")
        res = await sb(supabase.table("clinics").select("config, whatsapp_number, phone_number_id").eq("id", scope))
        if not res.data:
            raise HTTPException(status_code=404, detail="Clinic not found")
        config = res.data[0].get("config") or {}
        config["home_collection"] = settings
        # unscoped: unique_row_key
        await sb(supabase.table("clinics").update({"config": config}).eq("id", scope))
        invalidate_tenant_cache(res.data[0].get("whatsapp_number"), res.data[0].get("phone_number_id"))
    await log_admin_action(user, "update_home_collection_settings", "clinic", scope,
                           {"branch_id": branch_id, **settings}, _ip(request))
    return {"success": True, "settings": _settings_out(settings)}


# ═══════ visits (centre view) ═══════


@router.get("/visits")
async def list_visits(
    clinic_id: str = "default",
    date: Optional[str] = None,
    branch_id: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    scope, _ = await _manager_scope(user, clinic_id)
    day = _parse_date(date)
    if branch_id:
        enforce_branch_scope(user, branch_id)
    q = (
        supabase.table("appointments").select(VISIT_COLUMNS)
        .eq("clinic_id", scope)
        .eq("collection_mode", "home")
        .eq("appointment_date", day)
        .in_("status", ["pending_payment", "confirmed", "completed", "cancelled"])
    )
    if branch_id:
        q = q.eq("branch_id", branch_id)
    elif user.role == "staff" and getattr(user, "branch_id", None):
        q = restrict_to_branch(q, user.branch_id)
    rows = (await sb(q.order("collection_slot").order("created_at").limit(1000))).data or []
    phlebs = {str(p["id"]): p for p in await hc.list_phlebotomists(scope, active_only=False)}
    tests = await _fasting_by_test(scope, rows)
    summary: dict = {}
    for r in rows:
        key = r["collection_status"] if r["status"] in ("confirmed", "completed") else r["status"]
        summary[key] = summary.get(key, 0) + 1
    return {"date": day, "visits": [_card(r, phlebs, tests) for r in rows], "summary": summary}


@router.get("/phlebotomists")
async def list_phlebotomists(
    clinic_id: str = "default",
    date: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    scope, _ = await _manager_scope(user, clinic_id)
    day = _parse_date(date)
    phlebs = await hc.list_phlebotomists(scope, active_only=False)
    res = await sb(
        supabase.table("appointments").select("phlebotomist_id, collection_status")
        .eq("clinic_id", scope).eq("collection_mode", "home").eq("appointment_date", day)
        .in_("status", list(hc.LIVE_BOOKING_STATUSES)).not_.is_("phlebotomist_id", "null").limit(2000)
    )
    load: dict = {}
    for r in res.data or []:
        entry = load.setdefault(str(r["phlebotomist_id"]), {"total": 0, "done": 0})
        entry["total"] += 1
        if r.get("collection_status") in ("collected", "delivered"):
            entry["done"] += 1
    if user.role == "staff" and getattr(user, "branch_id", None):
        phlebs = [p for p in phlebs if not p.get("branch_id") or str(p["branch_id"]) == str(user.branch_id)]
    return {
        "date": day,
        "phlebotomists": [
            {
                **p,
                "name": hc.display_name(p),
                "off_dates": hc.upcoming_off_dates(p.get("off_dates")),
                "off_on_date": hc.is_off(p, day),
                **load.get(str(p["id"]), {"total": 0, "done": 0}),
            }
            for p in sorted(phlebs, key=lambda p: hc.display_name(p).lower())
        ],
    }


class OffDatesIn(BaseModel):
    #: The phlebotomist's complete list of upcoming leave days (replaces it).
    dates: list[str] = Field(default_factory=list, max_length=120)


@router.put("/phlebotomists/{phleb_id}/off-dates")
async def set_off_dates(
    phleb_id: str,
    body: OffDatesIn,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Mark a phlebotomist on leave. Visits already assigned to them on a newly
    added date are handed to an available colleague (admins are alerted when
    nobody is free); when every phlebotomist serving a branch is off, that
    date offers no home slots on WhatsApp."""
    scope, _ = await _manager_scope(user, clinic_id)
    if not is_uuid(phleb_id):
        raise HTTPException(status_code=404, detail="Phlebotomist not found")
    phleb = next((p for p in await hc.list_phlebotomists(scope, active_only=False) if str(p["id"]) == phleb_id), None)
    if not phleb:
        raise HTTPException(status_code=404, detail="Phlebotomist not found")
    if user.role == "staff" and getattr(user, "branch_id", None) and str(phleb.get("branch_id") or "") != str(user.branch_id):
        raise HTTPException(status_code=403, detail="Branch staff can only manage their own branch's phlebotomists.")
    bad = [d for d in body.dates if d not in hc.upcoming_off_dates([d], "0000-00-00")]
    if bad:
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD.")
    dates = hc.upcoming_off_dates(body.dates)
    before = set(hc.upcoming_off_dates(phleb.get("off_dates")))
    await sb(
        supabase.table("clinic_admins").update({"off_dates": dates})
        .eq("clinic_id", scope).eq("id", phleb_id)
    )
    added = sorted(set(dates) - before)
    released = await hc.release_visits_of(scope, phleb_id, added) if added else 0
    await log_admin_action(user, "set_phlebotomist_off_dates", "clinic_admin", phleb_id,
                           {"dates": dates, "added": added, "released_visits": released}, _ip(request))
    return {"success": True, "off_dates": dates, "released_visits": released}


async def _owned_visit(scope: str, visit_id: str, user: AdminUser) -> dict:
    if not is_uuid(visit_id):
        raise HTTPException(status_code=404, detail="Visit not found")
    visit = await hc.get_visit(scope, visit_id)
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found")
    if not _is_phleb(user):
        enforce_branch_scope(user, visit.get("branch_id"))
    return visit


class AssignIn(BaseModel):
    phlebotomist_id: str


@router.post("/visits/{visit_id}/assign")
async def assign_visit(
    visit_id: str,
    body: AssignIn,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    scope, _ = await _manager_scope(user, clinic_id)
    visit = await _owned_visit(scope, visit_id, user)
    phleb = next(
        (p for p in await hc.list_phlebotomists(scope) if str(p["id"]) == body.phlebotomist_id), None
    )
    if not phleb:
        raise HTTPException(status_code=404, detail="Phlebotomist not found or inactive")
    if not hc.eligible_for(phleb, visit):
        raise HTTPException(status_code=422, detail="That phlebotomist works at a different branch.")
    if hc.is_off(phleb, visit.get("appointment_date")):
        raise HTTPException(status_code=422, detail="That phlebotomist is on leave on this visit's date.")
    if visit.get("status") != "confirmed":
        raise HTTPException(status_code=409, detail="Only a confirmed booking can be assigned.")
    if not await hc.assign(scope, visit, phleb):
        raise HTTPException(status_code=409, detail="This visit changed meanwhile. Refresh and try again.")
    await log_admin_action(user, "assign_home_collection", "appointment", visit_id,
                           {"phlebotomist_id": body.phlebotomist_id, "from": visit.get("phlebotomist_id")}, _ip(request))
    return {"success": True}


@router.post("/auto-assign")
async def auto_assign_pending(
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    """Assign every upcoming confirmed visit that has no phlebotomist yet."""
    scope, _ = await _manager_scope(user, clinic_id)
    today = datetime.now(hc.IST).strftime("%Y-%m-%d")
    q = (
        supabase.table("appointments").select("id, branch_id")
        .eq("clinic_id", scope).eq("collection_mode", "home").eq("status", "confirmed")
        .is_("phlebotomist_id", "null").gte("appointment_date", today)
    )
    if user.role == "staff" and getattr(user, "branch_id", None):
        q = restrict_to_branch(q, user.branch_id)
    rows = (await sb(q.limit(200))).data or []
    assigned = 0
    for r in rows:
        if await hc.auto_assign(scope, str(r["id"])):
            assigned += 1
    return {"success": True, "assigned": assigned, "remaining": len(rows) - assigned}


# ═══════ phlebotomist view ═══════


async def _phleb_account(user: AdminUser, scope: str) -> dict:
    res = await sb(
        supabase.table("clinic_admins").select("id, full_name, phone, username, is_active, staff_role, branch_id")
        .eq("clinic_id", scope).eq("id", str(user.user_id)).limit(1)
    )
    row = (res.data or [None])[0]
    if not row or not row.get("is_active") or row.get("staff_role") != PHLEBOTOMIST:
        raise HTTPException(status_code=403, detail="This login is not an active phlebotomist account.")
    return row


@router.get("/my")
async def my_visits(
    clinic_id: str = "default",
    date: Optional[str] = None,
    user: AdminUser = Depends(verify_credentials),
):
    if not _is_phleb(user):
        raise HTTPException(status_code=403, detail="Only a phlebotomist login has its own visit list.")
    scope, _ = await _scope(user, clinic_id)
    me = await _phleb_account(user, scope)
    day = _parse_date(date)
    rows = (await sb(
        supabase.table("appointments").select(VISIT_COLUMNS)
        .eq("clinic_id", scope).eq("phlebotomist_id", me["id"]).eq("collection_mode", "home")
        .eq("appointment_date", day).in_("status", list(hc.LIVE_BOOKING_STATUSES))
        .order("collection_slot").limit(200)
    )).data or []
    upcoming = (await sb(
        supabase.table("appointments").select("appointment_date")
        .eq("clinic_id", scope).eq("phlebotomist_id", me["id"]).eq("collection_mode", "home")
        .eq("status", "confirmed").gte("appointment_date", datetime.now(hc.IST).strftime("%Y-%m-%d"))
        .in_("collection_status", ["assigned", "en_route"]).limit(500)
    )).data or []
    by_day: dict = {}
    for r in upcoming:
        by_day[r["appointment_date"]] = by_day.get(r["appointment_date"], 0) + 1
    tests = await _fasting_by_test(scope, rows)
    return {
        "date": day,
        "me": {"name": hc.display_name(me), "phone": me.get("phone")},
        "visits": [_card(r, {str(me["id"]): me}, tests) for r in rows],
        "upcoming": [{"date": d, "count": n} for d, n in sorted(by_day.items())],
    }


class StatusIn(BaseModel):
    status: str
    note: Optional[str] = Field(None, max_length=500)


@router.post("/visits/{visit_id}/status")
async def update_visit_status(
    visit_id: str,
    body: StatusIn,
    request: Request,
    clinic_id: str = "default",
    user: AdminUser = Depends(verify_credentials),
):
    if body.status not in hc.STATUSES or body.status in ("unassigned", "assigned"):
        raise HTTPException(status_code=422, detail="Unknown status")
    if _is_phleb(user):
        scope, _ = await _scope(user, clinic_id)
        me = await _phleb_account(user, scope)
        visit = await _owned_visit(scope, visit_id, user)
        # Someone else's visit is indistinguishable from a missing one.
        if str(visit.get("phlebotomist_id") or "") != str(me["id"]):
            raise HTTPException(status_code=404, detail="Visit not found")
    else:
        scope, _ = await _manager_scope(user, clinic_id)
        visit = await _owned_visit(scope, visit_id, user)
    if visit.get("status") not in hc.LIVE_BOOKING_STATUSES:
        raise HTTPException(status_code=409, detail="This booking is no longer active.")
    note = (body.note or "").strip() or None
    if body.status == "failed" and not note:
        raise HTTPException(status_code=422, detail="Add a short reason (e.g. patient not at home).")
    if not await hc.set_status(scope, visit, body.status, note):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot move this visit from '{visit.get('collection_status')}' to '{body.status}'. Refresh and try again.",
        )
    await log_admin_action(user, "home_collection_status", "appointment", visit_id,
                           {"from": visit.get("collection_status"), "to": body.status}, _ip(request))
    return {"success": True, "status": body.status}
