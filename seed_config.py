"""One-off script to seed BusinessConfig defaults for the sample business."""

from sqlmodel import Session, select
from app.db import engine
from app.models import Business, BusinessConfig

with Session(engine) as session:
    # Find the business created by seed.py.
    business = session.exec(
        select(Business).where(Business.name == "The Shaky Razor")
    ).first()

    if not business:
        print("Business not found.")
        raise SystemExit

    existing = session.exec(
        select(BusinessConfig).where(BusinessConfig.business_id == business.id)
    ).first()

    if existing:
        print("Config already exists for business", business.id)
        raise SystemExit

    # Stores prompt wording and required field policy used by /voice flow.
    config = BusinessConfig(
        business_id=business.id,
        greeting="Hi, this is Test Barber Shop. Everyone is with clients right now, but I can help. What can I get you today?",
        fallback_message="Sorry, something went wrong. Please call back in a few minutes.",
        completion_message="Perfect, I’ve got everything I need. The shop will follow up with you soon.",
        required_fields_json='["caller_name", "service_requested", "preferred_time"]',
        services_json='["fade", "haircut", "lineup", "beard trim"]',
        staff_json='[]',
        ask_name_prompt="Can I get your name, please?",
        ask_service_prompt="What service are you looking for today?",
        ask_time_prompt="What specific time were you thinking? For example, 2 PM.",
        ask_staff_prompt="Do you have a preferred barber?",
        collect_notes=True,
        send_customer_sms=True,
        send_owner_sms=True,
    )

    session.add(config)
    session.commit()
    session.refresh(config)

    print("Created config:", config.id, "for business:", business.id)