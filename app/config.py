"""Application settings loaded from environment variables."""

from pydantic import BaseModel
from dotenv import load_dotenv
import os

# Load key/value pairs from .env into process environment.
load_dotenv()


class Settings(BaseModel):
    """Typed container for app, database, and integration configuration."""

    app_name: str = os.getenv("APP_NAME", "Shaky Razor AI Receptionist")
    app_env: str = os.getenv("APP_ENV", "development")
    base_url: str = os.getenv("BASE_URL", "http://localhost:8000")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./shaky_razor.db")

    twilio_account_sid: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_phone_number: str = os.getenv("TWILIO_PHONE_NUMBER", "")
    owner_phone_number: str = os.getenv("OWNER_PHONE_NUMBER", "")

    shop_booking_link: str = os.getenv("SHOP_BOOKING_LINK", "")
    shop_name: str = os.getenv("SHOP_NAME", "The Shaky Razor")
    shop_hours: str = os.getenv("SHOP_HOURS", "Mon-Sat 9:00 AM - 6:00 PM")
    shop_address: str = os.getenv("SHOP_ADDRESS", "Logan, Utah")

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5-mini")

    use_elevenlabs: bool = os.getenv("USE_ELEVENLABS", "false").lower() == "true"
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")
    elevenlabs_voice_id: str = os.getenv("ELEVENLABS_VOICE_ID", "")
    elevenlabs_model: str = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")


settings = Settings()