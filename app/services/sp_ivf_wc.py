"""Specialty OS — IVF (migration 154) and Women & Child (migration 155) depth.

IVF: couple record (identity type + last 4 only), cycle with the stimulation chart (drugs exactly as signed, follicles in
mm, endometrium, E2 / LH / P4), the trigger time exactly as the doctor entered it with an acknowledgement state (WhatsApp
button `spivf:ack:<cycle>` or staff-recorded), embryology grading entered by the embryologist (no AI, no sex field —
PCPNDT), double witnessing by two DIFFERENT logins within 15 minutes, hash-chained append-only (ivf_witness_verify), and
a result-release hold (a cycle outcome is never auto-sent). The ART register export is a structure for the owner's legal
review (SP-O2) — not a filed government format.
Women & Child: pregnancy record with the gestational age computed from the doctor-confirmed EDD, ANC timeline weeks 0–40
(visits, and the standard contact / scan / test markers as fixed reference points, not advice), and the immunisation
schedule (code-defined IAP 2023 / UIP tables, chosen by the clinic) with due / given / overdue chips and batch numbers.
Catch-up is decided by the doctor; Kriya only lists what the chosen schedule says is due. Zero-LLM.
"""

import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services.sp import _db_error, _patient, actor_name, now_iso, rows
from app.utils.helpers import today_ist
from app.utils.validators import mask_phone

IVF_PREFIX = "spivf:ack:"


# ── IVF ──────────────────────────────────────────────────────────────────────

async def create_couple(clinic_id: str, body: dict, user) -> dict:
    await _patient(clinic_id, body["female_patient_id"])
    if body.get("male_patient_id"):
        await _patient(clinic_id, body["male_patient_id"])
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("ivf_couples").insert({**body, "clinic_id": clinic_id,
                                                                    "identity_verified_by_name": actor_name(user)})))[0]
    except Exception as e:
        msg = str(e)
        for key, text in (("ivf_couples_partner", "Name either the male partner or a donor code (not both)."),
                          ("ivf_couples_single_uses_donor", "A single woman's cycle uses a donor code."),
                          ("ivf_couples_male_id", "Record the male partner's ID type and last 4 characters."),
                          ("ivf_couples_distinct", "The two partners must be different people.")):
            if key in msg:
                raise HTTPException(status_code=422, detail=text) from None
        _db_error(e)


async def list_couples(clinic_id: str) -> list:
    couples = rows(await sb(scoped_query("ivf_couples", clinic_id).order("created_at", desc=True).limit(300)))
    ids = list({c["female_patient_id"] for c in couples} | {c["male_patient_id"] for c in couples if c.get("male_patient_id")})
    pats = {p["id"]: p for p in rows(await sb(scoped_query("patients", clinic_id, "id, name, mrn").in_("id", ids)))} if ids else {}
    cycles: dict = {}
    if couples:
        for cy in rows(await sb(scoped_query("ivf_cycles", clinic_id, "id, couple_id, cycle_type, start_date, status")
                                .in_("couple_id", [c["id"] for c in couples]).order("start_date", desc=True))):
            cycles.setdefault(cy["couple_id"], []).append(cy)
    return [{**c, "cycles": cycles.get(c["id"], []), "female_name": (pats.get(c["female_patient_id"]) or {}).get("name"),
             "male_name": (pats.get(c.get("male_patient_id")) or {}).get("name") if c.get("male_patient_id") else None}
            for c in couples]


async def _cycle(clinic_id: str, cycle_id: str) -> dict:
    c = rows(await sb(scoped_query("ivf_cycles", clinic_id).eq("id", cycle_id).limit(1)))
    if not c:
        raise HTTPException(status_code=404, detail="Cycle not found.")
    return c[0]


async def create_cycle(clinic_id: str, body: dict, user) -> dict:
    couple = rows(await sb(scoped_query("ivf_couples", clinic_id, "id, status").eq("id", body["couple_id"]).limit(1)))
    if not couple or couple[0]["status"] != "active":
        raise HTTPException(status_code=404, detail="Active couple not found.")
    # unscoped: insert_scoped_by_payload
    return rows(await sb(supabase.table("ivf_cycles").insert({
        "clinic_id": clinic_id, "couple_id": body["couple_id"], "cycle_type": body["cycle_type"],
        "protocol_text": body.get("protocol_text"), "start_date": body["start_date"].isoformat(),
        "created_by_name": actor_name(user)})))[0]


