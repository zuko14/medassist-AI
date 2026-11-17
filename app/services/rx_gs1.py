"""GS1 element-string parser for pharma packs (ADVANCED-FEATURES A3, RX-71). Pure, deterministic.

Accepts the scanner forms: raw with FNC1 / GS (0x1D) separators and an optional symbology prefix
(]d2 DataMatrix, ]Q3 QR, ]C1 GS1-128), or the human-readable parentheses form "(01)…(17)…(10)…".
AIs used: 01 GTIN-14, 10 batch, 11 manufacture date, 17 expiry (YYMMDD, day 00 = month end), 21 serial.
Unknown AIs are refused rather than guessed.
"""

import calendar
import re
from datetime import date
from typing import Optional

GS = "\x1d"
_FIXED = {"00": 18, "01": 14, "02": 14, "11": 6, "13": 6, "15": 6, "16": 6, "17": 6}
_VARIABLE = {"10": 20, "21": 20, "240": 30, "30": 8}
_PREFIXES = ("]d2", "]Q3", "]C1", "]e0")


class GS1Error(ValueError):
    pass


def gtin_valid(gtin: str) -> bool:
    """GS1 mod-10 check digit (weights 3,1 from the right, excluding the check digit)."""
    if not re.fullmatch(r"\d{8}|\d{12,14}", gtin or ""):
        return False
    digits = [int(d) for d in gtin[:-1]][::-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(digits))
    return (10 - total % 10) % 10 == int(gtin[-1])


def _date(yymmdd: str, field: str) -> date:
    if not re.fullmatch(r"\d{6}", yymmdd):
        raise GS1Error(f"{field}: expected YYMMDD")
    y, m, d = 2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    if not 1 <= m <= 12:
        raise GS1Error(f"{field}: month {m:02d} is not valid")
    last = calendar.monthrange(y, m)[1]
    if d == 0:
        d = last                                     # GS1: day 00 means the last day of the month
    if d > last:
        raise GS1Error(f"{field}: day {d} is not valid")
    return date(y, m, d)


def _elements(code: str) -> list[tuple[str, str]]:
    if code.startswith("("):
        pairs = re.findall(r"\((\d{2,4})\)([^(]*)", code)
        if not pairs or "".join(f"({a}){v}" for a, v in pairs) != code:
            raise GS1Error("unreadable bracketed GS1 string")
        return [(a, v.strip()) for a, v in pairs]
    out, i = [], 0
    while i < len(code):
        if code[i] == GS:
            i += 1
            continue
        ai = next((a for a in ("240", *_FIXED, *_VARIABLE) if code.startswith(a, i)), None)
        if ai is None:
            raise GS1Error(f"unknown application identifier at position {i + 1}")
        i += len(ai)
        if ai in _FIXED:
            value = code[i:i + _FIXED[ai]]
            if len(value) != _FIXED[ai]:
                raise GS1Error(f"AI {ai}: expected {_FIXED[ai]} characters")
            i += _FIXED[ai]
        else:
            end = code.find(GS, i)
            end = len(code) if end < 0 else end
            value, i = code[i:end], end
            if not value or len(value) > _VARIABLE[ai]:
                raise GS1Error(f"AI {ai}: 1–{_VARIABLE[ai]} characters")
        out.append((ai, value))
    return out


def parse(raw: str) -> dict:
    """→ {gtin, batch_no, expiry_date, mfg_date, serial} (None when absent). GS1Error on bad input."""
    code = (raw or "").strip().replace("\\x1d", GS).replace("<GS>", GS)
    for p in _PREFIXES:
        if code.startswith(p):
            code = code[len(p):]
    code = code.lstrip(GS)
    if not code:
        raise GS1Error("empty scan")
    out: dict[str, Optional[object]] = {"gtin": None, "batch_no": None, "expiry_date": None, "mfg_date": None,
                                        "serial": None}
    for ai, value in _elements(code):
        if ai in ("01", "02"):
            if not gtin_valid(value):
                raise GS1Error("GTIN check digit does not match")
            out["gtin"] = value
        elif ai == "10":
            out["batch_no"] = value
        elif ai == "17":
            out["expiry_date"] = _date(value, "expiry")
        elif ai == "11":
            out["mfg_date"] = _date(value, "manufacture date")
        elif ai == "21":
            out["serial"] = value
    if not out["gtin"]:
        raise GS1Error("no GTIN (01) in the scan")
    return out


def barcode_candidates(gtin: str) -> list[str]:
    """How the same GTIN may be stored in rx_item_barcodes: GTIN-14, EAN-13 (one leading 0), UPC-12 (two)."""
    return [gtin] + ([gtin[1:]] if gtin.startswith("0") else []) + ([gtin[2:]] if gtin.startswith("00") else [])
