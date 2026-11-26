"""Continuous Care — the follow-up radar ("Find", migration 148; WORKFLOWS W6, ADR-CC-13).

A read model rebuilt per clinic twice a day from indexed reads of existing tables — no trigger anywhere. Each reason
opens a row keyed by its source and closes on the documented event; rows at or above the clinic's score threshold (or
urgent) open / update a case. Candidates (not enrolled; signed encounter ≤ 180 d with a pack ICD-10 code, plus
chronic lists from data import) are only ever proposed — never enrolled. A dismissed or declined candidate is not
raised again for 90 days. Verification never trusts appointments.status='completed' (ADR-CC-08). Zero-LLM.
"""

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services import cc_protocols
from app.services.cc import _now, _rows, actor_name, get_settings
from app.utils.helpers import today_ist

logger = logging.getLogger(__name__)
SCORES = {"followup_overdue": 40, "visit_missed": 45, "lab_overdue": 35, "refill_lapsed": 30, "off_target": 60,
          "not_responding": 25, "candidate": 20, "discharged_followup": 50}
REASON_LABELS = {"followup_overdue": "Overdue follow-up", "visit_missed": "Missed visit", "lab_overdue": "Lab overdue",
                 "refill_lapsed": "Refill lapsed", "off_target": "Off target", "not_responding": "Not responding",
                 "candidate": "Candidate", "discharged_followup": "After discharge"}


def score(reason: str, days_over: int = 0, tier: Optional[str] = None) -> int:
    base = SCORES.get(reason, 10) + min(max(days_over, 0), 60) // 2
    return min(1000, base + (40 if tier == "urgent" else 0))


async def _visit_since(clinic_id: str, patient_id: str, since: str) -> bool:
    """A checked-in appointment or a signed encounter on/after `since` (never status='completed')."""
    a = _rows(await sb(scoped_query("appointments", clinic_id, "id").eq("patient_id", patient_id)
                       .gte("checked_in_at", since).limit(1)))
    if a:
        return True
    e = _rows(await sb(scoped_query("opd_encounters", clinic_id, "id").eq("patient_id", patient_id).eq("status", "signed")
                       .gte("signed_at", since).limit(1)))
    return bool(e)


WATERMARK_KEY = "radar_candidate_wm"         # cc_settings key (not a staff setting; get_settings never returns it)
FULL_REBUILD_WEEKDAY = 6                      # Sunday: weekly full candidate scan (VERIFICATION D9)


def candidate_watermark(clinic: dict, today: date) -> Optional[str]:
    """The signed_at watermark for an incremental candidate scan, or None for a full scan (weekly, missing, or stale)."""
    wm = (clinic.get("cc_settings") or {}).get(WATERMARK_KEY)
    if not wm or today.weekday() == FULL_REBUILD_WEEKDAY:
        return None
    try:
        if (today - date.fromisoformat(str(wm)[:10])).days > 8:
            return None
    except ValueError:
        return None
    return str(wm)


