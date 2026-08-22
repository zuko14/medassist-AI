"""Unit tests for AI Report Summarizer using OpenRouter."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.report_summarizer import ReportSummarizer


@pytest.mark.asyncio
async def test_summarizer_short_text_fallback():
    summarizer = ReportSummarizer()
    res = await summarizer.summarize("Too short", "Ravi Kumar", "Blood Test")
    assert res["fallback"] is True
    assert res["has_abnormal"] is False
    assert res.get("summary") is None or res.get("patient_message") is None


@pytest.mark.asyncio
async def test_summarizer_openrouter_success():
    summarizer = ReportSummarizer()
    report_text = (
        "VISAKHA MULTISPECIALITY CLINIC\n"
        "COMPLETE BLOOD COUNT REPORT\n"
        "Patient: Ravi Kumar, Age: 42, Phone: +919876543210\n"
        "Hemoglobin: 14.5 g/dL (Normal 13.0 - 17.0)\n"
        "WBC Count: 7,500 /uL (Normal 4,000 - 11,000)\n"
        "Platelet Count: 250,000 /uL (Normal 150,000 - 450,000)\n"
        "Impression: All parameters within normal reference ranges.\n"
    )

    mock_llm_json = {
        "summary_lines": [
            "Hemoglobin is normal at 14.5 g/dL",
            "WBC and Platelets are within normal range",
        ],
        "has_abnormal_values": False,
        "patient_message": "Dear [PATIENT_1], your Complete Blood Count report is normal with all key counts within healthy ranges. Please consult your doctor if you have any questions.",
        "doctor_flag_reason": None,
    }

    mock_response = {
        "id": "gen-test-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(mock_llm_json),
                }
            }
        ],
    }

    with patch("app.services.report_summarizer.call_openrouter_with_backoff", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_response

        res = await summarizer.summarize(report_text, "Ravi Kumar", "Complete Blood Count")

        assert res["fallback"] is False
        assert res["has_abnormal"] is False
        assert "Ravi Kumar" in res["patient_message"]
        assert "[PATIENT_1]" not in res["patient_message"]

        # Verify PII was sanitized in the prompt sent to OpenRouter
        called_args = mock_call.call_args[1]["messages"]
        user_prompt = called_args[1]["content"]
        assert "9876543210" not in user_prompt
        assert "[PHONE_" in user_prompt or "[PATIENT_" in user_prompt


@pytest.mark.asyncio
async def test_summarizer_with_abnormal_values_and_markdown_fence():
    summarizer = ReportSummarizer()
    report_text = (
        "LIPID PROFILE TEST\n"
        "Patient: Sunita Rao, Age: 55\n"
        "Total Cholesterol: 280 mg/dL (High, Normal < 200)\n"
        "Triglycerides: 220 mg/dL (High, Normal < 150)\n"
        "HDL: 38 mg/dL (Low, Normal > 40)\n"
        "LDL: 190 mg/dL (High, Normal < 100)\n"
    )

    mock_llm_json = {
        "summary_lines": ["Cholesterol and Triglycerides are elevated"],
        "has_abnormal_values": True,
        "patient_message": "Dear [PATIENT_1], your Lipid Profile shows elevated cholesterol and triglyceride levels. Please consult your doctor for dietary guidance and treatment.",
        "doctor_flag_reason": "Elevated Total Cholesterol (280) and LDL (190)",
    }

    # Wrap in markdown code fence to test robust parsing
    fenced_content = f"```json\n{json.dumps(mock_llm_json)}\n```"

    mock_response = {
        "id": "gen-test-2",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": fenced_content,
                }
            }
        ],
    }

    with patch("app.services.report_summarizer.call_openrouter_with_backoff", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_response

        res = await summarizer.summarize(report_text, "Sunita Rao", "Lipid Profile")

        assert res["fallback"] is False
        assert res["has_abnormal"] is True
        assert res["doctor_flag_reason"] == "Elevated Total Cholesterol (280) and LDL (190)"
        assert "Sunita Rao" in res["patient_message"]


@pytest.mark.asyncio
async def test_summarizer_graceful_fallback_on_api_error():
    summarizer = ReportSummarizer()
    report_text = "Sample lab report text that is longer than 50 characters to trigger LLM execution."

    with patch("app.services.report_summarizer.call_openrouter_with_backoff", side_effect=RuntimeError("OpenRouter Timeout")):
        res = await summarizer.summarize(report_text, "Anil Varma", "Thyroid Profile")

        assert res["fallback"] is True
        assert res["has_abnormal"] is False