async def cycle_chart(clinic_id: str, cycle_id: str) -> dict:
    """Day columns × rows (drugs as signed, follicles L/R, E2/LH/P4, endometrium) + the pinned trigger card."""
    c = await _cycle(clinic_id, cycle_id)
    days = rows(await sb(scoped_query("ivf_cycle_days", clinic_id).eq("cycle_id", cycle_id).order("day_no")))
    embryos = rows(await sb(scoped_query("ivf_embryos", clinic_id).eq("cycle_id", cycle_id).order("label")))
    witness = rows(await sb(scoped_query("ivf_witness_events", clinic_id, "seq, step, embryo_id, performed_by_name, "
                                         "witnessed_by_name, performed_at, witnessed_at").eq("cycle_id", cycle_id)
                            .order("seq")))
    trigger = None
    if c.get("trigger_at"):
        trigger = {"at": c["trigger_at"], "entered_by": c["trigger_entered_by_name"],
                   "acknowledged": bool(c.get("trigger_ack_at")), "ack_at": c.get("trigger_ack_at"),
                   "ack_via": c.get("trigger_ack_via")}
    return {"cycle": c, "trigger": trigger, "days": days, "embryos": embryos, "witness": witness,
            "lead_follicles": {d["day_no"]: sum(1 for x in (d["follicles_left"] or []) + (d["follicles_right"] or [])
                                                if float(x) >= 17) for d in days}}


async def _signed_rx_drugs(clinic_id: str, prescription_id: str) -> list:
    rx = rows(await sb(scoped_query("opd_prescriptions", clinic_id, "id, status").eq("id", prescription_id).limit(1)))
    if not rx or rx[0]["status"] != "signed":
        raise HTTPException(status_code=409, detail="Drugs are copied only from a signed prescription.")
    items = rows(await sb(scoped_query("opd_prescription_items", clinic_id, "drug_name, strength, dosage, frequency")
                          .eq("prescription_id", prescription_id).order("line_no")))
    return [{"name": f"{i['drug_name']}{(' ' + i['strength']) if i.get('strength') else ''}"[:80],
             "dose": f"{i.get('dosage') or ''} {i.get('frequency') or ''}".strip()[:60]} for i in items][:10]


async def upsert_day(clinic_id: str, cycle_id: str, body: dict, user) -> dict:
    c = await _cycle(clinic_id, cycle_id)
    if c["status"] in ("cancelled", "completed"):
        raise HTTPException(status_code=409, detail="This cycle is closed.")
    for side in ("follicles_left", "follicles_right"):
        if any(not 1 <= float(x) <= 40 for x in body.get(side) or []):
            raise HTTPException(status_code=422, detail="Follicle sizes are 1–40 mm.")
    row = {"on_date": body["on_date"].isoformat(), "follicles_left": sorted(body.get("follicles_left") or [], reverse=True),
           "follicles_right": sorted(body.get("follicles_right") or [], reverse=True),
           **{k: body.get(k) for k in ("endometrium_mm", "e2_pg_ml", "lh_miu_ml", "p4_ng_ml")},
           "entered_by_name": actor_name(user), "updated_at": now_iso()}
    if body.get("prescription_id"):
        row["drugs"] = await _signed_rx_drugs(clinic_id, body["prescription_id"])
        row["prescription_id"] = body["prescription_id"]
    cur = rows(await sb(scoped_query("ivf_cycle_days", clinic_id, "id").eq("cycle_id", cycle_id).eq("day_no", body["day_no"])
                        .limit(1)))
    try:
        if cur:
            return rows(await sb(supabase.table("ivf_cycle_days").update(row).eq("clinic_id", clinic_id).eq("id", cur[0]["id"])))[0]
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("ivf_cycle_days").insert({**row, "clinic_id": clinic_id, "cycle_id": cycle_id,
                                                                       "day_no": body["day_no"]})))[0]
    except Exception as e:
        _db_error(e)


