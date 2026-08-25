import io
import logging
from typing import Optional
import pdfplumber

logger = logging.getLogger(__name__)


class PDFValidationError(Exception):
    """Raised when a PDF fails strict clinical validation."""
    pass


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract all text from a PDF file given as bytes. Raises on corruption or returns text."""
    if not file_bytes:
        raise PDFValidationError("PDF file is empty (0 bytes)")

    if len(file_bytes) < 32 or not file_bytes.startswith(b"%PDF"):
        raise PDFValidationError("File signature invalid (%PDF magic header missing)")

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            if not pdf.pages:
                raise PDFValidationError("PDF contains no pages")
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            return text.strip()
    except Exception as e:
        if isinstance(e, PDFValidationError):
            raise
        logger.warning(f"PDF extraction error ({type(e).__name__}: {e})")
        raise PDFValidationError(f"PDF extraction failed ({type(e).__name__}: {e})") from e


def validate_pdf_report(
    file_bytes: bytes,
    expected_patient_name: Optional[str] = None,
    expected_doc_type: str = "lab_report",
) -> bool:
    """Strict fail-closed validation for clinical PDF reports.

    Enforces:
    1. Valid magic header and minimum size.
    2. Successful PDF structure parsing without corruption/password locks.
    3. Non-empty extracted text (rejects blank/unscanned/image-only PDFs).
    4. Expected patient name verification (rejects cross-patient mixups).
    5. Document header consistency (rejects mismatched document types).
    """
    extracted_text = extract_text_from_pdf(file_bytes)
    if not extracted_text or len(extracted_text.strip()) < 10:
        raise PDFValidationError("Unreadable PDF: No extractable text found in document")

    text_lower = extracted_text.lower()

    # Reject invalid document type headers when expecting lab reports
    if expected_doc_type == "lab_report":
        invalid_type_markers = ["discharge summary", "invoice / receipt", "admission slip", "prescription only"]
        for marker in invalid_type_markers:
            if marker in text_lower and not any(kw in text_lower for kw in ["lab", "test", "investigation", "pathology", "diagnostic", "blood"]):
                raise PDFValidationError(f"Mismatched document type: detected '{marker}' instead of lab report")

    # Patient identity verification
    if expected_patient_name:
        name_tokens = [t.lower() for t in expected_patient_name.split() if len(t) > 2]
        if name_tokens:
            for line in text_lower.splitlines():
                clean_line = line.strip()
                if clean_line.startswith(("patient name:", "patient:", "name:", "patient name :")):
                    if not any(token in clean_line for token in name_tokens):
                        raise PDFValidationError(
                            f"Report patient header mismatch: expected '{expected_patient_name}', got '{clean_line}'"
                        )

    return True
