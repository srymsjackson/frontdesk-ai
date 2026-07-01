"""AI + fallback parsing for extracting structured booking info from caller speech."""

import json
import re
from openai import OpenAI
from app.config import settings

client = OpenAI(api_key=settings.openai_api_key)

# Base prompt — state context is injected per-call in _build_system_prompt()
_SYSTEM_PROMPT_BASE = """
You are a friendly, casual receptionist for a barbershop taking a booking over the phone.

Your job each turn:
1. Extract any booking info from what the caller just said
2. Write a short, natural reply that acknowledges what they said and asks for the NEXT missing piece
3. Never re-ask for information already listed under "Already collected"
4. Set enough_to_complete to true only when ALL required fields are filled

Rules for assistant_reply:
- 1-2 sentences max
- One question per turn, never two
- Sound like a real person, not a script
- Never say the appointment is confirmed or booked

Return ONLY valid JSON. No markdown. No explanation. No extra text.

JSON schema:
{
  "intent": "booking" | "question" | "callback",
  "assistant_reply": "short conversational response",
  "caller_name": string or null,
  "service_requested": string or null,
  "preferred_barber": string or null,
  "preferred_time": string or null,
  "enough_to_complete": true or false
}
""".strip()


def _build_system_prompt(state: dict) -> str:
    """Inject current booking state into the system prompt so the AI knows
    what's already collected and what to ask for next."""
    collected = []
    if state.get("caller_name"):
        collected.append(f"Name: {state['caller_name']}")
    if state.get("service_requested"):
        collected.append(f"Service: {state['service_requested']}")
    if state.get("preferred_time"):
        collected.append(f"Time: {state['preferred_time']}")
    if state.get("preferred_barber"):
        collected.append(f"Barber: {state['preferred_barber']}")

    needed = []
    if not state.get("caller_name"):
        needed.append("caller name")
    if not state.get("service_requested"):
        needed.append("service requested")
    if not state.get("preferred_time"):
        needed.append("preferred day and time")

    collected_str = ", ".join(collected) if collected else "nothing yet"
    needed_str = ", ".join(needed) if needed else "none — set enough_to_complete to true"

    return (
        f"{_SYSTEM_PROMPT_BASE}\n\n"
        f"Already collected: {collected_str}\n"
        f"Still needed: {needed_str}"
    )


def fallback_response():
    """Baseline response shape used when AI output is unavailable or invalid."""
    return {
        "intent": "booking",
        "assistant_reply": "Got you — what time were you thinking?",
        "caller_name": None,
        "service_requested": None,
        "preferred_barber": None,
        "preferred_time": None,
        "enough_to_complete": False,
    }


def clean_service(service):
    """Normalize free-text service names into simple canonical labels."""
    if not service:
        return None
    s = service.lower().strip()

    if "fade" in s:
        return "fade"
    if "beard" in s:
        return "beard trim"
    if "trim" in s:
        return "trim"
    if "haircut" in s or "hair cut" in s or "cut" in s:
        return "haircut"

    return s


def clean_time(value):
    """Normalize time text for easier downstream checks."""
    if not value:
        return None
    return value.replace(".", "").strip().lower()


def clean_barber(value):
    """Normalize barber preference; collapse flexible phrasing to 'no preference'."""
    if not value:
        return None
    v = value.strip().lower()
    if "any" in v or "whoever" in v or "doesn't matter" in v:
        return "no preference"
    return value.strip()


def clean_name(value):
    """Title-case and trim names for cleaner display/storage."""
    if not value:
        return None
    return value.strip().title()


def fallback_extract_name(text: str):
    """Regex fallback name extraction when model output is missing fields."""
    patterns = [
        r"my name is ([A-Za-z]+)",
        r"my name's ([A-Za-z]+)",
        r"this is ([A-Za-z]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).title()
    return None


def fallback_extract_barber(text: str):
    """Regex/phrase fallback for barber preference extraction."""
    lowered = text.lower()
    if (
        "anybody is fine" in lowered
        or "any barber is fine" in lowered
        or "whoever is open" in lowered
        or "no preference" in lowered
    ):
        return "no preference"
    return None


def fallback_extract_service(text: str):
    """Keyword fallback for service extraction."""
    lowered = text.lower()
    if "fade" in lowered:
        return "fade"
    if "beard" in lowered:
        return "beard trim"
    if "trim" in lowered:
        return "trim"
    if "haircut" in lowered or "hair cut" in lowered or "cut" in lowered:
        return "haircut"
    return None


def fallback_extract_time(text: str):
    """Regex fallback for specific-ish time expressions."""
    match = re.search(
        r"(tomorrow(?:\s+(?:morning|afternoon|evening|night))?)|"
        r"(around\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))|"
        r"(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?))",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(0).replace(".", "").strip().lower()
    return None


def extract_json(raw_text: str) -> dict | None:
    """Parse JSON from raw model output, including fenced/mixed text fallback."""
    raw_text = raw_text.strip()

    try:
        return json.loads(raw_text)
    except Exception:
        pass

    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None

    return None


def analyze_customer_turn(user_text: str, state: dict | None = None) -> dict:
    """Call the model and return a cleaned structured booking state update.

    state — the already-collected fields from previous turns. Passed into the
    system prompt so the AI knows what to ask for next and doesn't re-ask for
    things already collected. assistant_reply in the response is now safe to
    use directly as the next prompt.
    """
    current_state = state or {}

    try:
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {"role": "system", "content": _build_system_prompt(current_state)},
                {"role": "user", "content": user_text},
            ],
        )

        raw_text = response.output_text.strip()
        print("RAW AI RESPONSE:", raw_text)

        data = extract_json(raw_text)
        if not data:
            print("JSON PARSE FAILED")
            data = fallback_response()
            data["assistant_reply"] = raw_text or data["assistant_reply"]

        data["intent"] = data.get("intent", "booking")
        data["assistant_reply"] = data.get("assistant_reply", "Got you — what time were you thinking?")
        data["caller_name"] = clean_name(data.get("caller_name")) or fallback_extract_name(user_text)
        data["service_requested"] = clean_service(data.get("service_requested")) or fallback_extract_service(user_text)
        data["preferred_barber"] = clean_barber(data.get("preferred_barber")) or fallback_extract_barber(user_text)
        data["preferred_time"] = clean_time(data.get("preferred_time")) or fallback_extract_time(user_text)
        data["enough_to_complete"] = bool(data.get("enough_to_complete", False))

        print("CLEANED DATA:", data)
        return data

    except Exception as e:
        print("OPENAI ERROR:", str(e))
        data = fallback_response()
        data["caller_name"] = fallback_extract_name(user_text)
        data["service_requested"] = fallback_extract_service(user_text)
        data["preferred_barber"] = fallback_extract_barber(user_text)
        data["preferred_time"] = fallback_extract_time(user_text)
        return data