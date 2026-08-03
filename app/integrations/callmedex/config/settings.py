"""CallMedex Integration Configuration & Settings Model (Phase 2 Contract)."""

from typing import Literal
from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CallMedexSettings(BaseSettings):
    """Configuration model for CallMedex integration subsystem."""

    # Environment
    app_env: Literal["development", "staging", "production"] = Field(
        default="development", description="Application execution environment"
    )

    # API Endpoints & URLs
    mediassist_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for MediAssist AI internal backend API",
    )
    callmedex_callback_url: str = Field(
        default="http://localhost:8000/internal/integrations/callmedex/callback",
        description="Callback webhook URL for CallMedex status updates",
    )

    # Security & Authentication (Bearer + HMAC)
    integration_secret: SecretStr = Field(
        default=SecretStr("dev_integration_secret_change_in_prod"),
        description="Shared secret for machine-to-machine X-Integration-Secret header",
    )
    hmac_signature_secret: SecretStr = Field(
        default=SecretStr("dev_hmac_signature_secret_change_in_prod"),
        description="Secret key for signing callback webhooks via HMAC-SHA256",
    )
    bearer_token: SecretStr = Field(
        default=SecretStr("dev_bearer_token_change_in_prod"),
        description="Bearer token for CallMedex API authentication",
    )
    mocdoc_username: SecretStr = Field(
        default=SecretStr("mock_user"),
        description="Username credential for MocDoc EMR portal authentication",
    )
    mocdoc_password: SecretStr = Field(
        default=SecretStr("mock_password"),
        description="Password credential for MocDoc EMR portal authentication",
    )

    # Queue & Worker Settings
    queue_backend: Literal["apscheduler", "redis", "memory"] = Field(
        default="apscheduler",
        description="Task queue backend engine driver selection",
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL (used when queue_backend='redis')",
    )
    max_worker_retries: int = Field(
        default=3, description="Maximum retry attempts for failed queue tasks"
    )
    retry_backoff_seconds: int = Field(
        default=5, description="Initial retry backoff delay in seconds"
    )

    # Browser & EMR Automation Settings
    browser_timeout_ms: int = Field(
        default=30000, description="Default timeout for browser page actions in milliseconds"
    )
    browser_navigation_timeout_ms: int = Field(
        default=60000, description="Default page navigation timeout in milliseconds"
    )
    browser_headless: bool = Field(
        default=True, description="Run Playwright browser in headless mode"
    )
    download_dir: str = Field(
        default="app/integrations/callmedex/browser/artifacts/downloads",
        description="Directory for temporary report downloads",
    )
    artifacts_dir: str = Field(
        default="app/integrations/callmedex/browser/artifacts",
        description="Directory for failure screenshots and trace artifacts",
    )

    # Feature Flags
    enable_callmedex_integration: bool = Field(
        default=True, description="Master feature flag for CallMedex integration"
    )
    enable_parallel_legacy_run: bool = Field(
        default=False, description="Run sandboxed connector in parallel with legacy connector"
    )
    enable_screenshot_artifacts: bool = Field(
        default=True, description="Enable automatic screenshot capture on job failure"
    )

    # Meta WhatsApp Cloud API Settings
    whatsapp_api_token: SecretStr = Field(
        default=SecretStr("dev_whatsapp_token"),
        description="Meta WhatsApp Cloud API Access Token",
    )
    whatsapp_phone_number_id: str = Field(
        default="100000000000000",
        description="Meta WhatsApp Phone Number ID",
    )

    model_config = SettingsConfigDict(
        env_prefix="CALLMEDEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# Package-level configuration singleton contract
callmedex_settings = CallMedexSettings()
