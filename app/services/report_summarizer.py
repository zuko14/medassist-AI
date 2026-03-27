"""AI-powered lab report summarizer using Groq."""

import json
import logging

from groq import Groq

from app.config import settings

logger = logging.getLogger(__name__)

groq_client = Groq(api_key=settings.groq_api_key)


class ReportSummarizer:
    """Summarize lab reports into patient-friendly messages using Groq AI."""

    async def summarize(self, report_text: str, patient_name: str, report_type: str) -> dict:
        """Summarize a lab report and return structured result."""

        if not report_text or len(report_text) < 50:
            return {"summary": None, "has_abnormal": False, "fallback": True}

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
                            f"Patient name: {patient_name}\n"
                            f"Report type: {report_type}\n"
                            f"Report text:\n{report_text[:3000]}\n\n"
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

            return {
                "patient_message": parsed.get("patient_message"),
                "has_abnormal": parsed.get("has_abnormal_values", False),
                "doctor_flag_reason": parsed.get("doctor_flag_reason"),
                "fallback": False,
            }

        except Exception as e:
            logger.warning(f"Report summarizer failed: {e}")
            return {"summary": None, "has_abnormal": False, "fallback": True}
