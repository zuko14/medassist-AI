"""Kriya Diagnostic OS — Code128 barcodes and tube labels (DX-27, Lane PURE).

Pure: no database, no network, no clock. Deterministic bytes for the same input.
Design: docs/diagnostic-os/REPORT-TEMPLATES.md §6, AUDIT A7 / A28.

* Code128-C for an all-numeric even-length value (the 12-digit `YY cc SSSSSSSS`
  sample ID → 101 modules ≈ 25 mm at 0.25 mm/module); Code128-B for anything
  else (pre-printed alphanumeric stock). Check symbol = mod-103 weighted sum.
* 50 × 25 mm label PDF (fpdf2, bars drawn from the module string), ZPL for
  Zebra / TSC printers, and the register-book label (PDF and ZPL).

Zero-LLM (tests/test_dx_no_llm.py).
"""

import os
import re
import warnings
from datetime import datetime, timezone
from typing import Iterable, Optional

warnings.filterwarnings("ignore", category=UserWarning, module="fpdf")

from fpdf import FPDF  # noqa: E402

# Code128 symbol widths (bar, space, bar, space, bar, space); 103–105 = Start A/B/C, 106 = Stop (+ final bar).
PATTERNS = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
    "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
    "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
)
START_B, START_C, STOP = 104, 105, 106
MODULE_MM = 0.25            # X-dimension (REPORT-TEMPLATES §6)
QUIET_MODULES = 10          # quiet zone each side (ISO/IEC 15417 minimum)
LABEL_W, LABEL_H = 50.0, 25.0
NAME_CHARS, TESTS_CHARS = 22, 30
FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts")


# ── Code128 ──────────────────────────────────────────────────────────────────

def symbology(data: str) -> str:
    """'C' for all-numeric even length, else 'B'."""
    return "C" if data.isdigit() and len(data) % 2 == 0 and data else "B"


def check_value(values: list[int]) -> int:
    """Mod-103: start value + Σ position × value, positions from 1."""
    return (values[0] + sum(i * v for i, v in enumerate(values[1:], start=1))) % 103


def encode(data: str) -> list[int]:
    """Symbol values: start, data, check, stop."""
    if not data:
        raise ValueError("empty barcode")
    if symbology(data) == "C":
        values = [START_C] + [int(data[i:i + 2]) for i in range(0, len(data), 2)]
    else:
        if any(not (32 <= ord(ch) <= 126) for ch in data):
            raise ValueError("Code128-B takes printable ASCII only")
        values = [START_B] + [ord(ch) - 32 for ch in data]
    return values + [check_value(values), STOP]


def modules(data: str) -> str:
    """'1' = bar module, '0' = space module (no quiet zone)."""
    out = []
    for v in encode(data):
        for i, w in enumerate(PATTERNS[v]):
            out.append(("1" if i % 2 == 0 else "0") * int(w))
    return "".join(out)


# ── label text helpers ───────────────────────────────────────────────────────

def truncate(text: Optional[str], limit: int) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())[:limit].rstrip()


def short_codes(codes: Iterable[str], limit: int = TESTS_CHARS) -> str:
    """'CBC HBA1C LIPID' or, when it does not fit, as many as fit + '+N'."""
    codes = [c for c in (str(x or "").strip() for x in codes) if c]
    for k in range(len(codes), 0, -1):
        rest = len(codes) - k
        text = " ".join(codes[:k]) + (f" +{rest}" if rest else "")
        if len(text) <= limit:
            return text
    if not codes:
        return ""
    tail = f" +{len(codes) - 1}" if len(codes) > 1 else ""
    return codes[0][:limit - len(tail)] + tail


def age_sex(age_years, gender: Optional[str]) -> str:
    sex = {"male": "M", "female": "F", "other": "O"}.get((gender or "").lower(), "")
    age = f"{age_years}Y" if age_years not in (None, "") else ""
    return "/".join(x for x in (age, sex) if x)


def _hex_rgb(colour: Optional[str]) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", (colour or "").strip())
    if not m:
        return (255, 255, 255)
    h = m.group(1)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ── PDF ──────────────────────────────────────────────────────────────────────

def _pdf() -> FPDF:
    pdf = FPDF(orientation="L", unit="mm", format=(LABEL_H, LABEL_W))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    pdf.add_font("Noto", "", os.path.join(FONTS_DIR, "NotoSans-Regular.ttf"))
    pdf.add_font("Noto", "B", os.path.join(FONTS_DIR, "NotoSans-Bold.ttf"))
    pdf.set_creation_date(datetime(2000, 1, 1, tzinfo=timezone.utc))   # same input → same bytes
    return pdf


def _bars(pdf: FPDF, data: str, x: float, y: float, h: float, max_w: float) -> float:
    """Draw the bars; shrink the module (never below 0.17 mm) when the code is wider than max_w."""
    mods = modules(data)
    m = min(MODULE_MM, max_w / len(mods))
    pdf.set_fill_color(0, 0, 0)
    run_start = None
    for i, bit in enumerate(mods + "0"):
        if bit == "1" and run_start is None:
            run_start = i
        elif bit == "0" and run_start is not None:
            pdf.rect(x + run_start * m, y, (i - run_start) * m, h, style="F")
            run_start = None
    return len(mods) * m


def _text(pdf: FPDF, x: float, y: float, w: float, size: float, text: str, bold: bool = False, align: str = "L"):
    pdf.set_font("Noto", "B" if bold else "", size)
    pdf.set_xy(x, y)
    pdf.cell(w, size * 0.42, text, align=align)


