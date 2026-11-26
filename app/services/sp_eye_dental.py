"""Specialty OS — Eye (optical job board, spectacle Rx) and Dental (lab orders, compliance dates) (migration 153).

Optical job (S7, EYE-3): the Rx is copied from the eye_exam_v1 form of a SIGNED visit (never retyped); the board moves
Advance paid → At lab (sent_to_lab / received / qc_passed) → Ready → Delivered, or Remake with a reason. "Ready" sends
the patient a WhatsApp message only after a staff member confirms it (named on the row). Money stays in OPD / Pharmacy
billing — the job records amounts for the counter, it never charges. Dental lab orders (S8) and compliance renewal
dates (DEN-10) are plain records with due-date lists. Zero-LLM.
"""

from datetime import timedelta
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.services import sp_forms
from app.services.sp import _db_error, _patient, actor_name, now_iso, rows
from app.utils.helpers import today_ist
from app.utils.validators import mask_phone

#: board column → statuses (UI spec §7.9: Advance paid → At lab → Ready → Delivered)
BOARD = {"advance_paid": ("draft", "advance_paid"), "at_lab": ("sent_to_lab", "received", "qc_passed", "remake"),
         "ready": ("ready",), "delivered": ("delivered",)}
MOVES = {"draft": {"advance_paid", "cancelled"}, "advance_paid": {"sent_to_lab", "cancelled"},
         "sent_to_lab": {"received", "remake"}, "received": {"qc_passed", "remake"}, "qc_passed": {"ready", "remake"},
         "remake": {"sent_to_lab", "cancelled"}, "ready": {"delivered", "remake"}, "delivered": set(), "cancelled": set()}


async def _signed_eye_form(clinic_id: str, form_id: str) -> dict:
    f = rows(await sb(scoped_query("opd_encounter_forms", clinic_id).eq("id", form_id).eq("form_code", "eye_exam_v1").limit(1)))
    if not f:
        raise HTTPException(status_code=404, detail="Eye examination not found.")
    enc = rows(await sb(scoped_query("opd_encounters", clinic_id, "status, signed_at").eq("id", f[0]["encounter_id"]).limit(1)))
    if not enc or enc[0]["status"] != "signed":
        raise HTTPException(status_code=409, detail="Sign the visit first: a spectacle order copies a signed prescription.")
    return {**f[0], "signed_at": enc[0]["signed_at"]}


def rx_snapshot(form: dict) -> dict:
    d = form.get("data") or {}
    snap = {side: {k: v for k, v in ((d.get(side) or {}).get("refraction") or {}).items() if k != "method"}
            for side in ("od", "os")}
    return {**snap, "pd_mm": d.get("pd_mm"), "lens_advice": d.get("lens_advice"), "form_id": form["id"],
            "signed_at": form.get("signed_at")}


async def create_job(clinic_id: str, body: dict, user) -> dict:
    form = await _signed_eye_form(clinic_id, body["form_id"])
    snap = rx_snapshot(form)
    if not (snap["od"] or snap["os"]):
        raise HTTPException(status_code=422, detail="This examination has no refraction to make spectacles from.")
    status = "advance_paid" if int(body.get("advance_paise") or 0) > 0 else "draft"
    for key, kinds in (("frame_item_id", ("frame",)), ("lens_item_id", ("lens", "contact_lens"))):
        if body.get(key):
            it = await _item(clinic_id, str(body[key]))
            if it["kind"] not in kinds or not it["is_active"]:
                raise HTTPException(status_code=422, detail=f"Pick an active {' or '.join(kinds).replace('_', ' ')} item.")
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_optical_jobs").insert({
            "clinic_id": clinic_id, "patient_id": form["patient_id"], "form_id": form["id"], "rx_snapshot": snap,
            "frame_desc": body.get("frame_desc"), "lens_desc": body["lens_desc"], "lab_name": body.get("lab_name"),
            "due_on": body["due_on"].isoformat() if body.get("due_on") else None, "total_paise": int(body["total_paise"]),
            "advance_paise": int(body.get("advance_paise") or 0), "status": status,
            "frame_item_id": str(body["frame_item_id"]) if body.get("frame_item_id") else None,
            "lens_item_id": str(body["lens_item_id"]) if body.get("lens_item_id") else None,
            "created_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)


