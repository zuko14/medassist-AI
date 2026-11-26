"""Specialty OS — Women & Child completion (migration 156; docs/specialty-os/WOMEN-CHILD.md SP-WC-02, 04, 06–08).

Delivery → each live-born baby becomes a family member under the mother's account through the SAME registration path
the OPD desk uses (register_patient: MRN, duplicate check); birth registration is listed as due within 21 days of
birth (Registration of Births and Deaths Act) and recorded when staff enter the registrar's number — Kriya never files
it. Growth: z-scores from WHO LMS tables (sp_growth) stored with each measurement; no interpretation. Government ids
(RCH, MCP card, U-WIN, ABHA) are stored for exports only: the CSVs are Kriya's documented columns for upload or
transcription — no automation of any government portal. NICU flowsheet rows hang off an IPD admission. Zero-LLM.
"""

import csv
import io
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services import sp_growth
from app.services.sp import _db_error, _patient, actor_name, now_iso, rows
from app.utils.helpers import today_ist

BIRTH_REGISTRATION_DAYS = 21


# ── SP-WC-02 delivery → newborn ──────────────────────────────────────────────

async def record_delivery(clinic_id: str, pregnancy_id: str, body: dict, user) -> dict:
    from app.services.opd import register_patient
    preg = rows(await sb(scoped_query("obs_pregnancies", clinic_id).eq("id", pregnancy_id).limit(1)))
    if not preg:
        raise HTTPException(status_code=404, detail="Pregnancy record not found.")
    preg = preg[0]
    if preg["status"] != "active":
        raise HTTPException(status_code=409, detail="This pregnancy record is already closed.")
    mother = await _patient(clinic_id, preg["patient_id"], "id, name, phone")
    try:
        # unscoped: insert_scoped_by_payload
        delivery = rows(await sb(supabase.table("obs_deliveries").insert({
            "clinic_id": clinic_id, "pregnancy_id": pregnancy_id, "admission_id": body.get("admission_id"),
            "delivered_at": body["delivered_at"].isoformat(), "mode": body["mode"], "gestation_weeks": body.get("gestation_weeks"),
            "place": body.get("place") or "this_facility", "notes": body.get("notes"),
            "entered_by_name": actor_name(user)})))[0]
    except Exception as e:
        if "obs_deliveries_pregnancy_key" in str(e):
            raise HTTPException(status_code=409, detail="This delivery is already recorded.") from None
        _db_error(e)
    born_on = body["delivered_at"].date()
    newborns = []
    for n, baby in enumerate(body["babies"], start=1):
        fm_id = None
        if baby["outcome"] == "live_birth" and baby.get("create_child_record", True):
            reg = await _register_child(register_patient, clinic_id, mother, baby, n, len(body["babies"]), born_on, user)
            fm_id = reg.get("family_member_id")
        try:
            # unscoped: insert_scoped_by_payload
            newborns.append(rows(await sb(supabase.table("obs_newborns").insert({
                "clinic_id": clinic_id, "delivery_id": delivery["id"], "birth_order": n, "outcome": baby["outcome"],
                "sex": baby.get("sex"), "birth_weight_g": baby.get("birth_weight_g"), "length_cm": baby.get("length_cm"),
                "head_cm": baby.get("head_cm"), "apgar_1": baby.get("apgar_1"), "apgar_5": baby.get("apgar_5"),
                "nicu_admitted": bool(baby.get("nicu_admitted")), "family_member_id": fm_id})))[0])
        except Exception as e:
            _db_error(e)
        if fm_id and (baby.get("birth_weight_g") or baby.get("length_cm") or baby.get("head_cm")):
            await add_measurement(clinic_id, {"patient_id": mother["id"], "family_member_id": fm_id, "measured_on": born_on,
                                              "weight_kg": (baby["birth_weight_g"] / 1000) if baby.get("birth_weight_g") else None,
                                              "length_cm": baby.get("length_cm"), "position": "recumbent" if baby.get("length_cm") else None,
                                              "head_cm": baby.get("head_cm")}, user)
    await sb(supabase.table("obs_pregnancies").update({"status": "delivered", "updated_at": now_iso()})
             .eq("clinic_id", clinic_id).eq("id", pregnancy_id).eq("status", "active"))
    return {**delivery, "newborns": newborns}


