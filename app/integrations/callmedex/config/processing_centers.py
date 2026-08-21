"""Processing Center Configuration Registry.

Resolves `clinic_id` → MocDoc portal config (base_url, clinic_slug, username, password)
by reading from the `integration_connectors` Supabase table.

This allows every diagnostic center / processing center to have its OWN separate
MocDoc login credentials, portal URL, and clinic slug stored in Supabase — without
requiring redeployments or changing Render environment variables.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessingCenterConfig:
    """MocDoc portal configuration for a processing center.

    Attributes:
        base_url: Root URL of the MocDoc instance (e.g., "https://mocdoc.com")
        clinic_slug: URL slug used in the lab reports path
                     (e.g., "visakha-multispeciality-clinics")
        username: Optional per-center EMR username (overrides .env)
        password: Optional per-center EMR password (overrides .env)
    """

    base_url: str
    clinic_slug: str
    username: Optional[str] = None
    password: Optional[str] = None


async def resolve_processing_center(
    clinic_id: str,
    connector_type: str = "mocdoc",
) -> ProcessingCenterConfig:
    """Fetch MocDoc portal config and credentials for a processing center from Supabase.

    Reads from the `integration_connectors` table. The `config` JSONB column contains:
    - `base_url`: e.g. "https://mocdoc.com"
    - `clinic_slug`: e.g. "visakha-multispeciality-clinics"
    - `username`: e.g. "lab_user_1" (optional, per-center login)
    - `password` or `password_encrypted`: per-center password (optional)

    Args:
        clinic_id: The clinic/processing-center UUID sent by CallMedex.
        connector_type: EMR connector type (default: "mocdoc").

    Returns:
        ProcessingCenterConfig with base_url, clinic_slug, username, and password.

    Raises:
        ValueError: If no connector config exists for this clinic_id, or if
                    the config is missing required fields.
    """
    try:
        from app.database import supabase
    except ImportError:
        logger.error(
            "Supabase client unavailable — cannot resolve processing center config. "
            "Ensure app.database is importable."
        )
        raise ValueError(
            f"Database unavailable: cannot resolve processing center for clinic '{clinic_id}'"
        )

    try:
        result = (
            supabase.table("integration_connectors")
            .select("config")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
            .eq("is_enabled", True)
            .single()
            .execute()
        )
    except Exception as e:
        logger.error(
            f"Failed querying integration_connectors for clinic '{clinic_id}': {e}"
        )
        raise ValueError(
            f"Database query failed for processing center '{clinic_id}': {e}"
        ) from e

    if not result.data:
        raise ValueError(
            f"No enabled {connector_type} connector config found for clinic '{clinic_id}'. "
            f"Ensure an entry exists in integration_connectors with is_enabled=true."
        )

    config: dict = result.data.get("config", {})
    base_url = config.get("base_url", "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    clinic_slug = config.get("clinic_slug", "")
    username = config.get("username")
    password = config.get("password") or config.get("password_encrypted")

    if not base_url:
        raise ValueError(
            f"Missing 'base_url' in connector config for clinic '{clinic_id}'. "
            f"Add it to the integration_connectors.config JSONB column."
        )

    if not clinic_slug:
        raise ValueError(
            f"Missing 'clinic_slug' in connector config for clinic '{clinic_id}'. "
            f"Add it to the integration_connectors.config JSONB column."
        )

    logger.info(
        f"Resolved processing center config for clinic '{clinic_id}': "
        f"base_url='{base_url}', clinic_slug='{clinic_slug}', "
        f"has_custom_credentials={bool(username and password)}"
    )

    return ProcessingCenterConfig(
        base_url=base_url,
        clinic_slug=clinic_slug,
        username=username,
        password=password,
    )