async def board(clinic_id: str) -> dict:
    jobs = rows(await sb(scoped_query("sp_optical_jobs", clinic_id).neq("status", "cancelled")
                         .gte("created_at", (today_ist() - timedelta(days=120)).isoformat())
                         .order("created_at", desc=True).limit(500)))
    pats = {}
    if jobs:
        pats = {p["id"]: p for p in rows(await sb(scoped_query("patients", clinic_id, "id, name, mrn")
                                                  .in_("id", list({j["patient_id"] for j in jobs}))))}
    cols = {k: [] for k in BOARD}
    for j in jobs:
        col = next(k for k, sts in BOARD.items() if j["status"] in sts)
        cols[col].append({**j, "patient_name": (pats.get(j["patient_id"]) or {}).get("name"),
                          "mrn": (pats.get(j["patient_id"]) or {}).get("mrn")})
    return {"columns": cols, "labels": {"advance_paid": "Advance paid", "at_lab": "At lab", "ready": "Ready",
                                        "delivered": "Delivered"}}


async def move_job(clinic: dict, job_id: str, to: str, user, reason: Optional[str] = None, advance_paise: Optional[int] = None,
                   notify: bool = False) -> dict:
    """Status move by an explicit button. to='ready' with notify=True messages the patient (the staff member who
    pressed Confirm is recorded); nothing is ever sent without that confirmation."""
    cid = clinic["id"]
    j = rows(await sb(scoped_query("sp_optical_jobs", cid).eq("id", job_id).limit(1)))
    if not j:
        raise HTTPException(status_code=404, detail="Job not found.")
    j = j[0]
    if to not in MOVES.get(j["status"], set()):
        raise HTTPException(status_code=409, detail=f"A job that is {j['status'].replace('_', ' ')} cannot move to "
                                                    f"{to.replace('_', ' ')}.")
    patch: dict = {"status": to, "updated_at": now_iso(), "remake_reason": reason.strip() if to == "remake" and reason else None}
    if to == "remake" and not patch["remake_reason"]:
        raise HTTPException(status_code=422, detail="Say why it needs a remake.")
    if to == "advance_paid":
        if not advance_paise or advance_paise <= 0 or advance_paise > j["total_paise"]:
            raise HTTPException(status_code=422, detail="Enter the advance received (up to the total).")
        patch["advance_paise"] = advance_paise
    if to == "delivered":
        patch["delivered_at"] = now_iso()
    if to == "ready":
        patch["ready_confirmed_by_name"] = actor_name(user)
    await _stock_for_move(cid, j, to, user)              # first: out of stock refuses the move before anything changes
    try:
        out = rows(await sb(supabase.table("sp_optical_jobs").update(patch).eq("clinic_id", cid).eq("id", job_id)
                            .eq("status", j["status"])))
    except Exception as e:
        _db_error(e)
    if not out:
        if to == "sent_to_lab" and not j.get("stock_issued_at"):      # a parallel move won: give the stock back
            for i in (j.get("frame_item_id"), j.get("lens_item_id")):
                if i:
                    await stock_move(cid, i, 1, "return_job", user, job_id=j["id"])
        raise HTTPException(status_code=409, detail="Someone else moved this job — refresh the board.")
    out = out[0]
    if to == "ready" and notify:
        out["notified"] = await _notify_ready(clinic, out)
    return out


