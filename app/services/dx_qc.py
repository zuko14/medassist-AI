"""Diagnostic OS internal quality control (P10, DX-62): Westgard multi-rules and Levey-Jennings data.

Pure arithmetic over the material's target mean / SD and its earlier runs; no model is involved
(ADR-DX-10). The run log (dx_qc_results) is append-only: a rejected run is recorded as rejected,
and the next run is a new row.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import HTTPException

from app.database import scoped_query
from app.services import dx
from app.utils.helpers import today_ist

WARNING = ("1_2s",)
REJECT = ("1_3s", "2_2s", "R_4s", "4_1s", "10_x")
HISTORY = 9          # 10_x looks at the current run plus nine before it


def z_score(value, mean, sd) -> Decimal:
    return ((Decimal(str(value)) - Decimal(str(mean))) / Decimal(str(sd))).quantize(Decimal("0.0001"))


def westgard(z: Decimal, history: list) -> list[str]:
    """Flags for a run with z-score `z`; `history` = earlier z-scores of the same material, newest first."""
    z = Decimal(str(z))
    runs = [z, *(Decimal(str(h)) for h in history)]
    flags = []
    if abs(z) > 2:
        flags.append("1_2s")
    if abs(z) > 3:
        flags.append("1_3s")
    if len(runs) >= 2:
        a, b = runs[0], runs[1]
        if (a > 2 and b > 2) or (a < -2 and b < -2):
            flags.append("2_2s")
        if (a > 2 and b < -2) or (a < -2 and b > 2):
            flags.append("R_4s")
    if len(runs) >= 4 and (all(r > 1 for r in runs[:4]) or all(r < -1 for r in runs[:4])):
        flags.append("4_1s")
    if len(runs) >= 10 and (all(r > 0 for r in runs[:10]) or all(r < 0 for r in runs[:10])):
        flags.append("10_x")
    return flags


def accepted(flags: list[str]) -> bool:
    return not any(f in REJECT for f in flags)


async def list_materials(clinic_id: str, instrument_id: Optional[str] = None) -> list[dict]:
    q = scoped_query("dx_qc_materials", clinic_id).order("parameter_code").order("level")
    return await dx._rows(q.eq("instrument_id", instrument_id) if instrument_id else q)


async def get_material(clinic_id: str, material_id: str) -> dict:
    rows = await dx._rows(scoped_query("dx_qc_materials", clinic_id).eq("id", material_id).limit(1))
    if not rows:
        raise HTTPException(status_code=404, detail="QC material not found.")
    return rows[0]


async def _history(clinic_id: str, material_id: str, limit: int) -> list[dict]:
    return await dx._rows(scoped_query("dx_qc_results", clinic_id, "z_score, run_at").eq("material_id", material_id)
                          .order("run_at", desc=True).limit(limit))


async def record_qc_result(clinic_id: str, material_id: str, value, entered_by, note: Optional[str] = None,
                           on: Optional[date] = None) -> dict:
    """Score one QC run, apply the Westgard rules against the material's earlier runs, append it."""
    m = await get_material(clinic_id, material_id)
    day = on or today_ist()
    if not m["is_active"] or not (date.fromisoformat(str(m["valid_from"])) <= day <= date.fromisoformat(str(m["valid_to"]))):
        raise HTTPException(status_code=409, detail={"code": "dx_qc_material_expired",
                                                     "message": "This QC lot is inactive or outside its validity dates."})
    try:
        z = z_score(value, m["target_mean"], m["target_sd"])
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="The QC value must be a number.")
    flags = westgard(z, [h["z_score"] for h in await _history(clinic_id, material_id, HISTORY)])
    aid, name = dx._actor(entered_by)
    return await dx.save_config_row("dx_qc_results", clinic_id, {
        "material_id": material_id, "value": str(Decimal(str(value))), "z_score": str(z), "westgard_flags": flags,
        "accepted": accepted(flags), "entered_by": aid, "entered_by_name": name,
        "reviewer_note": (note or "").strip()[:500] or None})


async def list_results(clinic_id: str, material_id: str, days: int = 30) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return await dx._rows(scoped_query("dx_qc_results", clinic_id).eq("material_id", material_id)
                          .gte("run_at", since).order("run_at"))


async def get_levey_jennings_chart(clinic_id: str, material_id: str, days: int = 30) -> dict:
    """Target lines (mean, ±1s, ±2s, ±3s) and the runs of the last `days`, oldest first."""
    m = await get_material(clinic_id, material_id)
    mean, sd = Decimal(str(m["target_mean"])), Decimal(str(m["target_sd"]))
    lines = {"mean": str(mean), **{f"{sign}{k}s": str(mean + (k if sign == "+" else -k) * sd)
                                    for k in (1, 2, 3) for sign in "+-"}}
    points = [{"run_at": r["run_at"], "value": str(r["value"]), "z_score": str(r["z_score"]),
               "flags": r["westgard_flags"] or [], "accepted": r["accepted"],
               "entered_by_name": r.get("entered_by_name"), "note": r.get("reviewer_note")}
              for r in await list_results(clinic_id, material_id, days)]
    return {"material": m, "lines": lines, "points": points, "days": days}
