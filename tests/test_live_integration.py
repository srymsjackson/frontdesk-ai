"""Optional live tests that hit the real OpenAI API.

Deselected by default. Run explicitly with a real key:

    OPENAI_API_KEY=sk-... pytest -m live

These verify the model actually returns parseable, correctly-structured
extractions on representative turns — the thing the deterministic suite
deliberately stubs out.
"""

import os
import pytest

from app.services import ai_service as ai

_HAS_REAL_KEY = os.environ.get("OPENAI_API_KEY", "").startswith("sk-") and \
    os.environ.get("OPENAI_API_KEY") != "sk-test-dummy"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _HAS_REAL_KEY, reason="needs a real OPENAI_API_KEY"),
]


def test_live_full_booking_in_one_turn():
    result = ai.analyze_customer_turn(
        "Hi, this is Marcus, I'd like a fade on Thursday at 2pm"
    )
    assert result["caller_name"] == "Marcus"
    assert result["service_requested"] == "fade"
    assert result["preferred_time"] and "thursday" in result["preferred_time"].lower()
    assert result["enough_to_complete"] is True


def test_live_asks_for_missing_field():
    result = ai.analyze_customer_turn("I want a haircut", state={})
    # Name and time still missing → must not complete, must ask something.
    assert result["enough_to_complete"] is False
    assert result["assistant_reply"]