async def _notify_ready(clinic: dict, job: dict) -> dict:
    """The clinic's utility template `optical_ready_v1` (body: clinic name, balance) when configured, else a free-form
    text inside the 24-hour window. Never a clinical value in the message."""
    from app.services.whatsapp import whatsapp_service
    p = await _patient(clinic["id"], job["patient_id"])
    if not p.get("opted_in") or not p.get("phone"):
        return {"sent": False, "reason": "not_opted_in"}
    balance = f"₹{int(job['balance_paise'] or 0) / 100:,.0f}"
    tpl = ((clinic.get("opd_settings") or {}).get("templates") or {}).get("optical_ready")
    if tpl:
        ok = await whatsapp_service.send_template(clinic, p["phone"], tpl, components=[{"type": "body", "parameters": [
            {"type": "text", "text": str(clinic.get("name") or "")[:60]}, {"type": "text", "text": balance}]}],
            _source="sp_optical")
    else:
        ok = await whatsapp_service.send_text(clinic, p["phone"], f"{clinic.get('name', '')}: your spectacles are ready "
                                              f"to collect. Balance to pay: {balance}.", _source="sp_optical")
    if ok:
        await sb(supabase.table("sp_optical_jobs").update({"ready_notified_at": now_iso()})
                 .eq("clinic_id", clinic["id"]).eq("id", job["id"]))
    return {"sent": bool(ok), "to": mask_phone(p["phone"])}


