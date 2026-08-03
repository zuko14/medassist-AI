"""CallMedex Integration Exception Hierarchy (Phase 2 Contract)."""


class CallMedexException(Exception):
    """Base exception class for all CallMedex integration errors."""

    def __init__(self, message: str, error_code: str = "CALLMEDEX_ERROR"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code


class ConfigurationError(CallMedexException):
    """Raised when integration configuration or secrets are missing/invalid."""

    def __init__(self, message: str):
        super().__init__(message, error_code="CONFIG_ERROR")


class AuthenticationError(CallMedexException):
    """Raised when Bearer/HMAC or EMR portal authentication fails."""

    def __init__(self, message: str):
        super().__init__(message, error_code="AUTH_ERROR")


class ConnectorError(CallMedexException):
    """Base exception for laboratory connector execution errors."""

    def __init__(self, message: str, connector_type: str = "unknown"):
        super().__init__(message, error_code="CONNECTOR_ERROR")
        self.connector_type = connector_type


class ConnectorNavigationError(ConnectorError):
    """Raised when browser fails to navigate or find required DOM elements."""

    def __init__(self, message: str, connector_type: str = "unknown"):
        super().__init__(message, connector_type=connector_type)
        self.error_code = "NAV_ERROR"


class ReportDownloadError(ConnectorError):
    """Raised when downloading a report PDF fails or times out."""

    def __init__(self, message: str, connector_type: str = "unknown"):
        super().__init__(message, connector_type=connector_type)
        self.error_code = "DOWNLOAD_ERROR"


class QueueError(CallMedexException):
    """Raised when task queue enqueuing, worker dispatch, or DLQ processing fails."""

    def __init__(self, message: str):
        super().__init__(message, error_code="QUEUE_ERROR")


class StorageError(CallMedexException):
    """Raised when temporary report buffer or artifact storage operations fail."""

    def __init__(self, message: str):
        super().__init__(message, error_code="STORAGE_ERROR")


class ValidationError(CallMedexException):
    """Raised when request payloads, headers, or schema contracts fail validation."""

    def __init__(self, message: str):
        super().__init__(message, error_code="VALIDATION_ERROR")
