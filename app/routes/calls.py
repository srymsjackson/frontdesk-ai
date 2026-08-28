import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select
import json

from app.db import get_session
from app.models import Business
from app.schemas import LeadCreate
from app.services.ai_service import analyze_customer_turn
from app.services.config_service import (
    get_business_config,
    get_required_fields,
    get_first_missing_required_field,
    get_prompt_for_field,
)
from app.services.lead_service import (
    create_lead,
    mark_booking_link_sent,
    mark_owner_notified,
)
from app.services.twilio_service import send_sms
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calls", tags=["calls"])

class CallInitRequest(BaseModel):
    to_number: str
    from_number: str

class CallMergeRequest(BaseModel):
    business_id: str
    call_state: str
    user_message: str

class CallCompleteRequest(BaseModel):
    business_id: str
    call_state: str

def parse_json_list(raw: str | None) -> list[str]:
    """Helper to parse config fields that are stored as JSON lists."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []
    
@router.post("/init")
def init_call(payload: CallInitRequest, session: Session = Depends(get_session)):
    statement = select(Business).where(Business.twilio_number == payload.to_number)
    business = session.exec(statement).first()

    if not business:
        raise HTTPException(status_code=404, detail="Business not found for this phone number.")

    if not business.is_active:
        raise HTTPException(status_code=403, detail="This business's service is not currently active.")

    config = get_business_config(session, business.id)

    greeting = (
        config.greeting
        if config and config.greeting
        else f"Hi, this is {business.name}. Everyone is with clients right now, but I can help. What can I get you today?"
    )

    completion_message = (
        config.completion_message
        if config and config.completion_message
        else "Perfect, I’ve got everything I need. The business will follow up with you soon."
    )

    fallback_message = (
        config.fallback_message
        if config and config.fallback_message
        else "Sorry, something went wrong. Please call back in a few minutes."
    )

    return {
        "business_id": business.id,
        "business_name": business.name,
        "business_type": business.business_type,
        "timezone": business.timezone,
        "from_number": payload.from_number,
        "to_number": payload.to_number,
        "greeting": greeting,
        "required_fields": get_required_fields(config),
        "services": parse_json_list(config.services_json if config else "[]"),
        "staff": parse_json_list(config.staff_json if config else "[]"),
        "completion_message": completion_message,
        "fallback_message": fallback_message,
        "initial_state": {
            "phone_number": payload.from_number,
            "caller_name": None,
            "intent": "booking",
            "service_requested": None,
            "preferred_barber": None,
            "preferred_time": None,
            "notes": [],
},
    }

def merge_field(old_value, new_value):
    return new_value if new_value not in (None, "", "Unknown") else old_value

import json

@router.post("/merge-state")
def merge_state(payload: CallMergeRequest, session: Session = Depends(get_session)):
    state = json.loads(payload.call_state)

    statement = select(Business).where(Business.id == payload.business_id)
    business = session.exec(statement).first()

    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    config = get_business_config(session, business.id)

    result = analyze_customer_turn(payload.user_message)

    state["caller_name"] = merge_field(state.get("caller_name"), result.get("caller_name"))
    state["intent"] = merge_field(state.get("intent"), result.get("intent", "booking"))
    state["service_requested"] = merge_field(state.get("service_requested"), result.get("service_requested"))
    state["preferred_barber"] = merge_field(state.get("preferred_barber"), result.get("preferred_barber"))
    state["preferred_time"] = merge_field(state.get("preferred_time"), result.get("preferred_time"))

    if payload.user_message:
        state["notes"].append(payload.user_message)

    missing_field = get_first_missing_required_field(config, state)
    next_prompt = get_prompt_for_field(config, missing_field) if missing_field else None

    return {
        "updated_state": state,
        "missing_field": missing_field,
        "next_prompt": next_prompt,
        "is_complete": missing_field is None,
    }
    
import json

@router.post("/complete")
def complete_call(payload: CallCompleteRequest, session: Session = Depends(get_session)):
    state = json.loads(payload.call_state)

    statement = select(Business).where(Business.id == payload.business_id)
    business = session.exec(statement).first()

    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    config = get_business_config(session, business.id)

    combined_notes = " | ".join(state.get("notes", []))

    lead = create_lead(
        session,
        LeadCreate(
            business_id=business.id,
            phone_number=state.get("phone_number") or "",
            caller_name=state.get("caller_name"),
            intent=state.get("intent", "booking"),
            service_requested=state.get("service_requested"),
            preferred_barber=state.get("preferred_barber"),
            preferred_time=state.get("preferred_time"),
            notes=combined_notes,
        ),
    )

    customer_text = (
        f"Hey {lead.caller_name or ''}! We got your request for a "
        f"{lead.service_requested or 'service'} "
        f"{'at ' + lead.preferred_time if lead.preferred_time else ''}. "
        f"Book here to lock it in: {business.booking_link}"
    )

    owner_text = (
        f"📞 New missed call lead\n\n"
        f"👤 {lead.caller_name or 'Unknown'}\n"
        f"💈 {lead.service_requested or 'Unknown service'}\n"
        f"⏰ {lead.preferred_time or 'No time given'}\n"
        f"✂️ {lead.preferred_barber or 'No preference'}\n\n"
        f"📱 {lead.phone_number}"
    )

    try:
        if config and config.send_customer_sms and business.booking_link and settings.twilio_phone_number:
            customer_msg = send_sms(lead.phone_number, customer_text)
            logger.info("Customer SMS sent sid=%s", customer_msg.sid)
            mark_booking_link_sent(session, lead)
    except Exception as e:
        logger.error("Customer SMS failed: %s", e)

    try:
        if config and config.send_owner_sms and business.owner_phone and settings.twilio_phone_number:
            owner_msg = send_sms(business.owner_phone, owner_text)
            logger.info("Owner SMS sent sid=%s", owner_msg.sid)
            mark_owner_notified(session, lead)
    except Exception as e:
        logger.error("Owner SMS failed: %s", e)

    completion_message = (
        config.completion_message
        if config and config.completion_message
        else "Perfect, I’ve got everything I need. The business will follow up with you soon."
    )

    return {
        "success": True,
        "lead_id": lead.id,
        "completion_message": completion_message,
    }