def render_spectacle_rx(clinic: dict, patient: dict, form: dict, signer: dict) -> bytes:
    """A5 spectacle prescription from the signed eye_exam_v1 (distance / near rows per eye, PD, lens advice)."""
    from fpdf.enums import XPos, YPos

    from app.services.opd_pdf import OPDDocument
    d = form.get("data") or {}
    pdf = OPDDocument(doc_label=f"Spectacle Rx {str(form['id'])[:8]}", orientation="P", unit="mm", format="A5")
    pdf.set_margins(8, 8, 8)
    pdf.add_page()
    pdf.set_font("NotoSans", "B", 13)
    pdf.cell(0, 7, clinic.get("name") or "Eye Clinic", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("NotoSans", "B", 11)
    pdf.cell(0, 7, "Spectacle Prescription", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("NotoSans", "", 8)
    pdf.cell(0, 5, f"Patient: {patient.get('name', '')}   MRN: {patient.get('mrn') or '—'}   "
                   f"Date: {str(form.get('signed_at') or '')[:10]}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)
    pdf.set_font("NotoSans", "B", 8)
    for w, h in ((24, "Eye"), (24, "SPH"), (24, "CYL"), (20, "AXIS"), (0, "ADD")):
        pdf.cell(w, 6, h, border=1, align="C", **({"new_x": XPos.LMARGIN, "new_y": YPos.NEXT} if w == 0 else {}))
    pdf.set_font("NotoSans", "", 8)
    for row in sp_forms.spectacle_rows(d):
        for (w, _), v in zip(((24, 0), (24, 0), (24, 0), (20, 0), (0, 0)), row):
            pdf.cell(w, 6, v, border=1, align="C", **({"new_x": XPos.LMARGIN, "new_y": YPos.NEXT} if w == 0 else {}))
    pdf.ln(2)
    if d.get("pd_mm"):
        pdf.cell(0, 5, f"PD: {d['pd_mm']} mm", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if d.get("lens_advice"):
        pdf.multi_cell(0, 4, f"Lens advice: {d['lens_advice']}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(6)
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 5, signer.get("name") or "Ophthalmologist", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("NotoSans", "", 7)
    if signer.get("registration_number"):
        pdf.cell(0, 4, f"Reg. No: {signer['registration_number']}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


async def spectacle_rx_pdf(clinic: dict, form_id: str) -> bytes:
    cid = clinic["id"]
    form = await _signed_eye_form(cid, form_id)
    patient = await _patient(cid, form["patient_id"], "id, name, mrn")
    rx = rows(await sb(scoped_query("opd_prescriptions", cid, "signer_snapshot").eq("encounter_id", form["encounter_id"])
                       .order("created_at", desc=True).limit(1)))
    return render_spectacle_rx(clinic, patient, form, (rx[0].get("signer_snapshot") if rx else None) or {})


# ── dental lab orders (S8) and compliance dates (DEN-10) ─────────────────────

LAB_MOVES = {"sent": {"received", "cancelled"}, "received": {"fitted", "remake"}, "remake": {"received", "cancelled"},
             "fitted": set(), "cancelled": set()}


async def create_lab_order(clinic_id: str, body: dict, user) -> dict:
    teeth = sorted(set(body["teeth"]))
    if any(t not in sp_forms.FDI_ADULT + sp_forms.FDI_PRIMARY for t in teeth):
        raise HTTPException(status_code=422, detail="Teeth must be FDI numbers (11–48, 51–85).")
    await _patient(clinic_id, body["patient_id"])
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_dental_lab_orders").insert({
            "clinic_id": clinic_id, "patient_id": body["patient_id"], "plan_id": body.get("plan_id"),
            "lab_name": body["lab_name"].strip(), "work_type": body["work_type"], "teeth": ",".join(teeth),
            "shade": body.get("shade"), "due_on": body["due_on"].isoformat() if body.get("due_on") else None,
            "cost_paise": body.get("cost_paise"), "created_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)


async def list_lab_orders(clinic_id: str, status: Optional[str] = None) -> list:
    q = scoped_query("sp_dental_lab_orders", clinic_id)
    if status:
        q = q.eq("status", status)
    out = rows(await sb(q.order("due_on").limit(300)))
    today = today_ist().isoformat()
    return [{**o, "overdue": o["status"] in ("sent", "remake") and bool(o.get("due_on")) and str(o["due_on"]) < today}
            for o in out]


async def move_lab_order(clinic_id: str, order_id: str, to: str, reason: Optional[str]) -> dict:
    o = rows(await sb(scoped_query("sp_dental_lab_orders", clinic_id).eq("id", order_id).limit(1)))
    if not o:
        raise HTTPException(status_code=404, detail="Lab order not found.")
    if to not in LAB_MOVES[o[0]["status"]]:
        raise HTTPException(status_code=409, detail="That move is not allowed.")
    if to == "remake" and not (reason or "").strip():
        raise HTTPException(status_code=422, detail="Say why it needs a remake.")
    patch = {"status": to, "updated_at": now_iso(), "remake_reason": reason.strip() if to == "remake" else None,
             **({"received_at": now_iso()} if to == "received" else {}), **({"fitted_at": now_iso()} if to == "fitted" else {})}
    out = rows(await sb(supabase.table("sp_dental_lab_orders").update(patch).eq("clinic_id", clinic_id).eq("id", order_id)
                        .eq("status", o[0]["status"])))
    if not out:
        raise HTTPException(status_code=409, detail="Someone else moved this order — refresh.")
    return out[0]


async def upsert_compliance(clinic_id: str, body: dict, user, item_id: Optional[str] = None) -> dict:
    row = {**body, "valid_until": body["valid_until"].isoformat(), "updated_at": now_iso()}
    if item_id:
        out = rows(await sb(supabase.table("sp_compliance_items").update(row).eq("clinic_id", clinic_id).eq("id", item_id)))
        if not out:
            raise HTTPException(status_code=404, detail="Item not found.")
        return out[0]
    # unscoped: insert_scoped_by_payload
    return rows(await sb(supabase.table("sp_compliance_items").insert({**row, "clinic_id": clinic_id,
                                                                        "created_by_name": actor_name(user)})))[0]


async def list_compliance(clinic_id: str) -> list:
    today = today_ist()
    out = []
    for r in rows(await sb(scoped_query("sp_compliance_items", clinic_id).order("valid_until"))):
        from datetime import date
        days = (date.fromisoformat(str(r["valid_until"])[:10]) - today).days
        out.append({**r, "days_left": days, "state": "expired" if days < 0 else ("renew_soon" if days <= r["remind_days"]
                                                                                  else "valid")})
    return out


# ── SP-EYE-02 optical stock (frames, lenses, contact lenses) ─────────────────

async def _item(clinic_id: str, item_id: str) -> dict:
    it = rows(await sb(scoped_query("sp_optical_items", clinic_id).eq("id", item_id).limit(1)))
    if not it:
        raise HTTPException(status_code=404, detail="Optical item not found.")
    return it[0]


async def stock_move(clinic_id: str, item_id: str, delta: int, reason: str, user, job_id: Optional[str] = None,
                     note: Optional[str] = None) -> dict:
    try:
        return (await sb(supabase.rpc("sp_optical_move", {
            "p_clinic_id": clinic_id, "p_item_id": item_id, "p_delta": delta, "p_reason": reason, "p_job_id": job_id,
            "p_note": note, "p_by": actor_name(user)}))).data
    except Exception as e:
        msg = str(e)
        if "sp_optical_out_of_stock" in msg:
            raise HTTPException(status_code=409, detail="Not enough stock of that item.") from None
        if "sp_optical_item_missing" in msg:
            raise HTTPException(status_code=404, detail="Optical item not found.") from None
        if "sp_optical_stock_moves_note" in msg:
            raise HTTPException(status_code=422, detail="Say why (at least 3 characters).") from None
        raise


async def _stock_for_move(clinic_id: str, job: dict, to: str, user) -> None:
    """The frame and lens leave stock when the job goes to the lab the first time; a remake sent again uses another
    lens (recorded as issued); a job cancelled after issue returns both."""
    items = [i for i in (job.get("frame_item_id"), job.get("lens_item_id")) if i]
    if not items:
        return
    if to == "sent_to_lab" and not job.get("stock_issued_at"):
        for i in items:
            await stock_move(clinic_id, i, -1, "issue_job", user, job_id=job["id"])
        await sb(supabase.table("sp_optical_jobs").update({"stock_issued_at": now_iso()})
                 .eq("clinic_id", clinic_id).eq("id", job["id"]))
    elif to == "sent_to_lab" and job["status"] == "remake" and job.get("lens_item_id"):
        await stock_move(clinic_id, job["lens_item_id"], -1, "issue_job", user, job_id=job["id"], note="remake")
    elif to == "cancelled" and job.get("stock_issued_at"):
        for i in items:
            await stock_move(clinic_id, i, 1, "return_job", user, job_id=job["id"])


async def create_item(clinic_id: str, body: dict) -> dict:
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_optical_items").insert({**body, "clinic_id": clinic_id})))[0]
    except Exception as e:
        if "sp_optical_items_sku_key" in str(e) or "uq_sp_optical_items_barcode" in str(e):
            raise HTTPException(status_code=409, detail="That SKU or barcode is already used.") from None
        _db_error(e)


async def list_items(clinic_id: str, kind: Optional[str] = None, q: Optional[str] = None) -> list:
    qy = scoped_query("sp_optical_items", clinic_id)
    if kind:
        qy = qy.eq("kind", kind)
    items = rows(await sb(qy.order("name").limit(500)))
    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i["name"] + " " + (i.get("brand") or "") + " " + i["sku"] + " "
                                             + (i.get("barcode") or "") + " " + (i.get("power_desc") or "")).lower()]
    return [{**i, "low": i["is_active"] and i["qty_on_hand"] <= i["reorder_level"]} for i in items]


async def item_moves(clinic_id: str, item_id: str) -> list:
    await _item(clinic_id, item_id)
    return rows(await sb(scoped_query("sp_optical_stock_moves", clinic_id).eq("item_id", item_id)
                         .order("created_at", desc=True).limit(200)))
