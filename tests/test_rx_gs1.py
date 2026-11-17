"""GS1 / pharma QR parser vectors (ADVANCED-FEATURES A3, RX-71). Pure: no database."""

from datetime import date

import pytest

from app.services.rx_gs1 import GS, GS1Error, barcode_candidates, gtin_valid, parse

GTIN = "08901234567890"          # valid mod-10 check digit


@pytest.mark.parametrize("code", [
    f"]d201{GTIN}17271231" + "10AB12" + GS + "21XYZ9",               # DataMatrix prefix, GS after a variable AI
    f"01{GTIN}10AB12{GS}17271231{GS}21XYZ9",                          # no prefix, stray GS after fixed AI
    f"]Q301{GTIN}21XYZ9{GS}10AB12{GS}17271231",                       # QR prefix, any order
    f"(01){GTIN}(17)271231(10)AB12(21)XYZ9",                          # human-readable form
    f"01{GTIN}17271231" + "10AB12<GS>21XYZ9",                         # keyboard-wedge scanners that print <GS>
])
def test_forms_parse_to_the_same_fields(code):
    assert parse(code) == {"gtin": GTIN, "batch_no": "AB12", "expiry_date": date(2027, 12, 31), "mfg_date": None,
                           "serial": "XYZ9"}


def test_day_zero_is_month_end_and_mfg_date():
    assert parse(f"(01){GTIN}(17)280200(11)260115")["expiry_date"] == date(2028, 2, 29)
    assert parse(f"(01){GTIN}(17)270400(11)260115")["mfg_date"] == date(2026, 1, 15)


@pytest.mark.parametrize("code,msg", [
    ("", "empty"),
    (f"(01){GTIN[:-1]}1(17)271231", "check digit"),
    ("(10)AB12(17)271231", "no GTIN"),
    (f"(01){GTIN}(17)271331", "month"),
    (f"(01){GTIN}(17)270231", "day"),
    (f"01{GTIN}99ABC", "unknown application identifier"),
    (f"01{GTIN}17271", "expected 6"),
    (f"01{GTIN}10" + "X" * 21, "1–20"),
    ("(01)abc", "check digit"),
])
def test_bad_scans_are_refused(code, msg):
    with pytest.raises(GS1Error, match=msg):
        parse(code)


def test_gtin_check_and_stored_forms():
    assert gtin_valid(GTIN) and gtin_valid("8901234567890") and not gtin_valid("8901234567891")
    assert gtin_valid("96385074") and not gtin_valid("123")
    assert barcode_candidates(GTIN) == [GTIN, "8901234567890"]
    assert barcode_candidates("00012345678905") == ["00012345678905", "0012345678905", "012345678905"]
