"""Lead persistence operations used by API routes."""

import logging
from sqlmodel import Session, select
from app.models import Lead
from app.schemas import LeadCreate

logger = logging.getLogger(__name__)


def create_lead(session: Session, payload: LeadCreate) -> Lead:
    """Insert a lead row and return the refreshed database object."""
    logger.debug("Creating lead: %s", payload.model_dump())
    lead = Lead(**payload.model_dump())
    session.add(lead)
    session.commit()
    session.refresh(lead)
    return lead


def list_leads(session: Session) -> list[Lead]:
    """List leads newest-first for dashboard/API retrieval."""
    statement = select(Lead).order_by(Lead.created_at.desc())
    return list(session.exec(statement))


def mark_booking_link_sent(session: Session, lead: Lead) -> Lead:
    """Mark that the customer booking link SMS was sent."""
    lead.booking_link_sent = True
    session.add(lead)
    session.commit()
    session.refresh(lead)
    return lead


def mark_owner_notified(session: Session, lead: Lead) -> Lead:
    """Mark that the owner notification SMS was sent."""
    lead.owner_notified = True
    session.add(lead)
    session.commit()
    session.refresh(lead)
    return lead