"""Pharmacy OS for standalone pharmacies — the `pharmacy` plan (lane RXR, migration 150; docs/pharmacy-os/RETAIL-PLAN.md).

The patient front door of a medical store: a WhatsApp menu with NO doctor booking anywhere (RXR-ADR-5, VERIFICATION
D14) — Refill my medicines, Send prescription (photo), Is it in stock?, Timings & location, Talk to pharmacist,
Emergency. Every request lands in `pharmacy_order_requests` for the pharmacist; nothing is sold, reserved or promised by the
bot. The stock answer is the deterministic resolver (RX-15) and says only available / not available / ask at the
counter; Schedule H / H1 / X items always add "bring your prescription" and are never "available to order" (X13).
No OCR / LLM ever reads a prescription photo (ADR-RX-06): the pharmacist opens it. Zero-LLM, clinic-scoped.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.database import sb, scoped_query, supabase
from app.utils.validators import mask_phone

logger = logging.getLogger(__name__)
PHARMACY_PLANS = frozenset({"pharmacy"})
OPEN = ["new", "reviewing", "ready"]
STATE_PHOTO, STATE_STOCK = "rx_upload_rx", "rx_stock_query"
PRESCRIPTION_SCHEDULES = ("H", "H1", "X", "narcotic", "psychotropic")
MENU_IDS = ("rx_menu_refill", "rx_menu_send", "rx_menu_stock", "rx_menu_hours", "rx_menu_staff", "menu_emergency")

TEXT = {
    "menu": {"en": "Welcome to {name}. How can we help you today?",
             "hi": "{name} में आपका स्वागत है। आज हम आपकी क्या मदद करें?",
             "te": "{name} కి స్వాగతం. ఈరోజు మీకు ఎలా సహాయం చేయగలం?"},
    "ask_photo": {"en": "Please send a clear photo of your prescription. Our pharmacist will check it and tell you when "
                        "it is ready to collect.",
                  "hi": "कृपया अपने पर्चे की साफ़ फ़ोटो भेजें। हमारे फ़ार्मासिस्ट जाँचकर बताएँगे कि दवा कब तैयार होगी।",
                  "te": "దయచేసి మీ ప్రిస్క్రిప్షన్ స్పష్టమైన ఫోటో పంపండి. మా ఫార్మసిస్ట్ చూసి ఎప్పుడు సిద్ధంగా ఉంటుందో చెబుతారు."},
    "photo_ok": {"en": "Thank you. Your prescription is with our pharmacist. We will message you when your medicines are "
                       "ready to collect. Please bring the original prescription.",
                 "hi": "धन्यवाद। आपका पर्चा हमारे फ़ार्मासिस्ट के पास है। दवा तैयार होने पर हम संदेश भेजेंगे। कृपया मूल पर्चा साथ लाएँ।",
                 "te": "ధన్యవాదాలు. మీ ప్రిస్క్రిప్షన్ మా ఫార్మసిస్ట్ వద్ద ఉంది. మందులు సిద్ధమైనప్పుడు సందేశం పంపుతాం. అసలు ప్రిస్క్రిప్షన్ తీసుకురండి."},
    "already": {"en": "We already have your request; our pharmacist will message you soon.",
                "hi": "आपका अनुरोध हमारे पास पहले से है; फ़ार्मासिस्ट जल्द संदेश भेजेंगे।",
                "te": "మీ అభ్యర్థన ఇప్పటికే మా వద్ద ఉంది; ఫార్మసిస్ట్ త్వరలో సందేశం పంపుతారు."},
    "ask_stock": {"en": "Type the medicine name as written on the strip or prescription (for example: Dolo 650).",
                  "hi": "दवा का नाम वैसे ही लिखें जैसा पत्ते या पर्चे पर है (जैसे: Dolo 650)।",
                  "te": "స్ట్రిప్ లేదా ప్రిస్క్రిప్షన్‌పై ఉన్నట్లే మందు పేరు టైప్ చేయండి (ఉదా: Dolo 650)."},
    "in_stock": {"en": "{item}: available at the counter.", "hi": "{item}: काउंटर पर उपलब्ध है।",
                 "te": "{item}: కౌంటర్ వద్ద అందుబాటులో ఉంది."},
    "out_of_stock": {"en": "{item}: not available right now. Our pharmacist can tell you when it comes in.",
                     "hi": "{item}: अभी उपलब्ध नहीं है। फ़ार्मासिस्ट बता सकते हैं कि कब आएगी।",
                     "te": "{item}: ప్రస్తుతం అందుబాటులో లేదు. ఎప్పుడు వస్తుందో ఫార్మసిస్ట్ చెబుతారు."},
    "ask_counter": {"en": "We could not find that name. Please ask at the counter or tap Talk to pharmacist.",
                    "hi": "यह नाम नहीं मिला। कृपया काउंटर पर पूछें या 'फ़ार्मासिस्ट से बात' दबाएँ।",
                    "te": "ఆ పేరు దొరకలేదు. దయచేసి కౌంటర్ వద్ద అడగండి లేదా 'ఫార్మసిస్ట్‌తో మాట్లాడు' నొక్కండి."},
    "needs_rx": {"en": "This medicine is sold only against a valid prescription — please bring it.",
                 "hi": "यह दवा केवल वैध पर्चे पर मिलती है — कृपया पर्चा साथ लाएँ।",
                 "te": "ఈ మందు సరైన ప్రిస్క్రిప్షన్‌తో మాత్రమే ఇస్తాం — దయచేసి తీసుకురండి."},
    "refill_ok": {"en": "Your refill request is with our pharmacist. We will message you when it is ready to collect.",
                  "hi": "आपका रीफ़िल अनुरोध फ़ार्मासिस्ट के पास है। तैयार होने पर संदेश भेजेंगे।",
                  "te": "మీ రీఫిల్ అభ్యర్థన ఫార్మసిస్ట్ వద్ద ఉంది. సిద్ధమైనప్పుడు సందేశం పంపుతాం."},
    "staff": {"en": "Our pharmacist will call you back during opening hours.",
              "hi": "हमारे फ़ार्मासिस्ट खुलने के समय आपको कॉल करेंगे।",
              "te": "మా ఫార్మసిస్ట్ తెరిచిన సమయంలో మీకు కాల్ చేస్తారు."},
    "ready": {"en": "Your medicines are ready to collect at {name}. Please bring the original prescription.",
              "hi": "आपकी दवाइयाँ {name} पर लेने के लिए तैयार हैं। कृपया मूल पर्चा साथ लाएँ।",
              "te": "మీ మందులు {name} వద్ద తీసుకోవడానికి సిద్ధంగా ఉన్నాయి. అసలు ప్రిస్క్రిప్షన్ తీసుకురండి."},
    "hours": {"en": "📍 {name}\n{address}\n🕘 {hours}\n📞 {phone}", "hi": "📍 {name}\n{address}\n🕘 {hours}\n📞 {phone}",
              "te": "📍 {name}\n{address}\n🕘 {hours}\n📞 {phone}"},
}
ROWS = {
    "rx_menu_refill": {"en": "Refill my medicines", "hi": "दवा रीफ़िल", "te": "మందుల రీఫిల్"},
    "rx_menu_send": {"en": "Send prescription", "hi": "पर्चा भेजें", "te": "ప్రిస్క్రిప్షన్ పంపండి"},
    "rx_menu_stock": {"en": "Is it in stock?", "hi": "स्टॉक में है?", "te": "స్టాక్‌లో ఉందా?"},
    "rx_menu_hours": {"en": "Timings & location", "hi": "समय और पता", "te": "సమయం & చిరునామా"},
    "rx_menu_staff": {"en": "Talk to pharmacist", "hi": "फ़ार्मासिस्ट से बात", "te": "ఫార్మసిస్ట్‌తో మాట్లాడు"},
    "menu_emergency": {"en": "Emergency", "hi": "आपातकाल", "te": "అత్యవసరం"},
}


def is_pharmacy_plan(clinic: Optional[dict]) -> bool:
    return bool(clinic) and clinic.get("plan") in PHARMACY_PLANS


def t(key: str, lang: str, **kw) -> str:
    table = TEXT[key]
    return table.get(lang if lang in table else "en").format(**kw)


def menu_rows(lang: str) -> list:
    return [{"id": i, "title": ROWS[i].get(lang, ROWS[i]["en"])[:24], "description": ""} for i in MENU_IDS]


def _rows(res) -> list:
    return list(getattr(res, "data", None) or [])


# ── order requests (the pharmacist's queue) ──────────────────────────────────

async def create_request(clinic: dict, phone: str, kind: str, source: str, media_ref: Optional[str] = None,
                         note: Optional[str] = None) -> tuple[Optional[dict], bool]:
    """(row, created). One open request per (phone, kind): a second photo / tap returns the open one."""
    cid = clinic["id"]
    pat = _rows(await sb(scoped_query("patients", cid, "id").eq("phone", phone).limit(1)))
    row = {"clinic_id": cid, "patient_id": pat[0]["id"] if pat else None, "phone": phone,
           "phone_masked": mask_phone(phone)[:20], "kind": kind, "source": source, "media_ref": media_ref,
           "note": (note or "").strip()[:500] or None}
    try:
        # unscoped: insert_scoped_by_payload
        return _rows(await sb(supabase.table("pharmacy_order_requests").insert(row)))[0], True
    except Exception as e:                                 # noqa: BLE001
        if "uq_pharmacy_order_requests_open" not in str(e):
            raise
    cur = _rows(await sb(scoped_query("pharmacy_order_requests", cid).eq("phone", phone).eq("kind", kind).in_("status", OPEN)
                         .limit(1)))
    return (cur[0] if cur else None), False


async def list_requests(clinic_id: str, status: Optional[str], limit: int = 100) -> list:
    q = scoped_query("pharmacy_order_requests", clinic_id, "id, kind, source, status, phone_masked, patient_id, media_ref, note, "
                                                    "rejected_reason, sale_id, pickup_by, handled_by_name, created_at, "
                                                    "updated_at")
    q = q.eq("status", status) if status else q.in_("status", OPEN)
    rows = _rows(await sb(q.order("created_at", desc=True).limit(limit)))
    for r in rows:
        r["has_photo"] = bool(r.pop("media_ref", None))
    return rows


TRANSITIONS = {"reviewing": ("new",), "ready": ("new", "reviewing"), "collected": ("ready",),
               "rejected": ("new", "reviewing", "ready"), "cancelled": ("new", "reviewing", "ready")}


async def move(clinic: dict, request_id: str, to: str, user, *, reason: Optional[str] = None,
               sale_id: Optional[str] = None, pickup_by: Optional[str] = None) -> dict:
    """Pharmacist transitions (CAS on the current status). 'ready' messages the customer in-window only."""
    cid = clinic["id"]
    cur = _rows(await sb(scoped_query("pharmacy_order_requests", cid).eq("id", request_id).limit(1)))
    if not cur:
        raise HTTPException(status_code=404, detail="Request not found.")
    r = cur[0]
    if r["status"] not in TRANSITIONS.get(to, ()):
        raise HTTPException(status_code=409, detail=f"Cannot move a {r['status']} request to {to}.")
    if to == "rejected" and len((reason or "").strip()) < 3:
        raise HTTPException(status_code=422, detail="Say why the request was rejected.")
    if to == "collected" and not sale_id:
        raise HTTPException(status_code=422, detail="A collected request needs the sale it was billed on.")
    patch = {"status": to, "updated_at": datetime.now(timezone.utc).isoformat(),
             "handled_by_name": (getattr(user, "username", None) or "pharmacist")[:100]}
    if to == "rejected":
        patch["rejected_reason"] = reason.strip()[:300]
    if sale_id:
        patch["sale_id"] = sale_id
    if pickup_by:
        patch["pickup_by"] = pickup_by
    try:
        done = _rows(await sb(supabase.table("pharmacy_order_requests").update(patch).eq("clinic_id", cid).eq("id", request_id)
                              .eq("status", r["status"])))
    except Exception as e:                                 # noqa: BLE001
        if "kriya_cross_tenant_ref" in str(e):
            raise HTTPException(status_code=404, detail="Sale not found in this clinic.")
        raise
    if not done:
        raise HTTPException(status_code=409, detail="Changed meanwhile. Reload.")
    if to == "ready":
        from app.services.whatsapp import whatsapp_service
        try:
            await whatsapp_service.send_text(clinic, r["phone"], t("ready", "en", name=clinic.get("name") or "the pharmacy"),
                                             _source="rx_retail")
        except Exception as e:                             # noqa: BLE001 - the queue state stands
            logger.warning(f"rx order ready message failed clinic={cid}: {type(e).__name__}")
    return done[0]


async def media_ref(clinic_id: str, request_id: str) -> str:
    rows = _rows(await sb(scoped_query("pharmacy_order_requests", clinic_id, "media_ref").eq("id", request_id).limit(1)))
    if not rows or not rows[0].get("media_ref"):
        raise HTTPException(status_code=404, detail="No prescription photo on this request.")
    return rows[0]["media_ref"]


# ── deterministic stock answer (RXR-ADR-7) ───────────────────────────────────

async def stock_answer(clinic: dict, text: str, lang: str) -> str:
    from app.services import rx_catalogue
    cid = clinic["id"]
    q = re.sub(r"\s+", " ", (text or "").strip())[:80]
    if len(q) < 2:
        return t("ask_counter", lang)
    cands = await rx_catalogue.resolve(cid, q, 1)
    if not cands:
        return t("ask_counter", lang)
    item = cands[0]
    item_id = item.get("item_id") or item.get("id")
    brand = (item.get("brand_name") or q)[:60]
    stock = _rows(await sb(scoped_query("rx_v_item_stock", cid, "sellable_units").eq("item_id", item_id)))
    units = sum(int(r.get("sellable_units") or 0) for r in stock)
    msg = t("in_stock" if units > 0 else "out_of_stock", lang, item=brand)
    if (item.get("schedule") or "none") in PRESCRIPTION_SCHEDULES:
        msg += "\n" + t("needs_rx", lang)                  # never "available to order" for H / H1 / X (X13)
    return msg


# ── the WhatsApp front door (one guarded block in conversation.py) ───────────

async def send_menu(manager, clinic: dict, phone: str, lang: str) -> None:
    await manager.whatsapp.send_interactive_list(clinic, phone, t("menu", lang, name=clinic.get("name") or "our pharmacy"),
                                                 "Menu", [{"title": "Menu", "rows": menu_rows(lang)}])


async def _hours(manager, clinic: dict, phone: str, lang: str) -> None:
    from app.services.tenant import get_clinic_contact
    cfg = clinic.get("config") or {}
    await manager.whatsapp.send_text(clinic, phone, t(
        "hours", lang, name=clinic.get("name") or "", address=get_clinic_contact(clinic, "address", "") or "",
        hours=cfg.get("opening_hours") or cfg.get("timings") or "Please call us for today's timings",
        phone=get_clinic_contact(clinic, "phone", clinic.get("whatsapp_number") or "") or ""))


async def handle(manager, clinic: dict, phone: str, state: Optional[str], message: str, message_type: str,
                 interactive_data: Optional[dict], lang: str, set_state) -> bool:
    """The whole WhatsApp conversation of a pharmacy-plan clinic. Returns True when it answered (always, for this
    plan, except taps another handler owns — cc:, rx_refill_ — which the caller routes first)."""
    tap = str((interactive_data or {}).get("id") or "") if message_type in ("interactive", "button") else ""
    if tap == "rx_menu_send":
        await set_state(STATE_PHOTO)
        await manager.whatsapp.send_text(clinic, phone, t("ask_photo", lang))
        return True
    if tap == "rx_menu_stock":
        await set_state(STATE_STOCK)
        await manager.whatsapp.send_text(clinic, phone, t("ask_stock", lang))
        return True
    if tap == "rx_menu_refill":
        _, created = await create_request(clinic, phone, "refill", "whatsapp_text")
        await manager.whatsapp.send_text(clinic, phone, t("refill_ok" if created else "already", lang))
        await set_state("main_menu")
        return True
    if tap == "rx_menu_hours":
        await _hours(manager, clinic, phone, lang)
        return True
    if tap == "rx_menu_staff":
        _, created = await create_request(clinic, phone, "callback", "whatsapp_text")
        await manager.whatsapp.send_text(clinic, phone, t("staff" if created else "already", lang))
        return True
    if tap == "menu_emergency":
        return False                                        # the existing emergency handler answers it
    if message_type == "image" and state == STATE_PHOTO and (interactive_data or {}).get("media_id"):
        _, created = await create_request(clinic, phone, "prescription", "whatsapp_photo",
                                          media_ref=str(interactive_data["media_id"])[:200],
                                          note=(interactive_data or {}).get("caption"))
        await manager.whatsapp.send_text(clinic, phone, t("photo_ok" if created else "already", lang))
        await set_state("main_menu")
        return True
    if message_type == "text" and state == STATE_STOCK and message.strip():
        await manager.whatsapp.send_text(clinic, phone, await stock_answer(clinic, message, lang))
        await set_state("main_menu")
        return True
    await send_menu(manager, clinic, phone, lang)           # anything else: the pharmacy menu, never a booking
    await set_state("main_menu")
    return True


# ── Business & GST card (RXR-04, RXR-ADR-4) ──────────────────────────────────

async def business_profile(clinic: dict) -> dict:
    """GSTIN + pharmacist on duty + drug licence per shop. The GSTIN is edited here only when OPD is off (OPD clinics
    keep it in OPD setup — one owner per value)."""
    from app.services.rx import list_stores
    rx_s, opd_s = clinic.get("rx_settings") or {}, clinic.get("opd_settings") or {}
    stores = [{"id": s["id"], "code": s.get("code"), "name": s.get("name"), "drug_licence_no": s.get("drug_licence_no")}
              for s in await list_stores(clinic["id"]) if s.get("can_sell")]
    return {"gstin": opd_s.get("gstin") or None, "gstin_editable": not (clinic.get("features") or {}).get("opd_enabled"),
            "pharmacist_name": rx_s.get("pharmacist_name"), "pharmacist_registration": rx_s.get("pharmacist_registration"),
            "po_template": rx_s.get("po_template") or "", "stores": stores}


async def update_business_profile(clinic: dict, changes: dict) -> dict:
    """Writes only opd_settings.gstin (when OPD is off), rx_settings.pharmacist_* and rx_stores.drug_licence_no."""
    from app.services.rx import create_or_update_store, list_stores
    from app.services.tenant import invalidate_tenant_cache
    cid = clinic["id"]
    if changes.get("gstin") is not None and (clinic.get("features") or {}).get("opd_enabled"):
        raise HTTPException(status_code=409, detail="This clinic uses OPD: change the GSTIN in OPD setup.")
    licences = changes.get("licences") or []
    if licences:
        sellable = {str(s["id"]) for s in await list_stores(cid) if s.get("can_sell")}
        if any(str(x["store_id"]) not in sellable for x in licences):
            raise HTTPException(status_code=404, detail="Store not found.")
    # unscoped: unique_row_key
    fresh = _rows(await sb(supabase.table("clinics").select("opd_settings, rx_settings").eq("id", cid).limit(1)))
    row = fresh[0] if fresh else {}
    patch: dict = {}
    if changes.get("gstin") is not None:
        patch["opd_settings"] = {**(row.get("opd_settings") or {}), "gstin": changes["gstin"] or None}
    rx_patch = {k: changes[k].strip() for k in ("pharmacist_name", "pharmacist_registration", "po_template")
                if changes.get(k) is not None}
    if rx_patch:
        patch["rx_settings"] = {**(row.get("rx_settings") or {}), **rx_patch}
    if patch:
        # unscoped: unique_row_key
        await sb(supabase.table("clinics").update(patch).eq("id", cid))
        invalidate_tenant_cache()
    for x in licences:
        await create_or_update_store(cid, {"drug_licence_no": x["drug_licence_no"].strip()}, str(x["store_id"]))
    clinic = {**clinic, "opd_settings": patch.get("opd_settings", clinic.get("opd_settings")),
              "rx_settings": patch.get("rx_settings", clinic.get("rx_settings"))}
    return await business_profile(clinic)


# ── PO to distributor (RXR-10; RETAIL-PLAN §7 "Distributor ordering") ────────

PO_CSV_HEADER = ["PO number", "Line", "Item code", "Item", "Pack", "Ordered packs", "Free packs", "Expected rate per pack (Rs)"]


def po_csv(po: dict) -> str:
    """Distributor-friendly columns (one row per line); every cell formula-safe."""
    import csv
    import io

    from app.services.client_data import csv_cell
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(PO_CSV_HEADER)
    for ln in po.get("lines") or []:
        it = ln.get("item") or {}
        rate = ln.get("expected_rate_pack_paise")
        w.writerow([csv_cell(v) for v in (
            po.get("po_number") or "DRAFT", ln["line_no"], it.get("item_code", ""), it.get("brand_name", ""),
            f"{it.get('pack_size', '')} {it.get('base_unit', '')}/{it.get('pack_label', '')}".strip(),
            ln["ordered_packs"], ln.get("free_packs") or 0, "" if rate is None else f"{int(rate) / 100:.2f}")])
    return buf.getvalue()


async def send_po_whatsapp(clinic: dict, po: dict, pdf: bytes) -> dict:
    """The approved PO as a document on the distributor's WhatsApp through the clinic's own utility template
    (rx_settings.po_template; header = the PO PDF, body = clinic name + PO number). Business-initiated, so only a
    template works; a draft or cancelled PO is never sent."""
    from app.services.whatsapp import whatsapp_service
    if po["status"] not in ("sent", "partially_received"):
        raise HTTPException(status_code=409, detail="Approve (send) the purchase order first.")
    template = (clinic.get("rx_settings") or {}).get("po_template")
    if not template:
        raise HTTPException(status_code=409, detail="Set the approved WhatsApp template for purchase orders in "
                                                    "Pharmacy setup → Business & GST first.")
    phone = re.sub(r"[^\d+]", "", str((po.get("supplier") or {}).get("phone") or ""))
    if not re.fullmatch(r"\+?[0-9]{10,15}", phone):
        raise HTTPException(status_code=409, detail="This distributor has no WhatsApp number on record.")
    name = f"{po['po_number']}.pdf"
    media_id = await whatsapp_service.upload_media(clinic, pdf, name, "application/pdf")
    if not media_id:
        raise HTTPException(status_code=502, detail="WhatsApp did not accept the PDF. Try again or send the CSV.")
    ok = await whatsapp_service.send_template(clinic, phone, template, components=[
        {"type": "header", "parameters": [{"type": "document", "document": {"id": media_id, "filename": name}}]},
        {"type": "body", "parameters": [{"type": "text", "text": str(clinic.get("name") or "")[:60]},
                                        {"type": "text", "text": po["po_number"]}]}], _source="rx_po")
    if not ok:
        raise HTTPException(status_code=502, detail="WhatsApp refused the message (check the template is approved).")
    return {"sent": True, "to": mask_phone(phone), "po_number": po["po_number"]}
