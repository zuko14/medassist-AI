"""Kriya Diagnostic OS — foundation service (migration 125, DX-M1).

Settings (validated, CAS-versioned), sites (one per branch), provisioning,
the setup checklist and the owner's deactivation preview.
Design: docs/diagnostic-os/DATA-MODEL.md §2, ADR-DX-02 / ADR-DX-14.

Zero-LLM: imports nothing from the AI, voice or assistant packages
(tests/test_dx_no_llm.py).
"""

import logging
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.database import sb, scoped_query, supabase

logger = logging.getLogger(__name__)

PREFIX = r"^[A-Z0-9][A-Z0-9\-/]{0,7}$"


class DxSettings(BaseModel):
    """clinics.dx_settings (DATA-MODEL §2). Defaults apply to any missing key."""

    model_config = ConfigDict(extra="forbid")

    approval_levels: Literal[2, 3] = 2
    collect_before_payment: bool = False
    auto_release: Literal["off", "per_department", "when_complete"] = "when_complete"
    delivery_owner: dict[str, Literal["connector", "shadow", "dx"]] = Field(default_factory=dict)
    sample_seq_floor: int = Field(1, ge=1, le=99_999_999)
    label_mode: Literal["per_sample", "per_test"] = "per_sample"
    register_label: bool = False
    extra_labels: int = Field(0, ge=0, le=3)
    tat_clock: Literal["registration", "receipt"] = "registration"
    accession_prefix: str = Field("LAB-", pattern=PREFIX)
    batch_prefix: str = Field("TRN-", pattern=PREFIX)
    b2b_prefix: str = Field("B2B-", pattern=PREFIX)
    referrer_copy_default: bool = False
    critical_call_required: bool = True
    share_link_ttl_hours: int = Field(72, ge=1, le=720)
    instrument_auto_enter: bool = False
    max_staff_discount_percent: int = Field(10, ge=0, le=100)   # desk discount ceiling; DX_ADMIN / admins: 100
    went_live_at: Optional[str] = None
    version: int = Field(0, ge=0)


DEFAULT_SETTINGS = DxSettings().model_dump()