async def set_trigger(clinic_id: str, cycle_id: str, trigger_at: datetime, user) -> dict:
    """The trigger time exactly as the doctor entered it; a changed time resets the acknowledgement."""
    c = await _cycle(clinic_id, cycle_id)
    if c["status"] not in ("stimulation", "triggered"):
        raise HTTPException(status_code=409, detail="The trigger can be set only during stimulation.")
    return rows(await sb(supabase.table("ivf_cycles").update({
        "trigger_at": trigger_at.isoformat(), "trigger_entered_by_name": actor_name(user), "trigger_ack_at": None,
        "trigger_ack_via": None, "status": "triggered", "updated_at": now_iso()}).eq("clinic_id", clinic_id).eq("id", cycle_id)))[0]


async def send_trigger(clinic: dict, cycle_id: str) -> dict:
    """WhatsApp to the female partner: the time exactly as entered + an "I have noted it" button (in-window)."""
    from app.services.whatsapp import whatsapp_service
    cid = clinic["id"]
    c = await _cycle(cid, cycle_id)
    if not c.get("trigger_at"):
        raise HTTPException(status_code=409, detail="Set the trigger time first.")
    couple = rows(await sb(scoped_query("ivf_couples", cid, "female_patient_id").eq("id", c["couple_id"]).limit(1)))
    p = await _patient(cid, couple[0]["female_patient_id"])
    if not p.get("opted_in") or not p.get("phone"):
        raise HTTPException(status_code=409, detail="The patient has not opted in to WhatsApp: call and record the acknowledgement.")
    when = datetime.fromisoformat(str(c["trigger_at"]).replace("Z", "+00:00")).astimezone(
        timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")
    body = (f"{clinic.get('name', '')}: your trigger injection is at {when} (IST), as written by your doctor. "
            "Please tap below to confirm you have noted the time.")
    ok = await whatsapp_service.send_interactive_buttons(clinic, p["phone"], body, [
        {"id": f"{IVF_PREFIX}{cycle_id}", "title": "I have noted it"}], _source="sp_ivf")
    if not ok:
        raise HTTPException(status_code=409, detail="The patient's WhatsApp window is closed: call and record the acknowledgement.")
    return {"sent": True, "to": mask_phone(p["phone"]), "time_ist": when}


async def acknowledge_trigger(clinic_id: str, cycle_id: str, via: str, phone: Optional[str] = None) -> Optional[dict]:
    c = rows(await sb(scoped_query("ivf_cycles", clinic_id).eq("id", cycle_id).limit(1)))
    if not c or not c[0].get("trigger_at"):
        return None
    if phone:                                         # only the female partner's own number acknowledges by button
        couple = rows(await sb(scoped_query("ivf_couples", clinic_id, "female_patient_id").eq("id", c[0]["couple_id"]).limit(1)))
        p = rows(await sb(scoped_query("patients", clinic_id, "phone").eq("id", couple[0]["female_patient_id"]).limit(1)))
        digits = lambda x: "".join(ch for ch in str(x or "") if ch.isdigit())[-10:]   # noqa: E731
        if not p or digits(p[0]["phone"]) != digits(phone):
            return None
    out = rows(await sb(supabase.table("ivf_cycles").update({"trigger_ack_at": now_iso(), "trigger_ack_via": via,
                                                              "updated_at": now_iso()})
                        .eq("clinic_id", clinic_id).eq("id", cycle_id).is_("trigger_ack_at", "null")))
    return out[0] if out else c[0]


async def add_embryo(clinic_id: str, cycle_id: str, body: dict, user) -> dict:
    await _cycle(clinic_id, cycle_id)
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("ivf_embryos").insert({
            "clinic_id": clinic_id, "cycle_id": cycle_id, "label": body["label"], "day": body["day"],
            "grade": body["grade"], "graded_by_name": actor_name(user)})))[0]
    except Exception as e:
        if "ivf_embryos_grade_check" in str(e) or "ivf_embryos_label_check" in str(e):
            raise HTTPException(status_code=422, detail="Grade as Gardner (e.g. 4AA), pronuclei (2PN) or cell count "
                                                        "(8c, 8cG1); label E1–E99.") from None
        if "ivf_embryos_label_key" in str(e):
            raise HTTPException(status_code=409, detail="That embryo label is already used in this cycle.") from None
        _db_error(e)


