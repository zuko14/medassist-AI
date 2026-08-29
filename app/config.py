from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Meta WhatsApp Cloud API
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v22.0"  # Graph API version; bump here when Meta sunsets a version
    waba_display_name: str = "Kriya AI Hospital"

    # OpenRouter AI (Primary MedAssist LLM Provider)
    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-chat"
    openrouter_base_url: str = "https://openrouter.ai/api/v1/chat/completions"
    openrouter_timeout: int = 8

    # Groq AI (Deprecated Fallback)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Supabase
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # Hospital Config
    hospital_name: str = "City Care Hospital"
    hospital_emergency_number: str = "108"
    hospital_staff_alert_number: str = ""  # optional — WhatsApp number reception monitors for emergency alerts; blank = disabled
    hospital_phone: str = "+919876543210"
    hospital_maps_link: str = ""
    hospital_website: str = ""
    hospital_privacy_policy_url: str = ""
    hospital_address: str = ""
    hospital_landmark: str = ""
    booking_ref_prefix: str = "MC"

    # App
    app_env: str = "production"
    app_port: int = 8000
    log_level: str = "INFO"

    # Admin
    admin_username: str = "admin"
    admin_password: str = "admin"
    owner_username: str = ""  # platform-owner dashboard login — blank = disabled
    owner_password: str = ""  # must be set explicitly; never ship a real default

    # Multi-tenant (optional — used when clinics table exists)
    admin_secret: str = ""  # protects /admin/clinics routes
    meta_verify_token: str = ""  # for Meta webhook handshake (global fallback)

    # Security
    meta_app_secret: str = ""  # Meta App Secret for X-Hub-Signature-256 verification
    rate_limit_login: str = "5/minute"  # Rate limit for admin login attempts
    allow_unsigned_webhooks_dev: bool = False  # NEVER set true outside local dev — explicit opt-in only
    metrics_token: str = ""  # Bearer token for /metrics scraping endpoint (T3.1 / KRIYA-009)

    # KA-03: Tenant scope enforcement is ENABLED by default. An unscoped
    # non-super-admin is DENIED. The shadow-mode observation period (Rule 4)
    # from the 2026-08-27 remediation is concluded.
    tenant_scope_enforce: bool = True

    # T1.4 (KRIYA-016): when False, message_queue errors log MESSAGE_QUEUE_FAIL_OPEN
    # in shadow mode. Flip to True to enforce fail-closed across all queue methods.
    queue_fail_closed_enforce: bool = False

    # ABDM / ABHA Integration (optional — leave empty to skip live verification)
    abdm_client_id: str = ""
    abdm_client_secret: str = ""
    abdm_base_url: str = "https://abhasbx.abdm.gov.in/abha/api"  # sandbox default

    # Data Retention (NMC + DPDP compliance)
    clinical_retention_years: int = 7  # NMC mandate: 7 years minimum
    conversation_purge_days: int = 30  # DPDP minimization: 30-day chat log purge

    # Razorpay Payment Gateway
    razorpay_key_id: str = ""  # From Razorpay Dashboard → Settings → API Keys
    razorpay_key_secret: str = ""  # Keep this secret — never expose in frontend
    razorpay_webhook_secret: str = ""  # From Razorpay Dashboard → Webhooks → Secret
    booking_fee_paise: int = (
        50000  # Fallback fee (₹500) if doctor has no consultation_fee
    )
    booking_hold_minutes: int = 10  # How long a pending_payment slot is held
    refund_window_hours: int = 4  # Minimum hours before slot for refund eligibility

    # Integration Connectors (MocDoc, Practo, etc.)
    integration_secret: str = ""  # Shared secret for /internal/integrations/* endpoints
    connector_encryption_key: str = (
        ""  # Fernet key for encrypting HMIS credentials at rest
    )
    medassist_url: str = "http://localhost:8000"  # Base URL where FastAPI is running
    lab_report_template_name: str = "lab_report_delivery"  # Meta pre-approved utility template for reports outside 24h window
    # Optional 3-variable variant ({{1}} name, {{2}} report, {{3}} AI summary).
    # A template send does not open the 24h window, so outside it the summary
    # can only reach the patient inside the template itself. Unset = summary is
    # generated and stored but NOT delivered (recorded as ai_summary_sent=False).
    lab_report_summary_template_name: str = ""

    # ── Post-visit patient follow-up ──────────────────────────────────────────
    # Platform-wide defaults; each clinic overrides these from the admin panel
    # (Hospital Profile -> Patient Follow-ups), stored in clinics.config.
    followup_enabled_default: bool = True
    followup_days_after_visit: int = 1
    # Optional template whose {{2}} carries the clinic's own follow-up message
    # ({{1}} is the patient's first name). A business-initiated message must be
    # a template, so without this the admin's custom wording cannot be
    # delivered and the built-in post_appointment_followup is used instead.
    followup_template_name: str = "post_appointment_followup"
    followup_message_template_name: str = ""
    admin_alert_template_name: str = ""  # Meta utility template (1 body var) for connector alerts outside 24h window

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
