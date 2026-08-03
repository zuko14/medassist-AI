"""Browser Abstraction Interface (Phase 2 Contract)."""

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseBrowserSession(ABC):
    """Abstract interface defining Playwright/browser lifecycle and session management.

    Decouples connector code from direct browser driver APIs.
    """

    @abstractmethod
    async def create_context(
        self, session_id: str, headless: bool = True
    ) -> Any:
        """Create an isolated browser context for a connector execution session.

        Args:
            session_id: Unique session tracking identifier.
            headless: Whether to run browser in headless mode.

        Returns:
            Any: Browser context handle.
        """
        pass

    @abstractmethod
    async def capture_screenshot(
        self, page_handle: Any, artifact_name: str
    ) -> Optional[str]:
        """Capture a failure/diagnostic screenshot and save to artifacts directory.

        Args:
            page_handle: Active page instance handle.
            artifact_name: Name label for screenshot file.

        Returns:
            Optional[str]: Saved absolute file path of screenshot artifact.
        """
        pass

    @abstractmethod
    async def close_context(self, session_id: str) -> None:
        """Close browser context and clean up temporary storage.

        Args:
            session_id: Active session tracking identifier.
        """
        pass
