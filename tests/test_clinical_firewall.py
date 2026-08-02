"""Tests for the Clinical Firewall (app/services/clinical_firewall.py).

Verifies that:
  - Medication names are intercepted before reaching the LLM
  - Diagnostic questions are blocked
  - Treatment-seeking patterns are caught
  - Normal booking messages pass through unblocked
  - LLM output containing medication names is caught by output validator
  - All three languages (en, hi, te) are handled
"""

from app.services.clinical_firewall import screen_message, validate_llm_output


class TestClinicalFirewallScreenMessage:
    """Tests for the pre-LLM input screening."""

    # ── Medication Keywords ───────────────────────────────────────────────────

    def test_blocks_paracetamol_english(self):
        blocked, response = screen_message("can I take paracetamol for fever", "en")
        assert blocked is True
        assert response is not None
        assert "appointment" in response.lower()

    def test_blocks_antibiotic_english(self):
        blocked, response = screen_message(
            "which antibiotic should I take for infection", "en"
        )
        assert blocked is True

    def test_blocks_dolo_english(self):
        blocked, response = screen_message("is dolo safe for cold", "en")
        assert blocked is True

    def test_blocks_ibuprofen_english(self):
        blocked, response = screen_message("ibuprofen dosage for headache", "en")
        assert blocked is True

    def test_blocks_insulin_english(self):
        blocked, response = screen_message("how much insulin should I take", "en")
        assert blocked is True

    def test_blocks_hindi_medication(self):
        """Test Hindi medication keyword blocking."""
        blocked, response = screen_message("कौन सी दवा लूं", "hi")
        assert blocked is True
        assert response is not None

    def test_blocks_telugu_medication(self):
        """Test Telugu medication keyword blocking."""
        blocked, response = screen_message("ఏ మందు తీసుకోవాలి", "te")
        assert blocked is True

    # ── Diagnostic Questions ──────────────────────────────────────────────────

    def test_blocks_diagnose_me(self):
        blocked, response = screen_message("please diagnose me", "en")
        assert blocked is True

    def test_blocks_what_disease(self):
        blocked, response = screen_message("what disease do I have", "en")
        assert blocked is True

    def test_blocks_is_this_cancer(self):
        blocked, response = screen_message("is this cancer?", "en")
        assert blocked is True

    def test_blocks_am_i_diabetic(self):
        blocked, response = screen_message("am i diabetic doctor?", "en")
        assert blocked is True

    # ── Treatment Seeking Patterns ────────────────────────────────────────────

    def test_blocks_what_medicine_for_fever(self):
        blocked, response = screen_message(
            "what medicine should I take for fever", "en"
        )
        assert blocked is True

    def test_blocks_which_tablet_for_pain(self):
        blocked, response = screen_message("which tablet for my back pain", "en")
        assert blocked is True

    def test_blocks_prescribe_me(self):
        blocked, response = screen_message("can you prescribe me something", "en")
        assert blocked is True

    def test_blocks_how_to_cure(self):
        blocked, response = screen_message("how to cure my cough at home", "en")
        assert blocked is True

    # ── Normal Messages — Should NOT be blocked ───────────────────────────────

    def test_allows_book_appointment(self):
        blocked, _ = screen_message("I want to book an appointment", "en")
        assert blocked is False

    def test_allows_fever_symptom(self):
        """Patient reporting symptom to book appointment — should not be blocked."""
        blocked, _ = screen_message("I have a fever and need to see a doctor", "en")
        assert blocked is False

    def test_allows_greeting(self):
        blocked, _ = screen_message("Hello, good morning", "en")
        assert blocked is False

    def test_allows_hindi_booking(self):
        blocked, _ = screen_message("डॉक्टर से अपॉइंटमेंट चाहिए", "hi")
        assert blocked is False

    def test_allows_telugu_booking(self):
        blocked, _ = screen_message("నాకు డాక్టర్ అపాయింట్‌మెంట్ కావాలి", "te")
        assert blocked is False

    def test_allows_empty_message(self):
        blocked, _ = screen_message("", "en")
        assert blocked is False

    def test_allows_cancel_appointment(self):
        blocked, _ = screen_message("I want to cancel my appointment", "en")
        assert blocked is False

    def test_allows_lab_report_query(self):
        blocked, _ = screen_message("I want to check my lab report", "en")
        assert blocked is False

    # ── Language-Specific Responses ───────────────────────────────────────────

    def test_english_response_format(self):
        blocked, response = screen_message("which antibiotic", "en")
        assert blocked is True
        assert "🏥" in response
        assert "appointment" in response.lower()

    def test_hindi_response_format(self):
        blocked, response = screen_message("which antibiotic", "hi")
        assert blocked is True
        assert "🏥" in response

    def test_telugu_response_format(self):
        blocked, response = screen_message("which antibiotic", "te")
        assert blocked is True
        assert "🏥" in response


class TestClinicalFirewallOutputValidation:
    """Tests for the post-LLM output scanning."""

    def test_catches_dosage_in_output(self):
        llm_output = "You should take 500mg of paracetamol twice daily."
        is_safe, final = validate_llm_output(llm_output, "en")
        assert is_safe is False
        assert "appointment" in final.lower()

    def test_catches_medication_name_in_output(self):
        llm_output = "I recommend taking dolo for your fever."
        is_safe, final = validate_llm_output(llm_output, "en")
        assert is_safe is False

    def test_catches_mg_dosage(self):
        llm_output = "Take 400mg ibuprofen every 6 hours."
        is_safe, final = validate_llm_output(llm_output, "en")
        assert is_safe is False

    def test_allows_clean_output(self):
        llm_output = "I can help you book an appointment with General Medicine."
        is_safe, final = validate_llm_output(llm_output, "en")
        assert is_safe is True
        assert final == llm_output

    def test_allows_department_recommendation(self):
        llm_output = "Based on your symptoms, you should see the Cardiology department."
        is_safe, final = validate_llm_output(llm_output, "en")
        assert is_safe is True

    def test_empty_output_is_safe(self):
        is_safe, final = validate_llm_output("", "en")
        assert is_safe is True
