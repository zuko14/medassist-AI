"""Specialty OS — shared blocks S1, S2, S4, S6, S9 (migrations 151–152; docs/specialty-os/GAP-ANALYSIS.md §2).

S2 forms on an OPD encounter (validated by sp_forms; frozen by the DB once the encounter is signed), S1 clinical media
vault (separate photo consent, private bucket, EXIF stripped, signed URLs, side-by-side compare, shared with the
patient only on an explicit staff action and only when the consent allows it, deleted on DPDP erasure), S6 imaging
links, S4 consent templates recorded through clinical_consents (119), S9 camps (verified only by a check-in or a signed
encounter — never appointments.status). Every read and write is clinic-scoped; zero-LLM: no photo, scan or form is ever
read by a model.
"""

import asyncio
import hashlib
import io
import logging
from datetime import date, datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException

from app.database import fetch_all, sb, scoped_query, supabase
from app.services import sp_forms
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)
MEDIA_BUCKET = "lab-reports"                       # private bucket (same conventions as 094 sitting photos)
MEDIA_URL_SECONDS = 300
MEDIA_MAX_BYTES = 15 * 1024 * 1024
MEDIA_NOTICE = "sp-media-1"
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def actor_name(user) -> str:
    n = str(getattr(user, "display_name", None) or getattr(user, "username", None) or "Staff").strip()[:100]
    return n if len(n) >= 2 else f"Staff {n}"                      # every *_by_name column needs ≥ 2 characters


def rows(res) -> list:
    data = getattr(res, "data", None)
    return data if isinstance(data, list) else []


def _db_error(e: Exception) -> None:
    msg = str(getattr(e, "message", None) or e)
    for key, (code, text) in {
        "sp_form_frozen": (409, "This visit is signed: its examination record can no longer change."),
        "kriya_cross_tenant_ref": (404, "Not found."),
        "sp_package_not_active": (409, "This package is not active."),
        "sp_package_sessions_exhausted": (409, "Every session of this package has been used."),
        "sp_package_expired": (409, "This package has expired."),
        "uq_sp_package_consumption_live": (409, "That session was just recorded by someone else — try again."),
        "ivf_witness_events_two_people": (422, "The witness must be a different person (a different login)."),
        "ivf_witness_events_window": (422, "The witness must confirm within 15 minutes of the step."),
        "ivf_embryos_consent": (422, "Freezing or discarding needs the signed consent."),
        "uq_ivf_embryos_cryo": (409, "That straw position is already in use."),
        "uq_obs_pregnancies_active": (409, "This patient already has an active pregnancy record."),
        "uq_imm_doses_dose": (409, "That dose is already recorded."),
        "uq_ivf_couples_active": (409, "This patient already has an active couple record."),
    }.items():
        if key in msg:
            raise HTTPException(status_code=code, detail=text) from None
    raise e


# ── gating helpers ───────────────────────────────────────────────────────────

async def forms_for_print(clinic: Optional[dict], encounter_id: Optional[str]) -> Optional[list]:
    """Form rows for the prescription PDF; None (and no query at all) for a clinic without Specialty OS."""
    from app.services.tenant import sp_access
    if not encounter_id or not sp_access(clinic):
        return None
    try:
        return rows(await sb(scoped_query("opd_encounter_forms", clinic["id"], "form_code, data, computed")
                             .eq("encounter_id", encounter_id).order("form_code")))
    except Exception as e:                       # noqa: BLE001 - the prescription prints without the block
        logger.error(f"SP_FORMS_PRINT_FAILED clinic={clinic.get('id')}: {type(e).__name__}")
        return None


async def _encounter(clinic_id: str, encounter_id: str) -> dict:
    enc = rows(await sb(scoped_query("opd_encounters", clinic_id, "id, patient_id, status, appointment_id, signed_at")
                        .eq("id", encounter_id).limit(1)))
    if not enc:
        raise HTTPException(status_code=404, detail="Visit not found.")
    return enc[0]


async def _patient(clinic_id: str, patient_id: str, cols: str = "id, name, phone, age_years, date_of_birth, opted_in") -> dict:
    p = rows(await sb(scoped_query("patients", clinic_id, cols).eq("id", patient_id).limit(1)))
    if not p:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return p[0]


def _age(p: dict) -> Optional[int]:
    if p.get("age_years") is not None:
        return int(p["age_years"])
    if p.get("date_of_birth"):
        dob = date.fromisoformat(str(p["date_of_birth"])[:10])
        t = date.today()
        return t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))
    return None


# ── S2 forms ─────────────────────────────────────────────────────────────────

def form_pack(form_code: str) -> str:
    if form_code not in sp_forms.FORMS:
        raise HTTPException(status_code=404, detail="Unknown form.")
    return sp_forms.FORMS[form_code][1]