# dx_* codes raised by migrations 125/126 and the constraint names behind user mistakes.
# Matched longest-first against the database message; the raw text is never returned.
_ERRORS = {
    "dx_settings_conflict": (409, "These settings were changed by someone else. Reload and try again."),
    "dx_settings_invalid": (422, "Settings must be an object."),
    "dx_clinic_not_found": (404, "Clinic not found."),
    "dx_component_self": (422, "A panel or package cannot contain itself."),
    "dx_component_parent_kind": (422, "Only a panel or a package can hold other tests."),
    "dx_component_depth": (422, "A panel holds tests; a package holds panels or tests. Nothing deeper."),
    "dx_kind_locked": (409, "This test is part of a panel or package (or holds members): remove those links first."),
    "dx_parameters_on_group": (409, "Panels and packages have no parameters of their own; add them to a test."),
    "dx_test_changed": (409, "The test changed since you reviewed it. Reload, review again and approve."),
    "dx_test_not_found": (404, "Test not found."),
    # migration 127: registration, billing, cancellation
    "dx_test_not_approved": (409, "This test is not approved by the lab director yet"),
    "dx_test_inactive": (409, "This test is inactive"),
    "dx_gender_restricted": (409, "This test is not offered for the patient's sex"),
    "dx_group_empty": (409, "This panel or package has no member tests"),
    "dx_booking_invalid": (409, "A booking is not a confirmed lab-test booking of this patient."),
    "dx_booking_linked": (409, "A booking is already part of another visit."),
    "dx_b2b_on_hold": (409, "This B2B account is on hold or closed."),
    "dx_b2b_not_enabled": (409, "B2B credit billing is not available yet; bill the patient for now."),
    "dx_no_collection_site": (409, "This branch has no active Diagnostic OS site. Add one in DX Setup."),
    "dx_no_processing_site": (409, "No processing site can take these samples. Check the sites in DX Setup."),
    "dx_discount_over_limit": (403, "The discount is above your limit."),
    "dx_discount_invalid": (422, "The discount must be between 0 and the price."),
    "dx_fee_invalid": (422, "A fee needs a non-negative amount."),
    "dx_no_items": (422, "Add at least one test."),
    "dx_idempotency_required": (422, "An idempotency key is required."),
    "dx_patient_not_found": (404, "Patient not found."),
    "dx_referrer_not_found": (404, "Referrer not found."),
    "dx_account_not_found": (404, "B2B account not found."),
    "dx_visit_not_found": (404, "Visit not found."),
    "dx_item_not_found": (404, "Item not found."),
    "dx_visit_closed": (409, "This visit is cancelled or fully reported."),
    "dx_item_in_progress": (409, "This test is already being processed and cannot be cancelled."),
    "dx_refund_first": (409, "This test is on an issued bill. Refund and void the bill first."),
    "dx_visit_has_samples": (409, "A sample of this visit has been collected; cancel tests individually."),
    "dx_not_patient_billing": (409, "This visit is billed to its B2B account, not the patient."),
    "dx_bill_lines_managed": (409, "This is a Diagnostic OS bill. Change its tests from DX Registration."),
    "dx_bill_line_invalid": (409, "That bill line does not match the visit's items."),
    "dx_item_already_billed": (409, "This test is already on a bill."),
    "dx_managed": (409, "Diagnostic OS records change only through Diagnostic OS actions."),
    "dx_test_ordered": (409, "This test has been ordered in Diagnostic OS — deactivate it instead."),
}
_CONSTRAINTS = (
    ("dx_sites_clinic_code_key", 409, "Another site already uses this code."),
    ("dx_sites_clinic_branch_key", 409, "This branch already has a site."),
    ("dx_sites_branch_fk", 404, "Branch not found."),
    ("dx_departments_clinic_code_key", 409, "Another department already uses this code."),
    ("dx_containers_clinic_code_key", 409, "Another container already uses this code."),
    ("uq_dx_signatories_doctor_all", 409, "This doctor is already a signatory for every department."),
    ("dx_signatories_doctor_dept_key", 409, "This doctor is already a signatory for this department."),
    ("dx_signatories_doctor_fk", 404, "Doctor not found."),
    ("dx_signatories_department_fk", 404, "Department not found."),
    ("dx_test_components_pair_key", 409, "This test is already in the panel or package."),
    ("dx_test_components_not_self", 422, "A panel or package cannot contain itself."),
    ("dx_test_components_child_fk", 404, "Test not found."),
    ("lab_tests_department_fk", 404, "Department not found."),
    ("lab_tests_container_fk", 404, "Container not found."),
    ("uq_lab_tests_test_code", 409, "Another test already uses this code."),
    ("idx_unique_lab_test_name_per_branch", 409, "A test with this name already exists."),
    ("lab_tests_dx_check", 422, "One of the test fields is out of range."),
    ("lab_test_parameters_dx_check", 422, "One of the parameter fields is out of range."),
    ("dx_b2b_payer_routed", 422, "Insurance / TPA and corporate accounts deliver reports to the payer. A patient copy "
                                 "needs a recorded authorisation (reason)."),
    ("dx_b2b_authorisation_shape", 422, "A patient-copy authorisation needs a reason."),
    ("dx_b2b_accounts_clinic_code_key", 409, "Another B2B account already uses this code."),
    ("dx_referrers_account_fk", 404, "B2B account not found."),
    ("lab_test_parameters_ranges", 422, "Ranges must be ordered: critical low <= normal low <= normal high <= critical high."),
)


def raise_for(exc: Exception) -> None:
    """Map a dx_* database error; anything else is a fixed 500 (never raw DB text)."""
    if isinstance(exc, HTTPException):
        raise exc
    msg = str(getattr(exc, "message", None) or exc)
    for code in sorted(_ERRORS, key=len, reverse=True):
        if code in msg:
            status, message = _ERRORS[code]
            # 'dx_test_not_approved:CBC' carries the test name the desk needs to see
            m = re.search(re.escape(code) + r":([^\n'\"}]+)", msg)
            reason = m.group(1).strip() if m else None
            raise HTTPException(status_code=status, detail={"code": code, "reason": reason,
                                                            "message": f"{message}: {reason}." if reason else message})
    for constraint, status, message in _CONSTRAINTS:
        if constraint in msg:
            raise HTTPException(status_code=status, detail=message)
    logger.error(f"DX_DB_ERROR type={type(exc).__name__}")
    raise HTTPException(status_code=500, detail="The diagnostic operation failed. Nothing was changed; please retry.")