STEP_FOR = {"transferred": "transfer", "frozen": "freeze", "discarded": "discard", "thawed": "thaw"}


async def witness(clinic_id: str, cycle_id: str, body: dict, performer, witness_user) -> dict:
    """One witnessed step: performer = the login doing the step; witness = a SECOND login (separately authenticated by
    the route) confirming within 15 minutes. Disposition steps also move the embryo."""
    await _cycle(clinic_id, cycle_id)
    pid, wid = str(getattr(performer, "user_id", "") or ""), str(getattr(witness_user, "user_id", "") or "")
    if not pid or not wid or pid == wid:
        raise HTTPException(status_code=422, detail="The witness must be a different person (a different login).")
    now = datetime.now(timezone.utc)
    performed_at = body.get("performed_at") or now
    embryo_id = body.get("embryo_id")
    if body["step"] in ("transfer", "freeze", "thaw", "discard"):
        if not embryo_id:
            raise HTTPException(status_code=422, detail="Pick the embryo.")
        disp = {v: k for k, v in STEP_FOR.items()}[body["step"]]
        patch = {"disposition": disp, "disposed_at": now_iso(), "updated_at": now_iso(),
                 "consent_id": body.get("consent_id"), "cryo_location": body.get("cryo_location") if disp == "frozen" else None}
        if disp == "thawed":
            patch["disposed_at"] = now_iso()
        try:
            moved = rows(await sb(supabase.table("ivf_embryos").update({k: v for k, v in patch.items()
                                                                         if v is not None or k == "cryo_location"})
                                  .eq("clinic_id", clinic_id).eq("id", embryo_id).eq("cycle_id", cycle_id)))
        except Exception as e:
            if "ivf_embryos_cryo" in str(e) and "uq_" not in str(e):
                raise HTTPException(status_code=422, detail="A frozen embryo needs its straw position (T1-C1-K1-S1).") from None
            _db_error(e)
        if not moved:
            raise HTTPException(status_code=404, detail="Embryo not found in this cycle.")
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("ivf_witness_events").insert({
            "clinic_id": clinic_id, "seq": 0, "cycle_id": cycle_id, "embryo_id": embryo_id, "step": body["step"],
            "performed_by": pid, "performed_by_name": actor_name(performer), "witnessed_by": wid,
            "witnessed_by_name": actor_name(witness_user), "performed_at": performed_at.isoformat(),
            "witnessed_at": now.isoformat(), "note": body.get("note"), "row_hash": "\\x00"})))[0]
    except Exception as e:
        _db_error(e)


async def verify_chain(clinic_id: str) -> Optional[int]:
    return (await sb(supabase.rpc("ivf_witness_verify", {"p_clinic_id": clinic_id}))).data


async def release_result(clinic_id: str, cycle_id: str, outcome: str, user) -> dict:
    """The counsellor / doctor releases the outcome (IVF-7): recorded here; it is told to the couple by a person."""
    out = rows(await sb(supabase.table("ivf_cycles").update({
        "outcome": outcome, "result_released_at": now_iso(), "result_released_by_name": actor_name(user),
        "updated_at": now_iso()}).eq("clinic_id", clinic_id).eq("id", cycle_id).is_("result_released_at", "null")))
    if not out:
        raise HTTPException(status_code=409, detail="The result was already released.")
    return out[0]


