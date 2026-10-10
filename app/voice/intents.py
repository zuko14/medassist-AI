"""Versioned healthcare intent registry for the voice receptionist.

Every intent in the product taxonomy is listed so analytics, the LLM schema and
the admin panel share one vocabulary. HANDLED is the subset the dialog engine
executes autonomously today; every other intent is answered honestly
("I'll connect you to our staff") and routed to a human — never guessed at.
"""

TAXONOMY_VERSION = "intents-2026.10.08"

ALL_INTENTS = (
    "GREETING", "GENERAL_INFORMATION", "HOSPITAL_INFORMATION", "DEPARTMENT_INFORMATION",
    "DOCTOR_INFORMATION", "DOCTOR_AVAILABILITY", "BOOK_APPOINTMENT", "RESCHEDULE_APPOINTMENT",
    "CANCEL_APPOINTMENT", "CONFIRM_APPOINTMENT", "APPOINTMENT_STATUS", "FOLLOW_UP_APPOINTMENT",
    "NEW_PATIENT_REGISTRATION", "PATIENT_PROFILE_UPDATE", "FAMILY_MEMBER_BOOKING", "QUEUE_STATUS",
    "WAIT_TIME", "DIRECTIONS", "LOCATION", "CONTACT_INFORMATION", "FEES", "INSURANCE_INFORMATION",
    "PAYMENT_STATUS", "PAYMENT_LINK", "PAYMENT_FAILED", "REFUND_STATUS", "LAB_INFORMATION",
    "LAB_TEST_SEARCH", "LAB_TEST_BOOKING", "HOME_SAMPLE_COLLECTION", "SAMPLE_STATUS", "REPORT_STATUS",
    "REPORT_READY", "REPORT_DELIVERY", "REPORT_REDELIVERY", "DOWNLOAD_REPORT", "PRESCRIPTION_STATUS",
    "PRESCRIPTION_DELIVERY", "PHARMACY_INFORMATION", "FOLLOW_UP_REMINDER", "HEALTH_CAMP",
    "CORPORATE_SCREENING", "DOCTOR_LEAVE", "SERVICE_AVAILABILITY", "COMPLAINT", "FEEDBACK",
    "CALLBACK_REQUEST", "HUMAN_AGENT_REQUEST", "EMERGENCY", "CLINICAL_QUERY", "UNKNOWN", "MULTI_INTENT",
    # A question about the hospital's services, treatments, doctors, facilities or policies:
    # answered from its records mid-call (dialog._answer_question), never a workflow of its own.
    "KNOWLEDGE_QUESTION",
    # dialog acts (not business intents)
    "AFFIRM", "DENY", "GOODBYE", "REPEAT", "LANGUAGE_CHANGE", "ASK_IF_AI",
)

# Collapsed onto the workflow that serves them.
WORKFLOW_OF = {
    "BOOK_APPOINTMENT": "BOOKING", "DOCTOR_AVAILABILITY": "BOOKING", "FOLLOW_UP_APPOINTMENT": "BOOKING",
    "FAMILY_MEMBER_BOOKING": "BOOKING", "DOCTOR_INFORMATION": "BOOKING",
    "CANCEL_APPOINTMENT": "CANCEL", "RESCHEDULE_APPOINTMENT": "RESCHEDULE",
    "APPOINTMENT_STATUS": "STATUS", "CONFIRM_APPOINTMENT": "STATUS", "PAYMENT_STATUS": "STATUS",
    "REPORT_STATUS": "REPORT", "REPORT_READY": "REPORT", "REPORT_DELIVERY": "REPORT",
    "REPORT_REDELIVERY": "REPORT", "DOWNLOAD_REPORT": "REPORT",
    "LAB_TEST_SEARCH": "LAB_PRICE", "LAB_INFORMATION": "LAB_PRICE", "LAB_TEST_BOOKING": "LAB_BOOKING",
    "FEES": "FEES",
    "HOSPITAL_INFORMATION": "INFO", "GENERAL_INFORMATION": "INFO", "LOCATION": "INFO",
    "DIRECTIONS": "INFO", "CONTACT_INFORMATION": "INFO",
    "QUEUE_STATUS": "QUEUE", "WAIT_TIME": "QUEUE",
    # A receptionist serves these directly instead of passing the caller on: a new patient
    # is registered by booking them; "is Dr. X in tomorrow?" is a slot lookup (leaves and
    # holidays are applied); payment and sample questions read the caller's own records.
    "NEW_PATIENT_REGISTRATION": "BOOKING", "DOCTOR_LEAVE": "BOOKING",
    "PAYMENT_LINK": "STATUS", "PAYMENT_FAILED": "STATUS", "SAMPLE_STATUS": "REPORT",
    "HUMAN_AGENT_REQUEST": "HUMAN", "CALLBACK_REQUEST": "CALLBACK",
    "COMPLAINT": "CALLBACK", "FEEDBACK": "CALLBACK",
}

HANDLED = frozenset(WORKFLOW_OF)

# Risk class decides confirmation and verification requirements (policy.py).
RISK = {
    "BOOKING": "transactional", "LAB_BOOKING": "transactional", "CANCEL": "transactional",
    "RESCHEDULE": "transactional", "REPORT": "sensitive", "STATUS": "sensitive", "QUEUE": "sensitive",
    "FEES": "informational", "LAB_PRICE": "informational", "INFO": "informational",
    "HUMAN": "routing", "CALLBACK": "routing",
}