async def _rows(query) -> list[dict]:
    res = await sb(query)
    return list(res.data or [])


# ── settings ────────────────────────────────────────────────────────────────

def get_dx_settings(clinic: dict) -> dict:
    return {**DEFAULT_SETTINGS, **(clinic.get("dx_settings") or {})}


def validate_settings(current: dict, changes: dict) -> dict:
    """Merge `changes` over the stored settings and validate the result (422 on error)."""
    merged = {**get_dx_settings({"dx_settings": current}), **changes}
    try:
        out = DxSettings(**merged).model_dump()
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=[{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()])
    # The sample counter is raised to the floor at go-live and never lowered (ADR-DX-05 / AUDIT A8).
    old_floor = int((current or {}).get("sample_seq_floor") or 1)
    if (current or {}).get("went_live_at") and out["sample_seq_floor"] < old_floor:
        raise HTTPException(status_code=409, detail="The sample number floor cannot be lowered after go-live.")
    return out


async def update_dx_settings(clinic_id: str, clinic: dict, changes: dict, expected_version: int) -> dict:
    """Validated write with compare-and-swap on settings.version (dx_update_settings)."""
    merged = validate_settings(clinic.get("dx_settings") or {}, changes)
    merged.pop("version", None)
    try:
        res = await sb(supabase.rpc("dx_update_settings", {"p_clinic_id": clinic_id, "p_settings": merged,
                                                           "p_expected_version": expected_version}))
    except Exception as e:
        raise_for(e)
    return {**DEFAULT_SETTINGS, **(res.data or {})}


# ── sites ───────────────────────────────────────────────────────────────────

async def list_sites(clinic_id: str, branch_id: Optional[str] = None) -> list[dict]:
    q = scoped_query("dx_sites", clinic_id).order("code")
    return await _rows(q.eq("branch_id", branch_id) if branch_id else q)