async def list_forms(clinic_id: str, encounter_id: str) -> list:
    await _encounter(clinic_id, encounter_id)
    return rows(await sb(scoped_query("opd_encounter_forms", clinic_id).eq("encounter_id", encounter_id).order("form_code")))


async def save_form(clinic_id: str, encounter_id: str, form_code: str, data: dict, user,
                    staff_confirmed_primary: bool = False) -> dict:
    enc = await _encounter(clinic_id, encounter_id)
    if enc["status"] != "draft":
        raise HTTPException(status_code=409, detail="This visit is signed: its examination record can no longer change.")
    p = await _patient(clinic_id, enc["patient_id"])
    clean, computed = sp_forms.validate(form_code, data, {"age_years": _age(p),
                                                          "staff_confirmed_primary": staff_confirmed_primary})
    version = sp_forms.FORMS[form_code][0]
    row = {"data": clean, "computed": computed, "form_version": version, "updated_by_name": actor_name(user),
           "updated_at": now_iso()}
    try:
        cur = rows(await sb(scoped_query("opd_encounter_forms", clinic_id, "id").eq("encounter_id", encounter_id)
                            .eq("form_code", form_code).limit(1)))
        if cur:
            out = rows(await sb(supabase.table("opd_encounter_forms").update(row).eq("clinic_id", clinic_id)
                                .eq("id", cur[0]["id"])))
        else:
            # unscoped: insert_scoped_by_payload
            out = rows(await sb(supabase.table("opd_encounter_forms").insert({
                **row, "clinic_id": clinic_id, "encounter_id": encounter_id, "patient_id": enc["patient_id"],
                "form_code": form_code})))
    except HTTPException:
        raise
    except Exception as e:
        _db_error(e)
    return out[0]


async def delete_form(clinic_id: str, encounter_id: str, form_code: str) -> None:
    try:
        out = rows(await sb(supabase.table("opd_encounter_forms").delete().eq("clinic_id", clinic_id)
                            .eq("encounter_id", encounter_id).eq("form_code", form_code)))
    except Exception as e:
        _db_error(e)
    if not out:
        raise HTTPException(status_code=404, detail="Form not found.")


async def patient_forms(clinic_id: str, patient_id: str, form_code: str, limit: int = 20) -> list:
    """History of one form for a patient (newest first) — how a mouth / an eye changed across signed visits."""
    return rows(await sb(scoped_query("opd_encounter_forms", clinic_id, "id, encounter_id, form_code, data, computed, "
                                      "updated_at").eq("patient_id", patient_id).eq("form_code", form_code)
                         .order("created_at", desc=True).limit(limit)))


# ── S1 media vault ───────────────────────────────────────────────────────────

