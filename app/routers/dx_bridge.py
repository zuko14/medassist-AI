"""Diagnostic OS analyser bridge API (P10, DX-60; ADR-DX-13).

/internal/dx/bridge/*: called by the lab's outbound-only bridge agent, authenticated by its key prefix,
a ±300 s timestamp and an HMAC over method, path, timestamp and body (app/services/dx_bridge.py).
/admin/dx/bridge-agents, /instruments, /instrument-results: staff, same order as every DX route
(permission -> tenant -> dx_access -> site scope). Zero-LLM.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field, ValidationError

from app.routers.admin import AdminUser, verify_credentials
from app.routers.dx import UUID_RX, _audit, _dx_order, _dx_scope, _In, _lab_perm, _user_site_ids
from app.services import dx_bridge

internal = APIRouter(prefix="/internal/dx/bridge", tags=["Diagnostic OS bridge"])
router = APIRouter(prefix="/admin/dx", tags=["Diagnostic OS"])


class BridgeResultsIn(_In):
    instrument_id: str = Field(..., pattern=UUID_RX)
    message_id: str = Field(..., min_length=1, max_length=120)
    raw_message: str = Field(..., min_length=1, max_length=200000)


async def _bridge(request: Request) -> tuple[dict, dict, bytes]:
    body = await request.body()
    if len(body) > dx_bridge.MAX_BODY:
        raise HTTPException(status_code=413, detail="Message too large.")
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    agent, clinic = await dx_bridge.authenticate_bridge_request(request.headers, request.method, path, body)
    return agent, clinic, body


@internal.get("/orders")
async def bridge_orders(request: Request, barcode: str = Query(..., min_length=3, max_length=64,
                                                               pattern=r"^[A-Za-z0-9._/-]+$")):
    agent, clinic, _ = await _bridge(request)
    return await dx_bridge.handle_host_query(str(clinic["id"]), str(agent["site_id"]), barcode)


@internal.post("/results")
async def bridge_results(request: Request):
    agent, clinic, body = await _bridge(request)
    try:
        data = BridgeResultsIn.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Body must be {instrument_id, message_id, raw_message}.")
    return await dx_bridge.ingest_instrument_results(clinic, agent, data.instrument_id, data.message_id,
                                                     data.raw_message)


@internal.post("/heartbeat")
async def bridge_heartbeat(request: Request):
    agent, clinic, _ = await _bridge(request)
    return await dx_bridge.heartbeat(str(clinic["id"]), str(agent["id"]))


# ── staff administration ───────────────────────────────────────────────────

class AgentIn(_In):
    site_id: str = Field(..., pattern=UUID_RX)
    name: str = Field(..., min_length=2, max_length=80)


class AgentPatch(_In):
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    is_active: Optional[bool] = None
    rotate_key: bool = False


class InstrumentIn(_In):
    agent_id: str = Field(..., pattern=UUID_RX)
    name: str = Field(..., min_length=2, max_length=80)
    model: Optional[str] = Field(None, max_length=80)
    protocol: Literal["astm", "hl7v2"]
    department_id: Optional[str] = Field(None, pattern=UUID_RX)


class InstrumentPatch(_In):
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    model: Optional[str] = Field(None, max_length=80)
    department_id: Optional[str] = Field(None, pattern=UUID_RX)
    is_active: Optional[bool] = None


class MapIn(_In):
    order_id: str = Field(..., pattern=UUID_RX)
    codes: dict[str, str] = Field(default_factory=dict, max_length=100)   # analyser code -> parameter id


def _mine(row: dict, sites: Optional[list[str]]) -> dict:
    if sites is not None and row["site_id"] not in sites:
        raise HTTPException(status_code=404, detail="Not found.")
    return row


@router.get("/bridge-agents")
async def list_agents(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    return {"agents": await dx_bridge.list_agents(scope, await _user_site_ids(user, scope, None))}


@router.post("/bridge-agents", status_code=status.HTTP_201_CREATED)
async def create_agent(body: AgentIn, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    """The response carries `secret` ONCE; Kriya keeps only its SHA-256."""
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    await _user_site_ids(user, scope, body.site_id)          # 404 outside the caller's sites
    out = await dx_bridge.create_agent(scope, body.site_id, body.name)
    await _audit(user, request, "dx_bridge_agent_create", "dx_bridge_agent", out["id"],
                 {"site_id": body.site_id, "key_prefix": out["key_prefix"]})
    return out


@router.patch("/bridge-agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentPatch, request: Request, clinic_id: str = "default",
                       user: AdminUser = Depends(verify_credentials)):
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    _mine(await dx_bridge.get_agent(scope, agent_id), await _user_site_ids(user, scope, None))
    out = await dx_bridge.update_agent(scope, agent_id, {"name": body.name, "is_active": body.is_active},
                                       body.rotate_key)
    await _audit(user, request, "dx_bridge_agent_update", "dx_bridge_agent", agent_id,
                 {"rotated": body.rotate_key, "is_active": body.is_active})
    return out


@router.get("/instruments")
async def list_instruments(clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    _lab_perm(user, "DX_ADMIN", "DX_QC", "LAB_ENTRY")
    scope, _ = await _dx_scope(user, clinic_id)
    return {"instruments": await dx_bridge.list_instruments(scope, await _user_site_ids(user, scope, None))}


@router.post("/instruments", status_code=status.HTTP_201_CREATED)
async def create_instrument(body: InstrumentIn, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    from app.services import dx
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN", new_work=True)
    agent = _mine(await dx_bridge.get_agent(scope, body.agent_id), await _user_site_ids(user, scope, None))
    out = await dx.save_config_row("dx_instruments", scope, {**body.model_dump(), "site_id": agent["site_id"]})
    await _audit(user, request, "dx_instrument_create", "dx_instrument", out["id"],
                 {"agent_id": body.agent_id, "protocol": body.protocol})
    return out


@router.patch("/instruments/{instrument_id}")
async def update_instrument(instrument_id: str, body: InstrumentPatch, request: Request, clinic_id: str = "default",
                            user: AdminUser = Depends(verify_credentials)):
    from app.services import dx
    scope, _ = await _dx_scope(user, clinic_id, "DX_ADMIN")
    _mine(await dx_bridge.get_instrument(scope, instrument_id), await _user_site_ids(user, scope, None))
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    out = await dx.save_config_row("dx_instruments", scope, changes, instrument_id)
    await _audit(user, request, "dx_instrument_update", "dx_instrument", instrument_id, changes)
    return out


@router.get("/instrument-results")
async def list_instrument_results(clinic_id: str = "default",
                                  status_: Optional[Literal["pending", "applied", "unmatched", "ignored"]] =
                                  Query(None, alias="status"),
                                  user: AdminUser = Depends(verify_credentials)):
    _lab_perm(user, "LAB_ENTRY", "LAB_VALIDATE", "DX_READ")
    scope, _ = await _dx_scope(user, clinic_id)
    return {"results": await dx_bridge.list_instrument_results(scope, status_,
                                                               await _user_site_ids(user, scope, None))}


async def _iresult(user: AdminUser, scope: str, rid: str) -> dict:
    row = await dx_bridge.get_instrument_result(scope, rid)
    inst = await dx_bridge.get_instrument(scope, row["instrument_id"])
    _mine(inst, await _user_site_ids(user, scope, None))
    return row


@router.get("/instrument-results/{result_id}/candidates")
async def map_candidates(result_id: str, barcode: str = Query(..., min_length=3, max_length=64,
                                                              pattern=r"^[A-Za-z0-9._/-]+$"),
                         clinic_id: str = "default", user: AdminUser = Depends(verify_credentials)):
    """Open orders (and their parameters) on the tube the technician says the message belongs to."""
    from app.routers.dx import _visit
    _lab_perm(user, "LAB_ENTRY")
    scope, _ = await _dx_scope(user, clinic_id)
    await _iresult(user, scope, result_id)
    out = await dx_bridge.map_candidates(scope, barcode)
    await _visit(user, scope, out["accession_id"])            # the tube's visit inside the caller's site scope
    return out


@router.post("/instrument-results/{result_id}/map")
async def map_instrument_result(result_id: str, body: MapIn, request: Request, clinic_id: str = "default",
                                user: AdminUser = Depends(verify_credentials)):
    _lab_perm(user, "LAB_ENTRY")
    scope, _ = await _dx_scope(user, clinic_id)
    await _iresult(user, scope, result_id)
    await _dx_order(user, scope, body.order_id)               # a DX order inside the caller's site scope
    out = await dx_bridge.map_result(scope, result_id, body.order_id, body.codes, user)
    await _audit(user, request, "dx_instrument_result_map", "dx_instrument_result", result_id,
                 {"order_id": body.order_id, "codes": len(body.codes)})
    return out


@router.post("/instrument-results/{result_id}/ignore")
async def ignore_instrument_result(result_id: str, request: Request, clinic_id: str = "default",
                                   user: AdminUser = Depends(verify_credentials)):
    _lab_perm(user, "LAB_ENTRY")
    scope, _ = await _dx_scope(user, clinic_id)
    await _iresult(user, scope, result_id)
    out = await dx_bridge.ignore_result(scope, result_id, user)
    await _audit(user, request, "dx_instrument_result_ignore", "dx_instrument_result", result_id, {})
    return out