def _sample_label(pdf: FPDF, lab: dict) -> None:
    """One tube label. lab: barcode, patient_name, age_years, gender, accession_number, test_codes,
    colour_hex, site_code."""
    pdf.add_page()
    pdf.set_fill_color(*_hex_rgb(lab.get("colour_hex")))
    pdf.rect(0, 0, 3, LABEL_H, style="F")                      # tube colour bar
    pdf.set_draw_color(0, 0, 0)
    pdf.rect(0, 0, 3, LABEL_H)
    x0, w = 4.5, LABEL_W - 6
    _text(pdf, x0, 1.0, w - 10, 7, truncate(lab.get("patient_name"), NAME_CHARS), bold=True)
    _text(pdf, x0 + w - 10, 1.0, 10, 6.5, age_sex(lab.get("age_years"), lab.get("gender")), align="R")
    bw = _bars(pdf, lab["barcode"], x0 + 1.5, 4.6, 9.0, w - 3)
    _text(pdf, x0 + 1.5, 13.9, bw, 6.5, lab["barcode"], align="C")
    _text(pdf, x0, 17.2, w - 9, 6, str(lab.get("accession_number") or ""), bold=True)
    _text(pdf, x0 + w - 9, 17.2, 9, 6, str(lab.get("site_code") or ""), bold=True, align="R")
    _text(pdf, x0, 20.6, w, 6, short_codes(lab.get("test_codes") or []))


def _register_label(pdf: FPDF, reg: dict) -> None:
    """Paper lab-register label: visit no., name, age/sex, tests, date."""
    pdf.add_page()
    x0, w = 2.0, LABEL_W - 4
    _text(pdf, x0, 1.2, w, 8, str(reg.get("accession_number") or ""), bold=True)
    _text(pdf, x0, 5.6, w - 12, 7, truncate(reg.get("patient_name"), NAME_CHARS), bold=True)
    _text(pdf, x0 + w - 12, 5.6, 12, 7, age_sex(reg.get("age_years"), reg.get("gender")), align="R")
    _text(pdf, x0, 11.0, w, 6.5, short_codes(reg.get("test_codes") or [], 34))
    _text(pdf, x0, 19.6, w, 6.5, str(reg.get("date_text") or ""))


def render_labels_pdf(labels: list[dict], register: Optional[dict] = None, copies: int = 1) -> bytes:
    """One page per label (× copies), then the register-book label when given."""
    if not labels and not register:
        raise ValueError("nothing to print")
    pdf = _pdf()
    for lab in labels:
        for _ in range(max(1, copies)):
            _sample_label(pdf, lab)
    if register:
        _register_label(pdf, register)
    return bytes(pdf.output())


# ── ZPL ──────────────────────────────────────────────────────────────────────

DOTS_PER_MM = 8             # 203 dpi


def _zf(text: Optional[str]) -> str:
    """ZPL field text: no control characters (^ ~), single line."""
    return re.sub(r"[\^~\r\n\t]", " ", str(text or "")).strip()


def _zpl_open() -> list[str]:
    return ["^XA", "^CI28", f"^PW{int(LABEL_W * DOTS_PER_MM)}", f"^LL{int(LABEL_H * DOTS_PER_MM)}", "^LH0,0"]


def zpl_sample_label(lab: dict, copies: int = 1) -> str:
    code = lab["barcode"]
    if not re.fullmatch(r"[A-Za-z0-9._/-]{3,64}", code):
        raise ValueError("barcode not printable")
    start = ">;" if symbology(code) == "C" else ">:"          # ZPL invocation: Start C / Start B
    lines = _zpl_open() + [
        "^FO0,0^GB24,200,24^FS",                                  # tube band (thermal: solid black)
        f"^FO36,8^A0N,22,20^FD{_zf(truncate(lab.get('patient_name'), NAME_CHARS))}^FS",
        f"^FO300,8^A0N,20,18^FB92,1,0,R^FD{_zf(age_sex(lab.get('age_years'), lab.get('gender')))}^FS",
        f"^FO48,36^BY2^BCN,64,Y,N,N^FD{start}{code}^FS",
        f"^FO36,136^A0N,20,18^FD{_zf(lab.get('accession_number'))}^FS",
        f"^FO300,136^A0N,20,18^FB92,1,0,R^FD{_zf(lab.get('site_code'))}^FS",
        f"^FO36,164^A0N,20,18^FD{_zf(short_codes(lab.get('test_codes') or []))}^FS",
        f"^PQ{max(1, copies)}",
        "^XZ",
    ]
    return "\n".join(lines)


def zpl_register_label(reg: dict) -> str:
    return "\n".join(_zpl_open() + [
        f"^FO16,10^A0N,30,26^FD{_zf(reg.get('accession_number'))}^FS",
        f"^FO16,50^A0N,24,22^FD{_zf(truncate(reg.get('patient_name'), NAME_CHARS))}^FS",
        f"^FO290,50^A0N,22,20^FB100,1,0,R^FD{_zf(age_sex(reg.get('age_years'), reg.get('gender')))}^FS",
        f"^FO16,88^A0N,22,20^FB368,2,4,L^FD{_zf(short_codes(reg.get('test_codes') or [], 60))}^FS",
        f"^FO16,160^A0N,22,20^FD{_zf(reg.get('date_text'))}^FS",
        "^XZ",
    ])


def render_labels_zpl(labels: list[dict], register: Optional[dict] = None, copies: int = 1) -> str:
    if not labels and not register:
        raise ValueError("nothing to print")
    parts = [zpl_sample_label(lab, copies) for lab in labels]
    if register:
        parts.append(zpl_register_label(register))
    return "\n".join(parts) + "\n"
