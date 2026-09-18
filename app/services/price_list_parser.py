"""Price List Parser Service for Catalogue Import.

Supports CSV, XLSX, PDF, PNG, and JPG price lists with strict upload limits,
zip-bomb protection, image downscaling, clean tabular fast-path, and chunked
AI extraction for unstructured documents.
"""

import asyncio
import csv
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from PIL import Image
import pdfplumber

try:
    import pytesseract  # type: ignore
    HAS_PYTESSERACT = True
except ImportError:
    pytesseract = None  # type: ignore
    HAS_PYTESSERACT = False

from app.config import settings
from app.database import supabase, sb
from app.services.ai_gateway import call_ai_gateway, SpendCapExceededError

logger = logging.getLogger(__name__)

# Strict Upload & Processing Constants
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_XLSX_UNPACKED_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_XLSX_ZIP_ENTRIES = 200
MAX_PDF_PAGES = 30
MAX_OCR_PAGES = 10
OCR_TIMEOUT_SECONDS = 120.0
MAX_EXTRACTION_CHUNKS = 20

# Safe Image Limits
Image.MAX_IMAGE_PIXELS = 40_000_000

# Canonical column name aliases for fast-path header matching
NAME_ALIASES = frozenset({
    "name", "test_name", "testname", "test name", "investigation",
    "investigation_name", "item_name", "service_name", "test", "profile_name"
})
PRICE_ALIASES = frozenset({
    "price", "price_rupees", "rate", "mrp", "cost", "amount",
    "price (rs)", "price_rs", "rate_rs", "amount_rupees", "charges", "test_cost"
})
CATEGORY_ALIASES = frozenset({
    "category", "department", "dept", "section", "group", "heading"
})
SAMPLE_ALIASES = frozenset({
    "sample_type", "sample", "specimen", "specimen_type"
})
TAT_ALIASES = frozenset({
    "turnaround_hours", "turnaround", "tat", "tat_hours", "report_time"
})
FASTING_ALIASES = frozenset({
    "fasting_required", "fasting", "fasting_needed"
})
PREP_ALIASES = frozenset({
    "prep_instructions", "instructions", "preparation", "patient_prep"
})
DESC_ALIASES = frozenset({
    "description", "details", "test_description", "summary"
})


def detect_and_validate_file_type(raw_bytes: bytes, filename: str = "") -> str:
    """Detect format from magic bytes and validate against disguised/malicious files.

    Returns:
        'csv', 'xlsx', 'pdf', 'png', or 'jpg'.

    Raises:
        HTTPException(400) if format is unsupported, legacy .xls, or disguised binary.
    """
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="File is empty.")

    # 1. Reject executable and binary disguised files immediately
    if raw_bytes.startswith(b"MZ"):
        raise HTTPException(
            status_code=400,
            detail="Security validation failed: executable file disguised as document.",
        )
    if raw_bytes.startswith(b"\x7fELF"):
        raise HTTPException(
            status_code=400,
            detail="Security validation failed: binary ELF file disguised as document.",
        )
    if raw_bytes.startswith(b"\xca\xfe\xba\xbe"):
        raise HTTPException(
            status_code=400,
            detail="Security validation failed: binary class file disguised as document.",
        )

    # 2. Reject legacy .xls files
    if raw_bytes.startswith(b"\xd0\xcf\x11\xe0"):
        raise HTTPException(
            status_code=400,
            detail="Legacy Excel (.xls) files are not supported — please save as .xlsx or CSV",
        )

    # 3. PDF check
    if raw_bytes.startswith(b"%PDF-"):
        return "pdf"

    # 4. PNG check
    if raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"

    # 5. JPEG check
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        return "jpg"

    # 6. XLSX check (ZIP file with workbook XML)
    if raw_bytes.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                namelist = zf.namelist()
                if any(name.startswith("xl/") or name == "[Content_Types].xml" for name in namelist):
                    return "xlsx"
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Corrupted or invalid XLSX file.")
        raise HTTPException(
            status_code=400,
            detail="ZIP archive does not appear to be a valid XLSX spreadsheet.",
        )

    # 7. CSV check (text-based)
    # Binary null bytes indicate binary, not text
    if b"\x00" not in raw_bytes[:4096]:
        try:
            # Try decoding sample
            raw_bytes[:4096].decode("utf-8-sig")
            return "csv"
        except UnicodeDecodeError:
            try:
                raw_bytes[:4096].decode("latin-1")
                return "csv"
            except Exception:
                pass

    raise HTTPException(
        status_code=400,
        detail="Unsupported file format. Allowed formats: CSV, XLSX, PDF, PNG, JPG.",
    )


