"""Tests for AI engine."""

import pytest
from unittest.mock import patch, MagicMock

from app.services import ai_engine


class TestAIEngine:
    """Test AI engine functionality."""

    @pytest.mark.asyncio
    async def test_detect_intent_booking(self):
        """Test intent detection for booking."""
        # Mock Groq client
        with patch.object(ai_engine, 'groq_client') as mock_client:
            mock_response = MagicMock()
            mock_response.choices = [MagicMock(message=MagicMock(content="book_appointment"))]
            mock_client.chat.completions.create.return_value = mock_response

            result = await ai_engine.detect_intent("I want to book an appointment")
            assert result == "book_appointment"

    @pytest.mark.asyncio
    async def test_detect_intent_emergency(self):
        """Test intent detection for emergency."""
        result = await ai_engine.detect_intent(" bleeding help emergency")
        assert result == "emergency"

    @pytest.mark.asyncio
    async def test_detect_intent_fallback(self):
        """Test intent detection fallback."""
        # Make Groq fail
        with patch.object(ai_engine, 'groq_client') as mock_client:
            mock_client.chat.completions.create.side_effect = Exception("API error")

            result = await ai_engine.detect_intent("book appointment please")
            assert result == "book_appointment"

    @pytest.mark.asyncio
    async def test_keyword_intent_fallback(self):
        """Test keyword fallback for intent detection."""
        # Emergency keywords
        assert await ai_engine.detect_intent("heart attack") == "emergency"
        assert await ai_engine.detect_intent("severe bleeding") == "emergency"

        # Booking keywords
        assert ai_engine.keyword_intent_fallback("I want to book") == "book_appointment"

        # Opt-out
        assert ai_engine.keyword_intent_fallback("stop messaging me") == "opt_out"

        # Human escalation
        assert ai_engine.keyword_intent_fallback("talk to human") == "human_escalation"

    @pytest.mark.asyncio
    async def test_map_symptom_to_department(self):
        """Test symptom to department mapping."""
        # Test fallback mapping
        result = await ai_engine.map_symptom_to_department("chest pain")
        assert result["suggested_department"] == "Cardiology"
        assert result["is_emergency"] is True

        result = await ai_engine.map_symptom_to_department("toothache")
        assert result["suggested_department"] == "Dental"

        result = await ai_engine.map_symptom_to_department("fever and cold")
        assert result["suggested_department"] == "General Medicine"

    def test_language_detection(self):
        """Test language detection."""
        assert ai_engine.detect_language("Hello") == "en"
        assert ai_engine.detect_language("नमस्ते") == "hi"
        assert ai_engine.detect_language("నమస్కారం") == "te"

    @pytest.mark.asyncio
    async def test_generate_response(self):
        """Test response generation."""
        with patch.object(ai_engine, 'groq_client') as mock_client:
            mock_response = MagicMock()
            mock_response.choices = [MagicMock(message=MagicMock(content="Hello, how can I help?"))]
            mock_client.chat.completions.create.return_value = mock_response

            result = await ai_engine.generate_response("Hi", {}, "en")
            assert "Hello" in result or "how" in result.lower()


class TestSymptomMap:
    """Test symptom department mapping."""

    def test_symptom_map_keys(self):
        """Test that symptom map has expected keys."""
        assert "chest pain" in ai_engine.SYMPTOM_DEPARTMENT_MAP
        assert "fever" in ai_engine.SYMPTOM_DEPARTMENT_MAP
        assert "tooth" in ai_engine.SYMPTOM_DEPARTMENT_MAP

    def test_symptom_map_structure(self):
        """Test symptom map structure."""
        for symptom, (dept, is_emergency) in ai_engine.SYMPTOM_DEPARTMENT_MAP.items():
            assert isinstance(dept, str)
            assert isinstance(is_emergency, bool)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
