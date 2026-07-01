"""
prompts.py
---
Optimized system prompts for the AI receptionist.

Key changes from a naive prompt:
  1. Much shorter — fewer tokens = faster OpenAI inference (~200-400ms saved per turn)
  2. One-question-per-turn rule enforced explicitly — reduces multi-question
     responses that confuse callers and extend calls unnecessarily
  3. No repetition of what the caller just said — a common LLM habit that
     sounds robotic and wastes time
  4. Hard length limit on responses — 2 sentences max keeps audio short
     (less ElevenLabs latency) and sounds like a real receptionist, not a chatbot
  5. Clear terminal condition — the AI knows exactly when to wrap up

Usage:
    from app.prompts import build_system_prompt

    system_prompt = build_system_prompt(
        business_name="The Shaky Razor",
        services=["Haircut", "Beard Trim", "Haircut & Beard", "Fade", "Kids Cut"],
        owner_phone="(435) 555-0100",
        collected=state.collected_info,   # dict of what we have so far
    )
"""

from typing import Optional


def build_system_prompt(
    business_name: str,
    services: list[str],
    owner_phone: str,
    collected: Optional[dict] = None,
) -> str:
    services_str = ", ".join(services) if services else "various services"

    collected_str = "Nothing yet."
    if collected:
        parts = []
        if collected.get("name"):
            parts.append(f"Name: {collected['name']}")
        if collected.get("phone"):
            parts.append(f"Phone: {collected['phone']}")
        if collected.get("service"):
            parts.append(f"Service: {collected['service']}")
        if collected.get("preferred_time"):
            parts.append(f"Time: {collected['preferred_time']}")
        if parts:
            collected_str = " · ".join(parts)

    # What we still need — used to keep the AI on track
    needs = []
    c = collected or {}
    if not c.get("name"):
        needs.append("name")
    if not c.get("phone"):
        needs.append("callback phone number")
    if not c.get("service"):
        needs.append(f"which service ({services_str})")
    if not c.get("preferred_time"):
        needs.append("preferred day and time")

    if needs:
        next_ask = f"Ask for their {needs[0]} next."
    else:
        next_ask = (
            "You have everything. Confirm the booking by reading back all 4 details "
            "and tell them someone will follow up to confirm. Then end warmly."
        )

    return f"""You are the AI receptionist for {business_name}. Your only job: collect booking info and make callers feel taken care of.

COLLECT (one at a time, in any natural order):
• Name
• Callback phone number
• Service — options: {services_str}
• Preferred day and time

RULES — follow these strictly:
• 2 sentences max per response. Never longer.
• One question per turn. Never ask two things at once.
• Don't repeat what the caller just said. Acknowledge briefly and move on.
• Don't describe what you're doing ("I'll collect your information..."). Just do it.
• If they ask about price, wait times, or anything operational: "For that, it's best to reach us directly at {owner_phone} — but I can get you booked right now if you'd like."
• Never make up availability or pricing.

CURRENT INFO COLLECTED: {collected_str}

NEXT ACTION: {next_ask}"""


# ── Pre-generated opening greeting ──────────────────────────────────────────
# This is the one response you CAN cache as ElevenLabs audio.
# Pre-generate it on startup and serve as a static file → zero TTS latency
# on first turn.

def build_greeting(business_name: str) -> str:
    return (
        f"Thanks for calling {business_name}! "
        f"I'm an AI receptionist — I can get you booked in about 30 seconds. "
        f"What's your name?"
    )


# ── Barbershop-specific defaults ─────────────────────────────────────────────
# Import these in your Business seeding script or use as fallbacks.

SHAKY_RAZOR_DEFAULTS = {
    "business_name": "The Shaky Razor",
    "services": [
        "Haircut",
        "Beard Trim",
        "Haircut & Beard Trim",
        "Fade",
        "Kids Cut",
        "Hot Towel Shave",
    ],
    "owner_phone": "(435) xxx-xxxx",  # fill in real number
}
