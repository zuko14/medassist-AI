"""Local Temporary Storage Provider (Phase 3 & Security Hardened Implementation)."""

import os
import re
import time
import logging
from typing import Optional
from app.integrations.callmedex.storage.base import BaseStorageProvider
from app.integrations.callmedex.config.settings import callmedex_settings
from app.integrations.callmedex.api.exceptions import ValidationError

logger = logging.getLogger(__name__)


class LocalStorageProvider(BaseStorageProvider):
    """Local filesystem storage provider for temporary report PDF buffering with path traversal guards & restricted file permissions."""

    def __init__(self):
        self.download_dir = os.path.abspath(callmedex_settings.download_dir)
        os.makedirs(self.download_dir, mode=0o700, exist_ok=True)

    def _sanitize_part(self, val: str) -> str:
        """Sanitize filename/ID component to prevent directory traversal and NUL byte injections."""
        if not val or "\x00" in val:
            raise ValidationError("Invalid filename component containing NUL byte or empty value")

        clean = os.path.basename(val).replace("..", "").replace("/", "").replace("\\", "")
        clean = re.sub(r"[^A-Za-z0-9_\-\.]", "_", clean)
        return clean or "temp"

    def _verify_path_bounds(self, filepath: str) -> str:
        """Verify that resolved absolute path remains strictly within the configured storage directory."""
        real_base = os.path.abspath(self.download_dir)
        real_target = os.path.abspath(filepath)
        if not real_target.startswith(real_base):
            logger.critical(f"Security Alert: Path traversal attempt blocked target='{real_target}', base='{real_base}'")
            raise ValidationError("Path traversal security violation: path escapes target directory")
        return real_target

    async def save_temp_report(
        self, report_id: str, file_bytes: bytes, filename: str
    ) -> str:
        """Buffer downloaded report PDF bytes to local disk with restricted file permissions (0o600)."""
        clean_report_id = self._sanitize_part(report_id)
        clean_filename = self._sanitize_part(filename)
        safe_filename = f"{clean_report_id}_{clean_filename}"
        filepath = os.path.join(self.download_dir, safe_filename)
        safe_filepath = self._verify_path_bounds(filepath)

        # Open file with 0o600 permissions (read/write by owner only)
        fd = os.open(safe_filepath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with open(fd, "wb", closefd=True) as f:
                f.write(file_bytes)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise

        logger.info(f"Buffered temp report PDF ({len(file_bytes)} bytes) to {safe_filepath}")
        return safe_filepath

    async def get_temp_report(self, storage_uri: str) -> Optional[bytes]:
        """Retrieve buffered report bytes by file path after path boundary verification."""
        safe_uri = self._verify_path_bounds(storage_uri)
        if os.path.exists(safe_uri):
            with open(safe_uri, "rb") as f:
                return f.read()
        logger.warning(f"Temp report file not found at {safe_uri}")
        return None

    async def cleanup_temp_report(self, storage_uri: str) -> bool:
        """Purge temporary report buffer post-processing after path boundary verification."""
        safe_uri = self._verify_path_bounds(storage_uri)
        if os.path.exists(safe_uri):
            try:
                os.remove(safe_uri)
                logger.info(f"Cleaned up temp report file: {safe_uri}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete temp file {safe_uri}: {e}")
                return False
        return True

    def cleanup_stale_temp_files(self, max_age_seconds: float = 3600.0) -> int:
        """Purge orphan temporary files older than max_age_seconds (default 1 hour)."""
        purged_count = 0
        now = time.time()
        try:
            for entry in os.scandir(self.download_dir):
                if entry.is_file():
                    file_age = now - entry.stat().st_mtime
                    if file_age > max_age_seconds:
                        try:
                            os.remove(entry.path)
                            purged_count += 1
                        except Exception as e:
                            logger.warning(f"Failed deleting stale temp file {entry.path}: {e}")
        except Exception as err:
            logger.error(f"Error scanning temp download directory for stale files: {err}")
        return purged_count