async def _register_child(register_patient, clinic_id, mother, baby, n, count, born_on, user) -> dict:
    """The OPD registration path. Its duplicate check lists the mother's account (same phone) and any sibling already on
    it — the family this child joins — so exactly those are acknowledged, as the desk would; anyone else stays a 409."""
    body = {"phone": mother["phone"], "name": (baby.get("name") or f"Baby of {mother['name']}" + (f" ({n})" if count > 1 else ""))[:100],
            "is_account_holder": False, "relationship": "child", "date_of_birth": born_on,
            "gender": {"male": "male", "female": "female"}.get(baby.get("sex"), "undisclosed"), "data_consent": True}
    try:
        return await register_patient(clinic_id, body, user)
    except HTTPException as e:
        cands = (e.detail or {}).get("candidates") if e.status_code == 409 and isinstance(e.detail, dict) else None
        if not cands or any(c.get("phone") != mother["phone"] for c in cands):
            raise
        ack = [f"{c['patient_id']}:{c.get('family_member_id') or ''}" for c in cands] + [c["patient_id"] for c in cands]
        return await register_patient(clinic_id, {**body, "acknowledged_duplicates": ack}, user)


async def birth_registrations(clinic_id: str) -> list:
    """Live births and their registration state (due within 21 days of birth)."""
    today = today_ist()
    dels = {d["id"]: d for d in await fetch_all(lambda: scoped_query("obs_deliveries", clinic_id, "id, delivered_at, pregnancy_id")
                                                .order("id"), label="sp:wc:dels")}
    out = []
    for nb in await fetch_all(lambda: scoped_query("obs_newborns", clinic_id).eq("outcome", "live_birth").order("id"),
                              label="sp:wc:nb"):
        d = dels.get(nb["delivery_id"]) or {}
        born = datetime.fromisoformat(str(d.get("delivered_at")).replace("Z", "+00:00")).date() if d.get("delivered_at") else today
        due = born + timedelta(days=BIRTH_REGISTRATION_DAYS)
        state = "registered" if nb.get("birth_reg_no") else ("overdue" if today > due else "due")
        out.append({**nb, "born_on": born.isoformat(), "register_by": due.isoformat(), "state": state})
    return sorted(out, key=lambda x: (x["state"] == "registered", x["register_by"]))


async def mark_registered(clinic_id: str, newborn_id: str, reg_no: str, on: date) -> dict:
    out = rows(await sb(supabase.table("obs_newborns").update({"birth_reg_no": reg_no.strip(), "birth_registered_on": on.isoformat()})
                        .eq("clinic_id", clinic_id).eq("id", newborn_id).eq("outcome", "live_birth")))
    if not out:
        raise HTTPException(status_code=404, detail="Live birth not found.")
    return out[0]


# ── SP-WC-04 growth ──────────────────────────────────────────────────────────

async def _child_facts(clinic_id: str, patient_id: str, family_member_id: Optional[str]) -> dict:
    if family_member_id:
        fm = rows(await sb(scoped_query("family_members", clinic_id, "id, full_name, date_of_birth, gender")
                           .eq("id", family_member_id).limit(1)))
        if not fm:
            raise HTTPException(status_code=404, detail="Child not found.")
        return {"name": fm[0]["full_name"], "dob": fm[0].get("date_of_birth"), "sex": fm[0].get("gender")}
    p = await _patient(clinic_id, patient_id, "id, name, date_of_birth, gender")
    return {"name": p["name"], "dob": p.get("date_of_birth"), "sex": p.get("gender")}