async def art_register_csv(clinic_id: str) -> str:
    """Cycle-level register for the owner's legal review (SP-O2). Identity is the ID type + last 4 only; no sex field."""
    from app.services.client_data import csv_cell
    couples = {c["id"]: c for c in await fetch_all(lambda: scoped_query("ivf_couples", clinic_id), label="sp:art:c")}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Cycle start", "Cycle type", "Couple ID proof (female)", "Partner / donor", "Status", "OPU", "Transfer",
                "Embryos", "Frozen", "Outcome (released)"])
    for c in await fetch_all(lambda: scoped_query("ivf_cycles", clinic_id).order("start_date"), label="sp:art:cy"):
        cp = couples.get(c["couple_id"]) or {}
        emb = rows(await sb(scoped_query("ivf_embryos", clinic_id, "disposition").eq("cycle_id", c["id"])))
        w.writerow([csv_cell(v) for v in (
            c["start_date"], c["cycle_type"], f"{cp.get('female_id_type', '')} …{cp.get('female_id_last4', '')}",
            "donor " + cp["donor_code"] if cp.get("donor_code") else "partner", c["status"], str(c.get("opu_at") or "")[:10],
            str(c.get("transfer_at") or "")[:10], len(emb), sum(1 for e in emb if e["disposition"] == "frozen"),
            c["outcome"] if c.get("result_released_at") else "held")])
    return buf.getvalue()


# ── Women & Child: pregnancy + ANC ───────────────────────────────────────────

def ga_on(edd: date, on: date) -> tuple[int, int]:
    """Gestational age (weeks, days) on a date from the confirmed EDD (EDD = 40 + 0)."""
    days = 280 - (edd - on).days
    return max(days, 0) // 7, max(days, 0) % 7


#: fixed reference markers shown on the timeline (WHO 2016 ANC contacts; scans / tests as commonly scheduled).
ANC_MARKERS = (
    (8, "contact", "First contact"), (12, "scan", "Dating / NT scan"), (20, "contact", "Contact 2"),
    (20, "scan", "Anomaly scan"), (26, "contact", "Contact 3"), (26, "test", "Glucose tolerance test"),
    (30, "contact", "Contact 4"), (34, "contact", "Contact 5"), (36, "contact", "Contact 6"), (38, "contact", "Contact 7"),
    (40, "contact", "Contact 8"),
)


async def create_pregnancy(clinic_id: str, body: dict, user) -> dict:
    await _patient(clinic_id, body["patient_id"])
    row = {**body, "clinic_id": clinic_id, "edd": body["edd"].isoformat(), "lmp": body["lmp"].isoformat() if body.get("lmp") else None,
           "created_by_name": actor_name(user)}
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("obs_pregnancies").insert(row)))[0]
    except Exception as e:
        for key, text in (("obs_pregnancies_edd", "The EDD must be 26–44 weeks after the LMP."),
                          ("obs_pregnancies_gpal", "Para + abortions must be less than gravida."),
                          ("obs_pregnancies_lmp_source", "An LMP-based EDD needs the LMP.")):
            if key in str(e):
                raise HTTPException(status_code=422, detail=text) from None
        _db_error(e)


async def _pregnancy(clinic_id: str, pregnancy_id: str) -> dict:
    p = rows(await sb(scoped_query("obs_pregnancies", clinic_id).eq("id", pregnancy_id).limit(1)))
    if not p:
        raise HTTPException(status_code=404, detail="Pregnancy record not found.")
    return p[0]


async def add_anc_visit(clinic_id: str, pregnancy_id: str, body: dict, user) -> dict:
    preg = await _pregnancy(clinic_id, pregnancy_id)
    if preg["status"] != "active":
        raise HTTPException(status_code=409, detail="This pregnancy record is closed.")
    weeks, _ = ga_on(date.fromisoformat(str(preg["edd"])[:10]), body["visit_date"])
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("obs_anc_visits").insert({
            **{k: v for k, v in body.items() if k != "visit_date"}, "visit_date": body["visit_date"].isoformat(),
            "clinic_id": clinic_id, "pregnancy_id": pregnancy_id, "ga_weeks": min(weeks, 44),
            "entered_by_name": actor_name(user)})))[0]
    except Exception as e:
        if "obs_anc_visits_bp" in str(e):
            raise HTTPException(status_code=422, detail="Enter both BP values (systolic above diastolic).") from None
        _db_error(e)