async def collect(clinic: dict, today: Optional[date] = None, watermark: Optional[str] = None) -> list:
    """Every current radar candidate row (pure reads): dicts with patient, reason, source_key, detail, score.

    With a watermark, the ICD-10 candidate scan reads diagnoses only of encounters signed at/after it and carries the
    still-open pack candidate rows forward (D9); without one it scans every signed encounter of the last 180 days.
    """
    cid = clinic["id"]
    today = today or today_ist()
    out: list = []
    enr = await fetch_all(lambda: scoped_query("cc_enrollments", cid).in_("status", ["active", "paused", "invited"]),
                          label="cc:radar:enr")
    enrolled = {(e["patient_id"], e.get("family_member_id")): e for e in enr}
    # followup_overdue: signed encounters whose follow_up_date + 3 d has passed with no visit since the encounter
    since = (today - timedelta(days=180)).isoformat()
    cols = "id, patient_id, family_member_id, follow_up_date, signed_at" + ("" if watermark else ", diagnoses")
    encs = await fetch_all(lambda: scoped_query("opd_encounters", cid, cols).eq("status", "signed").gte("signed_at", since),
                           label="cc:radar:enc")
    for e in encs:
        fu = e.get("follow_up_date")
        after = (date.fromisoformat(str(e["signed_at"])[:10]) + timedelta(days=1)).isoformat()
        if fu and date.fromisoformat(fu) + timedelta(days=3) < today and not await _visit_since(cid, e["patient_id"], after):
            over = (today - date.fromisoformat(fu)).days
            out.append({"patient_id": e["patient_id"], "family_member_id": e.get("family_member_id"),
                        "reason": "followup_overdue", "source_key": f"opd_encounters:{e['id']}",
                        "detail": {"follow_up_date": fu}, "score": score("followup_overdue", over),
                        "enrollment_id": (enrolled.get((e["patient_id"], e.get("family_member_id"))) or {}).get("id")})
    # visit_missed: a past confirmed / completed consultation with no check-in (an auto-completed no-show counts here)
    appts = await fetch_all(lambda: scoped_query("appointments", cid, "id, patient_id, family_member_id, appointment_date, "
                                                 "status, checked_in_at").eq("booking_type", "consultation")
                            .in_("status", ["confirmed", "completed", "no_show"]).gte("appointment_date", (today - timedelta(days=60)).isoformat())
                            .lt("appointment_date", today.isoformat()).is_("checked_in_at", "null"), label="cc:radar:appt")
    for a in appts:
        if not a.get("patient_id"):
            continue
        if await _visit_since(cid, a["patient_id"], a["appointment_date"]):
            continue                                       # they came later (rebooked and checked in)
        out.append({"patient_id": a["patient_id"], "family_member_id": a.get("family_member_id"), "reason": "visit_missed",
                    "source_key": f"appointments:{a['id']}", "detail": {"date": a["appointment_date"]},
                    "score": score("visit_missed", (today - date.fromisoformat(a["appointment_date"])).days),
                    "enrollment_id": (enrolled.get((a["patient_id"], a.get("family_member_id"))) or {}).get("id")})
    # lab_overdue: OPD orders still 'ordered' after 7 days
    orders = await fetch_all(lambda: scoped_query("opd_orders", cid, "id, patient_id, family_member_id, item_name, created_at")
                             .eq("status", "ordered").lt("created_at", (today - timedelta(days=7)).isoformat())
                             .gte("created_at", (today - timedelta(days=90)).isoformat()), label="cc:radar:orders")
    for o in orders:
        out.append({"patient_id": o["patient_id"], "family_member_id": o.get("family_member_id"), "reason": "lab_overdue",
                    "source_key": f"opd_orders:{o['id']}", "detail": {"item": o["item_name"], "ordered": str(o["created_at"])[:10]},
                    "score": score("lab_overdue", (today - date.fromisoformat(str(o["created_at"])[:10])).days - 7),
                    "enrollment_id": (enrolled.get((o["patient_id"], o.get("family_member_id"))) or {}).get("id")})
    # refill_lapsed (Pharmacy OS clinics): covered_until + 3 d passed, no completed sale of the item since
    try:
        plans = await fetch_all(lambda: scoped_query("rx_refill_plans", cid, "id, patient_id, item_id, covered_until, status")
                                .in_("status", ["scheduled", "sent", "expired"])
                                .lt("covered_until", (today - timedelta(days=3)).isoformat()), label="cc:radar:refill")
    except Exception:                                     # noqa: BLE001 - no Pharmacy OS tables
        plans = []
    for pl in plans:
        out.append({"patient_id": pl["patient_id"], "family_member_id": None, "reason": "refill_lapsed",
                    "source_key": f"rx_refill_plans:{pl['id']}", "detail": {"covered_until": pl["covered_until"]},
                    "score": score("refill_lapsed", (today - date.fromisoformat(pl["covered_until"])).days - 3),
                    "enrollment_id": (enrolled.get((pl["patient_id"], None)) or {}).get("id")})
    # off_target: the latest evaluation of an active enrollment is watch / urgent
    for e in enr:
        if e["status"] != "active":
            continue
        ev = _rows(await sb(scoped_query("cc_evaluations", cid, "id, tier, reasons").eq("id", e["last_evaluation_id"])
                            .limit(1))) if e.get("last_evaluation_id") else []
        if ev and ev[0]["tier"] in ("watch", "urgent"):
            out.append({"patient_id": e["patient_id"], "family_member_id": e.get("family_member_id"), "reason": "off_target",
                        "source_key": f"cc_evaluations:{ev[0]['id']}", "detail": {"tier": ev[0]["tier"],
                                                                                   "reasons": ev[0]["reasons"][:3]},
                        "score": score("off_target", 0, ev[0]["tier"]), "enrollment_id": e["id"]})
        if int(e.get("unanswered_streak") or 0) >= 3:
            out.append({"patient_id": e["patient_id"], "family_member_id": e.get("family_member_id"),
                        "reason": "not_responding", "source_key": f"cc_enrollments:{e['id']}",
                        "detail": {"unanswered": e["unanswered_streak"]}, "score": score("not_responding"),
                        "enrollment_id": e["id"]})
    # discharged_followup: structured IPD follow-up date + 3 d passed without a visit
    try:
        dfs = await fetch_all(lambda: scoped_query("cc_discharge_followups", cid).lt(
            "follow_up_on", (today - timedelta(days=3)).isoformat()).gte("follow_up_on", (today - timedelta(days=60)).isoformat()),
            label="cc:radar:dis")
    except Exception:                                     # noqa: BLE001
        dfs = []
    for d in dfs:
        if not await _visit_since(cid, d["patient_id"], d["follow_up_on"]):
            out.append({"patient_id": d["patient_id"], "family_member_id": None, "reason": "discharged_followup",
                        "source_key": f"cc_discharge_followups:{d['id']}", "detail": {"follow_up_on": d["follow_up_on"]},
                        "score": score("discharged_followup", (today - date.fromisoformat(d["follow_up_on"])).days),
                        "enrollment_id": (enrolled.get((d["patient_id"], None)) or {}).get("id")})
    # candidate: not enrolled, a signed encounter ≤ 180 d carrying a pack's ICD-10 code (signed packs only)
    acts = _rows(await sb(scoped_query("cc_protocol_activations", cid, "protocol_code").eq("status", "active")))
    prefixes = {code: cc_protocols.get(code).ICD10 for code in {a["protocol_code"] for a in acts} if cc_protocols.get(code)}
    scan = encs
    if watermark:
        scan = await fetch_all(lambda: scoped_query("opd_encounters", cid, "id, patient_id, family_member_id, signed_at, "
                                                    "diagnoses").eq("status", "signed").gte("signed_at", max(since, watermark)),
                               label="cc:radar:enc:new")
        for r in await fetch_all(lambda: scoped_query("cc_radar_rows", cid, "patient_id, family_member_id, source_key, detail, "
                                                      "score").eq("reason", "candidate").like("source_key", "pack:%")
                                 .is_("closed_on", "null"), label="cc:radar:carry"):
            if (r["patient_id"], r.get("family_member_id")) not in enrolled and r["source_key"][5:] in prefixes:
                out.append({"patient_id": r["patient_id"], "family_member_id": r.get("family_member_id"),
                            "reason": "candidate", "source_key": r["source_key"], "detail": r["detail"],
                            "score": r["score"], "enrollment_id": None})
    for e in scan:
        key = (e["patient_id"], e.get("family_member_id"))
        if key in enrolled:
            continue
        codes = [str(d.get("code") or "").upper() for d in (e.get("diagnoses") or []) if isinstance(d, dict)]
        for pack_code, pre in prefixes.items():
            if pre and any(c.startswith(p) for c in codes for p in pre):
                out.append({"patient_id": e["patient_id"], "family_member_id": e.get("family_member_id"),
                            "reason": "candidate", "source_key": f"pack:{pack_code}",
                            "detail": {"protocol_code": pack_code, "icd10": [c for c in codes if c][:5],
                                       "encounter_signed": str(e["signed_at"])[:10]}, "score": score("candidate"),
                            "enrollment_id": None})
    # candidate from data import (MI chronic list; MI-09): staff link the legacy record to a live patient first
    try:
        cands = await fetch_all(lambda: scoped_query("mi_chronic_candidates", cid, "id, patient_record_id, condition_text, "
                                                     "icd10_code").eq("status", "new"), label="cc:radar:mi")
        links = {ln["patient_record_id"]: ln["patient_id"] for ln in await fetch_all(
            lambda: scoped_query("mi_patient_links", cid, "patient_record_id, patient_id"), label="cc:radar:links")}
    except Exception:                                     # noqa: BLE001 - migration 144 not applied
        cands, links = [], {}
    for c in cands:
        pid = links.get(c["patient_record_id"])
        if pid and (pid, None) not in enrolled:
            out.append({"patient_id": pid, "family_member_id": None, "reason": "candidate",
                        "source_key": f"mi_chronic_candidates:{c['id']}",
                        "detail": {"condition": c["condition_text"], "icd10": [c.get("icd10_code")] if c.get("icd10_code") else [],
                                   "source": "import"}, "score": score("candidate"), "enrollment_id": None})
    return out