def strip_metadata(data: bytes, content_type: str) -> bytes:
    """Re-encode the pixels only: EXIF (GPS, device, time), XMP and text chunks never reach storage."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        out = io.BytesIO()
        fmt = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}[content_type]
        if fmt == "JPEG" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(out, format=fmt, **({"quality": 92} if fmt in ("JPEG", "WEBP") else {}))
        return out.getvalue()


async def grant_media_consent(clinic_id: str, body: dict, user) -> dict:
    await _patient(clinic_id, body["patient_id"])
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_media_consents").insert({
            "clinic_id": clinic_id, "patient_id": body["patient_id"], "family_member_id": body.get("family_member_id"),
            "allow_share": bool(body.get("allow_share")), "method": body.get("method") or "counter_signed",
            "signer_name": body["signer_name"].strip(), "signer_relationship": body.get("signer_relationship") or "self",
            "notice_version": MEDIA_NOTICE, "captured_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)


async def withdraw_media_consent(clinic_id: str, consent_id: str, reason: str) -> dict:
    out = rows(await sb(supabase.table("sp_media_consents").update({"withdrawn_at": now_iso(),
                                                                     "withdrawn_reason": reason.strip()})
                        .eq("clinic_id", clinic_id).eq("id", consent_id).is_("withdrawn_at", "null")))
    if not out:
        raise HTTPException(status_code=404, detail="Consent not found or already withdrawn.")
    return out[0]


async def _live_consent(clinic_id: str, patient_id: str) -> Optional[dict]:
    c = rows(await sb(scoped_query("sp_media_consents", clinic_id).eq("patient_id", patient_id)
                      .is_("withdrawn_at", "null").order("granted_at", desc=True).limit(1)))
    return c[0] if c else None


async def upload_media(clinic_id: str, patient_id: str, raw: bytes, meta: dict, user) -> dict:
    from app.services.dental_plans import sniff_image_type
    if not raw:
        raise HTTPException(status_code=422, detail="The photo is empty.")
    if len(raw) > MEDIA_MAX_BYTES:
        raise HTTPException(status_code=413, detail="The photo is larger than 15 MB.")
    ctype = sniff_image_type(raw)
    if ctype not in _EXT:
        raise HTTPException(status_code=415, detail="Please upload a photo (JPG, PNG or WebP).")
    await _patient(clinic_id, patient_id)
    consent = await _live_consent(clinic_id, patient_id)
    if not consent:
        raise HTTPException(status_code=409, detail={"code": "sp_media_no_consent",
                                                     "message": "Record the patient's photo consent first."})
    try:
        clean = await asyncio.to_thread(strip_metadata, raw, ctype)
    except Exception:                                # noqa: BLE001 - not a decodable image
        raise HTTPException(status_code=415, detail="This file could not be read as a photo.") from None
    path = f"{clinic_id}/clinical-media/{patient_id}/{uuid4().hex}.{_EXT[ctype]}"
    await asyncio.to_thread(supabase.storage.from_(MEDIA_BUCKET).upload, path, clean, {"content-type": ctype})
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_media").insert({
            "clinic_id": clinic_id, "patient_id": patient_id, "family_member_id": meta.get("family_member_id"),
            "consent_id": consent["id"], "encounter_id": meta.get("encounter_id"),
            "package_sale_id": meta.get("package_sale_id"), "context": meta.get("context") or "general",
            "body_site": meta.get("body_site"), "angle": meta.get("angle") or "front", "session_no": meta.get("session_no"),
            "storage_path": path, "content_type": ctype, "size_bytes": len(clean),
            "sha256": hashlib.sha256(clean).hexdigest(), "captured_by_name": actor_name(user)})))[0]
    except Exception as e:
        try:                                         # no orphan file: the row is the only way a photo is found
            await asyncio.to_thread(supabase.storage.from_(MEDIA_BUCKET).remove, [path])
        except Exception as cleanup:                 # noqa: BLE001
            logger.error(f"SP_MEDIA_ORPHAN path={path}: {type(cleanup).__name__}")
        _db_error(e)


async def _signed(paths: list) -> dict:
    if not paths:
        return {}
    try:
        signed = await asyncio.to_thread(supabase.storage.from_(MEDIA_BUCKET).create_signed_urls, paths, MEDIA_URL_SECONDS)
    except Exception as e:                           # noqa: BLE001 - list without thumbnails, never a broken page
        logger.error(f"SP_MEDIA_SIGN_FAILED: {type(e).__name__}")
        return {}
    return {i.get("path"): (i.get("signedURL") or i.get("signedUrl")) for i in signed or [] if i.get("path")}


async def list_media(clinic_id: str, patient_id: str, context: Optional[str] = None) -> list:
    q = scoped_query("sp_media", clinic_id, "id, context, body_site, angle, session_no, captured_at, captured_by_name, "
                     "shared_at, package_sale_id, encounter_id, storage_path").eq("patient_id", patient_id) \
        .is_("deleted_at", "null")
    if context:
        q = q.eq("context", context)
    items = rows(await sb(q.order("captured_at", desc=True).limit(200)))
    urls = await _signed([m["storage_path"] for m in items])
    return [{**{k: v for k, v in m.items() if k != "storage_path"}, "url": urls.get(m["storage_path"])} for m in items]


async def compare(clinic_id: str, patient_id: str, before_id: str, after_id: str) -> dict:
    """Side-by-side pair: the same patient, both live; the older one is 'before' whatever order they come in."""
    items = rows(await sb(scoped_query("sp_media", clinic_id, "id, captured_at, angle, body_site, session_no, storage_path")
                          .eq("patient_id", patient_id).in_("id", [before_id, after_id]).is_("deleted_at", "null")))
    if len(items) != 2:
        raise HTTPException(status_code=404, detail="Pick two photos of this patient.")
    items.sort(key=lambda m: str(m["captured_at"]))
    urls = await _signed([m["storage_path"] for m in items])
    out = [{**{k: v for k, v in m.items() if k != "storage_path"}, "url": urls.get(m["storage_path"])} for m in items]
    return {"before": out[0], "after": out[1],
            "days_apart": (datetime.fromisoformat(str(out[1]["captured_at"]).replace("Z", "+00:00"))
                           - datetime.fromisoformat(str(out[0]["captured_at"]).replace("Z", "+00:00"))).days}


async def share_media(clinic: dict, media_id: str, user) -> dict:
    """Explicit staff action: one photo to the patient's own WhatsApp — only with a live consent that allows sharing,
    only to a patient who opted in, and only inside the 24-hour window (a free-form document)."""
    from app.services.whatsapp import whatsapp_service
    cid = clinic["id"]
    m = rows(await sb(scoped_query("sp_media", cid).eq("id", media_id).is_("deleted_at", "null").limit(1)))
    if not m:
        raise HTTPException(status_code=404, detail="Photo not found.")
    m = m[0]
    consent = rows(await sb(scoped_query("sp_media_consents", cid).eq("id", m["consent_id"]).limit(1)))
    if not consent or consent[0].get("withdrawn_at") or not consent[0].get("allow_share"):
        raise HTTPException(status_code=409, detail="The patient's photo consent does not allow sharing on WhatsApp.")
    p = await _patient(cid, m["patient_id"])
    if not p.get("opted_in") or not p.get("phone"):
        raise HTTPException(status_code=409, detail="This patient has not opted in to WhatsApp.")
    url = (await _signed([m["storage_path"]])).get(m["storage_path"])
    if not url:
        raise HTTPException(status_code=502, detail="The photo could not be prepared. Try again.")
    ok = await whatsapp_service.send_document(clinic, p["phone"], url, f"photo.{_EXT[m['content_type']]}",
                                              caption="Your clinical photo", _source="sp_media")
    if not ok:
        raise HTTPException(status_code=409, detail="The patient's WhatsApp window is closed: share after they message "
                                                    "the clinic, or show the photo in person.")
    await sb(supabase.table("sp_media").update({"shared_at": now_iso(), "shared_by_name": actor_name(user)})
             .eq("clinic_id", cid).eq("id", media_id))
    return {"shared": True, "to": mask_phone(p["phone"])}


async def delete_media(clinic_id: str, media_id: str, reason: str) -> dict:
    out = rows(await sb(supabase.table("sp_media").update({"deleted_at": now_iso(), "deleted_reason": reason.strip()})
                        .eq("clinic_id", clinic_id).eq("id", media_id).is_("deleted_at", "null")))
    if not out:
        raise HTTPException(status_code=404, detail="Photo not found.")
    try:
        await asyncio.to_thread(supabase.storage.from_(MEDIA_BUCKET).remove, [out[0]["storage_path"]])
    except Exception as e:                           # noqa: BLE001 - the row is hidden; the file goes with erasure
        logger.error(f"SP_MEDIA_FILE_NOT_REMOVED clinic={clinic_id}: {type(e).__name__}")
    return {"deleted": True}


async def erase_patient(clinic_id: str, patient_id: str) -> dict:
    """DPDP erasure step: sp_purge_patient removes the rows; the files are removed from the private bucket here."""
    out = (await sb(supabase.rpc("sp_purge_patient", {"p_clinic_id": clinic_id, "p_patient_id": patient_id}))).data
    paths = list((out or {}).get("paths") or [])
    if paths:
        try:
            await asyncio.to_thread(supabase.storage.from_(MEDIA_BUCKET).remove, paths)
        except Exception as e:                       # noqa: BLE001
            logger.error(f"SP_ERASURE_FILES_NOT_REMOVED clinic={clinic_id}: {type(e).__name__}")
    return {k: v for k, v in (out or {}).items() if k != "paths"}


# ── S6 imaging links ─────────────────────────────────────────────────────────

async def add_imaging_link(clinic_id: str, encounter_id: str, body: dict, user) -> dict:
    enc = await _encounter(clinic_id, encounter_id)
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_imaging_links").insert({
            "clinic_id": clinic_id, "encounter_id": encounter_id, "patient_id": enc["patient_id"],
            "modality": body["modality"], "device_id": body.get("device_id"), "viewer_url": body["viewer_url"],
            "label": body.get("label"), "added_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)


async def list_imaging_links(clinic_id: str, encounter_id: Optional[str] = None, patient_id: Optional[str] = None) -> list:
    q = scoped_query("sp_imaging_links", clinic_id)
    q = q.eq("encounter_id", encounter_id) if encounter_id else q.eq("patient_id", patient_id)
    return rows(await sb(q.order("created_at", desc=True).limit(100)))


# ── S4 specialty consent templates (text only; recorded through clinical_consents) ─

CONSENT_TEMPLATES: dict[str, dict] = {
    "eye_cataract_iol": {"specialty": "eye", "consent_type": "surgical", "procedure": "Cataract surgery with IOL implant",
                         "title": {"en": "Consent for cataract surgery", "hi": "मोतियाबिंद सर्जरी के लिए सहमति",
                                   "te": "కంటిశుక్లం శస్త్రచికిత్సకు సమ్మతి"},
                         "body": {"en": "I agree to cataract surgery on the eye named above with the lens implant explained to me. "
                                        "The doctor has explained the expected benefit, the alternatives, and the risks including "
                                        "infection, raised eye pressure, swelling, a need for glasses, and rarely loss of vision.",
                                  "hi": "मैं ऊपर बताई गई आँख में समझाए गए लेंस इम्प्लांट के साथ मोतियाबिंद सर्जरी के लिए सहमत हूँ। "
                                        "डॉक्टर ने अपेक्षित लाभ, विकल्प और जोखिम — संक्रमण, आँख का दबाव बढ़ना, सूजन, चश्मे की ज़रूरत "
                                        "और कभी-कभी दृष्टि हानि — समझाए हैं।",
                                  "te": "పైన పేర్కొన్న కంటికి, నాకు వివరించిన లెన్స్ ఇంప్లాంట్‌తో కంటిశుక్లం శస్త్రచికిత్సకు నేను అంగీకరిస్తున్నాను. "
                                        "ఆశించిన ప్రయోజనం, ప్రత్యామ్నాయాలు, మరియు ఇన్ఫెక్షన్, కంటి ఒత్తిడి పెరగడం, వాపు, అద్దాల అవసరం, "
                                        "అరుదుగా చూపు కోల్పోవడం వంటి ప్రమాదాలను డాక్టర్ వివరించారు."}},
    "dental_extraction": {"specialty": "dental", "consent_type": "high_risk_procedure", "procedure": "Tooth extraction",
                          "title": {"en": "Consent for tooth extraction", "hi": "दाँत निकालने के लिए सहमति",
                                    "te": "పన్ను తీయడానికి సమ్మతి"},
                          "body": {"en": "I agree to the extraction of the tooth / teeth named above under local anaesthesia. The "
                                         "dentist has explained the alternatives and the risks including pain, swelling, bleeding, "
                                         "infection, dry socket and numbness.",
                                   "hi": "मैं ऊपर बताए गए दाँत/दाँतों को लोकल एनेस्थीसिया में निकालने के लिए सहमत हूँ। दंत चिकित्सक ने "
                                         "विकल्प और जोखिम — दर्द, सूजन, खून आना, संक्रमण, ड्राई सॉकेट और सुन्नपन — समझाए हैं।",
                                   "te": "పైన పేర్కొన్న పన్ను/పళ్లను లోకల్ అనస్థీషియాతో తీయడానికి నేను అంగీకరిస్తున్నాను. ప్రత్యామ్నాయాలు "
                                         "మరియు నొప్పి, వాపు, రక్తస్రావం, ఇన్ఫెక్షన్, డ్రై సాకెట్, తిమ్మిరి వంటి ప్రమాదాలను దంత వైద్యులు వివరించారు."}},
    "dental_implant": {"specialty": "dental", "consent_type": "surgical", "procedure": "Dental implant",
                       "title": {"en": "Consent for dental implant", "hi": "डेंटल इम्प्लांट के लिए सहमति",
                                 "te": "డెంటల్ ఇంప్లాంట్‌కు సమ్మతి"},
                       "body": {"en": "I agree to placement of a dental implant as explained, in stages over several visits. The "
                                      "dentist has explained the alternatives, the cost, and the risks including infection, nerve "
                                      "injury, sinus involvement and implant failure.",
                                "hi": "मैं समझाए गए अनुसार कई चरणों में डेंटल इम्प्लांट लगवाने के लिए सहमत हूँ। दंत चिकित्सक ने विकल्प, "
                                      "खर्च और जोखिम — संक्रमण, नस में चोट, साइनस और इम्प्लांट विफलता — समझाए हैं।",
                                "te": "వివరించిన విధంగా పలు దశల్లో డెంటల్ ఇంప్లాంట్ అమర్చడానికి నేను అంగీకరిస్తున్నాను. ప్రత్యామ్నాయాలు, ఖర్చు "
                                      "మరియు ఇన్ఫెక్షన్, నరాల గాయం, సైనస్ సమస్య, ఇంప్లాంట్ విఫలం వంటి ప్రమాదాలను వివరించారు."}},
    "derma_laser": {"specialty": "derma", "consent_type": "high_risk_procedure", "procedure": "Laser procedure",
                    "title": {"en": "Consent for laser procedure", "hi": "लेज़र प्रक्रिया के लिए सहमति",
                              "te": "లేజర్ ప్రక్రియకు సమ్మతి"},
                    "body": {"en": "I agree to the laser procedure explained to me over the number of sessions advised. I have "
                                   "been told about the expected result, the need for sun protection, and the risks including "
                                   "redness, blistering, pigment change and scarring.",
                             "hi": "मैं सलाह दिए गए सत्रों में समझाई गई लेज़र प्रक्रिया के लिए सहमत हूँ। मुझे अपेक्षित परिणाम, धूप से "
                                   "बचाव की ज़रूरत और जोखिम — लालिमा, छाले, रंग में बदलाव और निशान — बताए गए हैं।",
                             "te": "సూచించిన సెషన్లలో నాకు వివరించిన లేజర్ ప్రక్రియకు నేను అంగీకరిస్తున్నాను. ఆశించిన ఫలితం, ఎండ నుండి "
                                   "రక్షణ అవసరం, ఎరుపు, బొబ్బలు, రంగు మార్పు, మచ్చలు వంటి ప్రమాదాల గురించి చెప్పారు."}},
    "derma_peel": {"specialty": "derma", "consent_type": "high_risk_procedure", "procedure": "Chemical peel",
                   "title": {"en": "Consent for chemical peel", "hi": "केमिकल पील के लिए सहमति", "te": "కెమికల్ పీల్‌కు సమ్మతి"},
                   "body": {"en": "I agree to a chemical peel as explained. I have been told about the aftercare, the need for "
                                  "sun protection, and the risks including burning, peeling, pigment change and infection.",
                            "hi": "मैं समझाए गए अनुसार केमिकल पील के लिए सहमत हूँ। मुझे देखभाल, धूप से बचाव और जोखिम — जलन, त्वचा "
                                  "उतरना, रंग में बदलाव और संक्रमण — बताए गए हैं।",
                            "te": "వివరించిన విధంగా కెమికల్ పీల్‌కు నేను అంగీకరిస్తున్నాను. తర్వాతి జాగ్రత్తలు, ఎండ రక్షణ, మంట, చర్మం "
                                  "ఊడటం, రంగు మార్పు, ఇన్ఫెక్షన్ వంటి ప్రమాదాల గురించి చెప్పారు."}},
    "ivf_embryo_freeze": {"specialty": "ivf", "consent_type": "high_risk_procedure", "procedure": "Embryo cryopreservation",
                          "title": {"en": "Consent for embryo freezing", "hi": "भ्रूण फ्रीज़ करने के लिए सहमति",
                                    "te": "పిండం ఫ్రీజింగ్‌కు సమ్మతి"},
                          "body": {"en": "We, the couple named above, agree to freeze our surplus embryos for future use for the "
                                         "storage period and charges explained to us, and understand that storage needs renewal and "
                                         "that some embryos may not survive thawing.",
                                   "hi": "हम, ऊपर नामित दंपति, अपने अतिरिक्त भ्रूणों को भविष्य के उपयोग के लिए समझाई गई अवधि और शुल्क पर "
                                         "फ्रीज़ करने के लिए सहमत हैं, और समझते हैं कि भंडारण का नवीनीकरण ज़रूरी है और कुछ भ्रूण पिघलने पर "
                                         "जीवित नहीं रह सकते।",
                                   "te": "పైన పేర్కొన్న దంపతులమైన మేము, మిగిలిన పిండాలను భవిష్యత్ వినియోగం కోసం వివరించిన కాలానికి మరియు ఛార్జీలకు "
                                         "ఫ్రీజ్ చేయడానికి అంగీకరిస్తున్నాము; నిల్వకు పునరుద్ధరణ అవసరమని, కొన్ని పిండాలు కరిగించినప్పుడు "
                                         "బతకకపోవచ్చని అర్థం చేసుకున్నాము."}},
    "ivf_embryo_discard": {"specialty": "ivf", "consent_type": "high_risk_procedure", "procedure": "Embryo discard",
                           "title": {"en": "Consent to discard embryos", "hi": "भ्रूण नष्ट करने के लिए सहमति",
                                     "te": "పిండాలను విస్మరించడానికి సమ్మతి"},
                           "body": {"en": "We, the couple named above, ask the clinic to discard the embryos listed, and understand "
                                          "that this cannot be undone.",
                                    "hi": "हम, ऊपर नामित दंपति, क्लिनिक से सूचीबद्ध भ्रूणों को नष्ट करने का अनुरोध करते हैं और समझते हैं कि "
                                          "इसे वापस नहीं किया जा सकता।",
                                    "te": "పైన పేర్కొన్న దంపతులమైన మేము, జాబితాలోని పిండాలను విస్మరించమని క్లినిక్‌ను కోరుతున్నాము; ఇది "
                                          "తిరిగి మార్చలేనిదని అర్థం చేసుకున్నాము."}},
}


async def record_template_consent(clinic_id: str, body: dict, user) -> dict:
    from app.services import clinical_documents
    t = CONSENT_TEMPLATES.get(body["template"])
    if not t:
        raise HTTPException(status_code=404, detail="Unknown consent template.")
    lang = body.get("language") or "en"
    return await clinical_documents.create_consent(clinic_id, {
        "patient_id": body["patient_id"], "family_member_id": body.get("family_member_id"),
        "consent_type": t["consent_type"], "language": lang, "title": t["title"][lang],
        "procedure_name": (body.get("procedure_detail") or t["procedure"])[:200], "body_text": t["body"][lang],
        "risks_explained": body.get("risks_explained"), "doctor_id": body.get("doctor_id"),
        "witness_name": body.get("witness_name"), "signer_relationship": body.get("signer_relationship") or "self",
        "signer_name": body["signer_name"], "idempotency_key": body.get("idempotency_key")}, user)


# ── S9 camps ─────────────────────────────────────────────────────────────────

async def create_camp(clinic_id: str, body: dict, user) -> dict:
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_camps").insert({**body, "clinic_id": clinic_id,
                                                                "camp_date": str(body["camp_date"]),
                                                                "created_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)


async def list_camps(clinic_id: str) -> list:
    return rows(await sb(scoped_query("sp_camps", clinic_id).order("camp_date", desc=True).limit(200)))


async def _camp(clinic_id: str, camp_id: str) -> dict:
    c = rows(await sb(scoped_query("sp_camps", clinic_id).eq("id", camp_id).limit(1)))
    if not c:
        raise HTTPException(status_code=404, detail="Camp not found.")
    return c[0]


async def register_at_camp(clinic_id: str, camp_id: str, body: dict, user) -> dict:
    camp = await _camp(clinic_id, camp_id)
    if camp["status"] == "closed":
        raise HTTPException(status_code=409, detail="This camp is closed.")
    try:
        # unscoped: insert_scoped_by_payload
        return rows(await sb(supabase.table("sp_camp_registrations").insert({
            "clinic_id": clinic_id, "camp_id": camp_id, "name": body["name"].strip(), "phone": body.get("phone"),
            "age_years": body.get("age_years"), "patient_id": body.get("patient_id"),
            "created_by_name": actor_name(user)})))[0]
    except Exception as e:
        _db_error(e)


async def screen_at_camp(clinic_id: str, reg_id: str, body: dict) -> dict:
    clean, computed = sp_forms.validate("camp_screening_v1", body.get("screening") or {})
    outcome = body["outcome"]
    patch = {"screening": {**clean, **({"computed": computed} if computed else {})}, "outcome": outcome,
             "referral_reason": body.get("referral_reason") if outcome == "referred" else None}
    if outcome == "referred" and not patch["referral_reason"]:
        raise HTTPException(status_code=422, detail="Say why the person is referred.")
    if body.get("appointment_id"):
        patch["appointment_id"] = body["appointment_id"]
    if body.get("patient_id"):
        patch["patient_id"] = body["patient_id"]
    try:
        out = rows(await sb(supabase.table("sp_camp_registrations").update(patch).eq("clinic_id", clinic_id).eq("id", reg_id)))
    except Exception as e:
        _db_error(e)
    if not out:
        raise HTTPException(status_code=404, detail="Registration not found.")
    return out[0]


async def verify_camp(clinic_id: str, camp_id: str) -> int:
    """A referred person's booked visit counts only with a check-in or a signed encounter for that appointment."""
    n = 0
    regs = await fetch_all(lambda: scoped_query("sp_camp_registrations", clinic_id, "id, appointment_id")
                           .eq("camp_id", camp_id).eq("outcome", "referred").not_.is_("appointment_id", "null")
                           .is_("verified_at", "null"), label="sp:camp:verify")
    for r in regs:
        a = rows(await sb(scoped_query("appointments", clinic_id, "id, checked_in_at").eq("id", r["appointment_id"]).limit(1)))
        ev = None
        if a and a[0].get("checked_in_at"):
            ev = "check_in"
        elif rows(await sb(scoped_query("opd_encounters", clinic_id, "id").eq("appointment_id", r["appointment_id"])
                           .eq("status", "signed").limit(1))):
            ev = "signed_encounter"
        if ev:
            await sb(supabase.table("sp_camp_registrations").update({"verified_at": now_iso(), "verified_evidence": ev})
                     .eq("clinic_id", clinic_id).eq("id", r["id"]).is_("verified_at", "null"))
            n += 1
    return n


