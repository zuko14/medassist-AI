"""Pure renderer tests (DX-43): the Taiyo HbA1c + Lipid fixture rendered by app/services/dx_pdf.py.

Deterministic bytes (same SHA-256), page count and "Page n of N", the patient block,
department -> test -> 4-column table, method lines, multi-line reference text, flags
(abnormal bold, critical red with *), "(range not applicable)", the mandatory
"Tested at" line on white-label releases (not removable by layout), "Performed at" for
outsourced tests, banners and watermarks, template validation, and the QR encoder.
"""

import copy
import io

import pdfplumber
import pytest

from app.services import dx_qr
from app.services.dx_pdf import ist_text, render_visit_report
from app.services.dx_template_schema import validate_layout
from app.services import dx_sample_report as F


def _text(pdf: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return "\n".join(p.extract_text() or "" for p in doc.pages)


def _words(pdf: bytes) -> list[dict]:
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return [w for p in doc.pages for w in p.extract_words(extra_attrs=["fontname", "size", "non_stroking_color"])]


def test_release_pdf_is_deterministic():
    a = render_visit_report(F.release(), F.results(), F.visit(), {})
    b = render_visit_report(F.release(), F.results(), F.visit(), {})
    assert a[0] == b[0] and a[1] == b[1] and len(a[1]) == 64 and a[2] == 1
    other = render_visit_report(F.release(verify_code="ABCDEFGH23"), F.results(), F.visit(), {})
    assert other[1] != a[1]
    assert a[0].startswith(b"%PDF-") and b"CreationDate (D:20260901013200" in a[0]


def test_taiyo_report_content():
    pdf, _, pages = render_visit_report(F.release(), F.results(), F.visit(), {})
    t = _text(pdf)
    for expected in ("Taiyo Diagnostics", "Mrs. Lakshmi Devi", "58 Y / F", "LAB-2026-000123", "Sep 01, 2026, 07:02 a.m.",
                     "CLINICAL BIOCHEMISTRY", "GLYCOSYLATED HAEMOGLOBIN (HbA1c)", "LIPID PROFILE",
                     "Test description", "Value", "Unit", "Reference range", "Non-diabetic: 4.0 - 6.0 %",
                     "Pre-diabetic: 6.0 - 6.5 %", "Diabetic: > 6.5 %", "168.55", "(range not applicable)",
                     "Method: HPLC", "Prev: 7.9 % (12-Mar-2026)", "Very High > 500", "Dr. K. Anitha",
                     "Reg. No: TSMC/2011/4471 (TSMC)", "END OF REPORT", f"Page 1 of {pages}",
                     "public/dx/verify/Q7RZ2MNP4K", "260100016583"):
        assert expected in t, expected
    assert "Tested at" not in t and "Performed at" not in t and "PARTIAL REPORT" not in t
    words = _words(pdf)
    method = next(w for w in words if w["text"] == "HPLC")
    assert "Oblique" in method["fontname"] and round(method["size"]) == 7          # 7 pt italic
    hb = next(w for w in words if w["text"] == "7.5")
    tg = next(w for w in words if w["text"] == "162")
    tc = next(w for w in words if w["text"] == "134")
    assert "Bold" in hb["fontname"] and "Bold" in tg["fontname"] and "Bold" not in tc["fontname"]


def test_critical_value_is_bold_red_with_star():
    res = copy.deepcopy(F.results())
    res[0]["values"][0].update(value="14.2", flag="critical_high")
    pdf, _, _ = render_visit_report(F.release(), res, F.visit(), {})
    assert "14.2 *" in _text(pdf)
    w = next(w for w in _words(pdf) if w["text"] == "14.2")
    assert "Bold" in w["fontname"] and w["non_stroking_color"][0] > 0.6 and w["non_stroking_color"][1] < 0.2


def test_white_label_keeps_tested_at_line():
    rel = F.release(routing_snapshot={"white_label": True})
    layout = {"header": {"composed": {"name": "City Hospital", "title": "LAB REPORT"}},
              "footer": {"lines": ["24x7 helpline 040 2222 3333"]}}
    pdf, _, _ = render_visit_report(rel, F.results(), F.visit(), layout)
    t = _text(pdf)
    assert "City Hospital" in t and "Taiyo Diagnostics Pvt Ltd" in t
    assert "Tested at: Taiyo Diagnostics Pvt Ltd, Banjara Hills, Hyderabad 500034, NABL: MC-4521" in t
    # no layout key can remove it
    with pytest.raises(ValueError):
        validate_layout({"tested_at": False})
    with pytest.raises(ValueError):
        validate_layout({"footer": {"tested_at": None}})


def test_outsourced_test_prints_performed_at():
    pdf, _, _ = render_visit_report(F.release(), F.results(outsourced_lipid=True), F.visit(), {})
    assert "Performed at: Metro Reference Laboratories LLP, Ameerpet, Hyderabad, NABL: MC-1102" in _text(pdf)


def test_multi_page_repeats_header_and_counts_pages():
    res = copy.deepcopy(F.results()) * 4
    pdf, _, pages = render_visit_report(F.release(), res, F.visit(), {})
    assert pages >= 3
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        texts = [p.extract_text() for p in doc.pages]
    assert len(texts) == pages
    for i, t in enumerate(texts, 1):
        assert "Taiyo Diagnostics" in t and "Mrs. Lakshmi Devi" in t and f"Page {i} of {pages}" in t
    assert any("(continued)" in t for t in texts[1:]) or all("Test description" in t for t in texts)


@pytest.mark.parametrize("mark,text", [("shadow", "SHADOW"), ("internal", "INTERNAL VIEW"), ("superseded", "SUPERSEDED"),
                                       ("partial", "PARTIAL REPORT")])
def test_watermarks(mark, text):
    pdf, sha, _ = render_visit_report(F.release(watermark=mark), F.results(), F.visit(), {})
    with pdfplumber.open(io.BytesIO(pdf)) as doc:                # drawn rotated: read the character stream
        stream = "".join(c["text"] for c in doc.pages[0].chars)
    assert text in stream
    assert sha != render_visit_report(F.release(), F.results(), F.visit(), {})[1]


def test_partial_and_amended_banners():
    assert "PARTIAL REPORT — remaining tests pending" in _text(
        render_visit_report(F.release(kind="partial"), F.results()[:1], F.visit(), {})[0])
    amended = F.release(kind="amended", replaces={"release_no": 1, "created_at": "2026-08-30T05:00:00Z"})
    assert "AMENDED — replaces R1 of 30-Aug-2026" in _text(render_visit_report(amended, F.results(), F.visit(), {})[0])


def test_graph_attachment_is_embedded():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 200), (30, 120, 200)).save(buf, format="PNG")
    res = copy.deepcopy(F.results())
    res[0]["attachments"] = [{"kind": "graph", "data": buf.getvalue(), "caption": "HPLC chromatogram"}]
    pdf, _, _ = render_visit_report(F.release(), res, F.visit(), {})
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        assert sum(len(p.images) for p in doc.pages) == 1
    assert "HPLC chromatogram" in _text(pdf)


