"""Report templates (DX-44): starter templates, CRUD, assets, preview. Zero-LLM.

Resolution at release time is the SQL cascade in dx_create_release (migration 131):
visit override -> white-label B2B account template -> registration branch -> clinic
default -> built-in "classic_band". Published templates never change (trigger); an edit
is a new version. Design: REPORT-TEMPLATES.md §4, ADR-DX-08.
"""

import asyncio
import copy
import hashlib
import io
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services.dx_template_schema import asset_paths, validate_layout

ASSET_BUCKET = "lab-reports"            # private; DX assets live under <clinic>/dx-assets/
ASSET_MAX_BYTES = 1024 * 1024
ASSET_MAX_PX = 3000
COLUMNS = "id, owner_kind, owner_id, name, version, status, published_at, published_by, created_at, updated_at"

STARTERS: dict[str, dict] = {
    # the lab's own letterhead with a coloured band (the look of most Indian lab reports)
    "classic_band": {"header": {"mode": "composed", "height_mm": 30,
                                "composed": {"title": "DIAGNOSTIC REPORT", "accent_hex": "#F28C28", "band": True}},
                     "body": {"font_pt": 9}},
    # quiet: no band, smaller header, signatures at the end
    "minimal": {"header": {"mode": "composed", "height_mm": 22,
                           "composed": {"title": "LABORATORY REPORT", "accent_hex": "#333333", "band": False}},
                "body": {"font_pt": 9, "show_previous_value": False},
                "signatures": {"placement": "end"}},
    # a B2B hospital's letterhead; the lab's "Tested at" line is added by the renderer (ADR-DX-08)
    "hospital_white_label": {"header": {"mode": "composed", "height_mm": 28,
                                        "composed": {"name": "Hospital name", "title": "LABORATORY REPORT",
                                                     "accent_hex": "#1F6FB2", "band": True,
                                                     "lines": ["Hospital address", "Phone"]}},
                             "footer": {"lines": []}},
}
DEFAULT_STARTER = "classic_band"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def starter_layout(name: str = DEFAULT_STARTER, overrides: Optional[dict] = None) -> dict:
    if name not in STARTERS:
        raise HTTPException(status_code=422, detail="Unknown starter template.")
    return validate_layout(_merge(STARTERS[name], overrides or {}))


def snapshot_layout(template_snapshot: dict) -> dict:
    """The layout a release renders with: its frozen template, else the built-in default."""
    layout = (template_snapshot or {}).get("layout")
    return validate_layout(layout) if layout else starter_layout()


def check_layout(layout: dict, clinic_id: str) -> dict:
    try:
        return validate_layout(layout, clinic_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"code": "dx_template_invalid",
                                                     "message": str(e).split("\n")[0][:300]})


async def _rows(query) -> list[dict]:
    return list((await sb(query)).data or [])


async def list_templates(clinic_id: str, owner_kind: Optional[str] = None, branch_id: Optional[str] = None) -> list[dict]:
    q = scoped_query("dx_report_templates", clinic_id, COLUMNS)
    if owner_kind:
        q = q.eq("owner_kind", owner_kind)
    rows = await _rows(q.order("name").order("version", desc=True))
    if branch_id:      # a branch-pinned admin sees the clinic default and their own branch
        rows = [r for r in rows if r["owner_kind"] == "clinic" or str(r.get("owner_id")) == branch_id]
    return rows