async def camp_report(clinic_id: str, camp_id: str) -> dict:
    camp = await _camp(clinic_id, camp_id)
    await verify_camp(clinic_id, camp_id)
    regs = await fetch_all(lambda: scoped_query("sp_camp_registrations", clinic_id).eq("camp_id", camp_id)
                           .order("created_at"), label="sp:camp:regs")
    return {"camp": camp, "registered": len(regs),
            "screened": sum(1 for r in regs if r["outcome"] != "registered"),
            "referred": sum(1 for r in regs if r["outcome"] == "referred"),
            "booked": sum(1 for r in regs if r.get("appointment_id")),
            "verified": sum(1 for r in regs if r.get("verified_at")),
            "registrations": [{**r, "phone": mask_phone(r["phone"]) if r.get("phone") else None} for r in regs],
            "note": "Verified = a check-in or a signed visit for the booked appointment (never a 'completed' status)."}


# ── Specialty → Continuous Care one-tap enrollment (SP-EYE-06, SP-DEN-04, SP-DER-05, SP-IVF-06, SP-WC-05) ─

SPECIALTY_CARE_PACKS: dict[str, tuple[str, ...]] = {
    "eye": ("eye_post_cataract", "eye_anti_vegf", "eye_glaucoma", "eye_dr_screen"),
    "dental": ("dental_recall", "dental_ortho", "dental_implant_review"),
    "derma": ("derma_procedure_series", "derma_isotretinoin", "derma_methotrexate"),
    "ivf": ("ivf_cycle", "ivf_cryo_renewal"),
    "anc": ("anc", "postnatal"),
    "imm": ("child_immunisation", "child_growth"),
}


