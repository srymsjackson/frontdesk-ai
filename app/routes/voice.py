"""Voice webhook endpoints that collect lead details over multi-turn calls."""

from fastapi import APIRouter, Depends, Form, Response
from sqlmodel import Session
from sqlmodel import select
from app.models import Business
from app.db import get_session
from app.schemas import LeadCreate
from app.services.lead_service import create_lead, mark_booking_link_sent, mark_owner_notified
from app.services.ai_service import analyze_customer_turn
from app.services.config_service import (
    get_business_config,
    get_first_missing_required_field,
    get_prompt_for_field,
)
from app.config import settings
from twilio.twiml.voice_response import VoiceResponse, Gather
from fastapi.responses import Response as FastAPIResponse
from app.services.voice_service import generate_audio, get_audio
import asyncio
from datetime import datetime, timezone
from ..websocket_manager import manager

router = APIRouter(prefix="/voice", tags=["voice"])

# In-memory per-caller state for a single process instance.
CALL_STATE = {}

def normalize_number(num: str):
    """Normalize phone values so state keys are consistent."""
    return num.replace("+", "").strip()

def full_url(path: str) -> str:
    """Build absolute callback URLs Twilio can post back to."""
    return f"{settings.base_url.rstrip('/')}{path}"

def _display_name(full_name: str | None) -> str:
    """Format 'Jackson Strazzo' → 'Jackson S.' for demo privacy."""
    if not full_name:
        return "Unknown Caller"
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def gather_response(prompt_text: str, action_path: str) -> str:
    """Return TwiML that asks for speech and posts the response to action_path.

    Uses ElevenLabs audio when enabled, falling back to Twilio <Say> on failure.
    """
    response = VoiceResponse()
    gather = Gather(
        input="speech",
        action=full_url(action_path),
        method="POST",
        timeout=5,
        speech_timeout="auto",
    )

    if settings.use_elevenlabs:
        audio_id = generate_audio(prompt_text)
        if audio_id:
            gather.play(full_url(f"/voice/audio/{audio_id}.mp3"))
        else:
            gather.say(prompt_text)
    else:
        gather.say(prompt_text)

    response.append(gather)
    response.say("We did not receive your response. Goodbye.")
    response.hangup()
    return str(response)


def merge_field(old_value, new_value):
    """Prefer newly extracted values unless they are empty/placeholder values."""
    return new_value if new_value not in (None, "", "Unknown") else old_value


def empty_state():
    """Default shape for per-call structured extraction state."""
    return {
        "caller_name": None,
        "intent": "booking",
        "service_requested": None,
        "preferred_barber": None,
        "preferred_time": None,
        "notes": [],
    }


