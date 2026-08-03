"""Selector Provider Interface & Versioning Contract (Phase 2 Contract)."""

from abc import ABC, abstractmethod
from typing import Dict, Any


class BaseSelectorProvider(ABC):
    """Abstract contract for versioned EMR DOM selector definitions.

    Ensures connectors query DOM elements via strongly-typed versioned interfaces
    rather than unmanaged string constants.
    """

    @property
    @abstractmethod
    def version(self) -> str:
        """Return selector layout version string (e.g. 'v1.0.0')."""
        pass

    @property
    @abstractmethod
    def login_username_input(self) -> str:
        """CSS/XPath selector for username input field."""
        pass

    @property
    @abstractmethod
    def login_password_input(self) -> str:
        """CSS/XPath selector for password input field."""
        pass

    @property
    @abstractmethod
    def login_submit_button(self) -> str:
        """CSS/XPath selector for login submit button."""
        pass

    @property
    @abstractmethod
    def search_barcode_input(self) -> str:
        """CSS/XPath selector for barcode / report search input field."""
        pass

    @property
    @abstractmethod
    def search_submit_button(self) -> str:
        """CSS/XPath selector for report search button."""
        pass

    @property
    @abstractmethod
    def download_pdf_button(self) -> str:
        """CSS/XPath selector for PDF report download button/link."""
        pass
