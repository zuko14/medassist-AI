"""AI Package & Lab Test Details Generator for Kriya AI.

Generates concise, non-promotional patient descriptions for diagnostic tests
and health checkup packages following the strict clinical firewall rules in
ai_engine.generate_treatment_description:
1. Input sanitization (sanitize_user_input, strip_injection_markers).
2. Administrative monthly spend cap check via ai_gateway.
3. Clinical output safety firewall (clinical_firewall.validate_llm_output, PROMISE_PATTERN).
4. Deterministic template fallback on any failure or budget exhaustion.
5. Never raises. Results stay a preview until an admin explicitly saves them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.services.ai_gateway import call_ai_gateway, check_admin_spend_cap
from app.services.clinical_firewall import validate_llm_output
from app.utils.security import sanitize_user_input, strip_injection_markers

logger = logging.getLogger("kriya.test_details")

# Re-use promise pattern to prevent unethical marketing guarantees (NMC guidelines)
import re
PROMISE_PATTERN = re.compile(
    r"\b(?:painless|pain[- ]free|guarantee[ds]?|permanent(?:ly)?|success\s+rate|"
    r"cure[sd]?|miracle|risk[- ]free|no\s+side[- ]effects?|instant\s+results?|"
    r"best|boy|girl|gender)\b|100\s*%|6/6",
    re.IGNORECASE,
)


def _template_test_details(
    name: str,
    category: Optional[str] = None,
    sample_type: Optional[str] = None,
    fasting_required: bool = False,
    source: str = "template",
) -> Dict[str, str]:
    """Deterministic, neutral template description. Deliberately plain for admin review."""
    clean_name = (name or "Diagnostic Investigation").strip()[:100]
    cat_lower = (category or "").lower()

    if "package" in cat_lower or "checkup" in cat_lower or "package" in clean_name.lower():
        desc = (
            f"{clean_name} includes essential health screenings and routine diagnostic parameters. "
            "Helps your physician assess overall wellness and detect underlying health conditions early."
        )
    elif "scan" in cat_lower or "radiology" in cat_lower or "mri" in clean_name.lower() or "ct" in clean_name.lower() or "x-ray" in clean_name.lower():
        desc = (
            f"{clean_name} provides detailed diagnostic imaging conducted by certified technicians. "
            "Your radiologist and physician will evaluate the images to guide care."
        )
    else:
        sample_txt = f" from a {sample_type.lower()} sample" if sample_type else ""
        fasting_txt = " Requires overnight fasting." if fasting_required else ""
        desc = (
            f"{clean_name} is performed{sample_txt} to evaluate clinical health markers.{fasting_txt} "
            "Your doctor will review findings in the context of your symptoms."
        )

    return {
        "description": desc[:400].strip(),
        "source": source,
    }


def _details_are_safe(text: str) -> bool:
    """Validate that AI generated text satisfies clinical safety and size boundaries."""
    if not text or len(text) > 400:
        return False
    if PROMISE_PATTERN.search(text):
        return False
    # Check clinical firewall (no off-label diagnosis, no unauthorized prescribing)
    is_safe, _ = validate_llm_output(text, "en")
    return is_safe


async def generate_test_details(
    name: str,
    category: Optional[str] = None,
    sample_type: Optional[str] = None,
    fasting_required: bool = False,
    clinic_id: Optional[str] = None,
) -> Dict[str, str]:
    """Draft a concise patient description for a lab test or package.

    Returns:
        {"description": "...", "source": "ai" | "template" | "template_budget_exceeded"}

    Never raises. Result is preview-only until saved by admin via PUT /admin/lab-tests/{test_id}.
    """
    raw_name = (name or "").strip()[:120]
    raw_category = (category or "").strip()[:60]
    raw_sample = (sample_type or "").strip()[:40]

    # 1. Input sanitization
    clean_name, susp_name = sanitize_user_input(raw_name)
    clean_category, susp_cat = sanitize_user_input(raw_category)
    clean_sample, susp_sample = sanitize_user_input(raw_sample)

    if susp_name or susp_cat or susp_sample or not raw_name:
        logger.warning(f"Lab test details request flagged for suspicious input — '{raw_name}'")
        return _template_test_details(raw_name, category, sample_type, fasting_required)

    clean_name = strip_injection_markers(clean_name).strip() or raw_name
    clean_category = strip_injection_markers(clean_category).strip() or "Pathology"
    clean_sample = strip_injection_markers(clean_sample).strip() or ""

    # 2. Spend cap check
    try:
        is_exceeded, _, _ = await check_admin_spend_cap(clinic_id)
        if is_exceeded:
            logger.info(f"Clinic {clinic_id} exceeded AI budget; using template for test details.")
            return _template_test_details(clean_name, clean_category, clean_sample, fasting_required, source="template_budget_exceeded")
    except Exception as e:
        logger.warning(f"Error checking AI spend cap: {e}")

    # 3. LLM Prompting
    prompt = f"""Write a concise, factual patient description for a diagnostic test or health package in an Indian clinic.

Investigation: "{clean_name}"
Category: "{clean_category}"
Sample: "{clean_sample or 'Not specified'}"
Fasting Required: {"Yes" if fasting_required else "No"}

Rules:
- Exactly 1 to 2 short sentences, maximum 220 characters in total.
- Plain English understandable to patients.
- Describe what the test or package assesses (e.g. key parameters or clinical purpose).
- Never give medical diagnoses, prognoses, or treatment advice.
- Never promise cures or use words like guaranteed, painless, 100%, permanent, or best.
- No medication dosages, prices, or promotion.

Respond ONLY with JSON: {{"description": "..."}}"""

    try:
        data = await call_ai_gateway(
            messages=[
                {
                    "role": "system",
                    "content": "You write short, objective, non-promotional patient descriptions for laboratory tests. You never provide medical diagnoses or advice.",
                },
                {"role": "user", "content": prompt},
            ],
            task_type="test_details",
            clinic_id=clinic_id,
            response_format={"type": "json_object"},
            max_tokens=250,
            temperature=0.2,
            timeout=10,
        )

        # Parse text
        choices = data.get("choices") or []
        if not choices:
            return _template_test_details(clean_name, clean_category, clean_sample, fasting_required)

        content = choices[0].get("message", {}).get("content", "")
        parsed = json.loads(content)
        draft_text = str(parsed.get("description") or "").strip()

        if _details_are_safe(draft_text):
            return {
                "description": draft_text,
                "source": "ai",
            }
        logger.warning(f"AI test details for '{clean_name}' failed safety checks — using template")
        return _template_test_details(clean_name, clean_category, clean_sample, fasting_required)

    except Exception as e:
        logger.warning(f"AI details generation failed for '{clean_name}': {e}. Using template.")
        return _template_test_details(clean_name, clean_category, clean_sample, fasting_required)
