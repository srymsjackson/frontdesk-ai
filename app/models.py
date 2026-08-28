"""Database models for businesses, captured leads, and per-business call config."""

from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class Business(SQLModel, table=True):
    """A barbershop/business that owns a Twilio number and booking settings."""

    id: Optional[int] = Field(default=None, primary_key=True)

    name: str
    twilio_number: str  # number Twilio receives calls on

    owner_phone: str
    owner_email: Optional[str] = None

    booking_link: Optional[str] = None
    business_type: Optional[str] = None
    timezone: Optional[str] = "UTC"

    is_active: bool = True

    # Stripe subscription tracking. plan is a slug like "basic" or "pro" (see
    # settings.stripe_price_basic / stripe_price_pro for the price IDs those map to).
    plan: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    subscription_status: Optional[str] = None  # mirrors Stripe's status string, e.g. "active", "canceled", "past_due"


class Lead(SQLModel, table=True):
    """A single inbound caller request captured by the receptionist flow."""

    id: Optional[int] = Field(default=None, primary_key=True)

    # Associates each lead with the owning business for multi-tenant usage.
    business_id: int = Field(foreign_key="business.id")

    caller_name: Optional[str] = None
    phone_number: str

    intent: Optional[str] = None
    service_requested: Optional[str] = None
    preferred_time: Optional[str] = None
    preferred_barber: Optional[str] = None

    notes: Optional[str] = None

    owner_notified: bool = False
    booking_link_sent: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)


class BusinessConfig(SQLModel, table=True):
    """Configurable prompts/rules used during call collection for one business."""

    id: Optional[int] = Field(default=None, primary_key=True)

    business_id: int = Field(foreign_key="business.id", unique=True)

    greeting: Optional[str] = None
    fallback_message: Optional[str] = None
    completion_message: Optional[str] = None

    required_fields_json: str = '["service_requested", "preferred_time"]'
    services_json: str = "[]"
    staff_json: str = "[]"

    ask_name_prompt: Optional[str] = "Can I get your name, please?"
    ask_service_prompt: Optional[str] = "What service are you looking for?"
    ask_time_prompt: Optional[str] = "What specific time were you thinking?"
    ask_staff_prompt: Optional[str] = "Do you have a preferred staff member?"

    collect_notes: bool = True
    send_customer_sms: bool = True
    send_owner_sms: bool = True


class AppSetting(SQLModel, table=True):
    """Small key/value store for global app state that needs to persist
    across restarts/redeploys but doesn't warrant its own table.

    Currently used for the dashboard session-revocation counter (see
    app/auth.py). Deliberately in the database rather than a local file:
    Railway's app container filesystem is not guaranteed to persist across
    redeploys, but the configured database (Postgres in production) is a
    separate managed service and does.
    """

    key: str = Field(primary_key=True)
    value: str