async def add_measurement(clinic_id: str, body: dict, user) -> dict:
    child = await _child_facts(clinic_id, body["patient_id"], body.get("family_member_id"))
    if not child.get("dob"):
        raise HTTPException(status_code=409, detail="Record the child's date of birth first.")
    on = body["measured_on"] if isinstance(body["measured_on"], date) else date.fromisoformat(str(body["measured_on"]))
    dob = date.fromisoformat(str(child["dob"])[:10])
    if on < dob or on > today_ist():
        raise HTTPException(status_code=422, detail="The measurement date must be between birth and today.")
    z = sp_growth.measurement_z(child.get("sex") or "", dob, on, body.get("weight_kg"), body.get("length_cm"),
                                body.get("position") or "recumbent", body.get("head_cm"))
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("child_growth_measurements").insert({
            "clinic_id": clinic_id, "patient_id": body["patient_id"], "family_member_id": body.get("family_member_id"),
            "measured_on": on.isoformat(), "weight_kg": body.get("weight_kg"), "length_cm": body.get("length_cm"),
            "position": body.get("position") if body.get("length_cm") else None, "head_cm": body.get("head_cm"),
            "z": z, "entered_by_name": actor_name(user)})))[0]
    except Exception as e:
        if "child_growth_measurements_has_value" in str(e):
            raise HTTPException(status_code=422, detail="Enter at least one of weight, length / height or head.") from None
        _db_error(e)


async def growth_chart(clinic_id: str, patient_id: str, family_member_id: Optional[str]) -> dict:
    child = await _child_facts(clinic_id, patient_id, family_member_id)
    q = scoped_query("child_growth_measurements", clinic_id).eq("patient_id", patient_id)
    q = q.eq("family_member_id", family_member_id) if family_member_id else q.is_("family_member_id", "null")
    pts = rows(await sb(q.order("measured_on")))
    return {"child": child, "points": [{**p, "bands": {k: sp_growth.band(v) for k, v in (p.get("z") or {}).items()
                                                       if k in sp_growth.INDICATORS}} for p in pts],
            "sex_known": child.get("sex") in ("male", "female"),
            "note": "WHO Child Growth Standards (0–5 y) and Growth Reference (5–19 y); z-scores are arithmetic — the doctor "
                    "interprets them."}


# ── SP-WC-07 government ids + exports ────────────────────────────────────────

async def upsert_gov_ids(clinic_id: str, body: dict, user) -> dict:
    await _child_facts(clinic_id, body["patient_id"], body.get("family_member_id"))
    q = scoped_query("wc_gov_ids", clinic_id, "id").eq("patient_id", body["patient_id"])
    q = q.eq("family_member_id", body["family_member_id"]) if body.get("family_member_id") else q.is_("family_member_id", "null")
    cur = rows(await sb(q.limit(1)))
    row = {k: (body.get(k) or None) for k in ("rch_id", "mcp_card_no", "uwin_id", "abha_number")}
    row.update(updated_by_name=actor_name(user), updated_at=now_iso())
    try:
        if cur:
            return rows(await sb(supabase.table("wc_gov_ids").update(row).eq("clinic_id", clinic_id).eq("id", cur[0]["id"])))[0]
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("wc_gov_ids").insert({**row, "clinic_id": clinic_id, "patient_id": body["patient_id"],
                                                                   "family_member_id": body.get("family_member_id")})))[0]
    except Exception as e:
        _db_error(e)


async def get_gov_ids(clinic_id: str, patient_id: str, family_member_id: Optional[str]) -> dict:
    q = scoped_query("wc_gov_ids", clinic_id).eq("patient_id", patient_id)
    q = q.eq("family_member_id", family_member_id) if family_member_id else q.is_("family_member_id", "null")
    r = rows(await sb(q.limit(1)))
    return r[0] if r else {}


def _csv(header: list, data: list) -> str:
    from app.services.client_data import csv_cell
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in data:
        w.writerow([csv_cell(v) for v in r])
    return buf.getvalue()


UWIN_HEADER = ["U-WIN beneficiary id", "RCH id", "ABHA", "Child name", "Date of birth", "Sex", "Vaccine", "Dose", "Date given",
               "Batch", "Expiry", "Site", "Given by", "Schedule"]
RCH_HEADER = ["RCH id", "MCP card", "Mother name", "LMP", "EDD", "EDD source", "G", "P", "A", "L", "Blood group",
              "ANC visits", "Last visit", "Last BP", "Last Hb", "Status"]


