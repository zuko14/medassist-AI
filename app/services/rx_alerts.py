"""Kriya Pharmacy OS — daily alerts and nightly integrity checks (migration 142; RX-56).

Zero-LLM. `run_scan` (scheduler, distributed_job_lock) calls rx_alerts_scan for every clinic whose pharmacy is not OFF:
low stock, near expiry, expired on the shelf (one alert per entity per day), plus the two integrity checks —
balance ≠ ledger (rx_ledger_check) and a broken register hash chain (rx_register_verify on every register). An
integrity failure is a P1: it also raises an admin notification. A STAT ward indent alerts the pharmacy at once.
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services.rx import _rows, get_rx_settings, raise_for
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)


def _upd(table: str, clinic_id: str, patch: dict):
    """A clinic-scoped UPDATE (scoped_query only builds SELECTs)."""
    return supabase.table(table).update(patch).eq("clinic_id", clinic_id)

KINDS = ("low_stock", "near_expiry", "expired_on_hand", "ledger_mismatch", "register_chain_break", "cold_chain",
         "stat_indent")


async def _notify(clinic_id: str, title: str, message: str) -> None:
    # unscoped: insert_scoped_by_payload
    await sb(supabase.table("admin_notifications").insert({
        "clinic_id": clinic_id, "admin_id": None, "title": title[:255], "message": message, "is_read": False}))


async def scan_clinic(clinic: dict, today: Optional[date] = None) -> dict:
    cid = clinic["id"]
    today = today or today_ist()
    out = (await sb(supabase.rpc("rx_alerts_scan", {
        "p_clinic_id": cid, "p_today": today.isoformat(),
        "p_near_days": int(get_rx_settings(clinic).get("near_expiry_days", 90))}))).data or {}
    if out.get("ledger_mismatch"):
        await _notify(cid, "[P1] Pharmacy stock ledger mismatch",
                      f"{len(out['ledger_mismatch'])} store × batch balance(s) disagree with the stock ledger. "
                      "Stop stock work and contact support; nothing has been changed automatically.")
    for b in out.get("register_chain_break") or []:
        await _notify(cid, f"[P1] Drug register tampering detected ({b['register']})",
                      f"The {b['register']} register's hash chain breaks at entry {b['first_bad_seq']}. "
                      "Do not edit it; export it and inform the pharmacy manager and the platform owner.")
    return out


async def run_scan(today: Optional[date] = None) -> dict:
    """Daily (scheduler.py, CAS-locked). Every clinic whose pharmacy is ACTIVE or WIND_DOWN; OFF is skipped."""
    from app.services.tenant import rx_access
    # unscoped: platform_sweep
    res = await sb(supabase.table("clinics").select("*").neq("rx_state", "OFF").eq("is_active", True))
    stats = {"clinics": 0, "integrity_failures": 0}
    for clinic in res.data or []:
        if clinic.get("status") == "DELETED" or rx_access(clinic) is None:
            continue
        try:
            out = await scan_clinic(clinic, today)
            stats["clinics"] += 1
            stats["integrity_failures"] += len(out.get("ledger_mismatch") or []) + len(out.get("register_chain_break") or [])
        except Exception as e:                            # noqa: BLE001 - one clinic never stops the sweep
            logger.error(f"RX alert scan failed for clinic={clinic.get('id')}: {type(e).__name__}: {e}")
    logger.info(f"RX alert scan: {stats}")
    return stats


async def stat_indent(clinic_id: str, indent: dict) -> None:
    """A STAT indent was raised: an alert row for the pharmacy screen and a notification bell entry."""
    try:
        # unscoped: insert_scoped_by_payload
        await sb(supabase.table("rx_alerts").insert({
            "clinic_id": clinic_id, "store_id": indent.get("from_store_id"), "kind": "stat_indent",
            "alert_date": today_ist().isoformat(),
            "payload": {"indent_id": indent.get("id"), "indent_number": indent.get("indent_number"),
                        "ward": indent.get("ward"), "bed": indent.get("bed")}}))
        await _notify(clinic_id, f"STAT indent {indent.get('indent_number') or ''}".strip(),
                      f"A STAT medicine indent was raised{' from ' + indent['ward'] if indent.get('ward') else ''}. "
                      "Dispense it first.")
    except Exception as e:                                # noqa: BLE001 - the indent stands without the alert
        logger.error(f"STAT indent alert failed clinic={clinic_id}: {type(e).__name__}")


async def list_alerts(clinic_id: str, store_ids: Optional[list[str]], kind: Optional[str], ack: Optional[bool],
                      limit: int) -> list[dict]:
    q = scoped_query("rx_alerts", clinic_id)
    if kind:
        q = q.eq("kind", kind)
    if ack is not None:
        q = q.not_.is_("acknowledged_at", "null") if ack else q.is_("acknowledged_at", "null")
    rows = await _rows(q.order("alert_date", desc=True).order("created_at", desc=True).limit(limit))
    # clinic-wide integrity alerts (no store) are shown to everyone allowed to read the pharmacy
    return [r for r in rows if store_ids is None or not r.get("store_id") or str(r["store_id"]) in store_ids]


async def get_alert(clinic_id: str, alert_id: str) -> dict:
    rows = await _rows(scoped_query("rx_alerts", clinic_id).eq("id", alert_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="Alert not found.")
    return rows[0]


async def acknowledge(clinic_id: str, alert: dict, actor_id: Optional[str]) -> dict:
    if not actor_id:
        raise HTTPException(status_code=403, detail="Acknowledging an alert needs a named staff login.")
    try:
        res = await sb(_upd("rx_alerts", clinic_id,
                       {"acknowledged_by": actor_id, "acknowledged_at": datetime.now(timezone.utc).isoformat()})
                       .eq("id", alert["id"]).is_("acknowledged_at", "null"))
    except Exception as e:
        raise_for(e)
    return (res.data or [alert])[0]


async def open_count(clinic_id: str) -> int:
    return len(await _rows(scoped_query("rx_alerts", clinic_id, "id").is_("acknowledged_at", "null").limit(1000)))
