"""Helper script to generate real PDF test fixtures for CallMeDex OCR pipeline testing."""

import os

FIXTURES_DIR = os.path.dirname(__file__)

def make_pdf_bytes(stream_text: str) -> bytes:
    stream_len = len(stream_text)
    pdf_text = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length {stream_len} >>
stream
{stream_text}
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000242 00000 n 
0000000300 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
380
%%EOF"""
    return pdf_text.encode("latin-1")

def generate_fixtures():
    os.makedirs(FIXTURES_DIR, exist_ok=True)

    # 1. Native text lab report
    native_stream = """BT
/F1 12 Tf
14 TL
50 750 Td
(LABORATORY TEST REPORT) Tj T*
(Hemoglobin 13.6 g/dL 13.0-17.0) Tj T*
(White Blood Cell Count 7500 /uL 4000-11000) Tj T*
(Platelet Count 250000 /uL 150000-450000) Tj T*
(Serum Creatinine 0.9 mg/dL 0.6-1.2) Tj T*
(Mantoux Test 10 mm 0-5) Tj T*
ET"""
    native_pdf = make_pdf_bytes(native_stream)
    with open(os.path.join(FIXTURES_DIR, "native_text_report.pdf"), "wb") as f:
        f.write(native_pdf)

    # 2. Corrupted PDF
    with open(os.path.join(FIXTURES_DIR, "corrupted_report.pdf"), "wb") as f:
        f.write(b"CORRUPTED_NON_PDF_HEADER_BYTES")

if __name__ == "__main__":
    generate_fixtures()