async def uwin_csv(clinic_id: str, since: date, until: date) -> str:
    """Vaccines given in the period with the child's government ids — Kriya's documented columns (U-WIN's own bulk
    format is not public: staff upload or transcribe from this sheet)."""
    doses = await fetch_all(lambda: scoped_query("imm_doses", clinic_id).gte("given_on", since.isoformat())
                            .lte("given_on", until.isoformat()).order("id"), label="sp:wc:uwin")
    ids = {(g["patient_id"], g.get("family_member_id")): g for g in await fetch_all(
        lambda: scoped_query("wc_gov_ids", clinic_id).order("id"), label="sp:wc:ids")}
    out = []
    for d in sorted(doses, key=lambda x: (str(x["given_on"]), x["vaccine_code"])):
        c = await _child_facts(clinic_id, d["patient_id"], d.get("family_member_id"))
        g = ids.get((d["patient_id"], d.get("family_member_id"))) or {}
        out.append([g.get("uwin_id") or "", g.get("rch_id") or "", g.get("abha_number") or "", c["name"], c.get("dob") or "",
                    c.get("sex") or "", d["vaccine_code"], d["dose_no"], d["given_on"], d["batch_no"], d["expiry"], d["site"],
                    d["given_by_name"], d["schedule_code"]])
    return _csv(UWIN_HEADER, out)


async def rch_csv(clinic_id: str) -> str:
    """Pregnancy register with RCH ids (Kriya's columns, for RCH portal data entry by staff)."""
    pregs = await fetch_all(lambda: scoped_query("obs_pregnancies", clinic_id).order("id"), label="sp:wc:rch")
    ids = {(g["patient_id"], g.get("family_member_id")): g for g in await fetch_all(
        lambda: scoped_query("wc_gov_ids", clinic_id).order("id"), label="sp:wc:ids2")}
    out = []
    for p in pregs:
        m = await _patient(clinic_id, p["patient_id"], "id, name")
        visits = rows(await sb(scoped_query("obs_anc_visits", clinic_id).eq("pregnancy_id", p["id"]).order("visit_date")))
        last = visits[-1] if visits else {}
        g = ids.get((p["patient_id"], None)) or {}
        out.append([g.get("rch_id") or "", g.get("mcp_card_no") or "", m["name"], p.get("lmp") or "", p["edd"], p["edd_source"],
                    p["gravida"], p["para"], p["abortions"], p["living"], p["blood_group"], len(visits),
                    last.get("visit_date") or "", f"{last['bp_sys']}/{last['bp_dia']}" if last.get("bp_sys") else "",
                    last.get("hb_g_dl") or "", p["status"]])
    return _csv(RCH_HEADER, out)


# ── SP-WC-08 NICU flowsheet ──────────────────────────────────────────────────

async def _admission(clinic_id: str, admission_id: str) -> dict:
    a = rows(await sb(scoped_query("ipd_admissions", clinic_id, "id, status, patient_id, family_member_id")
                      .eq("id", admission_id).limit(1)))
    if not a:
        raise HTTPException(status_code=404, detail="Admission not found.")
    return a[0]


async def add_nicu_entry(clinic_id: str, admission_id: str, body: dict, user) -> dict:
    adm = await _admission(clinic_id, admission_id)
    if adm["status"] not in ("admitted", "active", "discharge_pending"):
        raise HTTPException(status_code=409, detail="The baby is not currently admitted.")
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("nicu_flowsheet_entries").insert({
            **{k: v for k, v in body.items() if k != "recorded_at"}, "recorded_at": body["recorded_at"].isoformat(),
            "clinic_id": clinic_id, "admission_id": admission_id, "entered_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)


async def nicu_flowsheet(clinic_id: str, admission_id: str) -> dict:
    await _admission(clinic_id, admission_id)
    entries = rows(await sb(scoped_query("nicu_flowsheet_entries", clinic_id).eq("admission_id", admission_id)
                            .order("recorded_at")))
    weights = [e for e in entries if e.get("weight_g")]
    return {"entries": entries, "weight_change_g": (weights[-1]["weight_g"] - weights[0]["weight_g"]) if len(weights) > 1 else None,
            "feeds_24h_ml": sum(int(e.get("feed_ml") or 0) for e in entries
                                if str(e["recorded_at"]) >= (datetime.now().astimezone() - timedelta(hours=24)).isoformat())}