async def rebuild(clinic: dict, today: Optional[date] = None) -> dict:
    """Upsert current rows, close rows whose reason no longer holds, open / update cases above the threshold."""
    from app.services import cc_cases
    cid = clinic["id"]
    today = today or today_ist()
    wm = candidate_watermark(clinic, today)
    started = _now()
    rows = await collect(clinic, today, wm)
    recent_dismissed = {(r["patient_id"], r["reason"], r["source_key"]) for r in await fetch_all(
        lambda: scoped_query("cc_radar_rows", cid, "patient_id, reason, source_key").in_("close_reason", ["dismissed", "declined"])
        .gte("closed_on", (today - timedelta(days=90)).isoformat()), label="cc:radar:dismissed")}
    open_rows = {(r["patient_id"], r.get("family_member_id"), r["reason"], r["source_key"]): r for r in await fetch_all(
        lambda: scoped_query("cc_radar_rows", cid).is_("closed_on", "null"), label="cc:radar:open")}
    seen, opened, closed, cases = set(), 0, 0, 0
    threshold = int(get_settings(clinic).get("radar_threshold") or 30)
    for r in rows:
        k = (r["patient_id"], r.get("family_member_id"), r["reason"], r["source_key"])
        if (r["patient_id"], r["reason"], r["source_key"]) in recent_dismissed or k in seen:
            continue
        seen.add(k)
        cur = open_rows.get(k)
        if cur:
            await sb(supabase.table("cc_radar_rows").update({"last_seen_on": today.isoformat(), "score": r["score"],
                                                             "detail": r["detail"], "enrollment_id": r["enrollment_id"]})
                     .eq("clinic_id", cid).eq("id", cur["id"]))
            row_id = cur["id"]
        else:
            try:
                # unscoped: insert_scoped_by_payload
                row_id = _rows(await sb(supabase.table("cc_radar_rows").insert({
                    "clinic_id": cid, "patient_id": r["patient_id"], "family_member_id": r.get("family_member_id"),
                    "enrollment_id": r["enrollment_id"], "reason": r["reason"], "detail": r["detail"], "score": r["score"],
                    "source_key": r["source_key"], "first_seen_on": today.isoformat(), "last_seen_on": today.isoformat()})))[0]["id"]
                opened += 1
            except Exception as e:                         # noqa: BLE001 - a parallel rebuild inserted it
                if "uq_cc_radar_open" not in str(e):
                    raise
                continue
        tier = (r["detail"] or {}).get("tier") if r["reason"] == "off_target" else None
        if r["reason"] != "candidate" and (r["score"] >= threshold or tier == "urgent") and r["reason"] != "off_target":
            await cc_cases.open_case(clinic, patient_id=r["patient_id"], family_member_id=r.get("family_member_id"),
                                     enrollment_id=r["enrollment_id"], reason=r["reason"], tier="watch",
                                     radar_row_id=row_id, summary={"detail": r["detail"]}, urgent_alert=False)
            cases += 1
    for k, cur in open_rows.items():
        if k not in seen:
            await sb(supabase.table("cc_radar_rows").update({"closed_on": today.isoformat(), "close_reason": "resolved"})
                     .eq("clinic_id", cid).eq("id", cur["id"]).is_("closed_on", "null"))
            closed += 1
    # advance the watermark only after a complete pass (a failed rebuild re-scans from the old one)
    fresh = _rows(await sb(supabase.table("clinics").select("cc_settings").eq("id", cid).limit(1)))
    settings = dict((fresh[0] if fresh else {}).get("cc_settings") or {})
    settings[WATERMARK_KEY] = started
    await sb(supabase.table("clinics").update({"cc_settings": settings}).eq("id", cid))
    clinic["cc_settings"] = settings
    return {"rows": len(seen), "opened": opened, "closed": closed, "cases": cases, "full_scan": wm is None}


