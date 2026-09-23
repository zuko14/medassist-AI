import os
import time

import pytest

# ── Hermetic test environment (runs at conftest import, BEFORE any app module) ──
# pydantic-settings reads `.env`, and `.env` points at PRODUCTION Supabase and the
# live WhatsApp token. The session fixture below only did os.environ.setdefault,
# which runs after collection has already imported app.config — so a plain
# `pytest` ran against production: it took production scheduler/phone locks and
# could reach real patients. Real env vars beat `.env` in pydantic-settings, so
# forcing them here, first, makes every Settings() in the run a test Settings().
# Set KRIYA_TEST_LIVE=1 to opt back in to whatever `.env` names, deliberately.
_FORCED_TEST_CREDENTIALS = {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test_service_role_key",
    "WHATSAPP_TOKEN": "test_token",
    "WHATSAPP_PHONE_NUMBER_ID": "000000000000",
    "META_APP_SECRET": "test_meta_app_secret",
    "OPENROUTER_API_KEY": "test_openrouter_key",
    "GROQ_API_KEY": "test_groq_key",
    "ADMIN_SECRET": "test_admin_secret",
    "INTEGRATION_SECRET": "test_integration_secret",
    "METRICS_TOKEN": "test_metrics_token",
    "ABDM_CLIENT_SECRET": "",
    "RAZORPAY_KEY_ID": "",
    "RAZORPAY_KEY_SECRET": "",
    "RAZORPAY_WEBHOOK_SECRET": "",
}
if os.environ.get("KRIYA_TEST_LIVE") != "1":
    os.environ.update(_FORCED_TEST_CREDENTIALS)

pytest_plugins = ["tests.conftest_db"]

# Modules that exercise the real Postgres-backed lock and must not get the fake.
_REAL_LOCK_MODULES = {
    "test_lab_delivery_regressions",
    "test_phase_d_scheduler_distributed_safety",
    "test_phase_f_real_load_and_failure_injection",
    "test_phase3_multi_instance_concurrency",
    "test_phone_lease_heartbeat",
}


@pytest.fixture(autouse=True)
def _in_memory_distributed_lock(request, monkeypatch):
    """Scheduler + per-phone leases held in memory instead of scheduler_locks.

    Without this, every job and every handle_message() test needed the live
    database to acquire its lease, and failed (or stole production's) without it.
    """
    if os.environ.get("KRIYA_TEST_LIVE") == "1":
        return
    if request.module.__name__.rsplit(".", 1)[-1] in _REAL_LOCK_MODULES:
        return
    from app.services.distributed_lock import DistributedJobLock

    held: dict = {}

    async def acquire(self, job_name, lease_seconds=300):
        owner, expires = held.get(job_name, (None, 0.0))
        if owner not in (None, self.worker_id) and expires > time.monotonic():
            return False
        held[job_name] = (self.worker_id, time.monotonic() + lease_seconds)
        return True

    async def renew(self, job_name, lease_seconds=300):
        if held.get(job_name, (None,))[0] != self.worker_id:
            return False
        held[job_name] = (self.worker_id, time.monotonic() + lease_seconds)
        return True

    async def release(self, job_name):
        if held.get(job_name, (None,))[0] == self.worker_id:
            held.pop(job_name, None)
        return True

    monkeypatch.setattr(DistributedJobLock, "acquire", acquire)
    monkeypatch.setattr(DistributedJobLock, "renew", renew)
    monkeypatch.setattr(DistributedJobLock, "release", release)


@pytest.fixture(scope="session", autouse=True)
def set_dummy_env_vars():
    """Inject required env vars before any app module is imported."""
    env_defaults = {
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

    for key, value in env_defaults.items():
        os.environ.setdefault(key, value)
