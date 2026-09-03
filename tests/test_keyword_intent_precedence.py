"""Precedence and word-boundary rules for the deterministic intent fallback.

This path runs whenever the LLM is unavailable, rate-limited, or the message
looks like a prompt injection — in other words, exactly when the clinic can
least afford a misroute. Two defects lived here:

1. Plain dict order let a generic noun outrank an explicit action, so
   "cancel my appointment" matched book_appointment's "appointment" and pushed
   a patient trying to cancel into a NEW booking.
2. Bare substring matching fired on fragments of unrelated words: "fits"
   inside "benefits" raised an emergency, and "move" inside "remove my
   information" stole a DPDP deletion request.
"""

import pytest

from app.services.ai_engine import keyword_intent_fallback


@pytest.mark.parametrize(
    "message, expected",
    [
        # Acting on an existing booking outranks making one.
        ("cancel my appointment", "cancel_appointment"),
        ("please cancel my appointment tomorrow", "cancel_appointment"),
        ("reschedule my appointment", "reschedule_appointment"),
        ("change my appointment time", "reschedule_appointment"),
        ("postpone my visit", "reschedule_appointment"),
        ("follow up visit", "followup_booking"),
        ("free checkup", "followup_booking"),
        # An explicit phrase beats a generic word from a higher tier:
        # "stop booking" is a cancellation, not an opt-out that would silence
        # every future notification to this patient.
        ("stop booking", "cancel_appointment"),
        ("unsubscribe", "opt_out"),
        # Wanting to SEE a clinician is a booking, not a roster request.
        ("I want to see a doctor", "book_appointment"),
        ("book doctor", "book_appointment"),
        ("book appointment with doctor", "book_appointment"),
        ("I need a doctor", "book_appointment"),
    ],
)
def test_precedence_routes_the_patient_to_the_right_flow(message, expected):
    assert keyword_intent_fallback(message) == expected


@pytest.mark.parametrize(
    "message, expected",
    [
        # "fits" inside these must not raise an emergency alert to staff.
        ("what are the benefits", "unknown"),
        ("my outfits", "unknown"),
        # "move" inside "remove" must not steal the deletion request.
        ("remove my information", "data_deletion_request"),
        # "hi" inside "this" is not a greeting.
        ("is this the right number for reports", "view_reports"),
    ],
)
def test_keywords_match_whole_words_only(message, expected):
    assert keyword_intent_fallback(message) == expected


def test_curly_apostrophe_still_reaches_the_emergency_path():
    """Phone keyboards type U+2019, so the straight-quote keyword never matched."""
    assert keyword_intent_fallback("i can’t breathe") == "emergency"
    assert keyword_intent_fallback("i can't breathe") == "emergency"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Our Doctors", "doctor_availability"),
        ("doctor list", "doctor_availability"),
        ("Doctors", "doctor_availability"),
        ("Our Services", "view_services"),
        ("departments", "view_services"),
        ("Book Appointment", "book_appointment"),
        ("book a slot", "book_appointment"),
        ("I have fever", "book_appointment"),
        ("My Reports", "view_reports"),
        ("lab reports", "view_reports"),
        ("delete my data", "data_deletion_request"),
        ("what is my token number", "queue_status"),
        ("how many patients ahead of me", "queue_status"),
        ("heart attack help", "emergency"),
        ("severe bleeding", "emergency"),
        ("hello", "greeting"),
        ("talk to a human", "human_escalation"),
        # Hindi and Telugu keywords must keep matching as whole words.
        ("मा डॉक्टर", "doctor_availability"),
        ("మా డాక్టర్లు", "doctor_availability"),
        ("మా సేవలు", "view_services"),
        ("ల్యాబ్ రిపోర్టులు", "view_reports"),
        ("बेहोश", "emergency"),
    ],
)
def test_established_routing_is_unchanged(message, expected):
    assert keyword_intent_fallback(message) == expected