@router.post("/incoming")
async def incoming_call(
    session: Session = Depends(get_session),
    From: str = Form(default=""),
    To: str = Form(default=""),
    CallSid: str = Form(default=""),
):
    """Handle initial inbound call webhook and start first Gather prompt."""
    statement = select(Business).where(Business.twilio_number == To)
    business = session.exec(statement).first()

    print("INCOMING HIT")
    print("From:", From)
    print("To:", To)
    print("BASE_URL:", settings.base_url)
    print("COLLECT URL:", full_url("/voice/collect"))

    if not business:
        response = VoiceResponse()
        response.say("Sorry, this number is not configured.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    config = get_business_config(session, business.id)

    normalized_from = normalize_number(From)
    if normalized_from:
        CALL_STATE[normalized_from] = {
            **empty_state(),
            "business_id": business.id,
        }

    asyncio.create_task(manager.broadcast({
        "type": "call_started",
        "data": {
            "call_sid": CallSid,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }))

    greeting = (
        config.greeting
        if config and config.greeting
        else f"Hi, this is {business.name}. Everyone is with clients right now, but I can help. What can I get you today?"
    )

    return Response(
        content=gather_response(greeting, "/voice/collect"),
        media_type="application/xml",
    )


@router.post("/collect")
async def collect_turn(
    session: Session = Depends(get_session),
    From: str = Form(default=""),
    SpeechResult: str = Form(default=""),
):
    """Handle each spoken customer turn and decide ask-next vs. complete."""
    print("COLLECT HIT")
    print("From:", From)
    print("SpeechResult:", SpeechResult)

    normalized_from = normalize_number(From)

    if normalized_from not in CALL_STATE:
        print("WARNING: Missing CALL_STATE for", normalized_from)
        response = VoiceResponse()
        response.say("Sorry, something went wrong. Please call back.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    state = CALL_STATE[normalized_from]

    # Pass current state so the AI knows what's already collected and generates
    # the right next question — assistant_reply is now used directly as the prompt.
    result = analyze_customer_turn(SpeechResult, state)
    print("RESULT:", result)

    # Merge extracted fields into state.
    state["caller_name"] = merge_field(state["caller_name"], result.get("caller_name"))
    state["intent"] = merge_field(state["intent"], result.get("intent", "booking"))
    state["service_requested"] = merge_field(state["service_requested"], result.get("service_requested"))
    state["preferred_barber"] = merge_field(state["preferred_barber"], result.get("preferred_barber"))
    state["preferred_time"] = merge_field(state["preferred_time"], result.get("preferred_time"))

    if SpeechResult:
        state["notes"].append(SpeechResult)

    business_id = state.get("business_id")
    if not business_id:
        response = VoiceResponse()
        response.say("Something went wrong. Please try again later.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    statement = select(Business).where(Business.id == business_id)
    business = session.exec(statement).first()
    if not business:
        response = VoiceResponse()
        response.say("Sorry, this business could not be found.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    config = get_business_config(session, business.id)

    print("STATE:", state)
    print("BUSINESS:", business)

    # Simple inline required-field check — just needs to be non-empty.
    # Intentionally does NOT do format validation; "tomorrow", "Saturday",
    # "asap" are all valid enough to save.
    REQUIRED = ["caller_name", "service_requested", "preferred_time"]
    first_missing = next((f for f in REQUIRED if not state.get(f)), None)

    if not result.get("enough_to_complete") or first_missing:
        if first_missing:
            print(f"Still missing: {first_missing}")
        prompt = result.get("assistant_reply") or get_prompt_for_field(config, first_missing)
        return Response(
            content=gather_response(prompt, "/voice/collect"),
            media_type="application/xml",
        )

    combined_notes = " | ".join(state["notes"])

    try:
        lead = create_lead(
            session,
            LeadCreate(
                business_id=business.id,
                phone_number=normalized_from,
                caller_name=state["caller_name"],
                intent=state["intent"],
                service_requested=state["service_requested"],
                preferred_barber=state["preferred_barber"],
                preferred_time=state["preferred_time"],
                notes=combined_notes,
            ),
        )
    except Exception as e:
        print("LEAD CREATION ERROR:", str(e))
        response = VoiceResponse()
        response.say("Something went wrong saving your request. Please try again.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    asyncio.create_task(manager.broadcast({
        "type": "new_lead",
        "data": {
            "name": _display_name(lead.caller_name),
            "phone": lead.phone_number or "",
            "service": lead.service_requested or "Inquiry",
            "preferred_time": lead.preferred_time or "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }))

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
        if config and config.send_customer_sms and business.booking_link:
            # customer_msg = send_sms(lead.phone_number, customer_text)
            # print("CUSTOMER SMS SID:", customer_msg.sid)
            mark_booking_link_sent(session, lead)
    except Exception as e:
        print("CUSTOMER SMS ERROR:", str(e))

    try:
        if config and config.send_owner_sms and business.owner_phone:
            # owner_msg = send_sms(business.owner_phone, owner_text)
            # print("OWNER SMS SID:", owner_msg.sid)
            mark_owner_notified(session, lead)
    except Exception as e:
        print("OWNER SMS ERROR:", str(e))

    CALL_STATE.pop(normalized_from, None)

    completion_message = (
        config.completion_message
        if config and config.completion_message
        else "Perfect, I've got everything I need. I'll pass this along and they'll take care of you. Talk soon."
    )

    response = VoiceResponse()
    if settings.use_elevenlabs:
        audio_id = generate_audio(completion_message)
        if audio_id:
            response.play(full_url(f"/voice/audio/{audio_id}.mp3"))
        else:
            response.say(completion_message)
    else:
        response.say(completion_message)
    response.hangup()
    return Response(content=str(response), media_type="application/xml")


@router.get("/audio/{audio_id}.mp3")
def serve_audio(audio_id: str):
    """Serve a generated MP3 from the in-memory cache, for Twilio to <Play>."""
    audio_bytes = get_audio(audio_id)
    if not audio_bytes:
        return FastAPIResponse(status_code=404, content="Not found")
    return FastAPIResponse(content=audio_bytes, media_type="audio/mpeg")