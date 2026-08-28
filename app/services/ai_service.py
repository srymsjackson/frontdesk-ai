"""AI + fallback parsing for extracting structured booking info from caller speech."""

import json
import logging
import re
from openai import OpenAI
from app.config import settings

logger = logging.getLogger(__name__)

client = OpenAI(api_key=settings.openai_api_key)

_SYSTEM_PROMPT_BASE = """
You are a friendly, casual receptionist for a barbershop taking a booking over the phone.

REQUIRED fields (all 3 must be collected before completing):
  1. caller_name
  2. service_requested
  3. preferred_time — must include BOTH a day AND a time (e.g. "Thursday at 2pm")

OPTIONAL field (never ask for it, never let it block completion):
  - preferred_barber

Your job each turn:
1. Extract any booking info from what the caller just said
2. Write a short, natural reply that acknowledges what they said and asks for the NEXT missing required field
3. Never re-ask for anything already listed under "Already collected"
4. Set enough_to_complete to true as soon as all 3 required fields are filled

Time combination rule — IMPORTANT:
  If "Already collected" shows a time but no day (e.g. "Time: 2:00 pm"), and the caller gives a day
  (e.g. "Thursday"), combine them: output preferred_time as "Thursday at 2:00 pm".
  Never split day and time into separate turns.

Rules for assistant_reply:
- 1-2 sentences max
- One question per turn, never two
- Sound like a real person, not a script
- Never say the appointment is confirmed or booked
- Never ask about barber preference

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
    collected = []
    if state.get("caller_name"):
        collected.append(f"Name: {state['caller_name']}")
    if state.get("service_requested"):
        collected.append(f"Service: {state['service_requested']}")
    if state.get("preferred_time"):
        collected.append(f"Time: {state['preferred_time']}")

    needed = []
    if not state.get("caller_name"):
        needed.append("caller name")
    if not state.get("service_requested"):
        needed.append("service requested")
    if not state.get("preferred_time"):
        needed.append("preferred day AND time (get both in one answer if possible)")

    collected_str = ", ".join(collected) if collected else "nothing yet"
    needed_str = ", ".join(needed) if needed else "none — set enough_to_complete to true NOW"

    return (
        f"{_SYSTEM_PROMPT_BASE}\n\n"
        f"Already collected: {collected_str}\n"
        f"Still needed: {needed_str}"
    )


def fallback_response():
    return {
        "intent": "booking",
        "assistant_reply": "Got you — what day and time were you thinking?",
        "caller_name": None,
        "service_requested": None,
        "preferred_barber": None,
        "preferred_time": None,
        "enough_to_complete": False,
    }


# The fields voice.py's /voice/collect route requires before it will
# complete a call — same three the system prompt above tells the model are
# required. Defined once here so voice.py imports this instead of keeping
# its own separately-maintained copy that could drift out of sync.
REQUIRED_FIELDS = ("caller_name", "service_requested", "preferred_time")


def _merge_for_completion_check(current_state: dict, data: dict) -> dict:
    """Preview what state will look like after voice.py's own merge_field
    step runs moments later, using the same "ignore empty/placeholder
    values" rule it uses. This lets a fallback path (AI unreachable or
    returned garbage) judge completion from what's actually been collected
    across the whole call, not just this one turn."""
    merged = dict(current_state)
    for field in REQUIRED_FIELDS:
        new_value = data.get(field)
        if new_value not in (None, "", "Unknown"):
            merged[field] = new_value
    return merged


def _fields_complete(merged_state: dict) -> bool:
    return all(merged_state.get(field) for field in REQUIRED_FIELDS)


def clean_service(service):
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
    if not value:
        return None
    return value.replace(".", "").strip().lower()


def clean_barber(value):
    if not value:
        return None
    v = value.strip().lower()
    if "any" in v or "whoever" in v or "doesn't matter" in v:
        return "no preference"
    return value.strip()


def clean_name(value):
    if not value:
        return None
    return value.strip().title()


# Words that can follow "this is ..." without being a name. The bare
# "this is X" pattern is greedy — "this is stupid", "this is ridiculous",
# etc. would otherwise be parsed as the caller's name. For lead capture a
# missed name (which the owner can ask about) is far safer than a wrong one,
# so the fallback stays deliberately conservative.
_NON_NAME_WORDS = {
    "stupid", "dumb", "ridiculous", "annoying", "annoyed", "pointless",
    "useless", "insane", "crazy", "weird", "frustrating", "frustrated",
    "confused", "upset", "angry", "mad", "done", "here", "calling",
    "not", "so", "really", "very", "great", "awesome", "terrible",
    "awful", "bad", "good", "fine", "okay", "ok", "sure",
    "a", "an", "the", "my", "your", "for", "about", "regarding",
}


def fallback_extract_name(text: str):
    # Ordered most- to least-explicit. "my name is X" is unambiguous; the
    # bare "this is X" form is only trusted after the stop-list check below.
    patterns = [
        r"\bmy name is ([A-Za-z]+)",
        r"\bmy name's ([A-Za-z]+)",
        r"\bthis is ([A-Za-z]+)",
        r"\bi'?m ([A-Za-z]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            candidate = match.group(1)
            if candidate.lower() in _NON_NAME_WORDS:
                continue
            return candidate.title()
    return None


def fallback_extract_barber(text: str):
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
    """Call the model and return a cleaned structured booking state update."""
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
        logger.debug("Raw AI response: %s", raw_text)

        data = extract_json(raw_text)
        used_fallback = False
        if not data:
            logger.warning("JSON parse failed for AI response: %s", raw_text)
            data = fallback_response()
            data["assistant_reply"] = raw_text or data["assistant_reply"]
            used_fallback = True

        data["intent"] = data.get("intent", "booking")
        data["assistant_reply"] = data.get("assistant_reply", "Got you — what day and time were you thinking?")
        data["caller_name"] = clean_name(data.get("caller_name")) or fallback_extract_name(user_text)
        data["service_requested"] = clean_service(data.get("service_requested")) or fallback_extract_service(user_text)
        data["preferred_barber"] = clean_barber(data.get("preferred_barber")) or fallback_extract_barber(user_text)
        data["preferred_time"] = clean_time(data.get("preferred_time")) or fallback_extract_time(user_text)

        if used_fallback:
            # The model didn't give us a trustworthy enough_to_complete (it
            # returned unparseable text), so derive it from what's actually
            # been collected instead of trusting fallback_response()'s
            # hardcoded False — otherwise a call that already has every
            # required field can never complete just because this one turn's
            # response happened to be garbage.
            data["enough_to_complete"] = _fields_complete(
                _merge_for_completion_check(current_state, data)
            )
        else:
            data["enough_to_complete"] = bool(data.get("enough_to_complete", False))

        logger.debug("Cleaned data: %s", data)
        return data

    except Exception as e:
        logger.error("OpenAI call failed: %s", e)
        data = fallback_response()
        data["caller_name"] = fallback_extract_name(user_text)
        data["service_requested"] = fallback_extract_service(user_text)
        data["preferred_barber"] = fallback_extract_barber(user_text)
        data["preferred_time"] = fallback_extract_time(user_text)
        # Same reasoning as above: OpenAI being unreachable must not mean a
        # call can never finish once the caller has actually given every
        # required field over the course of the call.
        data["enough_to_complete"] = _fields_complete(
            _merge_for_completion_check(current_state, data)
        )
        return data