"""Visit report renderer (REPORT-TEMPLATES.md §5) — pure: dicts in, PDF bytes out. Zero-LLM.

render_visit_report(release, results, visit, template) -> (pdf_bytes, sha256_hex, page_count)

  release  {release_no, kind, verify_code, verify_url, created_at, letterhead_snapshot, routing_snapshot,
            replaces: {release_no, created_at} | None, watermark: 'shadow'|'internal'|'superseded'|'partial'|None,
            assets: {storage_path: bytes}}
  results  [{department: {code, heading, sort_order}, test_name, print_title, package_name,
             outsourced: {name, legal_name, address, nabl_certificate_no} | None, interpretation, notes,
             values: [{name, value, unit, flag, reference_text, reference_range_display, method,
                       previous_value, previous_at}],
             attachments: [{kind, data, caption}], signer: {name, qualification, registration_number,
             registration_council, signature_png}}]
  visit    {accession_number, patient: {name, age_sex, mrn}, referrer, external_ref, provider, bill_no,
            collected_at, received_at, reported_at, sample_ids}
  template a layout document (dx_template_schema), validated here

Same input -> same bytes (creation date = release created_at; no random ids), so a reprint
has the same SHA-256. "Page n of N" uses fpdf2's total-pages alias, resolved when the
document is closed — the exact page count without a second render.
"""

import hashlib
import io
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fpdf import FPDF

from app.services import dx_qr
from app.services.dx_barcode import modules as code128_modules
from app.services.dx_template_schema import validate_layout

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")
IST = timezone(timedelta(hours=5, minutes=30))
WATERMARKS = {"shadow": "SHADOW — NOT FOR PATIENT USE", "internal": "INTERNAL VIEW", "superseded": "SUPERSEDED",
              "partial": "PARTIAL REPORT"}
ABNORMAL = {"low", "high", "abnormal"}
CRITICAL = {"critical_low", "critical_high"}
FIELD_LABELS = {"name": "Patient", "age_sex": "Age / Sex", "referrer": "Referred by", "visit_no": "Visit No.",
                "mrn": "MRN", "bill_no": "Bill No.", "external_ref": "Hospital Ref.", "provider": "Provider",
                "collected": "Collected", "received": "Received", "reported": "Reported", "sample_ids": "Sample ID"}
GREY, INK, RED = (110, 110, 110), (20, 20, 20), (190, 20, 20)
PATIENT_H = 25.0


def _dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def ist_text(value: Any) -> str:
    """'Sep 01, 2026, 07:02 a.m.' in IST (REPORT-TEMPLATES §5 rule 2)."""
    d = _dt(value)
    if not d:
        return ""
    d = d.astimezone(IST)
    return d.strftime("%b %d, %Y, %I:%M ") + ("a.m." if d.hour < 12 else "p.m.")


def ist_date(value: Any) -> str:
    d = _dt(value)
    return d.astimezone(IST).strftime("%d-%b-%Y") if d else ""


def _rgb(hex_: str) -> tuple[int, int, int]:
    h = (hex_ or "#000000").lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _site_line(site: dict) -> str:
    parts = [site.get("legal_name") or site.get("display_name") or site.get("name"), site.get("address")]
    if site.get("nabl_certificate_no"):
        parts.append(f"NABL: {site['nabl_certificate_no']}")
    elif site.get("registration_no"):
        parts.append(f"Reg. No: {site['registration_no']}")
    return ", ".join(p for p in parts if p)


