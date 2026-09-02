"""Provider-based lab report routing.

Some diagnostic centres run panels for insurance/TPA providers (MEDIBUDDY,
MDINDIA TPA, HEALTH ASSURE TPA, ...). For those the patient never receives the
PDF — the report goes to the centre's own TPA desk number, which processes the
claim. Delivering to the patient instead is both wrong operationally and, for a
corporate panel, a disclosure the centre did not agree to.

The rule lives on the connector row's config so it is per-clinic (and per-branch)
and editable from the admin panel without a deploy:

    report_routing_providers  "VMSC MEDIBUDDY, MD INDIA TPA, ..."   (comma/newline separated)
    report_routing_phone      "9052024418"

Matching is deliberately loose on punctuation and spacing but exact on the
alphanumeric run: "MD INDIA TPA", "MDINDIA TPA" and "VMSC MD INDIA TPA" all
normalise so that the configured key "MD INDIA TPA" is found inside them.
"""

import logging
import re
from typing import Optional

from app.utils.validators import normalize_phone, validate_phone

logger = logging.getLogger(__name__)

PROVIDERS_KEY = "report_routing_providers"
PHONE_KEY = "report_routing_phone"


def normalize_provider(text: Optional[str]) -> str:
    """Uppercase, strip everything that is not a letter or digit.

    "VMSC MD INDIA TPA\ngmvmsc15@gmail.com" -> "VMSCMDINDIATPAGMVMSC15GMAILCOM"
    """
    if not text:
        return ""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def parse_provider_routing(config: Optional[dict]) -> tuple[list[str], Optional[str]]:
    """Read the routing rule off a connector config.

    Returns (normalized provider keys, E.164 desk phone). Either half missing
    means routing is off — a phone with no providers would silently redirect
    every report, so both are required.
    """
    if not isinstance(config, dict):
        return [], None

    raw_phone = (config.get(PHONE_KEY) or "").strip()
    if not raw_phone:
        return [], None

    phone = normalize_phone(raw_phone)
    if not validate_phone(phone):
        logger.error(
            f"{PHONE_KEY} in connector config is not a valid phone number "
            f"({raw_phone!r}) — provider routing disabled"
        )
        return [], None

    raw_providers = config.get(PROVIDERS_KEY) or ""
    if isinstance(raw_providers, (list, tuple)):
        parts = [str(p) for p in raw_providers]
    else:
        parts = re.split(r"[,\n;]", str(raw_providers))

    keys = []
    for part in parts:
        key = normalize_provider(part)
        # A one- or two-character key would match almost any provider cell.
        if len(key) >= 3 and key not in keys:
            keys.append(key)

    if not keys:
        return [], None

    return keys, phone


def route_recipient_for_provider(
    config: Optional[dict], provider: Optional[str]
) -> Optional[str]:
    """Desk phone this report must go to instead of the patient, or None."""
    if not provider:
        return None

    keys, phone = parse_provider_routing(config)
    if not keys:
        return None

    haystack = normalize_provider(provider)
    if not haystack:
        return None

    for key in keys:
        if key in haystack:
            return phone
    return None


async def is_routing_recipient(
    clinic_id: str, connector_type: str, phone: Optional[str]
) -> bool:
    """Server-side check that `phone` really is a desk number this clinic
    configured, for any branch.

    The connector is a trusted caller, but "skip the patient-match gate" is the
    one claim it makes that disables a safety control, so the API verifies the
    destination against the clinic's own config rather than taking its word.
    """
    if not phone:
        return False

    from app.database import sb, supabase

    try:
        result = await sb(
            # unscoped: reading this clinic's own connector rows, clinic_id pinned
            supabase.table("integration_connectors")
            .select("config")
            .eq("clinic_id", clinic_id)
            .eq("connector_type", connector_type)
        )
    except Exception as e:
        # Fail closed: an unverifiable claim does not get to skip the gate.
        logger.error(f"Provider-routing verification query failed: {e}")
        return False

    target = normalize_phone(phone)
    for row in (result.data or []):
        _, desk_phone = parse_provider_routing(row.get("config"))
        if desk_phone and desk_phone == target:
            return True
    return False