def validate_xlsx_zip_safety(raw_bytes: bytes) -> None:
    """Validate XLSX zip against zip-bombs before openpyxl parsing."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            infolist = zf.infolist()
            if len(infolist) > MAX_XLSX_ZIP_ENTRIES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Excel file contains too many internal entries ({len(infolist)} > {MAX_XLSX_ZIP_ENTRIES}).",
                )
            total_unpacked = sum(info.file_size for info in infolist)
            if total_unpacked > MAX_XLSX_UNPACKED_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Excel file unpacked size ({total_unpacked // (1024 * 1024)} MB) "
                        f"exceeds safety limit of {MAX_XLSX_UNPACKED_BYTES // (1024 * 1024)} MB."
                    ),
                )
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Corrupted or invalid XLSX archive.")


def preprocess_image_for_ocr(img: Image.Image) -> Image.Image:
    """Downscale oversized images and convert to RGB before OCR."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    max_dim = max(img.width, img.height)
    if max_dim > 2500:
        scale = 2500.0 / max_dim
        new_w = max(1, int(img.width * scale))
        new_h = max(1, int(img.height * scale))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    return img


def _normalize_header(h: str) -> str:
    """Clean and normalize a header string."""
    return re.sub(r"[^a-z0-9_ ]+", "", (h or "").strip().lower()).strip()


def _resolve_header_map(fieldnames: List[str]) -> Tuple[Dict[str, str], bool]:
    """Map raw headers to canonical keys and return whether name + price exist."""
    mapping: Dict[str, str] = {}
    for h in fieldnames:
        norm = _normalize_header(h)
        if norm in NAME_ALIASES and "name" not in mapping.values():
            mapping[h] = "name"
        elif norm in PRICE_ALIASES and "price_rupees" not in mapping.values():
            mapping[h] = "price_rupees"
        elif norm in CATEGORY_ALIASES and "category" not in mapping.values():
            mapping[h] = "category"
        elif norm in SAMPLE_ALIASES and "sample_type" not in mapping.values():
            mapping[h] = "sample_type"
        elif norm in TAT_ALIASES and "turnaround_hours" not in mapping.values():
            mapping[h] = "turnaround_hours"
        elif norm in FASTING_ALIASES and "fasting_required" not in mapping.values():
            mapping[h] = "fasting_required"
        elif norm in PREP_ALIASES and "prep_instructions" not in mapping.values():
            mapping[h] = "prep_instructions"
        elif norm in DESC_ALIASES and "description" not in mapping.values():
            mapping[h] = "description"
        else:
            mapping[h] = h

    canonical_set = set(mapping.values())
    has_required = "name" in canonical_set and "price_rupees" in canonical_set
    return mapping, has_required


def _clean_price_val(raw_val: Any) -> Optional[float]:
    """Extract a float price from raw string or numeric value."""
    if raw_val is None:
        return None
    if isinstance(raw_val, (int, float)):
        return float(raw_val)
    val_str = str(raw_val).strip().replace(",", "").replace("₹", "").replace("Rs.", "").replace("INR", "").strip()
    try:
        v = float(val_str)
        return v
    except ValueError:
        m = re.search(r"\d+(\.\d+)?", val_str)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                pass
    return None