class _Report(FPDF):
    def __init__(self, layout: dict, release: dict, visit: dict):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.L, self.rel, self.visit = layout, release, visit
        m = layout["page"]["margin_mm"]
        self.set_margins(m["left"], m["top"], m["right"])
        self.set_auto_page_break(True, margin=m["bottom"] + layout["footer"]["height_mm"])
        self.add_font("Noto", "", os.path.join(FONTS_DIR, "NotoSans-Regular.ttf"))
        self.add_font("Noto", "B", os.path.join(FONTS_DIR, "NotoSans-Bold.ttf"))
        self.set_creation_date(_dt(release.get("created_at")) or datetime(2000, 1, 1, tzinfo=timezone.utc))
        self.set_title(f"{visit.get('accession_number', '')} R{release.get('release_no', 1)}")
        self.set_producer("Kriya Diagnostic OS")
        self.core = layout["body"]["font_family"] != "noto_sans"
        self.W = self.w - m["left"] - m["right"]
        self.assets = release.get("assets") or {}
        self.letter = release.get("letterhead_snapshot") or {}
        self.route = release.get("routing_snapshot") or {}
        self.table_open: Optional[str] = None         # the test whose table continues on the next page

    # ── text helpers ────────────────────────────────────────────────────────
    def font(self, size: float, bold: bool = False, italic: bool = False):
        if italic:                                # no italic Noto face: methods are Latin (HPLC, CHO-POD…)
            self.set_font("helvetica", "I", size)
        elif self.core:
            self.set_font(self.L["body"]["font_family"], "B" if bold else "", size)
        else:
            self.set_font("Noto", "B" if bold else "", size)

    def txt(self, text: Any) -> str:
        s = "" if text is None else str(text)
        if self.core or self.font_family == "helvetica":
            s = s.replace("—", "-").replace("–", "-").replace("₹", "Rs ").encode("latin-1", "replace").decode("latin-1")
        return s

    def put(self, x, y, w, h, text, size, bold=False, align="L", colour=INK, italic=False):
        self.font(size, bold, italic)
        self.set_text_color(*colour)
        self.set_xy(x, y)
        self.cell(w, h, self.txt(text), align=align)

    def lines_of(self, text: str, w: float, size: float, bold=False, italic=False) -> list[str]:
        if not text:
            return []
        self.font(size, bold, italic)
        out: list[str] = []
        for para in str(text).split("\n"):
            out += self.multi_cell(w, size * 0.42, self.txt(para), dry_run=True, output="LINES") or [""]
        return out

    # ── page furniture ──────────────────────────────────────────────────────
    def header(self):
        mark = self.rel.get("watermark")
        if mark:
            text = WATERMARKS.get(mark, mark)
            self.font(34, bold=True)
            self.set_text_color(225, 225, 225)
            with self.rotation(35, self.w / 2, self.h / 2):
                self.set_xy(0, self.h / 2 - 8)
                self.cell(self.w, 16, self.txt(text), align="C")
        m = self.L["page"]["margin_mm"]
        h = self.L["header"]
        top, x0 = m["top"], m["left"]
        img = self.assets.get(h.get("image_asset") or "")
        if h["mode"] == "image" and img:
            self.image(io.BytesIO(img), x=x0, y=top, w=self.W, h=h["height_mm"])
        else:
            c = h["composed"]
            if c.get("band"):
                self.set_fill_color(*_rgb(c["accent_hex"]))
                self.rect(0, top + h["height_mm"] - 2.2, self.w, 2.2, style="F")
            logo = self.assets.get(c.get("logo_asset") or "")
            tx = x0
            if logo:
                self.image(io.BytesIO(logo), x=x0, y=top, h=min(18, h["height_mm"] - 4))
                tx = x0 + 34
            clinic = self.letter.get("clinic") or {}
            name = c.get("name") or clinic.get("name") or (self.letter.get("site") or {}).get("display_name") or ""
            self.put(tx, top, self.W - (tx - x0), 7, name, 14, bold=True)
            y = top + 7.5
            for line in (c.get("lines") or [clinic.get("address"), clinic.get("phone")])[:4]:
                if line:
                    self.put(tx, y, self.W - (tx - x0), 4, line, 8, colour=GREY)
                    y += 4
            self.put(x0, top, self.W, 7, c.get("title") or "", 9, bold=True, align="R", colour=_rgb(c["accent_hex"]))
        self.patient_box(top + h["height_mm"] + 1.5)
        y = top + h["height_mm"] + 2 + PATIENT_H
        banner = self.banner()
        if banner:
            self.set_fill_color(255, 243, 205)
            self.rect(x0, y, self.W, 6, style="F")
            self.put(x0, y + 0.5, self.W, 5, banner, 8.5, bold=True, align="C")
            y += 7
        self.set_y(y + 1)
        if self.table_open:
            self.table_head(continued=self.table_open)

    def banner(self) -> Optional[str]:
        kind = self.rel.get("kind")
        if kind == "partial":
            return "PARTIAL REPORT — remaining tests pending"
        if kind == "amended":
            old = self.rel.get("replaces") or {}
            return f"AMENDED — replaces R{old.get('release_no', '?')} of {ist_date(old.get('created_at'))}".rstrip(" of")
        return None

    def patient_box(self, y: float):
        x0 = self.L["page"]["margin_mm"]["left"]
        v, p = self.visit, self.visit.get("patient") or {}
        sample_ids = v.get("sample_ids") or []
        values = {"name": p.get("name"), "age_sex": p.get("age_sex"), "referrer": v.get("referrer"),
                  "visit_no": v.get("accession_number"), "mrn": p.get("mrn"), "bill_no": v.get("bill_no"),
                  "external_ref": v.get("external_ref"), "provider": v.get("provider"),
                  "collected": ist_text(v.get("collected_at")), "received": ist_text(v.get("received_at")),
                  "reported": ist_text(v.get("reported_at")), "sample_ids": ", ".join(sample_ids)}
        rows = [(FIELD_LABELS[f], values[f]) for f in self.L["patient_block"]["fields"] if values.get(f)]
        self.set_draw_color(180, 180, 180)
        self.rect(x0, y, self.W, PATIENT_H - 1.5)
        code = (sample_ids[0] if len(sample_ids) == 1 else v.get("accession_number")) or ""
        bar_w = 42 if self.L["patient_block"]["show_barcode"] and code else 0
        col_w = (self.W - bar_w) / 2
        half = (len(rows) + 1) // 2
        for i, (label, value) in enumerate(rows[:12]):
            cx = x0 + 2 + (col_w if i >= half else 0)
            cy = y + 1.2 + (i - (half if i >= half else 0)) * 3.6
            self.put(cx, cy, 22, 3.6, label, 7, colour=GREY)
            self.put(cx + 22, cy, col_w - 24, 3.6, f": {value}", 7.5, bold=(label == "Patient"))
        if bar_w:
            self.code128(code, x0 + self.W - bar_w - 1, y + 3, 10, bar_w - 2)
            self.put(x0 + self.W - bar_w - 1, y + 13.5, bar_w - 2, 3, code, 6.5, align="C")

    def code128(self, data: str, x: float, y: float, h: float, max_w: float):
        mods = code128_modules(data)
        m = min(0.3, max_w / len(mods))
        self.set_fill_color(0, 0, 0)
        start = None
        for i, bit in enumerate(mods + "0"):
            if bit == "1" and start is None:
                start = i
            elif bit == "0" and start is not None:
                self.rect(x + start * m, y, (i - start) * m, h, style="F")
                start = None

    def qr(self, text: str, x: float, y: float, size: float):
        mat = dx_qr.encode(text)
        cell = size / len(mat)
        self.set_fill_color(0, 0, 0)
        for r, row in enumerate(mat):
            for c, dark in enumerate(row):
                if dark:
                    self.rect(x + c * cell, y + r * cell, cell, cell, style="F")

    def footer(self):
        m = self.L["page"]["margin_mm"]
        f, ver = self.L["footer"], self.L["verification"]
        x0, top = m["left"], self.h - m["bottom"] - f["height_mm"]
        self.set_draw_color(180, 180, 180)
        self.line(x0, top + 1, x0 + self.W, top + 1)
        qr_w = 16 if ver["qr"] and self.rel.get("verify_url") else 0
        tx = x0 + (qr_w + 2 if ver["position"] == "footer_left" and qr_w else 0)
        tw = self.W - (qr_w + 2 if qr_w else 0)
        y = top + 2
        if self.route.get("white_label"):          # ADR-DX-08: never removable
            line = "Tested at: " + _site_line(self.letter.get("site") or {})
            for ln in self.lines_of(line, tw, 7, bold=True)[:2]:
                self.put(tx, y, tw, 3.2, ln, 7, bold=True)
                y += 3.2
        for line in f["lines"]:
            self.put(tx, y, tw, 3.2, line, 7, colour=GREY)
            y += 3.2
        verify = f"Verify: scan the QR or open {self.rel['verify_url']}" if qr_w else ""
        if verify:
            self.put(tx, y, tw, 3.2, verify, 6.5, colour=GREY)
            y += 3.2
        if f["page_numbers"]:
            self.put(tx, top + f["height_mm"] - 4, tw, 3.5,
                     f"{self.visit.get('accession_number', '')} · R{self.rel.get('release_no', 1)} · "
                     f"Page {self.page_no()} of {{nb}}", 7, align="R" if not qr_w or ver["position"] == "footer_left" else "L",
                     colour=GREY)
        if qr_w:
            qx = x0 if ver["position"] == "footer_left" else x0 + self.W - qr_w
            self.qr(self.rel["verify_url"], qx, top + 2, qr_w)
            self.put(qx - 2, top + 2 + qr_w, qr_w + 4, 3, self.rel.get("verify_code", ""), 6, align="C", colour=GREY)

    # ── body ────────────────────────────────────────────────────────────────
    def cols(self) -> list[float]:
        return [self.W * 0.40, self.W * 0.17, self.W * 0.13, self.W * 0.30]

    def table_head(self, continued: Optional[str] = None):
        x0, y = self.l_margin, self.get_y()
        if continued:
            self.put(x0, y, self.W, 4, f"{continued} (continued)", 8, bold=True, colour=GREY)
            y += 4.5
        self.set_fill_color(242, 242, 242)
        self.rect(x0, y, self.W, 5, style="F")
        x = x0
        for w, label in zip(self.cols(), ("Test description", "Value", "Unit", "Reference range")):
            self.put(x + 1, y + 0.5, w - 2, 4, label, 7.5, bold=True)
            x += w
        self.set_y(y + 5.8)

    def row_height(self, v: dict) -> float:
        pt = self.L["body"]["font_pt"]
        lh = pt * 0.45
        left = lh + (3.2 if self.L["body"]["show_method"] and v.get("method") else 0) \
            + (3.2 if self.L["body"]["show_previous_value"] and v.get("previous_value") else 0)
        rng = v.get("reference_text") or v.get("reference_range_display") or ""
        right = len(self.lines_of(rng, self.cols()[3] - 2, pt - 1)) * (pt - 1) * 0.45
        return max(left, right, lh) + 1.6

    def value_row(self, v: dict):
        pt = self.L["body"]["font_pt"]
        h = self.row_height(v)
        if self.will_page_break(h):
            self.add_page()
        x, y = self.l_margin, self.get_y()
        w = self.cols()
        flag = v.get("flag") or "normal"
        self.put(x + 1, y, w[0] - 2, pt * 0.45, v.get("name"), pt)
        yy = y + pt * 0.45
        if self.L["body"]["show_method"] and v.get("method"):
            self.put(x + 1, yy, w[0] - 2, 3.2, f"Method: {v['method']}", 7, colour=GREY, italic=True)
            yy += 3.2
        if self.L["body"]["show_previous_value"] and v.get("previous_value"):
            prev = f"Prev: {v['previous_value']} {v.get('unit') or ''}".rstrip()
            if v.get("previous_at"):
                prev += f" ({ist_date(v['previous_at'])})"
            self.put(x + 1, yy, w[0] - 2, 3.2, prev, 7, colour=GREY)
        value = "" if v.get("value") is None else str(v["value"])
        if flag in CRITICAL:
            self.put(x + w[0], y, w[1], pt * 0.45, f"{value} *", pt, bold=True, colour=RED)
        else:
            self.put(x + w[0], y, w[1], pt * 0.45, value, pt, bold=flag in ABNORMAL)
        self.put(x + w[0] + w[1], y, w[2], pt * 0.45, v.get("unit") or "", pt - 0.5)
        rng = v.get("reference_text") or v.get("reference_range_display") or ""
        if flag == "not_evaluated":
            rng = (rng + "\n" if rng else "") + "(range not applicable)"
        ry = y
        for ln in self.lines_of(rng, w[3] - 2, pt - 1):
            self.put(x + sum(w[:3]) + 1, ry, w[3] - 2, (pt - 1) * 0.45, ln, pt - 1)
            ry += (pt - 1) * 0.45
        self.set_y(y + h)
        self.set_draw_color(232, 232, 232)
        self.line(x, y + h - 0.6, x + self.W, y + h - 0.6)

    def paragraph(self, label: str, text: Optional[str]):
        if not text:
            return
        lines = self.lines_of(f"{label}: {text}", self.W - 2, 8)
        for ln in lines:
            if self.will_page_break(3.8):
                self.add_page()
            self.put(self.l_margin + 1, self.get_y(), self.W - 2, 3.8, ln, 8)
            self.set_y(self.get_y() + 3.8)

    def attachments(self, items: list[dict]):
        from PIL import Image
        imgs = [a for a in items if a.get("kind") in ("graph", "image") and a.get("data")]
        for i in range(0, len(imgs), 2):
            pair = imgs[i:i + 2]
            w = min(120.0, (self.W - 4) / len(pair))
            dims = []
            for a in pair:
                with Image.open(io.BytesIO(a["data"])) as im:
                    dims.append(w * im.height / max(im.width, 1))
            h = max(dims) + (4 if any(a.get("caption") for a in pair) else 0)
            if self.will_page_break(h + 2):
                self.add_page()
            y = self.get_y() + 1
            for j, a in enumerate(pair):
                x = self.l_margin + j * (w + 4)
                self.image(io.BytesIO(a["data"]), x=x, y=y, w=w)
                if a.get("caption"):
                    self.put(x, y + dims[j], w, 3.5, a["caption"], 7, colour=GREY, align="C")
            self.set_y(y + h + 1)
        if any(a.get("kind") == "pdf" for a in items):
            self.paragraph("Note", "The reference laboratory's own report is attached to this record.")

    def signatures(self, signers: list[dict]):
        sig = self.L["signatures"]
        uniq = list({(s.get("name"), s.get("registration_number")): s for s in signers if s}.values())
        if not uniq:
            return
        h = 22 if sig["show_image"] and any(s.get("signature_png") for s in uniq) else 12
        if self.will_page_break(h):
            self.add_page()
        y = self.get_y() + 2
        w = self.W / max(len(uniq), 3)
        for i, s in enumerate(uniq[:4]):
            x = self.l_margin + self.W - (i + 1) * w
            yy = y
            if sig["show_image"] and s.get("signature_png"):
                self.image(io.BytesIO(s["signature_png"]), x=x + 4, y=yy, h=9)
                yy += 10
            self.put(x, yy, w - 2, 4, s.get("name"), 8, bold=True, align="R")
            line = s.get("qualification") or ""
            if sig["show_registration_no"] and s.get("registration_number"):
                council = f" ({s['registration_council']})" if s.get("registration_council") else ""
                line = (line + " · " if line else "") + f"Reg. No: {s['registration_number']}{council}"
            self.put(x, yy + 4, w - 2, 3.5, line, 7, colour=GREY, align="R")
        self.set_y(y + h)