async def anc_timeline(clinic_id: str, pregnancy_id: str) -> dict:
    preg = await _pregnancy(clinic_id, pregnancy_id)
    edd = date.fromisoformat(str(preg["edd"])[:10])
    weeks, days = ga_on(edd, today_ist())
    visits = rows(await sb(scoped_query("obs_anc_visits", clinic_id).eq("pregnancy_id", pregnancy_id).order("visit_date")))
    markers = [{"week": w, "kind": k, "label": label, "date": (edd - timedelta(days=280 - 7 * w)).isoformat(),
                "state": "done" if any(abs(v["ga_weeks"] - w) <= 2 for v in visits) and k == "contact"
                else ("past" if w < weeks else "upcoming")} for w, k, label in ANC_MARKERS]
    return {"pregnancy": preg, "ga_today": {"weeks": min(weeks, 44), "days": days}, "edd": edd.isoformat(),
            "visits": visits, "markers": markers, "weeks": list(range(0, 41)),
            "note": "Markers are the standard schedule for orientation; the doctor decides each visit, scan and test."}


# ── immunisation (code-defined schedules; due by age in days) ────────────────

IMM_SCHEDULES: dict[str, tuple] = {
    # (vaccine_code, dose_no, label, due_days) — IAP 2023 (private practice) — verify against the current IAP chart
    "iap_2023": (
        ("bcg", 1, "BCG", 0), ("opv", 1, "OPV 0", 0), ("hepb", 1, "Hep B 1", 0),
        ("dtp", 1, "DTwP/DTaP 1", 42), ("ipv", 1, "IPV 1", 42), ("hib", 1, "Hib 1", 42), ("hepb", 2, "Hep B 2", 42),
        ("rota", 1, "Rotavirus 1", 42), ("pcv", 1, "PCV 1", 42),
        ("dtp", 2, "DTwP/DTaP 2", 70), ("ipv", 2, "IPV 2", 70), ("hib", 2, "Hib 2", 70), ("hepb", 3, "Hep B 3", 70),
        ("rota", 2, "Rotavirus 2", 70), ("pcv", 2, "PCV 2", 70),
        ("dtp", 3, "DTwP/DTaP 3", 98), ("ipv", 3, "IPV 3", 98), ("hib", 3, "Hib 3", 98), ("hepb", 4, "Hep B 4", 98),
        ("rota", 3, "Rotavirus 3", 98), ("pcv", 3, "PCV 3", 98),
        ("flu", 1, "Influenza 1", 183), ("flu", 2, "Influenza 2", 213), ("tcv", 1, "Typhoid conjugate", 183),
        ("mmr", 1, "MMR 1", 274), ("hepa", 1, "Hep A 1", 365), ("mmr", 2, "MMR 2", 456), ("varicella", 1, "Varicella 1", 456),
        ("pcv", 4, "PCV booster", 456), ("dtp", 4, "DTP booster 1", 487), ("ipv", 4, "IPV booster", 487),
        ("hib", 4, "Hib booster", 487), ("hepa", 2, "Hep A 2", 548), ("varicella", 2, "Varicella 2", 548),
        ("dtp", 5, "DTP booster 2", 1461), ("mmr", 3, "MMR 3", 1461), ("tdap", 1, "Tdap", 3653), ("hpv", 1, "HPV 1", 3288),
    ),
    # Universal Immunization Programme (national) — verify against the current MoHFW UIP schedule
    "uip": (
        ("bcg", 1, "BCG", 0), ("opv", 1, "OPV 0", 0), ("hepb", 1, "Hep B birth", 0),
        ("opv", 2, "OPV 1", 42), ("penta", 1, "Pentavalent 1", 42), ("fipv", 1, "fIPV 1", 42), ("rota", 1, "Rotavirus 1", 42),
        ("pcv", 1, "PCV 1", 42),
        ("opv", 3, "OPV 2", 70), ("penta", 2, "Pentavalent 2", 70), ("rota", 2, "Rotavirus 2", 70),
        ("opv", 4, "OPV 3", 98), ("penta", 3, "Pentavalent 3", 98), ("fipv", 2, "fIPV 2", 98), ("rota", 3, "Rotavirus 3", 98),
        ("pcv", 2, "PCV 2", 98),
        ("mr", 1, "MR 1", 274), ("pcv", 3, "PCV booster", 274), ("fipv", 3, "fIPV 3", 274),
        ("mr", 2, "MR 2", 487), ("dpt", 1, "DPT booster 1", 487), ("opv", 5, "OPV booster", 487),
        ("dpt", 2, "DPT booster 2", 1826), ("td", 1, "Td (10 y)", 3653), ("td", 2, "Td (16 y)", 5844),
    ),
}
OVERDUE_AFTER_DAYS = 28


