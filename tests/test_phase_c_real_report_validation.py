"""Phase C: Real Clinical PDF Report Validation Suite.

Verifies strict fail-closed enforcement across real binary PDF files and streams:
1. Valid structured PDF with headers and matching patient name -> Passes
2. Corrupted PDF bytes -> Fails closed (ValidationError)
3. Image-only / blank PDF without extractable text -> Fails closed (ValidationError)
4. Password-protected / encrypted PDF -> Fails closed (ValidationError)
5. Mismatched patient name in header -> Fails closed (ValidationError)
6. Truncated / partial PDF -> Fails closed (ValidationError)
7. Mismatched document header (e.g. Discharge Summary / Invoice) -> Fails closed (ValidationError)
"""

import pytest
from app.utils.pdf_reader import validate_pdf_report, PDFValidationError, extract_text_from_pdf
from app.integrations.callmedex.api.schemas import PatientIdentity
from app.integrations.callmedex.connectors.mocdoc.connector import MocDocConnector
from app.integrations.callmedex.connectors.cloudlims.connector import CloudLIMSConnector
from app.integrations.callmedex.connectors.crelio.connector import CrelioConnector
from app.integrations.callmedex.api.exceptions import ValidationError


def build_raw_pdf(text: str) -> bytes:
    """Generate a valid spec-compliant PDF byte stream containing specified text."""
    content_stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET"
    content_len = len(content_stream.encode("latin-1"))
    pdf = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
5 0 obj
<< /Length {content_len} >>
stream
{content_stream}
endstream
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000234 00000 n 
0000000307 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
400
%%EOF"""
    return pdf.encode("latin-1")


def build_blank_image_pdf() -> bytes:
    """Generate a valid PDF containing only vector bounding boxes (image placeholder) with NO extractable text."""
    content_stream = "q 10 0 0 10 50 700 cm /Im1 Do Q"
    content_len = len(content_stream.encode("latin-1"))
    pdf = f"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>
endobj
4 0 obj
<< /Length {content_len} >>
stream
{content_stream}
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000210 00000 n 
trailer
<< /Size 5 /Root 1 0 R >>
startxref
320
%%EOF"""
    return pdf.encode("latin-1")


def test_01_valid_structured_lab_report_pdf():
    """Scenario 1: Valid structured PDF with clinical test headers passes."""
    pdf_bytes = build_raw_pdf("Patient Name: Ramesh Patel - Diagnostic Lab Report - Blood Glucose Fasting: 95 mg/dL")
    assert validate_pdf_report(pdf_bytes, expected_patient_name="Ramesh Patel") is True


def test_02_corrupted_pdf_bytes_rejected():
    """Scenario 2: Corrupted binary garbage is rejected fail-closed."""
    corrupted_bytes = b"%PDF-1.4 \x00\xff\xfe\xca\xfe\xba\xbe GARBAGE UNPARSEABLE"
    with pytest.raises(PDFValidationError):
        validate_pdf_report(corrupted_bytes, expected_patient_name="Ramesh Patel")


def test_03_image_only_no_text_pdf_rejected():
    """Scenario 3: Valid PDF without extractable text fails validation."""
    blank_pdf = build_blank_image_pdf()
    with pytest.raises(PDFValidationError) as exc:
        validate_pdf_report(blank_pdf, expected_patient_name="Ramesh Patel")
    assert "no extractable text" in str(exc.value).lower()


def test_04_password_protected_pdf_rejected():
    """Scenario 4: Encrypted / password locked PDF fails extraction and validation."""
    encrypted_pdf = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Filter /Standard /V 2 /R 3 /O (encrypted) /U (encrypted) /P -4 >>
endobj
trailer << /Root 1 0 R >>
%%EOF"""
    with pytest.raises(PDFValidationError):
        validate_pdf_report(encrypted_pdf, expected_patient_name="Ramesh Patel")


def test_05_mismatched_patient_header_rejected():
    """Scenario 5: Report header containing a different patient name is rejected."""
    wrong_patient_pdf = build_raw_pdf("Patient Name: Suresh Kumar - Pathology Lab Report - Hemoglobin 14.2 g/dL")
    with pytest.raises(PDFValidationError) as exc:
        validate_pdf_report(wrong_patient_pdf, expected_patient_name="Ramesh Patel")
    assert "mismatch" in str(exc.value).lower()


def test_06_truncated_pdf_rejected():
    """Scenario 6: Truncated stream missing trailer/EOF fails closed."""
    valid_pdf = build_raw_pdf("Patient Name: Ramesh Patel - Diagnostic Lab Report")
    truncated_pdf = valid_pdf[:len(valid_pdf) // 2]
    with pytest.raises(PDFValidationError):
        validate_pdf_report(truncated_pdf, expected_patient_name="Ramesh Patel")


def test_07_mismatched_document_type_rejected():
    """Scenario 7: Non-lab document (Discharge Summary / Invoice) fails lab report validation."""
    invoice_pdf = build_raw_pdf("Hospital Bill - Invoice / Receipt - Total Amount Paid: Rs 5,000")
    with pytest.raises(PDFValidationError) as exc:
        validate_pdf_report(invoice_pdf, expected_patient_name="Ramesh Patel", expected_doc_type="lab_report")
    assert "mismatched document type" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_connectors_fail_closed_on_unreadable_documents():
    """Connectors (MocDoc, CloudLIMS, Crelio) raise ValidationError on invalid PDFs."""
    patient = PatientIdentity(patient_name="Ramesh Patel", patient_phone="+919876543210", barcode_id="BC123")
    blank_pdf = build_blank_image_pdf()
    corrupt_pdf = b"%PDF-1.4\x00\x00\x00corrupt"

    mocdoc = MocDocConnector()
    cloudlims = CloudLIMSConnector()
    crelio = CrelioConnector()

    for connector in [mocdoc, cloudlims, crelio]:
        with pytest.raises(ValidationError):
            await connector.validate_report(blank_pdf, patient)

        with pytest.raises(ValidationError):
            await connector.validate_report(corrupt_pdf, patient)