def _departments(results: list[dict]) -> list[tuple[dict, list[dict]]]:
    groups: dict[str, tuple[dict, list[dict]]] = {}
    for r in results:
        d = r.get("department") or {}
        key = d.get("code") or ""
        groups.setdefault(key, (d, []))[1].append(r)
    return sorted(groups.values(), key=lambda g: (g[0].get("sort_order") or 0, g[0].get("code") or ""))


def render_visit_report(release_dict: dict, results_list: list[dict], visit_dict: dict,
                        template_dict: dict) -> tuple[bytes, str, int]:
    layout = validate_layout(template_dict or {})
    if layout.get("watermark") and not release_dict.get("watermark"):
        release_dict = {**release_dict, "watermark": layout["watermark"]}
    pdf = _Report(layout, release_dict, visit_dict)
    body = layout["body"]
    pdf.add_page()
    groups = _departments(results_list) if body["group_by_department"] else [({}, list(results_list))]
    for dept, tests in groups:
        if body["show_department_heading"] and (dept.get("heading") or dept.get("name")):
            if pdf.will_page_break(22):
                pdf.add_page()
            pdf.put(pdf.l_margin, pdf.get_y() + 1, pdf.W, 5, dept.get("heading") or dept.get("name"), 10, bold=True,
                    align="C")
            pdf.set_y(pdf.get_y() + 7)
        package = None
        for t in tests:
            if body["show_package_heading"] and t.get("package_name") and t["package_name"] != package:
                package = t["package_name"]
                pdf.put(pdf.l_margin, pdf.get_y(), pdf.W, 4.5, package, 9, bold=True, colour=GREY)
                pdf.set_y(pdf.get_y() + 5)
            values = t.get("values") or []
            title = t.get("print_title") or t.get("test_name") or ""
            # keep the title with its table header and at least two rows
            need = 6 + 6 + sum(pdf.row_height(v) for v in values[:2])
            if pdf.will_page_break(need):
                pdf.add_page()
            pdf.put(pdf.l_margin, pdf.get_y(), pdf.W, 5, title, body["font_pt"] + 1.5, bold=True)
            pdf.set_y(pdf.get_y() + 5.5)
            out = t.get("outsourced")
            if out:
                pdf.put(pdf.l_margin, pdf.get_y(), pdf.W, 3.6, "Performed at: " + _site_line(out), 7.5, bold=True)
                pdf.set_y(pdf.get_y() + 4)
            pdf.table_head()
            pdf.table_open = title
            for v in values:
                pdf.value_row(v)
            pdf.table_open = None
            pdf.set_y(pdf.get_y() + 1)
            pdf.paragraph("Interpretation", t.get("interpretation"))
            pdf.paragraph("Notes", t.get("notes"))
            pdf.attachments(t.get("attachments") or [])
            pdf.set_y(pdf.get_y() + 2)
        if layout["signatures"]["placement"] == "per_department":
            pdf.signatures([t.get("signer") for t in tests])
    if layout["signatures"]["placement"] == "end":
        pdf.signatures([t.get("signer") for t in results_list])
    if pdf.will_page_break(12):
        pdf.add_page()
    pdf.put(pdf.l_margin, pdf.get_y() + 2, pdf.W, 4, layout["disclaimer"], 7, colour=GREY, align="C")
    pdf.put(pdf.l_margin, pdf.get_y() + 5, pdf.W, 5, f"** {body['end_marker']} **", 8.5, bold=True, align="C")
    data = bytes(pdf.output())
    return data, hashlib.sha256(data).hexdigest(), pdf.pages_count