async def care_packs(clinic: dict, specialty: str, patient_id: Optional[str], family_member_id: Optional[str]) -> dict:
    """The CC packs this specialty can enroll into, which are signed (active) at this clinic, and the pack facts Kriya
    already holds for the patient (EDD, trigger / transfer dates, vaccines due, last IOP / fundus, latest z-score) so
    the doctor does not retype them. Enrollment itself goes through /admin/cc/enrollments (doctor approval + consent).
    No CC query at all when Continuous Care is not active."""
    from app.services import cc_protocols
    from app.services.tenant import care_access
    if specialty not in SPECIALTY_CARE_PACKS:
        raise HTTPException(status_code=404, detail="Unknown specialty.")
    if care_access(clinic) != "ACTIVE":
        return {"care": False, "packs": []}
    cid = clinic["id"]
    codes = SPECIALTY_CARE_PACKS[specialty]
    active = {a["protocol_code"] for a in rows(await sb(scoped_query("cc_protocol_activations", cid, "protocol_code")
                                                        .eq("status", "active").in_("protocol_code", list(codes))))}
    facts = await _care_facts(cid, specialty, patient_id, family_member_id) if patient_id else {}
    return {"care": True, "packs": [{"code": c, "title": getattr(cc_protocols.get(c), "TITLE", c), "active": c in active,
                                     "facts": facts.get(c, {})} for c in codes]}


