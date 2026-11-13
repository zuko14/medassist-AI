"""DX-27: Code128 encoder and tube labels (pure, app/services/dx_barcode.py)."""

import pytest

from app.services import dx_barcode as b


def _decode(mods: str) -> list[int]:
    """Module string -> symbol values (inverse of the width table), to prove the table round-trips."""
    runs, cur, n = [], mods[0], 0
    for ch in mods:
        if ch == cur:
            n += 1
        else:
            runs.append(n)
            cur, n = ch, 1
    runs.append(n)
    inverse = {p: v for v, p in enumerate(b.PATTERNS)}
    out = []
    while runs:
        size = 7 if len(runs) == 7 else 6
        out.append(inverse["".join(map(str, runs[:size]))])
        runs = runs[size:]
    return out


def test_width_table_is_a_valid_code128_set():
    assert len(b.PATTERNS) == 107 and len(set(b.PATTERNS)) == 107
    assert all(sum(map(int, p)) == 11 for p in b.PATTERNS[:106]) and sum(map(int, b.PATTERNS[106])) == 13
    # published module patterns of the start / stop characters
    assert b.modules("A")[:11] == "11010010000"                       # Start B
    assert b.modules("12")[:11] == "11010011100"                      # Start C
    assert b.modules("12")[-13:] == "1100011101011"                   # Stop + termination bar


@pytest.mark.parametrize("data,expected_check", [
    ("PJJ123C", 55),          # Code128-B worked example: 104+48+84+126+68+90+114+245 = 879 ≡ 55 (mod 103)
    ("1234", 82),             # Code128-C: 105 + 12·1 + 34·2 = 185 ≡ 82
    ("260100016583", (105 + 26 + 2 * 1 + 3 * 0 + 4 * 1 + 5 * 65 + 6 * 83) % 103),
])
def test_check_symbol_vectors(data, expected_check):
    assert b.encode(data)[-2] == expected_check


def test_symbology_and_full_pattern_of_1234():
    assert b.symbology("260100016583") == "C" and b.symbology("12345") == "B" and b.symbology("MD16583") == "B"
    # Start C | 12 | 34 | check 82 | Stop
    assert b.modules("1234") == "11010011100" "10110011100" "10001011000" "10010011110" "1100011101011"


@pytest.mark.parametrize("data", ["260100016583", "260700016570", "MD-16583", "ab/c.9", "Hello World 128"])
def test_round_trip(data):
    vals = b.encode(data)
    assert _decode(b.modules(data)) == vals
    assert b.check_value(vals[:-2]) == vals[-2]


def test_twelve_digit_sample_id_is_101_modules_about_25mm():
    assert len(b.modules("260100016583")) == 101
    assert abs(101 * b.MODULE_MM - 25.25) < 1e-9


def test_refusals():
    with pytest.raises(ValueError):
        b.encode("")
    with pytest.raises(ValueError):
        b.encode("Ünïcode")


def test_text_helpers():
    assert b.short_codes(["CBC", "HBA1C", "LIPID"]) == "CBC HBA1C LIPID"
    assert b.short_codes(["HBSAG", "HIV", "LIPID", "TSH", "FT4", "GLU_F", "HBA1C"]) == "HBSAG HIV LIPID TSH FT4 +2"
    assert len(b.short_codes([f"TEST{i:02d}" for i in range(20)])) <= 30
    assert b.short_codes(["X" * 40, "CBC"]) == "X" * 27 + " +1"
    assert b.short_codes([]) == "" and b.short_codes([None, " "]) == ""
    assert b.truncate("  Asha   Kumari Venkataraman Reddy ", 22) == "Asha Kumari Venkataram"
    assert b.age_sex(34, "female") == "34Y/F" and b.age_sex(None, "male") == "M" and b.age_sex(None, None) == ""


LABEL = {"barcode": "260100016583", "patient_name": "Asha Kumari", "age_years": 34, "gender": "female",
         "accession_number": "LAB-2026-000123", "test_codes": ["HBSAG", "HIV"], "colour_hex": "#C0392B",
         "site_code": "KPHB"}
REGISTER = {"accession_number": "LAB-2026-000123", "patient_name": "Asha Kumari", "age_years": 34, "gender": "female",
            "test_codes": ["HBSAG", "HIV"], "date_text": "13 Nov 2026"}


def test_pdf_one_page_per_label_and_copy_plus_register_and_deterministic():
    pdf = b.render_labels_pdf([LABEL, {**LABEL, "barcode": "260400016583", "colour_hex": "bad"}], REGISTER, copies=2)
    assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") >= 1
    assert pdf.count(b"/MediaBox") >= 5 or b"/Count 5" in pdf
    assert b.render_labels_pdf([LABEL], REGISTER) == b.render_labels_pdf([LABEL], REGISTER)
    with pytest.raises(ValueError):
        b.render_labels_pdf([], None)


def test_zpl_label_and_injection_safety():
    z = b.render_labels_zpl([{**LABEL, "patient_name": "Evil^XZ~JR name"}], REGISTER, copies=3)
    assert z.count("^XA") == 2 and z.count("^XZ") == 2
    assert "^BCN,64,Y,N,N^FD>;260100016583^FS" in z and "^PQ3" in z
    assert "Evil XZ JR name" in z and "Evil^XZ" not in z
    assert ">:MD16583" in b.zpl_sample_label({**LABEL, "barcode": "MD16583"})
    with pytest.raises(ValueError):
        b.zpl_sample_label({**LABEL, "barcode": "bad code^"})
