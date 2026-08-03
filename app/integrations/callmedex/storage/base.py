"""Storage Abstraction Interface (Phase 2 Contract)."""

from abc import ABC, abstractmethod
from typing import Optional


class BaseStorageProvider(ABC):
    """Abstract contract for temporary integration storage and artifact management."""

    @abstractmethod
    async def save_temp_report(
        self, report_id: str, file_bytes: bytes, filename: str
    ) -> str:
        """Buffer downloaded report PDF bytes temporarily prior to delivery.

        Args:
            report_id: External report identifier.
            file_bytes: PDF file content.
            filename: Downloaded filename.

        Returns:
            str: Saved local/cloud file storage URI.
        """
        pass

    @abstractmethod
    async def get_temp_report(self, storage_uri: str) -> Optional[bytes]:
        """Retrieve buffered report bytes by storage URI.

        Args:
            storage_uri: Storage resource identifier.

        Returns:
            Optional[bytes]: File bytes if available, None if expired/deleted.
        """
        pass

    @abstractmethod
    async def cleanup_temp_report(self, storage_uri: str) -> bool:
        """Remove temporary report buffer after successful processing.

        Args:
            storage_uri: Storage resource identifier.

        Returns:
            bool: True if cleanup succeeded.
        """
        pass