async def list_rows(clinic_id: str, reason: Optional[str], min_score: Optional[int], limit: int = 200) -> list:
    from app.utils.validators import mask_phone
    q = scoped_query("cc_radar_rows", clinic_id).is_("closed_on", "null")
    if reason:
        q = q.eq("reason", reason)
    if min_score is not None:
        q = q.gte("score", min_score)
    rows = _rows(await sb(q.order("score", desc=True).limit(limit)))
    pats = {}
    if rows:
        pats = {p["id"]: p for p in _rows(await sb(scoped_query("patients", clinic_id, "id, name, phone, mrn")
                                                   .in_("id", list({r["patient_id"] for r in rows}))))}
    return [{**r, "label": REASON_LABELS.get(r["reason"], r["reason"]), "patient_name": (pats.get(r["patient_id"]) or {}).get("name"),
             "mrn": (pats.get(r["patient_id"]) or {}).get("mrn"),
             "phone_masked": mask_phone((pats.get(r["patient_id"]) or {}).get("phone") or "")} for r in rows]


async def dismiss(clinic_id: str, row_id: str, reason: str, user) -> dict:
    if len((reason or "").strip()) < 5:
        raise HTTPException(status_code=422, detail="Say why in at least 5 characters.")
    done = _rows(await sb(supabase.table("cc_radar_rows").update({
        "closed_on": today_ist().isoformat(), "close_reason": "dismissed", "dismiss_reason": reason.strip()[:300],
        "dismissed_by_name": actor_name(user)}).eq("clinic_id", clinic_id).eq("id", row_id).is_("closed_on", "null")))
    if not done:
        raise HTTPException(status_code=404, detail="Radar row not found or already closed.")
    return done[0]
