"""One-off script to seed a sample business row for local development."""

from sqlmodel import Session
from app.db import engine
from app.models import Business

with Session(engine) as session:
    # Creates a single business that incoming Twilio calls can resolve to.
    business = Business(
        name="The Shaky Razor",
        twilio_number="+14352654742",  # YOUR Twilio number
        owner_phone="+15156618184",
        booking_link="https://bookingsite.com"
    )

    session.add(business)
    session.commit()

    print("Business created:", business.id)