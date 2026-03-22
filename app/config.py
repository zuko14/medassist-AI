from pydantic_settings import BaseSettings
from pydantic import ConfigDict


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

    model_config = ConfigDict(env_file=".env")


settings = Settings()
