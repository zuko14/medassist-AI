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

    # T1.4 (KRIYA-016): Queue fail-closed is ENABLED by default. Message queue
    # failures fail closed to prevent duplicate processing. The 48h shadow-mode
    # observation period from the 2026-08-27 remediation is concluded.
    queue_fail_closed_enforce: bool = True

    # How many application processes serve this deployment.
    # render.yaml sets numInstances: 2 and the Dockerfile CMD uses
    # --workers ${WEB_CONCURRENCY:-2}, so the default is 2 x 2 = 4.
    # Used only to make per-process alerts self-describing, so an operator
    # reading one alert knows how many other processes to check
    # (app/services/scheduler.py:alert_message_queue_fail_closed).
    expected_process_count: int = 4

    # Worker threads available to app.database.sb() for off-loop PostgREST
    # execution (T5.1 / KA-P1-03). This is the per-process concurrency ceiling
    # for database work: 40 is AnyIO's default, raised here because every query
    # now takes a token.
    #
    # Do NOT raise this above the connection budget Supabase/PostgREST will
    # accept for this deployment — that only moves the queue from the limiter
    # to the connection pool, turning a bounded wait into a timeout.
    db_thread_pool_size: int = 64

    # Hard ceiling on a single PostgREST/storage call, in seconds.
    # sb() runs queries on a bounded worker pool, so a call that never returns
    # permanently retires a worker thread. Without this, a degraded Supabase
    # does not slow the service — it consumes the pool one thread at a time
    # until nothing can run, and presents as a hang rather than an error.
    # 5s — deliberately postgrest-py's own default, so making the timeout
    # explicit never makes any call slower than it was before it was set. The
    # value of stating it is that it now also covers the storage client and is
    # tunable per deployment; raising it above the library default would just
    # hold a worker thread out of the pool for longer on a call that is not
    # going to answer usefully anyway. Well under Meta's 20s webhook budget.
    db_query_timeout_seconds: int = 5

    # Whether THIS process runs connector polling (Playwright/Chromium).
    #
    # KA-P2-20: polling was folded into the web service, so a headless Chromium
    # launches inside the container that must acknowledge Meta webhooks within
    # 20s. Chromium's memory profile is spiky; an OOM kill takes the web process
    # down and every in-flight BackgroundTask with it.
    #
    # Default True preserves existing behaviour exactly — a deployment that has
    # not provisioned the dedicated worker keeps polling rather than silently
    # stopping. render.yaml sets this to false on the web services and runs
    # `python -m connectors.runner --all` in its own worker instead.
    #
    # Safe either way: run_connector() takes a per-connector CAS advisory lock
    # with a 5-minute lease (connectors/runner.py), so a worker and a stale web
    # tick cannot poll the same connector concurrently.
    run_connectors_in_web: bool = True


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
