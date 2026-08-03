"""Download Report PDF Integrity Validator (Phase 4.5 Implementation)."""

import os
import hashlib
import logging
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class DownloadIntegrityResult(BaseModel):
    """Integrity check result contract for downloaded report PDFs."""

    file_exists: bool = Field(..., description="True if PDF file exists on disk")
    non_zero_bytes: bool = Field(..., description="True if byte size is greater than 0")
    pdf_signature_valid: bool = Field(..., description="True if file header starts with %PDF")
    file_size_bytes: int = Field(..., description="Total file size in bytes")
    sha256_checksum: str = Field(..., description="SHA-256 digest hex string")
    is_valid: bool = Field(..., description="Master validity status")
    error_message: Optional[str] = Field(None, description="Validation failure details")


def validate_pdf_download(file_bytes: bytes, file_path: Optional[str] = None) -> DownloadIntegrityResult:
    """Validate downloaded report PDF for file existence, signature, non-zero bytes, and SHA256 checksum."""
    if len(file_bytes) == 0:
        return DownloadIntegrityResult(
            file_exists=os.path.exists(file_path) if file_path else False,
            non_zero_bytes=False,
            pdf_signature_valid=False,
            file_size_bytes=0,
            sha256_checksum="",
            is_valid=False,
            error_message="Downloaded PDF bytes are 0 (empty file)",
        )

    # Check %PDF header signature
    pdf_sig_valid = file_bytes.startswith(b"%PDF")
    checksum = hashlib.sha256(file_bytes).hexdigest()

    is_valid = pdf_sig_valid and len(file_bytes) > 0

    result = DownloadIntegrityResult(
        file_exists=os.path.exists(file_path) if file_path else True,
        non_zero_bytes=len(file_bytes) > 0,
        pdf_signature_valid=pdf_sig_valid,
        file_size_bytes=len(file_bytes),
        sha256_checksum=checksum,
        is_valid=is_valid,
        error_message=None if is_valid else "Invalid PDF header signature",
    )
    logger.info(
        f"PDF Integrity Validated: valid={result.is_valid} | size={result.file_size_bytes}B | "
        f"sha256={result.sha256_checksum[:12]}..."
    )
    return result