async def _child(clinic_id: str, patient_id: str, family_member_id: Optional[str]) -> dict:
    if family_member_id:
        fm = rows(await sb(scoped_query("family_members", clinic_id, "id, full_name, date_of_birth")
                           .eq("id", family_member_id).limit(1)))
        if not fm:
            raise HTTPException(status_code=404, detail="Child not found.")
        return {"name": fm[0]["full_name"], "dob": fm[0].get("date_of_birth")}
    p = await _patient(clinic_id, patient_id, "id, name, date_of_birth")
    return {"name": p["name"], "dob": p.get("date_of_birth")}


async def immunisation(clinic_id: str, patient_id: str, family_member_id: Optional[str], schedule: str) -> dict:
    if schedule not in IMM_SCHEDULES:
        raise HTTPException(status_code=422, detail="Pick the IAP 2023 or UIP schedule.")
    child = await _child(clinic_id, patient_id, family_member_id)
    if not child.get("dob"):                          # nothing to schedule yet — a normal answer, not an error
        return {"child": {"name": child["name"], "dob": None}, "schedule": schedule, "rows": [],
                "counts": {s: 0 for s in ("given", "due", "overdue", "upcoming")},
                "note": "Record the child's date of birth to see the schedule."}
    dob, today = date.fromisoformat(str(child["dob"])[:10]), today_ist()
    q = scoped_query("imm_doses", clinic_id).eq("patient_id", patient_id)
    q = q.eq("family_member_id", family_member_id) if family_member_id else q.is_("family_member_id", "null")
    given = {(d["vaccine_code"], d["dose_no"]): d for d in rows(await sb(q.order("given_on")))}
    rows_out = []
    for code, n, label, due_days in IMM_SCHEDULES[schedule]:
        due = dob + timedelta(days=due_days)
        g = given.get((code, n))
        state = ("given" if g else "overdue" if (today - due).days > OVERDUE_AFTER_DAYS else "due" if due <= today
                 else "upcoming")
        rows_out.append({"vaccine_code": code, "dose_no": n, "label": label, "due_on": due.isoformat(),
                         "age_label": _age_label(due_days), "state": state,
                         "given": {k: g[k] for k in ("given_on", "brand", "batch_no", "expiry", "site", "given_by_name")}
                         if g else None})
    return {"child": {"name": child["name"], "dob": dob.isoformat()}, "schedule": schedule, "rows": rows_out,
            "counts": {s: sum(1 for r in rows_out if r["state"] == s) for s in ("given", "due", "overdue", "upcoming")},
            "note": "Schedule chosen by the clinic; catch-up doses are decided by the doctor."}


def _age_label(days: int) -> str:
    if days == 0:
        return "Birth"
    if days < 120:
        return f"{days // 7} weeks"
    if days < 730:
        return f"{round(days / 30.44)} months"
    return f"{round(days / 365.25)} years"


async def give_dose(clinic_id: str, body: dict, user) -> dict:
    sched = {(c, n) for c, n, _, _ in IMM_SCHEDULES[body["schedule_code"]]}
    if (body["vaccine_code"], body["dose_no"]) not in sched:
        raise HTTPException(status_code=422, detail="That dose is not in the chosen schedule.")
    await _child(clinic_id, body["patient_id"], body.get("family_member_id"))
    if body["expiry"] < body["given_on"]:
        raise HTTPException(status_code=422, detail="This batch had expired on that date.")
    if body["given_on"] > today_ist():
        raise HTTPException(status_code=422, detail="A dose cannot be recorded in the future.")
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("imm_doses").insert({
            **body, "clinic_id": clinic_id, "given_on": body["given_on"].isoformat(), "expiry": body["expiry"].isoformat(),
            "batch_no": body["batch_no"].strip().upper(), "given_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)
