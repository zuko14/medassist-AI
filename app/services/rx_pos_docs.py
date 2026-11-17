"""Pharmacy counter documents (RX-33, RX-72): 80 mm receipt (HTML print view), A5 receipt PDF, dispensing
labels (A4) and the PCN credit note PDF. Content per docs/pharmacy-os/UI.md §2.5 and ADVANCED-FEATURES A4.

Receipts never print a phone number or full address (SECURITY §5). Labels copy the prescriber's own words
verbatim; only the closed vocabulary (frequency codes, timing) is rendered in the patient's language.
No dose is computed or suggested, and an OTC line prints no dosage text.
"""

from datetime import datetime
from html import escape
from typing import Optional

from app.utils.helpers import IST, doctor_title

FREQ_WORDS = {
    "en": {"OD": "once a day", "BD": "twice a day", "TDS": "three times a day", "QID": "four times a day",
           "HS": "at bedtime", "SOS": "only when needed", "STAT": "immediately, once"},
    "hi": {"OD": "दिन में 1 बार", "BD": "दिन में 2 बार", "TDS": "दिन में 3 बार", "QID": "दिन में 4 बार",
           "HS": "सोते समय", "SOS": "ज़रूरत पड़ने पर", "STAT": "तुरंत, एक बार"},
    "te": {"OD": "రోజుకు 1 సారి", "BD": "రోజుకు 2 సార్లు", "TDS": "రోజుకు 3 సార్లు", "QID": "రోజుకు 4 సార్లు",
           "HS": "నిద్రపోయే ముందు", "SOS": "అవసరమైనప్పుడు మాత్రమే", "STAT": "వెంటనే, ఒకసారి"},
}
TIMING_WORDS = {
    "en": {"before_food": "before food", "after_food": "after food", "with_food": "with food",
           "empty_stomach": "on an empty stomach", "bedtime": "at bedtime"},
    "hi": {"before_food": "खाना खाने से पहले", "after_food": "खाना खाने के बाद", "with_food": "खाने के साथ",
           "empty_stomach": "खाली पेट", "bedtime": "सोते समय"},
    "te": {"before_food": "భోజనానికి ముందు", "after_food": "భోజనం తర్వాత", "with_food": "భోజనంతో",
           "empty_stomach": "ఖాళీ కడుపుతో", "bedtime": "నిద్రపోయే ముందు"},
}
_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        return (_TENS[n // 10] + " " + _ONES[n % 10]).strip()
    if n < 1000:
        return (_ONES[n // 100] + " Hundred " + _words(n % 100)).strip()
    for size, name in ((10 ** 7, "Crore"), (10 ** 5, "Lakh"), (1000, "Thousand")):
        if n >= size:
            return (_words(n // size) + f" {name} " + _words(n % size)).strip()
    return ""


def rupees_in_words(paise: int) -> str:
    """Indian system: 123456789 paise → 'Rupees Twelve Lakh Thirty Four Thousand Five Hundred Sixty Seven and
    Eighty Nine Paise Only'."""
    r, p = divmod(abs(int(paise)), 100)
    text = "Rupees " + (_words(r) or "Zero")
    return text + (f" and {_words(p)} Paise" if p else "") + " Only"


def label_vocab(frequency: Optional[str], timing: Optional[str], lang: str) -> str:
    """Closed vocabulary only, in the patient's language; '' for anything outside it (never translated freely)."""
    lang = lang if lang in FREQ_WORDS else "en"
    parts = [FREQ_WORDS[lang].get((frequency or "").strip().upper(), ""), TIMING_WORDS[lang].get(timing or "", "")]
    return ", ".join(p for p in parts if p)


def _rs(paise) -> str:
    return "₹" + f"{int(paise or 0) / 100:,.2f}"


def _ist(ts) -> str:
    if not ts:
        return ""
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(IST).strftime("%d/%m/%Y %H:%M")


def receipt_model(sale: dict, clinic: dict, store: dict, settings: dict) -> dict:
    """Everything a receipt prints, in one place for both layouts."""
    slabs: dict[int, dict] = {}
    for ln in sale["lines"]:
        sl = slabs.setdefault(int(ln["tax_rate_bps"]), {"taxable": 0, "cgst": 0, "sgst": 0})
        sl["taxable"] += int(ln["taxable_paise"])
        sl["cgst"] += int(ln["cgst_paise"])
        sl["sgst"] += int(ln["sgst_paise"])
    rx = sale.get("prescription") or {}
    pending = [ln for ln in (rx.get("lines") or []) if ln.get("pending")]
    snap = sale.get("prescriber_snapshot") or {}
    panel = (sale.get("patient_panel") or {}).get("patient") or {}
    letter = (clinic.get("opd_settings") or {})
    return {
        "clinic": clinic.get("name") or "", "address": letter.get("address") or clinic.get("address") or "",
        "gstin": sale.get("clinic_gstin"), "licence": store.get("drug_licence_no"), "store": store.get("name"),
        "title": "TAX INVOICE" if sale.get("is_tax_invoice") else "BILL OF SUPPLY",
        "number": sale.get("sale_number") or "DRAFT", "when": _ist(sale.get("completed_at") or sale.get("created_at")),
        "patient": panel.get("name") or sale.get("walkin_name") or snap.get("patient_name") or "Walk-in",
        "mrn": panel.get("mrn"), "age_gender": " ".join(str(x) for x in (panel.get("age_years"), panel.get("gender")) if x),
        "prescriber": doctor_title(snap.get("name")) if snap.get("name") else None, "reg_no": snap.get("registration_number"),
        "buyer_gstin": sale.get("buyer_gstin"),
        "lines": [{"name": (ln.get("item") or {}).get("brand_name", ""), "batch": (ln.get("batch") or {}).get("batch_no", ""),
                   "exp": str((ln.get("batch") or {}).get("expiry_date") or "")[:7], "qty": ln["qty_units"],
                   "mrp": ln["mrp_pack_paise"], "amount": int(ln["line_gross_paise"]) - int(ln["line_discount_paise"]),
                   "schedule": ln["schedule"],
                   "note": (f"Substituted for {(ln.get('substituted_from') or {}).get('brand_name', '')}: "
                            f"{ln['substitution_reason']}") if ln.get("substitution_reason") else ""} for ln in sale["lines"]],
        "slabs": [{"rate": f"{k / 100:g}%", **v} for k, v in sorted(slabs.items())],
        "gross": sale["gross_paise"], "discount": sale["discount_paise"], "round_off": sale["round_off_paise"],
        "net": sale["net_paise"], "words": rupees_in_words(sale["net_paise"]),
        "payments": [f"{p['mode'].upper()} {_rs(p['amount_paise'])}" for p in sale.get("payments") or []],
        "pending": [f"{ln['drug_name']}: {ln['pending']} units still to collect" for ln in pending],
        "footer": f"Medicines once sold are taken back within {settings.get('return_days', 7)} days only with this bill, "
                  "unopened and unused.",
    }


def render_receipt_html(m: dict, fmt: str = "80mm") -> str:
    """Same-origin print view: the page calls window.print(); @page sizes the paper."""
    e = lambda v: escape(str(v if v is not None else ""))  # noqa: E731
    page = "@page { size: 80mm auto; margin: 0 } body { width: 72mm; }" if fmt == "80mm" \
        else "@page { size: A5; margin: 10mm } body { width: auto; }"
    rows = "".join(f"<tr><td>{e(ln['name'])}{' <b>' + e(ln['schedule']) + '</b>' if ln['schedule'] not in ('none', 'G') else ''}"
                   f"<br><small>{e(ln['batch'])} · {e(ln['exp'])}</small>{'<br><small>' + e(ln['note']) + '</small>' if ln['note'] else ''}"
                   f"</td><td class=r>{e(ln['qty'])}</td><td class=r>{e(_rs(ln['amount']))}</td></tr>" for ln in m["lines"])
    slabs = "".join(f"<tr><td>{e(s['rate'])}</td><td class=r>{e(_rs(s['taxable']))}</td><td class=r>{e(_rs(s['cgst']))}</td>"
                    f"<td class=r>{e(_rs(s['sgst']))}</td></tr>" for s in m["slabs"]) if m["gstin"] else ""
    head = [m["clinic"], m["address"], f"GSTIN {m['gstin']}" if m["gstin"] else "", f"DL {m['licence']}" if m["licence"] else ""]
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(m['number'])}</title><style>
{page} body {{ font: 12px/1.35 system-ui, sans-serif; color: #000; margin: 0 auto; padding: 4mm; }}
h1 {{ font-size: 14px; margin: 0; text-align: center; }} .c {{ text-align: center; }} .r {{ text-align: right; }}
table {{ width: 100%; border-collapse: collapse; }} td, th {{ padding: 1px 0; vertical-align: top; }}
hr {{ border: 0; border-top: 1px dashed #000; margin: 4px 0; }} small {{ font-size: 10px; }}
</style></head><body onload="window.print()">
<h1>{e(m['clinic'])}</h1><div class=c>{'<br>'.join(e(x) for x in head[1:] if x)}</div><hr>
<div class=c><b>{e(m['title'])}</b></div>
<div>Bill {e(m['number'])} · {e(m['when'])}</div>
<div>{e(m['patient'])}{' · MRN ' + e(m['mrn']) if m['mrn'] else ''}{' · ' + e(m['age_gender']) if m['age_gender'] else ''}</div>
{'<div>' + e(m['prescriber']) + ' · Reg ' + e(m['reg_no']) + '</div>' if m['prescriber'] else ''}
{'<div>Buyer GSTIN ' + e(m['buyer_gstin']) + '</div>' if m['buyer_gstin'] else ''}<hr>
<table><tr><th style="text-align:left">Item</th><th class=r>Qty</th><th class=r>Amount</th></tr>{rows}</table><hr>
<table><tr><td>Gross</td><td class=r>{e(_rs(m['gross']))}</td></tr><tr><td>Discount</td><td class=r>-{e(_rs(m['discount']))}</td></tr>
<tr><td>Round off</td><td class=r>{e(_rs(m['round_off']))}</td></tr><tr><td><b>Total</b></td><td class=r><b>{e(_rs(m['net']))}</b></td></tr></table>
<div><small>{e(m['words'])}</small></div>
{'<hr><table><tr><th style="text-align:left">GST</th><th class=r>Taxable</th><th class=r>CGST</th><th class=r>SGST</th></tr>' + slabs + '</table>' if slabs else ''}
<hr><div>{e(' · '.join(m['payments']))}</div>
{''.join('<div><small>' + e(p) + '</small></div>' for p in m['pending'])}
<hr><div class=c><small>{e(m['footer'])}</small></div></body></html>"""


def render_receipt_pdf(m: dict) -> bytes:
    from app.services.opd_pdf import OPDDocument, _footer, _kv_rows, _table, _title
    pdf = OPDDocument(doc_label="Pharmacy bill", orientation="P", unit="mm", format="A5")
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.set_font("NotoSans", "B", 11)
    pdf.cell(0, 6, m["clinic"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("NotoSans", "", 7)
    for x in (m["address"], f"GSTIN {m['gstin']}" if m["gstin"] else "", f"Drug licence {m['licence']}" if m["licence"] else ""):
        if x:
            pdf.cell(0, 3.6, x, align="C", new_x="LMARGIN", new_y="NEXT")
    _title(pdf, f"{m['title']} {m['number']}")
    _kv_rows(pdf, [("Date", m["when"]), ("Patient", m["patient"]), ("MRN", m["mrn"]), ("Age/Sex", m["age_gender"]),
                   ("Prescriber", m["prescriber"]), ("Reg. no", m["reg_no"])])
    _table(pdf, (62, 22, 14, 10, 20), ("Item", "Batch", "Exp", "Qty", "Amount"),
           [(ln["name"] + (f" [{ln['schedule']}]" if ln["schedule"] not in ("none", "G") else ""), ln["batch"], ln["exp"],
             ln["qty"], _rs(ln["amount"])) for ln in m["lines"]], emphasis=())
    if m["gstin"] and m["slabs"]:
        _table(pdf, (20, 36, 36, 36), ("GST", "Taxable", "CGST", "SGST"),
               [(s["rate"], _rs(s["taxable"]), _rs(s["cgst"]), _rs(s["sgst"])) for s in m["slabs"]], emphasis=())
    pdf.set_font("NotoSans", "B", 9)
    pdf.cell(0, 5, f"Total {_rs(m['net'])}  (gross {_rs(m['gross'])}, discount {_rs(m['discount'])}, "
                   f"round-off {_rs(m['round_off'])})", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("NotoSans", "", 7)
    for line in [m["words"], " · ".join(m["payments"]), *[ln["note"] for ln in m["lines"] if ln["note"]], *m["pending"],
                 m["footer"]]:
        if line:
            pdf.multi_cell(0, 3.6, line, new_x="LMARGIN", new_y="NEXT")
    return _footer(pdf, f"Pharmacy bill {m['number']}")


def label_text(line: dict, rx_line: Optional[dict], lang: str) -> dict:
    """What one dispensing label says about use: the prescriber's own words verbatim (dosage, frequency, duration,
    instructions) and the closed vocabulary in the patient's language. An OTC line (no Rx line) says nothing."""
    if not rx_line:
        return {"verbatim": "", "local": ""}
    verbatim = " · ".join(str(x) for x in (rx_line.get("dosage"), rx_line.get("frequency"),
                                           f"{rx_line['duration_days']} days" if rx_line.get("duration_days") else None,
                                           rx_line.get("instructions")) if x)
    return {"verbatim": verbatim, "local": label_vocab(rx_line.get("frequency"), rx_line.get("timing"), lang)}


def render_labels_pdf(sale: dict, clinic: dict, lang: str) -> bytes:
    """A4 dispensing labels, 50 × 25 mm (RX-72). Prescriber's text verbatim; closed vocabulary translated."""
    from app.services.opd_pdf import OPDDocument
    pdf = OPDDocument(doc_label="Labels", orientation="L", unit="mm", format=(25, 50))
    pdf.set_auto_page_break(False)
    pdf.set_margins(1.5, 1.5, 1.5)
    rx_lines = {str(ln["id"]): ln for ln in ((sale.get("prescription") or {}).get("lines") or [])}
    snap = sale.get("prescriber_snapshot") or {}
    panel = (sale.get("patient_panel") or {}).get("patient") or {}
    who = panel.get("name") or sale.get("walkin_name") or snap.get("patient_name") or ""
    when = _ist(sale.get("completed_at") or sale.get("created_at"))[:10]
    for ln in sale["lines"]:
        rx = rx_lines.get(str(ln.get("opd_prescription_item_id") or ""))
        pdf.add_page()
        pdf.set_font("NotoSans", "B", 6.5)
        pdf.set_xy(1.5, 1.2)
        pdf.cell(47, 3, f"{(ln.get('item') or {}).get('brand_name', '')} × {ln['qty_units']}"[:40])
        pdf.set_font("NotoSans", "", 5.2)
        pdf.set_xy(1.5, 4.4)
        pdf.cell(47, 2.4, f"{who[:26]}{' · MRN ' + panel['mrn'] if panel.get('mrn') else ''}")
        pdf.set_xy(1.5, 6.9)
        pdf.cell(47, 2.4, f"B {(ln.get('batch') or {}).get('batch_no', '')} · EXP {str((ln.get('batch') or {}).get('expiry_date', ''))[:7]}"
                          f" · {when}")
        words = label_text(ln, rx, lang)      # verbatim (AUDIT A4); OTC lines print no dose text
        if words["verbatim"]:
            pdf.set_xy(1.5, 9.6)
            pdf.multi_cell(47, 2.4, words["verbatim"][:120])
        if words["local"]:
            pdf.set_xy(1.5, 16.8)
            pdf.multi_cell(47, 2.4, words["local"][:80])
        pdf.set_xy(1.5, 21.6)
        pdf.cell(47, 2.4, doctor_title(snap["name"])[:40] if snap.get("name") else (clinic.get("name") or "")[:40])
    if not sale["lines"]:
        pdf.add_page()
    return bytes(pdf.output())


def render_credit_note_pdf(cn: dict, sale: dict, clinic: dict) -> bytes:
    from app.services.opd_pdf import _a4, _footer, _kv_rows, _title
    pdf = _a4("Credit note")
    _title(pdf, f"CREDIT NOTE {cn['cn_number']}")
    _kv_rows(pdf, [("Issued by", clinic.get("name")), ("GSTIN", sale.get("clinic_gstin")),
                   ("Against bill", sale.get("sale_number")), ("Bill date", _ist(sale.get("completed_at"))[:10]),
                   ("Date", _ist(cn.get("created_at"))), ("Reason", cn.get("reason")),
                   ("Value (incl. GST)", _rs(cn["amount_paise"])), ("GST reversed", _rs(cn["tax_paise"])),
                   ("Issued by (staff)", cn.get("issued_by_name"))])
    pdf.set_font("NotoSans", "", 8)
    pdf.multi_cell(0, 4.2, rupees_in_words(cn["amount_paise"]) + ". Redeemable against a future pharmacy bill at this "
                   "clinic; not exchangeable for cash.")
    return _footer(pdf, f"Credit note {cn['cn_number']}")
