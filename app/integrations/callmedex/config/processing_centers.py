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
from app.database import sb  # T5.1: off-loop query execution

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
        # No .single(): a clinic may hold a clinic-wide row AND branch rows
        # (migration 025), and .single() errored on 2+ rows — silently dropping
        # the job onto the env-var fallback credentials.
        result = (
            await sb(supabase.table("integration_connectors")
            .select("config, branch_id")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
            .eq("is_enabled", True))
        )
    except Exception as e:
        logger.error(
            f"Failed querying integration_connectors for clinic '{clinic_id}': {e}"
        )
        raise ValueError(
            f"Database query failed for processing center '{clinic_id}': {e}"
        ) from e

    rows = result.data if isinstance(result.data, list) else ([result.data] if result.data else [])
    if not rows:
        raise ValueError(
            f"No enabled {connector_type} connector config found for clinic '{clinic_id}'. "
            f"Ensure an entry exists in integration_connectors with is_enabled=true."
        )
    # CallMedex jobs carry no branch — the clinic-wide row is the canonical one.
    row = next((r for r in rows if not r.get("branch_id")), rows[0])

    config: dict = row.get("config") or {}
    base_url = config.get("base_url", "").strip().rstrip("/")
    if base_url and not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    clinic_slug = config.get("clinic_slug", "")
    username = config.get("username")
    password = config.get("password")
    if not password and config.get("password_encrypted"):
        # Owner/admin panels store only the Fernet ciphertext; sending that to
        # MocDoc as the password guaranteed a login failure.
        from app.config import settings as app_settings
        from app.utils.connector_crypto import decrypt_password

        try:
            password = decrypt_password(config["password_encrypted"], app_settings.connector_encryption_key)
        except Exception as e:
            logger.error(
                f"Could not decrypt connector password for clinic '{clinic_id}' "
                f"({type(e).__name__}) — re-enter it in the owner panel or check CONNECTOR_ENCRYPTION_KEY"
            )
            password = None

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


# ── CallMedex Center -> Kriya Clinic Mapping ────────────────────────────────

CALLMEDEX_CENTER_TO_CLINIC: dict[str, str] = {
    # Accumax Diagnostic Center (CallMedex ID -> Kriya ID)
    "e204185b-fd1c-4753-9243-58715d76b51c": "c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c",
}
DEFAULT_CALLMEDEX_CLINIC_ID = "c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c"


async def resolve_callmedex_clinic_id(processing_center_id: Optional[str]) -> str:
    """Resolve incoming CallMedex processing_center_id to a valid Kriya clinic_id.

    Resolution order:
    1. Static mapping for known processing centers (e.g. Accumax).
    2. Direct match against active clinics table in Supabase.
    3. Match against integration_connectors table (connector.clinic_id).
    4. Fallback default to Accumx Diagnostics (c2a14afe-27a9-4a13-b7c3-5ece8d05dc6c).
    """
    if not processing_center_id:
        return DEFAULT_CALLMEDEX_CLINIC_ID

    clean_id = str(processing_center_id).strip().lower()
    for cmx_id, clinic_id in CALLMEDEX_CENTER_TO_CLINIC.items():
        if clean_id == cmx_id.lower():
            return clinic_id

    try:
        from app.database import supabase
        # Check if processing_center_id matches an active clinic in clinics table
        # Note: clinics table is platform-level, not in TENANT_OWNED_TABLES
        clinic_res = await sb(
            supabase.table("clinics")
            .select("id")
            .eq("id", processing_center_id)
            .limit(1)
        )
        if clinic_res.data and len(clinic_res.data) > 0:
            return str(clinic_res.data[0]["id"])

        # Check if processing_center_id matches an integration_connectors row
        # unscoped: unique_row_key
        conn_res = await sb(
            supabase.table("integration_connectors")
            .select("clinic_id")
            .eq("id", processing_center_id)
            .limit(1)
        )
        if conn_res.data and len(conn_res.data) > 0:
            return str(conn_res.data[0]["clinic_id"])
    except Exception as e:
        logger.warning(
            f"Failed dynamic resolution for CallMedex center '{processing_center_id}': {e}. "
            f"Falling back to default clinic {DEFAULT_CALLMEDEX_CLINIC_ID}."
        )

    return DEFAULT_CALLMEDEX_CLINIC_ID

