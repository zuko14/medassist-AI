import os
import pytest

pytest_plugins = ["tests.conftest_db"]


# ── Environment injection ────────────────────────────────────────────────────
# app/config.py builds its `settings` singleton at import time, and pytest
# imports test modules (and through them the app package) during collection —
# which happens BEFORE any fixture runs. Injecting these inside a fixture is
# therefore too late: `settings` would already be frozen with empty values and
# every credential-gated route would answer 503. Keep this at module scope;
# conftest.py is imported before collection, so the values land in time.
ENV_DEFAULTS = {
    "WHATSAPP_TOKEN": "test_token",
    "WHATSAPP_PHONE_NUMBER_ID": "000000000000",
    "WHATSAPP_VERIFY_TOKEN": "test_verify_token",
    "WABA_DISPLAY_NAME": "Test Hospital",
    "OPENROUTER_API_KEY": "test_openrouter_key",
    "OPENROUTER_MODEL": "deepseek/deepseek-chat",
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
    "OWNER_USERNAME": "test_owner",
    "OWNER_PASSWORD": "test_owner_password_12345",
}

for _key, _value in ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(scope="session", autouse=True)
def set_dummy_env_vars():
    """Re-assert the dummy env vars for anything that reloads settings mid-run."""
    for key, value in ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)


@pytest.fixture
def granted_job_lock():
    """Grant the distributed scheduler lease for tests that exercise a job body.

    Scheduler jobs are distributed singletons and `acquire` fails CLOSED, so an
    unreachable database is indistinguishable from a lease held elsewhere and
    the job body is skipped. That is the correct production behaviour, but it
    means a test asserting on what a job DOES has to be granted the lease
    first, or it silently asserts against a job that never ran.

    Tests covering the lock itself must not use this fixture.
    """
    from unittest.mock import AsyncMock, patch

    from app.services.distributed_lock import distributed_lock_manager

    # distributed_job_lock binds the manager as a default argument at import
    # time, so the instance's methods are patched rather than the module name.
    with patch.object(
        distributed_lock_manager, "acquire", new=AsyncMock(return_value=True)
    ), patch.object(
        distributed_lock_manager, "renew", new=AsyncMock(return_value=True)
    ), patch.object(
        distributed_lock_manager, "release", new=AsyncMock(return_value=True)
    ):
        yield distributed_lock_manager
