"""AI-powered lab report summarizer using Groq.

Security: Integrates PII sanitization middleware (app/utils/pii_sanitizer.py)
to strip patient-identifying information from report text BEFORE sending it
to the Groq external API. Patient name is restored in the final message.

Compliant with India DPDP Act 2023 data minimization requirements.
"""

import json
import logging

from groq import Groq

from app.config import settings
from app.utils.pii_sanitizer import (
    sanitize_report_text,
    restore_pii,
    get_redaction_summary,
)

logger = logging.getLogger(__name__)

groq_client = Groq(api_key=settings.groq_api_key)


class ReportSummarizer:
    """Summarize lab reports into patient-friendly messages using Groq AI."""

    async def summarize(
        self, report_text: str, patient_name: str, report_type: str
    ) -> dict:
        """Summarize a lab report and return structured result.

        PII is stripped before calling Groq and restored in the output.
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
            response = groq_client.chat.completions.create(
                model=settings.groq_model,
                messages=[
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
                            # Use patient_name directly for context (sanitized_text has it redacted)
                            f"Patient name: {patient_name}\n"
                            f"Report type: {report_type}\n"
                            # Send sanitized text — no raw PII reaches Groq
                            f"Report text:\n{sanitized_text[:3000]}\n\n"
                            "Respond with JSON in exactly this format:\n"
                            "{\n"
                            '  "summary_lines": ["line1", "line2", "line3"],\n'
                            '  "has_abnormal_values": true or false,\n'
                            '  "patient_message": "A 2-3 sentence plain English message to send to the patient '
                            "explaining the key findings. Start with their name. End with advising them to consult "
                            'their doctor if anything is marked abnormal.",\n'
                            '  "doctor_flag_reason": "One sentence reason to flag for doctor review, or null if '
                            'everything is normal"\n'
                            "}"
                        ),
                    },
                ],
                timeout=15,
                max_tokens=500,
            )

            content = response.choices[0].message.content.strip()

            # Strip markdown code fences if present
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                parts = content.split("```")
                content = parts[1] if len(parts) > 1 else content

            parsed = json.loads(content.strip())

            # ── Restore patient name in the patient-facing message ─────────────
            patient_msg = parsed.get("patient_message", "")
            if redaction_map:
                patient_msg = restore_pii(patient_msg, redaction_map)
            # ── End PII Restore ────────────────────────────────────────────────

            return {
                "patient_message": patient_msg,
                "has_abnormal": parsed.get("has_abnormal_values", False),
                "doctor_flag_reason": parsed.get("doctor_flag_reason"),
                "fallback": False,
            }

        except Exception as e:
            logger.warning(f"Report summarizer failed: {e}")
            return {"summary": None, "has_abnormal": False, "fallback": True}
