"""Data import engine (MI-02) — pure: reading CSV / XLSX, caps, presets, row validation, keys, formula guard.

Parser vectors per SPEC §7: day-first and ISO dates, Indian phone formats, Devanagari / Telugu names, rupee amounts,
day ranges and slot lists; MocDoc / Practo Insta synthetic exports (tests/fixtures/mi) map through their presets.
"""

import io
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import data_import as mi

FX = Path(__file__).parent / "fixtures" / "mi"


def _grid(text):
    return mi.read_grid(text.encode("utf-8"), "x.csv")


def test_cell_parsers():
    assert mi._date("15/08/1965") == "1965-08-15" and mi._date("2026-03-02") == "2026-03-02"
    assert mi._date("03/04/2024") == "2024-04-03"                       # India: day first
    for bad in ("32/13/2026", "tomorrow"):
        with pytest.raises(mi.RowError):
            mi._date(bad)
    assert mi._phone("+91 98480 22338") == mi._phone("9848022338") == mi._phone("098480-22338")
    with pytest.raises(mi.RowError):
        mi._phone("123")
    assert mi._paise("₹1,23,456.50") == 12345650 and mi._paise("Rs. 500") == 50000
    with pytest.raises(mi.RowError):
        mi._paise("-5")
    assert mi._time("10:30 AM") == "10:30" and mi._time("17:00") == "17:00" and mi._time("5 pm") == "17:00"
    assert mi._days("Mon-Sat") == "Mon,Tue,Wed,Thu,Fri,Sat" and mi._days("fri, mon ,Wed") == "Mon,Wed,Fri"
    with pytest.raises(mi.RowError):
        mi._days("Funday")
    assert mi._slots("09:30, 09:00,9:00 am") == ["09:00", "09:30"]
    assert mi._gst("18%") == 1800 and mi._icd("e11.9") == "E11.9"
    for f, v in ((mi._gst, "7"), (mi._icd, "diabetes"), (mi._code, "a"), (mi._hsn, "12")):
        with pytest.raises(mi.RowError):
            f(v)
    assert mi._code("xray chest") == "XRAY_CHEST" and mi._cats("Bed, Nursing") == ["bed", "nursing"]


def test_caps_and_unreadable_files():
    with pytest.raises(HTTPException) as e:
        mi.read_grid(b"x" * (mi.MAX_BYTES + 1), "big.csv")
    assert e.value.status_code == 413
    with pytest.raises(HTTPException) as e:
        mi.read_grid(("name\n" + "a\n" * (mi.MAX_ROWS + 1)).encode(), "many.csv")
    assert e.value.status_code == 413
    for raw in (b"", b"   ", b"only,header\n"):
        with pytest.raises(HTTPException):
            mi.read_grid(raw, "x.csv")
    with pytest.raises(HTTPException):
        mi.read_grid(b"PK\x03\x04 not really a zip", "x.xlsx")
    # cp1252 from Excel on Windows and ; separators both read
    assert mi.read_grid("Name;Fee\nDr. Rén;500\n".encode("cp1252"), "x.csv")[1] == ["Dr. Rén", "500"]


def test_xlsx_values_only_dates_become_iso():
    import openpyxl
    from datetime import date
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Patient ID", "Visit Date", "Diagnosis"])
    ws.append(["MR-1", date(2026, 3, 2), "=1+1"])                 # a formula cell arrives as its text, never run
    buf = io.BytesIO()
    wb.save(buf)
    grid = mi.read_grid(buf.getvalue(), "h.xlsx")
    assert grid[1][:2] == ["MR-1", "2026-03-02"] and grid[1][2] in ("=1+1", "")


def test_mocdoc_doctor_preset_row_errors_and_duplicates():
    grid = mi.read_grid((FX / "mocdoc_doctors.csv").read_bytes(), "d.csv")
    out = mi.parse_rows("doctors", "mocdoc", grid)
    assert [r["name"] for r in out["rows"]] == ["Dr. Ramesh Rao", "Dr. Sunita Reddy"] and out["duplicates"] == 1
    r = out["rows"][0]
    assert (r["registration_number"], r["consultation_fee"], r["available_days"], r["specialization"]) == (
        "TSMC/2010/1234", 800, "Mon,Tue,Wed,Thu,Fri,Sat", "Cardiologist")
    probs = {(e["row"], e["column"]) for e in out["errors"]}
    assert {(5, "name"), (5, "consultation_fee"), (5, "available_days"), (5, "phone")} <= probs
    assert out["error_rows"] == 1


def test_generic_header_works_without_a_preset_and_missing_columns_refuse():
    out = mi.parse_rows("doctors", "generic", _grid("Doctor,Dept,Fee\nDr. A,ENT,300\n"))
    assert out["rows"][0]["department"] == "ENT"
    with pytest.raises(HTTPException) as e:
        mi.parse_rows("doctors", "generic", _grid("Doctor,Fee\nDr. A,300\n"))
    assert e.value.detail["missing"] == ["department"]
    with pytest.raises(HTTPException) as e:
        mi.parse_rows("history", "generic", _grid("Visit Date,Diagnosis\n01/01/2026,Fever\n"))
    assert "legacy_id or phone" in e.value.detail["missing"]


def test_practo_services_preset():
    out = mi.parse_rows("services", "practo_insta", mi.read_grid((FX / "practo_services.csv").read_bytes(), "s.csv"))
    got = {r["service_code"]: (r["category"], r["standard_price_paise"], r.get("tax_rate_bps")) for r in out["rows"]}
    assert got == {"CONS_GEN": ("consultation", 50000, 0), "ECG": ("diagnostic", 35000, 0),
                   "XRAY_CHEST": ("radiology", 120000, 1800)}
    assert {e["column"] for e in out["errors"] if e["row"] == 5} >= {"name", "standard_price_paise", "hsn_sac",
                                                                    "tax_rate_bps"}


def test_patients_use_the_088_parser_after_the_preset_rename():
    out = mi.parse_rows("patients", "mocdoc", mi.read_grid((FX / "mocdoc_patients.csv").read_bytes(), "p.csv"))
    names = [r["full_name"] for r in out["rows"]]
    assert "రవి కుమార్" in names and "सीता देवी" in names and out["errors"] == []
    lak = next(r for r in out["rows"] if r["full_name"] == "Lakshmi Devi")
    assert (lak["external_id"], lak["date_of_birth"], lak["dedupe_key"]) == ("MR-1001", "1965-08-15", "id:mr-1001")
    assert all("opted_in" not in r and "consent" not in str(r).lower() for r in out["rows"])   # consent never set


def test_row_keys_are_stable_and_scoped_by_kind():
    a = {"legacy_id": "MR-1", "visit_date": "2026-01-01", "diagnosis_text": "Fever", "medications_text": None}
    assert mi._row_key("history", a) == mi._row_key("history", dict(a)) != mi._row_key("history", {**a, "diagnosis_text": "Cough"})
    assert mi._row_key("history", a).startswith("history:")


def test_formula_injection_guard_on_every_export():
    csv_text = mi.errors_csv([{"row_no": 2, "column_name": "name", "problem": "=cmd|' /C calc'!A0"}])
    assert "'=cmd" in csv_text
    for kind in mi.SPECS:
        assert mi.template_csv(kind).strip()                         # every kind has a template


def test_presets_only_map_known_fields():
    for preset, kinds in mi.PRESET_HEADERS.items():
        for kind, cols in kinds.items():
            if kind == "patients":
                continue
            assert set(cols.values()) <= {f for f, *_ in mi.SPECS[kind]}, (preset, kind)
