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
    went_live_at: Optional[str] = None
    version: int = Field(0, ge=0)


DEFAULT_SETTINGS = DxSettings().model_dump()

_ERRORS = {
    "dx_settings_conflict": (409, "These settings were changed by someone else. Reload and try again."),
    "dx_settings_invalid": (422, "Settings must be an object."),
    "dx_clinic_not_found": (404, "Clinic not found."),
}


def _code_of(exc: Exception) -> str:
    m = re.search(r"dx_[a-z_]+", str(getattr(exc, "message", None) or exc))
    return m.group(0) if m else ""


def raise_for(exc: Exception) -> None:
    """Map a dx_* database error; anything else is a fixed 500 (never raw DB text)."""
    code, msg = _code_of(exc), str(getattr(exc, "message", None) or exc)
    if code in _ERRORS:
        status, message = _ERRORS[code]
        raise HTTPException(status_code=status, detail={"code": code, "message": message})
    for constraint, status, message in (
        ("dx_sites_clinic_code_key", 409, "Another site already uses this code."),
        ("dx_sites_clinic_branch_key", 409, "This branch already has a site."),
        ("dx_sites_branch_fk", 404, "Branch not found."),
    ):
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


def setup_checklist(settings: dict, sites: list[dict]) -> list[dict]:
    """What the lab still has to configure. Departments, containers and signatories
    arrive with the catalogue (DX-M2); until then they show as not yet available."""
    active = [s for s in sites if s.get("is_active")]
    return [
        {"key": "sites", "label": "Sites", "done": bool(active),
         "detail": f"{len(active)} active site(s)" if active else "Add a site for each branch"},
        {"key": "departments", "label": "Departments", "done": False, "available": False},
        {"key": "containers", "label": "Containers", "done": False, "available": False},
        {"key": "signatories", "label": "Signatories", "done": False, "available": False},
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