async def get_template(clinic_id: str, template_id: str) -> dict:
    rows = await _rows(scoped_query("dx_report_templates", clinic_id).eq("id", template_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Template not found.")
    return rows[0]


async def _check_owner(clinic_id: str, owner_kind: str, owner_id: Optional[str]) -> None:
    if owner_kind == "clinic":
        if owner_id:
            raise HTTPException(status_code=422, detail="A clinic template has no owner id.")
        return
    if not owner_id:
        raise HTTPException(status_code=422, detail="Choose the branch or B2B account this template belongs to.")
    table = "branches" if owner_kind == "branch" else "dx_b2b_accounts"
    if not await _rows(scoped_query(table, clinic_id, "id").eq("id", owner_id).limit(1)):
        raise HTTPException(status_code=404, detail="Branch not found." if owner_kind == "branch" else
                            "B2B account not found.")


async def create_template(clinic_id: str, body: dict, actor_id: Optional[str]) -> dict:
    """A new draft: version = the next number for this owner and name."""
    from app.services.dx import raise_for
    await _check_owner(clinic_id, body["owner_kind"], body.get("owner_id"))
    base = STARTERS.get(body.get("starter") or DEFAULT_STARTER)
    if base is None:
        raise HTTPException(status_code=422, detail="Unknown starter template.")
    layout = check_layout(_merge(base, body.get("layout") or {}), clinic_id)
    q = (scoped_query("dx_report_templates", clinic_id, "version").eq("owner_kind", body["owner_kind"])
         .eq("name", body["name"].strip()))
    q = q.eq("owner_id", body["owner_id"]) if body.get("owner_id") else q.is_("owner_id", "null")
    prior = await _rows(q.order("version", desc=True).limit(1))
    try:
        # unscoped: insert_scoped_by_payload
        res = await sb(supabase.table("dx_report_templates").insert({
            "clinic_id": clinic_id, "owner_kind": body["owner_kind"], "owner_id": body.get("owner_id"),
            "name": body["name"].strip(), "version": (prior[0]["version"] + 1) if prior else 1, "layout": layout}))
    except Exception as e:  # noqa: BLE001
        raise_for(e)
    return res.data[0]


async def update_draft(clinic_id: str, template_id: str, layout: dict) -> dict:
    from app.services.dx import raise_for
    t = await get_template(clinic_id, template_id)
    if t["status"] != "draft":
        raise_for(Exception("dx_template_published"))
    try:
        res = await sb(supabase.table("dx_report_templates").update({"layout": check_layout(layout, clinic_id)})
                       .eq("clinic_id", clinic_id).eq("id", template_id))
    except Exception as e:  # noqa: BLE001
        raise_for(e)
    return res.data[0]


async def publish(clinic_id: str, template_id: str, actor_id: Optional[str]) -> dict:
    from app.services.dx import rpc
    await get_template(clinic_id, template_id)
    return await rpc("dx_publish_template", {"p_clinic_id": clinic_id, "p_template_id": template_id,
                                             "p_actor_id": actor_id})


async def retire(clinic_id: str, template_id: str) -> dict:
    from app.services.dx import raise_for
    t = await get_template(clinic_id, template_id)
    if t["status"] != "published":
        raise_for(Exception("dx_template_not_published"))
    try:
        res = await sb(supabase.table("dx_report_templates").update({"status": "retired"})
                       .eq("clinic_id", clinic_id).eq("id", template_id))
    except Exception as e:  # noqa: BLE001
        raise_for(e)
    return res.data[0]


# ── assets (logo / header image) ────────────────────────────────────────────

async def store_asset(clinic_id: str, raw: bytes) -> dict:
    """PNG / JPEG <= 1 MB, <= 3000 px wide, content-addressed under the clinic's prefix."""
    from PIL import Image
    if not raw or len(raw) > ASSET_MAX_BYTES:
        raise HTTPException(status_code=413 if raw else 400, detail="Upload a PNG or JPEG of at most 1 MB.")
    ext = "png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "jpg" if raw.startswith(b"\xff\xd8\xff") else None
    if not ext:
        raise HTTPException(status_code=415, detail="Upload a PNG or JPEG image.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            width = im.width
            im.verify()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=415, detail="This image could not be read.")
    if width > ASSET_MAX_PX:
        raise HTTPException(status_code=422, detail="The image is wider than 3000 px.")
    path = f"{clinic_id}/dx-assets/templates/{hashlib.sha256(raw).hexdigest()}.{ext}"
    try:
        await asyncio.to_thread(supabase.storage.from_(ASSET_BUCKET).upload, path, raw,
                                {"content-type": "image/png" if ext == "png" else "image/jpeg", "upsert": "true"})
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="The image could not be stored. Please retry.")
    return {"path": path}


async def load_assets(clinic_id: str, layout: dict) -> dict[str, bytes]:
    """Asset bytes for rendering; a path outside this clinic is never read."""
    out: dict[str, bytes] = {}
    for path in asset_paths(layout):
        if not path.startswith(f"{clinic_id}/"):
            continue
        try:
            out[path] = await asyncio.to_thread(supabase.storage.from_(ASSET_BUCKET).download, path)
        except Exception:  # noqa: BLE001 - a missing logo must not stop a report
            continue
    return out


async def preview(clinic_id: str, layout: dict, letterhead: dict) -> bytes:
    """Render the sample visit (Taiyo HbA1c + lipid) with `layout` — "Test with Sample Visit"."""
    from app.services import dx_sample_report as sample
    from app.services.dx_pdf import render_visit_report
    layout = check_layout(layout, clinic_id)
    release = sample.release(letterhead_snapshot=letterhead, watermark="internal", verify_code="SAMPLE2345",
                             verify_url="https://example.invalid/public/dx/verify/SAMPLE2345",
                             routing_snapshot={"white_label": letterhead.get("white_label", False)},
                             assets=await load_assets(clinic_id, layout))
    pdf, _, _ = await asyncio.to_thread(render_visit_report, release, sample.results(), sample.visit(), layout)
    return pdf
