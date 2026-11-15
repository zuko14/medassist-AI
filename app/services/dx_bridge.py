"""Diagnostic OS analyser bridge (P10, DX-60; ADR-DX-13, SECURITY §2).

The lab's bridge agent connects OUTBOUND only. Each request is signed:
    X-Bridge-Key-Prefix: kba_xxxxxxxx
    X-Bridge-Timestamp:  ISO 8601 UTC, within ±300 s
    X-Bridge-Signature:  hex HMAC-SHA256(key = sha256(agent secret) hex,
                         msg = METHOD \\n PATH[?QUERY] \\n TIMESTAMP \\n sha256(body) hex)
Kriya keeps only sha256(secret) (`key_hash`); the secret is shown once. Instrument values are matched by
lab_test_parameters.machine_code and only PREFILL the worksheet; they are saved as 'entered' only when
dx_settings.instrument_auto_enter is on and the order has no result yet (actor "instrument:<id>").
Verification and approval stay human. Zero-LLM.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services import dx

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 300
MAX_BODY = 300_000
OPEN_ORDER = ["ordered", "billed", "sample_collected", "in_progress", "resulted"]
_DENIED = HTTPException(status_code=401, detail="Bridge authentication failed.")


# ── keys and signatures ─────────────────────────────────────────────────────

def new_agent_key() -> tuple[str, str, str]:
    """(secret, key_prefix, key_hash): 32 random bytes; the secret is returned once and never stored."""
    secret = "kba_" + secrets.token_urlsafe(32)
    return secret, secret[:12], hashlib.sha256(secret.encode()).hexdigest()


def sign(key_hash: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    msg = f"{method.upper()}\n{path}\n{timestamp}\n{hashlib.sha256(body or b'').hexdigest()}"
    return hmac.new(key_hash.encode(), msg.encode(), hashlib.sha256).hexdigest()


def sign_with_secret(secret: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    """What the bridge computes (it holds the secret, derives the same signing key)."""
    return sign(hashlib.sha256(secret.encode()).hexdigest(), method, path, timestamp, body)


async def authenticate_bridge_request(headers, method: str, path: str, body: bytes,
                                      now: Optional[datetime] = None) -> tuple[dict, dict]:
    """(agent, clinic) or 401 — one message for every failure, so a probe learns nothing."""
    from app.services.tenant import dx_access, get_clinic_by_id
    prefix = headers.get("x-bridge-key-prefix") or ""
    ts = headers.get("x-bridge-timestamp") or ""
    sig = headers.get("x-bridge-signature") or ""
    if not (prefix.startswith("kba_") and len(prefix) == 12 and ts and sig):
        raise _DENIED
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError
    except ValueError:
        raise _DENIED
    if abs(((now or datetime.now(timezone.utc)) - when).total_seconds()) > WINDOW_SECONDS:
        raise _DENIED
    # unscoped: global_auth_lookup
    rows = (await sb(supabase.table("dx_bridge_agents").select("*").eq("key_prefix", prefix).limit(1))).data or []
    agent = rows[0] if rows else None
    if not agent or not agent["is_active"]:
        raise _DENIED
    if not hmac.compare_digest(sign(agent["key_hash"], method, path, ts, body), sig.lower()):
        raise _DENIED
    clinic = await get_clinic_by_id(str(agent["clinic_id"]))
    if dx_access(clinic) is None:
        raise _DENIED
    return agent, clinic


async def heartbeat(clinic_id: str, agent_id: str) -> dict:
    row = await dx.save_config_row("dx_bridge_agents", clinic_id,
                                   {"last_seen_at": datetime.now(timezone.utc).isoformat()}, agent_id)
    return {"ok": True, "last_seen_at": row["last_seen_at"]}


# ── host query and matching ─────────────────────────────────────────────────

async def _sample(clinic_id: str, barcode: str) -> Optional[dict]:
    rows = await dx._rows(scoped_query("dx_samples", clinic_id, "id, accession_id, barcode, sample_type, status, "
                                       "collection_site_id, processing_site_id, received_site_id")
                          .eq("barcode", barcode).limit(1))
    return rows[0] if rows else None


async def _orders_and_codes(clinic_id: str, sample_id: str) -> tuple[list[dict], dict[str, list[dict]]]:
    """Open orders on the tube, and per order its parameters that carry a machine code."""
    orders = await dx._rows(scoped_query("opd_orders", clinic_id, "id, item_name, catalog_reference_id, status")
                            .eq("dx_sample_id", sample_id).in_("status", OPEN_ORDER))
    params = await dx._rows(scoped_query("lab_test_parameters", clinic_id, "id, lab_test_id, name, parameter_code, "
                                         "machine_code, unit").in_("lab_test_id",
                                                                   [o["catalog_reference_id"] for o in orders] or [""])
                            .not_.is_("machine_code", "null")) if orders else []
    by_test: dict[str, list[dict]] = {}
    for p in params:
        by_test.setdefault(p["lab_test_id"], []).append(p)
    return orders, {o["id"]: by_test.get(o["catalog_reference_id"], []) for o in orders}


def _at_site(sample: dict, site_id: str) -> bool:
    return str(site_id) in {str(sample.get(k)) for k in ("processing_site_id", "received_site_id", "collection_site_id")}


async def handle_host_query(clinic_id: str, site_id: str, barcode: str) -> dict:
    """ASTM / HL7 host query: what the analyser should run on this tube (its open tests' machine codes)."""
    sample = await _sample(clinic_id, barcode)
    if not sample or not _at_site(sample, site_id) or sample["status"] in ("rejected", "discarded"):
        raise HTTPException(status_code=404, detail="No open tube with this barcode at this site.")
    orders, codes = await _orders_and_codes(clinic_id, sample["id"])
    return {"barcode": sample["barcode"], "sample_type": sample["sample_type"],
            "tests": [{"order_id": o["id"], "name": o["item_name"],
                       "machine_codes": sorted({p["machine_code"] for p in codes[o["id"]]})} for o in orders
                      if codes[o["id"]]]}


def match(results: list[dict], codes: dict[str, list[dict]]) -> tuple[dict, list[str]]:
    """Parsed analyser results -> ({order_id: [{parameter_id, value, unit, machine_code}]}, unmatched codes).
    Machine codes compare case-insensitively; a code used by two orders' parameters fills both."""
    out: dict[str, list[dict]] = {}
    unmatched = []
    for r in results:
        code = str(r.get("code") or "").strip().upper()
        hits = [(oid, p) for oid, ps in codes.items() for p in ps if (p["machine_code"] or "").strip().upper() == code]
        if not hits or r.get("value") in (None, ""):
            unmatched.append(r.get("code"))
            continue
        for oid, p in hits:
            out.setdefault(oid, []).append({"parameter_id": p["id"], "parameter_name": p["name"],
                                            "value": str(r["value"]), "unit": r.get("unit") or p.get("unit"),
                                            "machine_code": p["machine_code"]})
    return out, unmatched


# ── ingestion ───────────────────────────────────────────────────────────────

async def get_instrument(clinic_id: str, instrument_id: str) -> dict:
    rows = await dx._rows(scoped_query("dx_instruments", clinic_id).eq("id", instrument_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Instrument not found.")
    return rows[0]


async def _existing(clinic_id: str, instrument_id: str, message_id: str) -> Optional[dict]:
    rows = await dx._rows(scoped_query("dx_instrument_results", clinic_id, "id, mapping_status, applied_result_id, "
                                       "barcode").eq("instrument_id", instrument_id).eq("message_id", message_id)
                          .limit(1))
    return rows[0] if rows else None


async def ingest_instrument_results(clinic: dict, agent: dict, instrument_id: str, message_id: str,
                                    raw_message: str) -> dict:
    """Store the raw message once per (instrument, message id), match it, maybe auto-enter. Replays return
    the first outcome."""
    from connectors.bridge import parsers
    clinic_id = str(clinic["id"])
    inst = await get_instrument(clinic_id, instrument_id)
    if str(inst["agent_id"]) != str(agent["id"]) or not inst["is_active"]:
        raise HTTPException(status_code=404, detail="Instrument not found.")
    seen = await _existing(clinic_id, instrument_id, message_id)
    if seen:
        return {**seen, "replayed": True}
    try:
        parsed = parsers.parse(inst["protocol"], raw_message)
    except parsers.ParseError as e:
        raise HTTPException(status_code=422, detail={"code": "dx_bridge_parse_error", "message": str(e)[:200]})
    if len(parsed["specimens"]) != 1:
        raise HTTPException(status_code=422, detail={"code": "dx_bridge_one_specimen",
                                                     "message": "Send one specimen per message."})
    spec = parsed["specimens"][0]
    sample = await _sample(clinic_id, spec["barcode"])
    mapping: dict = {"orders": {}, "unmatched_codes": [r["code"] for r in spec["results"]]}
    if sample and _at_site(sample, inst["site_id"]):
        _, codes = await _orders_and_codes(clinic_id, sample["id"])
        mapping["orders"], mapping["unmatched_codes"] = match(spec["results"], codes)
    status = "pending" if mapping["orders"] else "unmatched"
    try:
        row = await dx.save_config_row("dx_instrument_results", clinic_id, {
            "instrument_id": instrument_id, "message_id": message_id, "barcode": spec["barcode"],
            "raw_message": raw_message, "parsed": parsed, "mapping": mapping, "mapping_status": status})
    except HTTPException:
        seen = await _existing(clinic_id, instrument_id, message_id)      # a parallel retry won the insert
        if seen:
            return {**seen, "replayed": True}
        raise
    if status == "pending" and dx.get_dx_settings(clinic).get("instrument_auto_enter"):
        row = await _auto_enter(clinic_id, row, inst)
    return {k: row.get(k) for k in ("id", "mapping_status", "applied_result_id", "barcode")} | {
        "replayed": False, "matched_orders": len(mapping["orders"]), "unmatched_codes": mapping["unmatched_codes"]}


async def _auto_enter(clinic_id: str, row: dict, inst: dict) -> dict:
    """Save matched values as 'entered' (actor instrument:<id>) for orders that have no result yet; any other
    case stays a prefill for the technician. Never raises: the raw row is already safe."""
    from app.services import dx_results
    actor = SimpleNamespace(user_id=None, username=f"instrument:{inst['id']}")
    applied = None
    for order_id, values in row["mapping"]["orders"].items():
        has = await dx._rows(scoped_query("lab_results", clinic_id, "id").eq("order_id", order_id).limit(1))
        if has:
            continue
        try:
            out = await dx_results.enter_worksheet_results(
                clinic_id, order_id, [{"parameter_id": v["parameter_id"], "value": v["value"]} for v in values], actor,
                instrument={"id": row["id"], "values": {v["parameter_id"]: v["value"] for v in values}})
            applied = out["id"]
        except HTTPException as e:
            logger.info(f"DX_INSTRUMENT_AUTO_ENTER_SKIPPED order={order_id} status={e.status_code}")
    if applied:
        row = await dx.save_config_row("dx_instrument_results", clinic_id, {
            "mapping_status": "applied", "applied_result_id": applied, "mapped_by_name": actor.username}, row["id"])
    return row


# ── worksheet prefill, technician mapping ───────────────────────────────────

async def prefill_for_order(clinic_id: str, order_id: str) -> Optional[dict]:
    """The newest instrument values for this order (pending or applied), for the worksheet drawer."""
    o = await dx._rows(scoped_query("opd_orders", clinic_id, "dx_sample_id").eq("id", order_id).limit(1))
    if not o or not o[0].get("dx_sample_id"):
        return None
    s = await dx._rows(scoped_query("dx_samples", clinic_id, "barcode").eq("id", o[0]["dx_sample_id"]).limit(1))
    if not s or not s[0].get("barcode"):
        return None
    rows = await dx._rows(scoped_query("dx_instrument_results", clinic_id, "id, instrument_id, mapping, mapping_status, "
                                       "received_at").eq("barcode", s[0]["barcode"])
                          .in_("mapping_status", ["pending", "applied"]).order("received_at", desc=True).limit(20))
    row = next((r for r in rows if order_id in ((r.get("mapping") or {}).get("orders") or {})), None)
    if not row:
        return None
    inst = await get_instrument(clinic_id, row["instrument_id"])
    return {"instrument_result_id": row["id"], "instrument_name": inst["name"], "received_at": row["received_at"],
            "status": row["mapping_status"],
            "values": {v["parameter_id"]: {"value": v["value"], "unit": v.get("unit"), "machine_code": v["machine_code"]}
                       for v in row["mapping"]["orders"][order_id]}}


async def get_instrument_result(clinic_id: str, rid: str) -> dict:
    rows = await dx._rows(scoped_query("dx_instrument_results", clinic_id).eq("id", rid).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Instrument result not found.")
    return rows[0]


async def map_candidates(clinic_id: str, barcode: str) -> dict:
    """For the technician's Map form: the open orders on a tube and every parameter of each order's test."""
    sample = await _sample(clinic_id, barcode)
    if not sample:
        raise HTTPException(status_code=404, detail="No tube with this barcode.")
    orders = await dx._rows(scoped_query("opd_orders", clinic_id, "id, item_name, catalog_reference_id, accession_id")
                            .eq("dx_sample_id", sample["id"]).in_("status", OPEN_ORDER))
    params = await dx._rows(scoped_query("lab_test_parameters", clinic_id, "id, lab_test_id, name, machine_code")
                            .in_("lab_test_id", [o["catalog_reference_id"] for o in orders] or [""])) if orders else []
    return {"barcode": barcode, "accession_id": sample["accession_id"], "orders": [
        {"id": o["id"], "name": o["item_name"], "parameters": [
            {k: p[k] for k in ("id", "name", "machine_code")} for p in params if p["lab_test_id"] == o["catalog_reference_id"]]}
        for o in orders]}


async def map_result(clinic_id: str, rid: str, order_id: str, codes: dict[str, str], user) -> dict:
    """A technician links an unmatched message to an order: machine codes match as usual, and `codes`
    ({analyser code: parameter id}) covers the ones the catalogue does not know yet."""
    row = await get_instrument_result(clinic_id, rid)
    if row["mapping_status"] not in ("unmatched", "pending"):
        raise HTTPException(status_code=409, detail="This instrument result is already applied or ignored.")
    order = (await dx._rows(scoped_query("opd_orders", clinic_id, "id, catalog_reference_id, status")
                            .eq("id", order_id).limit(1)) or [None])[0]
    if not order or order["status"] not in OPEN_ORDER:
        raise HTTPException(status_code=409, detail="That order is not open for results.")
    params = await dx._rows(scoped_query("lab_test_parameters", clinic_id, "id, lab_test_id, name, machine_code, unit")
                            .eq("lab_test_id", order["catalog_reference_id"]))
    by_id = {p["id"]: p for p in params}
    if any(pid not in by_id for pid in codes.values()):
        raise HTTPException(status_code=422, detail="A mapped parameter is not part of that order's test.")
    explicit = {str(code).strip().upper(): by_id[pid] for code, pid in codes.items()}
    lookup = {order_id: [*(p for p in params if p.get("machine_code")),
                         *({**p, "machine_code": code} for code, p in explicit.items())]}
    results = [r for sp in row["parsed"]["specimens"] for r in sp["results"]]
    mapped, unmatched = match(results, lookup)
    if not mapped:
        raise HTTPException(status_code=422, detail="None of the analyser codes map to that order's parameters.")
    mapping = {"orders": {**((row.get("mapping") or {}).get("orders") or {}), **mapped}, "unmatched_codes": unmatched}
    return await dx.save_config_row("dx_instrument_results", clinic_id, {
        "mapping": mapping, "mapping_status": "pending", "mapped_by_name": dx._actor(user)[1]}, rid)


async def ignore_result(clinic_id: str, rid: str, user) -> dict:
    row = await get_instrument_result(clinic_id, rid)
    if row["mapping_status"] == "applied":
        raise HTTPException(status_code=409, detail="An applied instrument result cannot be ignored.")
    return await dx.save_config_row("dx_instrument_results", clinic_id, {
        "mapping_status": "ignored", "mapped_by_name": dx._actor(user)[1]}, rid)


async def mark_applied(clinic_id: str, rid: str, result_id: str, user) -> None:
    """The technician saved the worksheet from this prefill."""
    row = await get_instrument_result(clinic_id, rid)
    if row["mapping_status"] == "pending":
        await dx.save_config_row("dx_instrument_results", clinic_id, {
            "mapping_status": "applied", "applied_result_id": result_id, "mapped_by_name": dx._actor(user)[1]}, rid)


async def list_instrument_results(clinic_id: str, status: Optional[str], site_ids: Optional[list[str]],
                                  limit: int = 200) -> list[dict]:
    insts = {i["id"]: i for i in await dx._rows(scoped_query("dx_instruments", clinic_id, "id, name, site_id"))}
    q = scoped_query("dx_instrument_results", clinic_id, "id, instrument_id, message_id, barcode, parsed, mapping, "
                     "mapping_status, applied_result_id, mapped_by_name, received_at")
    rows = await dx._rows((q.eq("mapping_status", status) if status else q).order("received_at", desc=True).limit(limit))
    out = []
    for r in rows:
        inst = insts.get(r["instrument_id"]) or {}
        if site_ids is not None and inst.get("site_id") not in site_ids:
            continue
        out.append({**r, "instrument_name": inst.get("name")})
    return out


# ── administration (DX_ADMIN) ───────────────────────────────────────────────

AGENT_COLS = "id, site_id, name, key_prefix, last_seen_at, is_active, created_at"


async def list_agents(clinic_id: str, site_ids: Optional[list[str]]) -> list[dict]:
    rows = await dx._rows(scoped_query("dx_bridge_agents", clinic_id, AGENT_COLS).order("created_at"))
    return [r for r in rows if site_ids is None or r["site_id"] in site_ids]


async def create_agent(clinic_id: str, site_id: str, name: str) -> dict:
    secret, prefix, key_hash = new_agent_key()
    row = await dx.save_config_row("dx_bridge_agents", clinic_id, {"site_id": site_id, "name": name.strip(),
                                                                   "key_hash": key_hash, "key_prefix": prefix})
    return {**{k: row[k] for k in AGENT_COLS.split(", ")}, "secret": secret}


async def update_agent(clinic_id: str, agent_id: str, changes: dict, rotate: bool) -> dict:
    body = {k: v for k, v in changes.items() if v is not None}
    secret = None
    if rotate:
        secret, body["key_prefix"], body["key_hash"] = new_agent_key()
    if not body:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await dx.save_config_row("dx_bridge_agents", clinic_id, body, agent_id)
    out = {k: row[k] for k in AGENT_COLS.split(", ")}
    return {**out, "secret": secret} if secret else out


async def get_agent(clinic_id: str, agent_id: str) -> dict:
    rows = await dx._rows(scoped_query("dx_bridge_agents", clinic_id, AGENT_COLS).eq("id", agent_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Bridge agent not found.")
    return rows[0]


async def list_instruments(clinic_id: str, site_ids: Optional[list[str]]) -> list[dict]:
    rows = await dx._rows(scoped_query("dx_instruments", clinic_id).order("created_at"))
    return [r for r in rows if site_ids is None or r["site_id"] in site_ids]