def test_template_validation():
    full = validate_layout({})
    assert full["schema"] == 1 and full["verification"]["qr"] is True and full["body"]["font_pt"] == 9
    for bad in ({"header": {"composed": {"accent_hex": "orange"}}}, {"disclaimer": "<b>x</b>"},
                {"header": {"mode": "image"}}, {"body": {"font_pt": 30}}, {"schema": 2}, {"unknown": 1}):
        with pytest.raises(ValueError):
            validate_layout(bad)
    clinic = "11111111-2222-3333-4444-555555555555"
    ok = f"{clinic}/dx-assets/templates/{'a' * 64}.png"
    assert validate_layout({"header": {"mode": "image", "image_asset": ok}}, clinic)["header"]["image_asset"] == ok
    with pytest.raises(ValueError):
        validate_layout({"header": {"mode": "image", "image_asset": ok}}, "99999999-2222-3333-4444-555555555555")


def test_ist_text():
    assert ist_text("2026-09-01T01:32:00Z") == "Sep 01, 2026, 07:02 a.m."
    assert ist_text("2026-09-01T07:00:00Z") == "Sep 01, 2026, 12:30 p.m."
    assert ist_text(None) == ""


# ── QR (verified separately against a browser's native QR decoder, status.md §9) ─

def test_qr_reed_solomon_and_format_vectors():
    # ISO/IEC 18004 worked example: "HELLO WORLD", 1-M
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert dx_qr.rs_ecc(data, 10) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
    assert dx_qr.format_bits(0) == 0b101010000010010


def test_qr_matrix_structure_and_round_trip():
    url = "https://app.kriyaai.in/public/dx/verify/Q7RZ2MNP4K"
    version, cws = dx_qr.codewords(url)
    m = dx_qr.encode(url)
    size = len(m)
    assert size == 17 + 4 * version and version == 4
    for x0, y0 in ((0, 0), (size - 7, 0), (0, size - 7)):                     # finder patterns
        assert all(m[y0][x0 + i] for i in range(7)) and m[y0 + 3][x0 + 3] and not m[y0 + 1][x0 + 1]
    assert m[size - 8][8] is True                                            # dark module
    # read the format bits back, unmask, walk the zigzag: the codewords come back
    fmt = sum(int(m[8][size - 1 - i]) << i for i in range(8)) | sum(int(m[size - 15 + i][8]) << i for i in range(8, 15))
    mask = next(k for k in range(8) if dx_qr.format_bits(k) == fmt)
    _, func = dx_qr._function_grid(version)
    bits = [m[y][x] ^ dx_qr._MASKS[mask](x, y) for x, y in dx_qr.zigzag(size, func)]
    back = [int("".join("1" if b else "0" for b in bits[i:i + 8]), 2) for i in range(0, len(cws) * 8, 8)]
    assert back == cws
    with pytest.raises(ValueError):
        dx_qr.codewords("x" * 200)
