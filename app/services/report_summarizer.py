"""AI-powered lab report summarizer using OpenRouter (DeepSeek / configurable).

Security: Integrates PII sanitization middleware (app/utils/pii_sanitizer.py)
to strip patient-identifying information from report text BEFORE sending it
to the OpenRouter external API. Patient name is restored in the final message.

Compliant with India DPDP Act 2023 data minimization requirements.
"""

import json
import logging
from typing import Any, Dict

from app.config import settings
from app.services.ai_engine import call_openrouter_with_backoff
from app.utils.pii_sanitizer import (
    sanitize_report_text,
    restore_pii,
    get_redaction_summary,
)

logger = logging.getLogger(__name__)


class ReportSummarizer:
    """Summarize lab reports into patient-friendly messages using OpenRouter AI."""

    async def summarize(
        self, report_text: str, patient_name: str, report_type: str
    ) -> Dict[str, Any]:
        """Summarize a lab report and return structured result.

        PII is stripped before calling OpenRouter and restored in the output.
        """

        if not report_text or len(report_text) < 50:
            return {"summary": None, "has_abnormal": False, "fallback": True}

        # ── PII Sanitization: Strip before LLM call ────────────────────────────
        sanitized_text, redaction_map = sanitize_report_text(
            text=report_text,
            patient_name=patient_name,
        )
        logger.info(f"Report summarizer: {get_redaction_summary(redaction_map)}")
        # ── End PII Sanitization ───────────────────────────────────────────────

        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a medical report interpreter for a hospital WhatsApp bot in India. "
                        "Your job is to read lab reports and explain them in simple, clear language that a "
                        "non-medical patient can understand. Always be reassuring but honest. Use simple English. "
                        "Never give diagnosis. Always recommend consulting the doctor for anything abnormal. "
                        "Respond ONLY in valid JSON with no markdown, no backticks, no preamble."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Patient name: [PATIENT]\n"
                        f"Report type: {report_type}\n"
                        # Send sanitized text — no raw PII reaches OpenRouter
                        f"Report text:\n{sanitized_text[:3000]}\n\n"
                        "Respond with JSON in exactly this format:\n"
                        "{\n"
                        '  "summary_lines": ["line1", "line2", "line3"],\n'
                        '  "has_abnormal_values": true or false,\n'
                        '  "patient_message": "A 2-3 sentence plain English message to send to the patient '
                        "explaining the key findings. Refer to the patient as [PATIENT]. End with advising them to consult "
                        'their doctor if anything is marked abnormal.",\n'
                        '  "doctor_flag_reason": "One sentence reason to flag for doctor review, or null if '
                        'everything is normal"\n'
                        "}"
                    ),
                },
            ]

            response = await call_openrouter_with_backoff(
                messages=messages,
                timeout=float(settings.openrouter_timeout or 12),
                max_tokens=600,
                response_format={"type": "json_object"},
            )

            if isinstance(response, dict) and "choices" in response:
                content = response["choices"][0]["message"]["content"]
            elif hasattr(response, "choices"):
                choice = response.choices[0]
                content = getattr(choice.message, "content", None) or choice.message["content"]
            else:
                raise ValueError(f"Unexpected LLM response format: {type(response)}")

            content = content.strip()

            # Strip markdown code fences if present
            if "```json" in content:
                content = content.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in content:
                content = content.split("```", 1)[1].split("```", 1)[0].strip()

            # Robust JSON boundary extraction
            start_idx = content.find("{")
            end_idx = content.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                content = content[start_idx : end_idx + 1]

            parsed = json.loads(content.strip())

            # ── Restore patient name in the patient-facing message ─────────────
            patient_msg = parsed.get("patient_message", "")
            if redaction_map and patient_msg:
                patient_msg = restore_pii(patient_msg, redaction_map)
            # ── End PII Restore ────────────────────────────────────────────────

            return {
                "patient_message": patient_msg,
                "has_abnormal": bool(parsed.get("has_abnormal_values", False)),
                "doctor_flag_reason": parsed.get("doctor_flag_reason"),
                "fallback": False,
            }

        except Exception as e:
            logger.warning(f"Report summarizer failed: {e}")
            return {"summary": None, "has_abnormal": False, "fallback": True}
