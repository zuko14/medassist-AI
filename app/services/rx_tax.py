"""Pharmacy money and quantity rules in Python — exact mirrors of the SQL in migration 138 (RX-30).

The database is the authority (rx_sale_line_calc prices every line); this module lets the API
preview totals and pre-fill quantities without a round trip, and is property-tested against SQL
(tests/test_rx_tax.py). Integer paise, half-up, no floats (NFR-5, ADR-RX-08).
"""

import math
import re
from decimal import Decimal
from typing import Optional

_FREQ = {"OD": 1, "QD": 1, "HS": 1, "BD": 2, "BID": 2, "TDS": 3, "TID": 3, "QID": 4,
         "1-0-0": 1, "0-1-0": 1, "0-0-1": 1, "1-0-1": 2, "1-1-0": 2, "0-1-1": 2, "1-1-1": 3, "1-1-1-1": 4}


def gst_split_inclusive(gross_paise: int, rate_bps: int, has_gstin: bool = True) -> tuple[int, int, int]:
    """(taxable, cgst, sgst) of a GST-inclusive amount. No GSTIN → bill of supply, no tax."""
    if not has_gstin or rate_bps == 0:
        return gross_paise, 0, 0
    taxable = (gross_paise * 10000 + (10000 + rate_bps) // 2) // (10000 + rate_bps)
    tax = gross_paise - taxable
    return taxable, tax // 2, tax - tax // 2


def calculate_loose_unit_price(mrp_pack_paise: int, pack_size: int, units: int) -> int:
    """Price of `units` base units at MRP: round_half_up(mrp_pack × units / pack_size)."""
    return (mrp_pack_paise * units + pack_size // 2) // pack_size


unit_price = calculate_loose_unit_price


def prescribed_units(dosage: Optional[str], frequency: Optional[str], duration_days: Optional[int],
                     formulation: Optional[str]) -> Optional[int]:
    """Units an OPD Rx line asks for, from the CLOSED frequency map only; None when not computable
    (SOS / STAT / free text / liquids). Never guesses (ADR-RX-06)."""
    if not duration_days or formulation not in ("tablet", "capsule"):
        return None
    freq = _FREQ.get((frequency or "").strip().upper())
    if freq is None:
        return None
    text = (dosage or "").strip()
    m = re.match(r"^(\d+)\s*/\s*(\d+)", text)
    if m and int(m.group(2)) > 0:
        dose = Decimal(m.group(1)) / Decimal(m.group(2))
    else:
        m = re.match(r"^(\d+(\.\d+)?)", text)
        if not m:
            return None
        dose = Decimal(m.group(1))
    if dose <= 0 or dose > 20:
        return None
    return math.ceil(dose * freq * duration_days)


def round_off_to_rupee(amount_paise: int) -> int:
    """Round-off that brings an amount to the nearest rupee, half-up (−49 … +50)."""
    return ((amount_paise + 50) // 100) * 100 - amount_paise


def spread(total: int, weights: list[int]) -> list[int]:
    """Split `total` over `weights` proportionally, largest remainder, ties by position (as rx_sale_refresh)."""
    whole = sum(weights)
    if not whole:
        return [0] * len(weights)
    base = [total * w // whole for w in weights]
    rem = sorted(range(len(weights)), key=lambda i: (-(total * weights[i] % whole), i))
    for i in rem[:total - sum(base)]:
        base[i] += 1
    return base


def price_lines(lines: list[dict], discount_paise: int = 0, has_gstin: bool = True, round_off: bool = True) -> dict:
    """Preview a bill: lines [{mrp_pack_paise, pack_size, qty_units, tax_rate_bps}] → per-line and header totals,
    exactly as the database will compute them."""
    gross = [calculate_loose_unit_price(ln["mrp_pack_paise"], ln["pack_size"], ln["qty_units"]) for ln in lines]
    disc = min(discount_paise, sum(gross))
    shares = spread(disc, gross)
    out, taxable, cgst, sgst = [], 0, 0, 0
    for ln, g, d in zip(lines, gross, shares):
        t, c, s = gst_split_inclusive(g - d, ln["tax_rate_bps"], has_gstin)
        out.append({"line_gross_paise": g, "line_discount_paise": d, "taxable_paise": t, "cgst_paise": c, "sgst_paise": s})
        taxable, cgst, sgst = taxable + t, cgst + c, sgst + s
    ro = round_off_to_rupee(sum(gross) - disc) if round_off else 0
    return {"lines": out, "gross_paise": sum(gross), "discount_paise": disc, "round_off_paise": ro,
            "net_paise": sum(gross) - disc + ro, "taxable_paise": taxable, "cgst_paise": cgst, "sgst_paise": sgst}