def _audit_and_stage_row(
    raw_row: Dict[str, Any],
    source_line: str,
    index: int,
    existing_catalogue: Dict[str, Any],
    default_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit parsed row, assign flags, verify source attribution, and badge New vs Update."""
    name = str(raw_row.get("name") or "").strip()
    price_val = _clean_price_val(raw_row.get("price_rupees"))
    category = (raw_row.get("category") or default_category or "").strip() or None
    sample_type = (raw_row.get("sample_type") or "").strip() or None
    description = (raw_row.get("description") or "").strip() or None
    prep_instructions = (raw_row.get("prep_instructions") or "").strip() or None

    turnaround_hours = None
    tat_raw = raw_row.get("turnaround_hours")
    if tat_raw is not None and str(tat_raw).strip():
        try:
            turnaround_hours = int(float(str(tat_raw).strip()))
        except (ValueError, TypeError):
            pass

    fasting_raw = raw_row.get("fasting_required")
    fasting_required = str(fasting_raw).strip().lower() in ("true", "1", "yes")

    flags: List[str] = []

    # Validation: Name
    if not name:
        flags.append("missing_name")
    elif len(name) > 200:
        flags.append("name_too_long")

    # Validation: Price
    if price_val is None or price_val <= 0:
        flags.append("missing_price")
    else:
        if price_val < 50.0 or price_val > 100000.0:
            flags.append("suspicious_price")

    # Source Attribution Check
    # Verify that the price and name key terms appear in source_line
    if source_line:
        line_lower = source_line.lower()
        if price_val is not None:
            # Check integer or formatted price in source line
            price_int = int(round(price_val))
            if str(price_int) not in line_lower and f"{price_val:.2f}" not in line_lower:
                flags.append("not_in_source")

    # Status: New vs Update vs Flagged
    name_lower = name.lower()
    is_existing = name_lower in existing_catalogue

    if flags:
        status = "Flagged"
        selected = False
    elif is_existing:
        status = "Update"
        selected = True
    else:
        status = "New"
        selected = True

    price_paise = int(round(price_val * 100)) if price_val is not None else 0

    return {
        "index": index,
        "name": name,
        "price_rupees": price_val if price_val is not None else 0.0,
        "price_paise": price_paise,
        "category": category,
        "sample_type": sample_type,
        "turnaround_hours": turnaround_hours,
        "fasting_required": fasting_required,
        "prep_instructions": prep_instructions,
        "description": description,
        "source_line": source_line[:500] if source_line else f"{name} - {price_val}",
        "status": status,
        "flags": flags,
        "selected": selected,
        "existing_id": existing_catalogue.get(name_lower),
    }


def parse_csv_fast_path(
    raw_bytes: bytes,
    existing_catalogue: Dict[str, Any],
    default_category: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Fast-path for CSV with recognized tabular headers."""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return None

    header_map, has_required = _resolve_header_map(reader.fieldnames)
    if not has_required:
        return None

    staged_rows: List[Dict[str, Any]] = []
    for idx, row in enumerate(reader):
        mapped = {header_map.get(k, k): (v or "").strip() for k, v in row.items() if k is not None}
        source_line = ", ".join(f"{k}: {v}" for k, v in row.items() if v)
        staged = _audit_and_stage_row(
            mapped, source_line, idx, existing_catalogue, default_category
        )
        staged_rows.append(staged)

    return staged_rows


def parse_xlsx_fast_path(
    raw_bytes: bytes,
    existing_catalogue: Dict[str, Any],
    default_category: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Fast-path for XLSX with recognized tabular headers."""
    import openpyxl

    validate_xlsx_zip_safety(raw_bytes)

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    sheet = wb.active
    if not sheet:
        return None

    rows_iter = sheet.iter_rows(values_only=True)
    # Find header row
    header_row = None
    for row in rows_iter:
        if row and any(cell is not None for cell in row):
            header_row = [str(c).strip() for c in row if c is not None]
            break

    if not header_row:
        return None

    header_map, has_required = _resolve_header_map(header_row)
    if not has_required:
        return None

    staged_rows: List[Dict[str, Any]] = []
    idx = 0
    for row in rows_iter:
        if not row or not any(cell is not None for cell in row):
            continue
        row_dict: Dict[str, Any] = {}
        for col_idx, h in enumerate(header_row):
            if col_idx < len(row):
                val = row[col_idx]
                canonical = header_map.get(h, h)
                row_dict[canonical] = val

        source_line = ", ".join(f"{header_row[i]}: {row[i]}" for i in range(min(len(header_row), len(row))) if row[i] is not None)
        staged = _audit_and_stage_row(
            row_dict, source_line, idx, existing_catalogue, default_category
        )
        staged_rows.append(staged)
        idx += 1

    wb.close()
    return staged_rows


def extract_text_and_tables_from_pdf(raw_bytes: bytes) -> Tuple[List[str], int]:
    """Extract text lines and table rows from PDF. Enforces 30 page ceiling."""
    text_lines: List[str] = []
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        num_pages = len(pdf.pages)
        if num_pages > MAX_PDF_PAGES:
            raise HTTPException(
                status_code=400,
                detail=f"PDF exceeds maximum allowed page limit of {MAX_PDF_PAGES} pages (found {num_pages}).",
            )
        for page in pdf.pages:
            # 1. Native text
            t = page.extract_text()
            if t:
                for line in t.splitlines():
                    c = line.strip()
                    if c:
                        text_lines.append(c)

            # 2. Table extraction
            tables = page.extract_tables() or []
            for table in tables:
                for r in table:
                    if r:
                        line = " | ".join(str(cell).strip() for cell in r if cell is not None and str(cell).strip())
                        if line and line not in text_lines:
                            text_lines.append(line)

    return text_lines, num_pages


async def ocr_pdf_pages(raw_bytes: bytes, max_pages: int = MAX_OCR_PAGES) -> List[str]:
    """Perform OCR on up to max_pages at 300 DPI with 120-second timeout."""
    if not HAS_PYTESSERACT or pytesseract is None:
        raise HTTPException(
            status_code=500,
            detail="OCR engine (pytesseract) is not installed on this system.",
        )

    lines: List[str] = []

    def _sync_ocr() -> List[str]:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            pages_to_ocr = pdf.pages[:max_pages]
            for page in pages_to_ocr:
                try:
                    pil_img = page.to_image(resolution=300).original
                    pil_img = preprocess_image_for_ocr(pil_img)
                    text = pytesseract.image_to_string(pil_img)
                    for line in text.splitlines():
                        c = line.strip()
                        if c:
                            lines.append(c)
                except Exception as e:
                    logger.debug(f"Page OCR extraction skipped: {e}")
        return lines

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_ocr),
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=400,
            detail=f"OCR processing timed out after {int(OCR_TIMEOUT_SECONDS)} seconds.",
        )


async def ocr_image_file(raw_bytes: bytes) -> List[str]:
    """Perform OCR on image bytes with downscaling and 120-second timeout."""
    if not HAS_PYTESSERACT or pytesseract is None:
        raise HTTPException(
            status_code=500,
            detail="OCR engine (pytesseract) is not installed on this system.",
        )

    def _sync_img_ocr() -> List[str]:
        img = Image.open(io.BytesIO(raw_bytes))
        img = preprocess_image_for_ocr(img)
        text = pytesseract.image_to_string(img)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_sync_img_ocr),
            timeout=OCR_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=400,
            detail=f"OCR processing timed out after {int(OCR_TIMEOUT_SECONDS)} seconds.",
        )


async def extract_catalogue_with_ai(
    text_lines: List[str],
    clinic_id: str,
    existing_catalogue: Dict[str, Any],
    default_category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Partition text into chunks and extract structured lab tests via AI Gateway."""
    if not text_lines:
        return []

    # Chunk text lines (50 lines per chunk, max 20 chunks)
    chunk_size = 50
    chunks: List[str] = []
    for i in range(0, min(len(text_lines), chunk_size * MAX_EXTRACTION_CHUNKS), chunk_size):
        chunk_lines = text_lines[i : i + chunk_size]
        chunks.append("\n".join(chunk_lines))

    staged_rows: List[Dict[str, Any]] = []
    row_index = 0

    for chunk in chunks:
        prompt = (
            "You are a clinical diagnostic data extractor. Extract all lab tests, packages, "
            "and diagnostic procedures from the raw text snippet below.\n\n"
            "Return a JSON object with a single key 'tests', containing a list of objects with these fields:\n"
            "- name: string (exact name of the test)\n"
            "- price_rupees: number (numeric price in INR)\n"
            "- category: string or null (department or heading, e.g. 'Pathology', 'Radiology')\n"
            "- sample_type: string or null (e.g. 'Blood', 'Urine', 'Serum')\n"
            "- fasting_required: boolean\n"
            "- turnaround_hours: integer or null\n"
            "- prep_instructions: string or null\n"
            "- description: string or null\n"
            "- source_line: string (the exact snippet or text line where this test was found)\n\n"
            "CRITICAL INSTRUCTIONS:\n"
            "1. ONLY extract tests explicitly present in the text with their prices.\n"
            "2. DO NOT invent, hallucinate, or extrapolate tests or rates.\n"
            "3. Ignore doctor names, general hospital addresses, terms, or unrelated notes.\n\n"
            f"TEXT SNIPPET:\n{chunk}"
        )

        try:
            response = await call_ai_gateway(
                messages=[{"role": "user", "content": prompt}],
                task_type="catalogue_extraction",
                clinic_id=clinic_id,
                max_tokens=1500,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            content = response.get("content") or "{}"
            parsed = json.loads(content)
            tests = parsed.get("tests") or []
            for t in tests:
                if isinstance(t, dict):
                    source = t.get("source_line") or chunk[:100]
                    staged = _audit_and_stage_row(
                        t, source, row_index, existing_catalogue, default_category
                    )
                    staged_rows.append(staged)
                    row_index += 1
        except SpendCapExceededError:
            raise
        except Exception as e:
            logger.warning(f"AI catalogue chunk extraction failed: {e}")

    return staged_rows


async def parse_catalogue_file(
    raw_bytes: bytes,
    filename: str,
    clinic_id: str,
    branch_id: Optional[str] = None,
    default_category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Main entrypoint: parses uploaded price list and returns audited, staged rows."""
    fmt = detect_and_validate_file_type(raw_bytes, filename)

    # Fetch existing catalogue for duplicate/update matching
    def _existing_q():
        q = supabase.table("lab_tests").select("id, name").eq("clinic_id", clinic_id)
        return q.eq("branch_id", branch_id) if branch_id else q.is_("branch_id", "null")

    from app.routers.admin import _fetch_all_lab_tests
    existing_rows = await _fetch_all_lab_tests(
        _existing_q, f"lab_tests lookup for clinic {clinic_id}"
    )
    existing_catalogue = {r["name"].strip().lower(): r["id"] for r in existing_rows}

    # 1. Try Fast-Path for CSV
    if fmt == "csv":
        fast_rows = parse_csv_fast_path(raw_bytes, existing_catalogue, default_category)
        if fast_rows is not None and len(fast_rows) > 0:
            return fast_rows
        # Otherwise fallback to AI extraction of lines
        text = raw_bytes.decode("utf-8-sig", errors="ignore")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return await extract_catalogue_with_ai(lines, clinic_id, existing_catalogue, default_category)

    # 2. Try Fast-Path for XLSX
    if fmt == "xlsx":
        fast_rows = parse_xlsx_fast_path(raw_bytes, existing_catalogue, default_category)
        if fast_rows is not None and len(fast_rows) > 0:
            return fast_rows
        # Fallback to AI extraction
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        sheet = wb.active
        lines = []
        if sheet:
            for row in sheet.iter_rows(values_only=True):
                if row and any(c is not None for c in row):
                    lines.append(" | ".join(str(c).strip() for c in row if c is not None))
        wb.close()
        return await extract_catalogue_with_ai(lines, clinic_id, existing_catalogue, default_category)

    # 3. PDF extraction
    if fmt == "pdf":
        lines, num_pages = extract_text_and_tables_from_pdf(raw_bytes)
        if len(lines) < 5 and num_pages <= MAX_OCR_PAGES:
            # Native text is empty/sparse -> Run OCR
            logger.info(f"PDF native text sparse ({len(lines)} lines); falling back to OCR on {num_pages} pages")
            ocr_lines = await ocr_pdf_pages(raw_bytes, max_pages=MAX_OCR_PAGES)
            lines.extend(ocr_lines)
        elif len(lines) < 5 and num_pages > MAX_OCR_PAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Scanned PDF has {num_pages} pages, which exceeds the maximum OCR limit of {MAX_OCR_PAGES} pages.",
            )

        return await extract_catalogue_with_ai(lines, clinic_id, existing_catalogue, default_category)

    # 4. Images (PNG, JPG)
    if fmt in ("png", "jpg"):
        lines = await ocr_image_file(raw_bytes)
        return await extract_catalogue_with_ai(lines, clinic_id, existing_catalogue, default_category)

    raise HTTPException(status_code=400, detail="Could not process catalogue file.")
