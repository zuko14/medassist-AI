"""Shared test fixtures — sets dummy env vars so tests never need a .env file."""

import os
import pytest


@pytest.fixture(scope="session", autouse=True)
def set_dummy_env_vars():
    """Inject required env vars before any app module is imported."""
    env_defaults = {
        "WHATSAPP_TOKEN": "test_token",
        "WHATSAPP_PHONE_NUMBER_ID": "000000000000",
        "WHATSAPP_VERIFY_TOKEN": "test_verify_token",
        "WABA_DISPLAY_NAME": "Test Hospital",
        "GROQ_API_KEY": "test_groq_key",
        "GROQ_MODEL": "llama-3.3-70b-versatile",
        "SUPABASE_URL": "https://test.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "test_service_role_key",
        "HOSPITAL_NAME": "City Care Hospital",
        "HOSPITAL_EMERGENCY_NUMBER": "108",
        "HOSPITAL_PHONE": "+919876543210",
        "HOSPITAL_MAPS_LINK": "https://maps.google.com",
        "HOSPITAL_WEBSITE": "https://test.hospital.com",
        "HOSPITAL_PRIVACY_POLICY_URL": "https://test.hospital.com/privacy",
        "HOSPITAL_ADDRESS": "Test Address",
        "HOSPITAL_LANDMARK": "Test Landmark",
        "BOOKING_REF_PREFIX": "MC",
        "APP_ENV": "testing",
        "APP_PORT": "8000",
        "LOG_LEVEL": "DEBUG",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "admin",
    }

    for key, value in env_defaults.items():
        os.environ.setdefault(key, value)