async def _care_facts(cid: str, specialty: str, patient_id: str, family_member_id: Optional[str]) -> dict:
    out: dict = {}
    if specialty == "anc":
        p = rows(await sb(scoped_query("obs_pregnancies", cid, "id, edd, status").eq("patient_id", patient_id)
                          .order("created_at", desc=True).limit(1)))
        if p:
            out["anc"] = {"edd": str(p[0]["edd"])}
            if p[0]["status"] == "delivered":
                d = rows(await sb(scoped_query("obs_deliveries", cid, "delivered_at").eq("pregnancy_id", p[0]["id"]).limit(1)))
                if d:
                    out["postnatal"] = {"delivered_on": str(d[0]["delivered_at"])[:10]}
    elif specialty == "ivf":
        c = rows(await sb(scoped_query("ivf_couples", cid, "id").eq("female_patient_id", patient_id).eq("status", "active").limit(1)))
        if c:
            cy = rows(await sb(scoped_query("ivf_cycles", cid, "trigger_at, trigger_ack_at, transfer_at").eq("couple_id", c[0]["id"])
                               .order("start_date", desc=True).limit(1)))
            if cy:
                f = {}
                if cy[0].get("trigger_at"):
                    f.update(trigger_on=str(cy[0]["trigger_at"])[:10], trigger_text=str(cy[0]["trigger_at"])[:16].replace("T", " "),
                             trigger_acknowledged=bool(cy[0].get("trigger_ack_at")))
                if cy[0].get("transfer_at"):
                    f["transfer_on"] = str(cy[0]["transfer_at"])[:10]
                out["ivf_cycle"] = f
    elif specialty == "imm":
        from app.services import sp_ivf_wc
        try:
            s = await sp_ivf_wc.immunisation(cid, patient_id, family_member_id, "iap_2023")
            out["child_immunisation"] = {"due_vaccines": [[r["label"], r["due_on"]] for r in s["rows"]
                                                          if r["state"] in ("due", "overdue")][:10]}
        except HTTPException:
            pass
        q = scoped_query("child_growth_measurements", cid, "z").eq("patient_id", patient_id)
        q = q.eq("family_member_id", family_member_id) if family_member_id else q.is_("family_member_id", "null")
        g = rows(await sb(q.order("measured_on", desc=True).limit(1)))
        if g and (g[0].get("z") or {}).get("wfa") is not None:
            out["child_growth"] = {"weight_for_age_z": g[0]["z"]["wfa"]}
    elif specialty == "eye":
        forms = rows(await sb(scoped_query("opd_encounter_forms", cid, "data, created_at").eq("patient_id", patient_id)
                              .eq("form_code", "eye_exam_v1").order("created_at", desc=True).limit(10)))
        iop = next((f for f in forms if any(((f["data"].get(s) or {}).get("iop") or {}).get("mmhg") is not None for s in ("od", "os"))), None)
        fun = next((f for f in forms if any((f["data"].get(s) or {}).get("fundus") for s in ("od", "os"))), None)
        if iop:
            out["eye_glaucoma"] = {"last_iop_on": str(iop["created_at"])[:10]}
        if fun:
            out["eye_dr_screen"] = {"last_fundus_on": str(fun["created_at"])[:10]}
    return out