async def get_site(clinic_id: str, site_id: str) -> dict:
    rows = await _rows(scoped_query("dx_sites", clinic_id).eq("id", site_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Site not found.")
    return rows[0]


async def create_or_update_site(clinic_id: str, body: dict, site_id: Optional[str] = None) -> dict:
    try:
        if site_id:
            res = await sb(supabase.table("dx_sites").update(body).eq("clinic_id", clinic_id).eq("id", site_id))
        else:
            # unscoped: insert_scoped_by_payload
            res = await sb(supabase.table("dx_sites").insert({**body, "clinic_id": clinic_id}))
    except Exception as e:
        raise_for(e)
    if not res.data:
        raise HTTPException(status_code=404, detail="Site not found.")
    return res.data[0]


def _site_code(name: str, taken: set[str]) -> str:
    """Branch name -> a free 2–6 character label code ("Banjara Hills" -> "BANJ")."""
    base = (re.sub(r"[^A-Z0-9]", "", (name or "").upper()) or "SITE")[:4].ljust(2, "X")
    code, n = base, 1
    while code in taken:
        n += 1
        code = f"{base[:6 - len(str(n))]}{n}"
    return code


# ── provisioning, checklist, go-live, deactivation ──────────────────────────

async def provision_defaults(clinic_id: str) -> None:
    """Owner enable: one site per active branch that has none (existing sites and
    their edits are kept); settings keys are filled by get_dx_settings on read."""
    branches = await _rows(scoped_query("branches", clinic_id, "id, name, is_active"))
    sites = await list_sites(clinic_id)
    have = {s["branch_id"] for s in sites}
    taken = {s["code"] for s in sites}
    for b in branches:
        if b.get("is_active") is False or b["id"] in have:
            continue
        code = _site_code(b.get("name") or "", taken)
        taken.add(code)
        name = (b.get("name") or "").strip()
        await create_or_update_site(clinic_id, {"branch_id": b["id"], "code": code,
                                                "display_name": name if len(name) >= 2 else f"Site {code}"})


def setup_checklist(settings: dict, sites: list[dict], counts: Optional[dict] = None) -> list[dict]:
    """What the lab still has to configure; `counts` = active departments / containers /
    signatories (catalogue, migration 126)."""
    active = [s for s in sites if s.get("is_active")]
    counts = counts or {}

    def item(key: str, label: str, todo: str) -> dict:
        n = int(counts.get(key) or 0)
        return {"key": key, "label": label, "done": n > 0, "detail": f"{n} active" if n else todo}
    return [
        {"key": "sites", "label": "Sites", "done": bool(active),
         "detail": f"{len(active)} active site(s)" if active else "Add a site for each branch"},
        item("departments", "Departments", "Add departments (or apply the seed library)"),
        item("containers", "Containers", "Add tube types (or apply the seed library)"),
        item("signatories", "Signatories", "Add the doctors who approve reports"),
        {"key": "settings", "label": "Settings", "done": int(settings.get("version") or 0) >= 1,
         "detail": "Saved" if int(settings.get("version") or 0) >= 1 else "Review and save the lab settings"},
    ]


async def go_live(clinic_id: str, clinic: dict, expected_version: int) -> dict:
    """Stamp went_live_at once at least one active site can process samples."""
    settings = get_dx_settings(clinic)
    if settings.get("went_live_at"):
        raise HTTPException(status_code=409, detail="This lab is already live.")
    sites = await list_sites(clinic_id)
    if not any(s.get("is_active") and s.get("kind") != "collection" for s in sites):
        raise HTTPException(status_code=409, detail="Add an active processing site before going live.")
    return await update_dx_settings(clinic_id, clinic, {"went_live_at": datetime.now(timezone.utc).isoformat()},
                                    expected_version)


async def deactivation_preview(clinic_id: str) -> dict:
    """What an owner disable would leave running (ADR-DX-14).
    shortcut: always zero until visits/samples/results exist (DX-M3/M4/M6); each of
    those migrations must count its open rows here and set blocking."""
    return {"visits_in_process": 0, "samples_in_transit": 0, "unreleased_results": 0, "blocking": False}


# ── visits (migration 127, DX-20/21/23) ─────────────────────────────────────
# Pricing, shares, explosion, tubes and numbering happen in SQL (dx_register_visit,
# dx_price_items); this layer resolves who is acting and what they may do.

def _actor(user) -> tuple[Optional[str], str]:
    from app.utils.helpers import actor_uuid
    return actor_uuid(user), str(getattr(user, "username", None) or "system")[:100]


def discount_limit_percent(user, clinic: dict) -> int:
    """Admins and DX_ADMIN may discount fully; everyone else up to the lab's desk ceiling."""
    from app.services.permissions import holds_dx_permission
    if holds_dx_permission(user, "DX_ADMIN"):      # admins hold every DX grant
        return 100
    return int(get_dx_settings(clinic).get("max_staff_discount_percent", 10))


async def rpc(name: str, params: dict):
    try:
        res = await sb(supabase.rpc(name, params))
    except Exception as e:
        raise_for(e)
    return res.data


async def quote(clinic_id: str, clinic: dict, payload: dict, user) -> list[dict]:
    return await rpc("dx_quote", {"p_clinic_id": clinic_id, "p_payload": {
        **payload, "discount_limit_percent": discount_limit_percent(user, clinic)}})


async def register_visit(clinic_id: str, clinic: dict, payload: dict, user) -> dict:
    aid, name = _actor(user)
    return await rpc("dx_register_visit", {"p_clinic_id": clinic_id, "p_actor_id": aid, "p_actor_name": name,
                                           "p_payload": {**payload,
                                                         "discount_limit_percent": discount_limit_percent(user, clinic)}})


async def add_items(clinic_id: str, clinic: dict, accession_id: str, items: list[dict], key: str, user) -> dict:
    aid, name = _actor(user)
    return await rpc("dx_add_items", {"p_clinic_id": clinic_id, "p_accession_id": accession_id, "p_items": items,
                                      "p_idempotency_key": key,
                                      "p_discount_limit_percent": discount_limit_percent(user, clinic),
                                      "p_actor_id": aid, "p_actor_name": name})


async def cancel_item(clinic_id: str, item_id: str, reason: str, user) -> dict:
    aid, name = _actor(user)
    return await rpc("dx_cancel_item", {"p_clinic_id": clinic_id, "p_item_id": item_id, "p_reason": reason,
                                        "p_actor_id": aid, "p_actor_name": name})


async def cancel_visit(clinic_id: str, accession_id: str, reason: str, user) -> dict:
    aid, name = _actor(user)
    return await rpc("dx_cancel_visit", {"p_clinic_id": clinic_id, "p_accession_id": accession_id,
                                         "p_reason": reason, "p_actor_id": aid, "p_actor_name": name})


async def get_visit(clinic_id: str, accession_id: str) -> dict:
    out = await rpc("dx_visit_json", {"p_clinic_id": clinic_id, "p_accession_id": accession_id})
    if not out:
        raise HTTPException(status_code=404, detail="Visit not found.")
    out["timeline"] = await _rows(scoped_query("dx_events", clinic_id, "id, kind, actor_name, details, created_at")
                                  .eq("accession_id", accession_id).order("id"))
    return out


async def item_of(clinic_id: str, accession_id: str, item_id: str) -> dict:
    rows = await _rows(scoped_query("dx_accession_items", clinic_id, "id, accession_id").eq("id", item_id)
                       .eq("accession_id", accession_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Item not found.")
    return rows[0]


VISIT_LIST_COLUMNS = ("id, accession_number, branch_id, patient_id, family_member_id, source, status, priority, "
                      "billing_mode, b2b_account_id, referrer_id, patient_snapshot, created_at")


async def site_branch_ids(clinic_id: str, accession_ids: list[str]) -> dict[str, set]:
    """Branches of the sites a visit's tubes are collected / processed at (site scope, SECURITY §4)."""
    if not accession_ids:
        return {}
    samples = await _rows(scoped_query("dx_samples", clinic_id, "accession_id, collection_site_id, processing_site_id")
                          .in_("accession_id", accession_ids))
    sites = {s["id"]: s["branch_id"] for s in await _rows(scoped_query("dx_sites", clinic_id, "id, branch_id"))}
    out: dict[str, set] = {}
    for s in samples:
        for col in ("collection_site_id", "processing_site_id"):
            if s.get(col) in sites:
                out.setdefault(s["accession_id"], set()).add(sites[s[col]])
    return out


async def list_visits(clinic_id: str, pinned_branch: Optional[str], day: Optional[str] = None,
                      status: Optional[str] = None, q: Optional[str] = None, limit: int = 200) -> list[dict]:
    from datetime import date as _date, datetime as _dt, time as _time, timedelta
    from app.utils.helpers import IST, today_ist
    d = _date.fromisoformat(day) if day else today_ist()
    start = _dt.combine(d, _time.min, tzinfo=IST).astimezone(timezone.utc)
    query = scoped_query("dx_accessions", clinic_id, VISIT_LIST_COLUMNS).gte("created_at", start.isoformat()) \
        .lt("created_at", (start + timedelta(days=1)).isoformat())
    if status:
        query = query.eq("status", status)
    rows = await _rows(query.order("created_at", desc=True).limit(limit))
    if pinned_branch:
        reach = await site_branch_ids(clinic_id, [r["id"] for r in rows if str(r["branch_id"]) != pinned_branch])
        rows = [r for r in rows if str(r["branch_id"]) == pinned_branch or pinned_branch in
                {str(b) for b in reach.get(r["id"], set())}]
    if q:
        needle = q.strip().casefold()
        rows = [r for r in rows if needle in (r.get("accession_number") or "").casefold()
                or needle in str((r.get("patient_snapshot") or {}).get("name") or "").casefold()
                or needle in str((r.get("patient_snapshot") or {}).get("mrn") or "").casefold()]
    return rows


# ── B2B subjects without their own phone (ADR-DX-21) ────────────────────────

async def ensure_b2b_subject(clinic_id: str, account_id: str, body: dict) -> dict:
    """A subject with their own distinct phone is a normal registry patient (the caller
    registers them). Otherwise: a dependant of the account's holder patient, flagged
    dx_b2b_account_id so the desk phone's WhatsApp chat never lists them."""
    acc_rows = await _rows(scoped_query("dx_b2b_accounts", clinic_id).eq("id", account_id).limit(1))
    if not acc_rows:
        raise HTTPException(status_code=404, detail="B2B account not found.")
    acc = acc_rows[0]
    holder_phone = acc.get("desk_whatsapp") or acc.get("contact_phone")
    if not holder_phone:
        raise HTTPException(status_code=422, detail="Give the account a contact or desk phone first.")
    phone = (body.get("phone") or "").strip()
    if phone and phone != holder_phone:
        raise HTTPException(status_code=422, detail="This patient has their own phone: register them as a patient.")
    holder_id = acc.get("holder_patient_id")
    if not holder_id:
        found = await _rows(scoped_query("patients", clinic_id, "id").eq("phone", holder_phone).limit(1))
        if found:
            holder_id = found[0]["id"]
        else:
            # unscoped: insert_scoped_by_payload
            ins = await sb(supabase.table("patients").insert({"clinic_id": clinic_id, "phone": holder_phone,
                                                              "name": acc["name"][:100]}))
            holder_id = ins.data[0]["id"]
        await sb(supabase.table("dx_b2b_accounts").update({"holder_patient_id": holder_id})
                 .eq("clinic_id", clinic_id).eq("id", account_id))
    row = {"clinic_id": clinic_id, "primary_phone": holder_phone, "full_name": body["name"].strip()[:100],
           "relationship": "b2b_subject", "gender": body.get("gender"), "dx_b2b_account_id": account_id}
    if body.get("age_years") is not None:
        row.update({"age_years": body["age_years"], "age_recorded_on": datetime.now(timezone.utc).date().isoformat()})
    try:
        # unscoped: insert_scoped_by_payload
        fm = (await sb(supabase.table("family_members").insert(row))).data[0]
    except Exception as e:
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            # same name under the same desk phone: one subject, reused
            fm = (await _rows(scoped_query("family_members", clinic_id).eq("primary_phone", holder_phone)
                              .eq("full_name", row["full_name"]).limit(1)))[0]
        else:
            raise_for(e)
    from app.services.opd import assign_mrn
    mrn = fm.get("mrn") or await assign_mrn(clinic_id, holder_id, fm["id"])
    return {"patient_id": holder_id, "family_member_id": fm["id"], "mrn": mrn, "name": fm["full_name"]}


# ── today's bookings (W3) ───────────────────────────────────────────────────

async def bookings_today(clinic_id: str, branch_id: Optional[str], day: Optional[str] = None) -> list[dict]:
    from app.utils.helpers import today_ist
    d = day or today_ist().isoformat()
    q = scoped_query("appointments", clinic_id, "id, patient_id, family_member_id, patient_name, patient_phone, "
                     "lab_test_id, lab_test_name, appointment_date, collection_mode, booking_channel, amount_paise, "
                     "payment_id, home_collection_fee_paise, branch_id, booking_ref, status") \
        .eq("booking_type", "lab_test").eq("status", "confirmed").eq("appointment_date", d)
    if branch_id:
        q = q.eq("branch_id", branch_id)
    rows = await _rows(q.order("created_at"))
    if rows:
        linked = {r["appointment_id"] for r in await _rows(scoped_query("dx_accession_bookings", clinic_id,
                                                                        "appointment_id")
                                                           .in_("appointment_id", [r["id"] for r in rows]))}
        rows = [r for r in rows if r["id"] not in linked]
    return rows


async def booking_patient(clinic_id: str, appointments: list[dict]) -> tuple[str, Optional[str]]:
    """All bookings must belong to one person (patient + family member)."""
    people = {(a.get("patient_id"), a.get("family_member_id"), a.get("patient_phone")) for a in appointments}
    if len(people) != 1:
        raise HTTPException(status_code=422, detail="Convert one patient's bookings at a time.")
    pid, fmid, phone = people.pop()
    if not pid:
        found = await _rows(scoped_query("patients", clinic_id, "id").eq("phone", phone).limit(1))
        if not found:
            raise HTTPException(status_code=409, detail="Register this patient first (no registry record for the "
                                                        "booking's phone).")
        pid = found[0]["id"]
    return pid, fmid
