from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Meta WhatsApp Cloud API
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_verify_token: str
    waba_display_name: str = "MediAssist Hospital"

    # Groq AI
    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"

    # Supabase
    supabase_url: str
    supabase_service_role_key: str

    # Hospital Config
    hospital_name: str = "City Care Hospital"
    hospital_emergency_number: str = "108"
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

    # Multi-tenant (optional — used when clinics table exists)
    admin_secret: str = ""         # protects /admin/clinics routes
    meta_verify_token: str = ""    # for Meta webhook handshake (global fallback)

    # Security
    meta_app_secret: str = ""      # Meta App Secret for X-Hub-Signature-256 verification
    rate_limit_login: str = "5/minute"  # Rate limit for admin login attempts

    # ABDM / ABHA Integration (optional — leave empty to skip live verification)
    abdm_client_id: str = ""
    abdm_client_secret: str = ""
    abdm_base_url: str = "https://abhasbx.abdm.gov.in/abha/api"  # sandbox default

    # Data Retention (NMC + DPDP compliance)
    clinical_retention_years: int = 7    # NMC mandate: 7 years minimum
    conversation_purge_days: int = 30    # DPDP minimization: 30-day chat log purge

    # Razorpay Payment Gateway
    razorpay_key_id: str = ""            # From Razorpay Dashboard → Settings → API Keys
    razorpay_key_secret: str = ""        # Keep this secret — never expose in frontend
    razorpay_webhook_secret: str = ""    # From Razorpay Dashboard → Webhooks → Secret
    booking_fee_paise: int = 50000       # Fallback fee (₹500) if doctor has no consultation_fee
    booking_hold_minutes: int = 10       # How long a pending_payment slot is held
    refund_window_hours: int = 4         # Minimum hours before slot for refund eligibility

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
