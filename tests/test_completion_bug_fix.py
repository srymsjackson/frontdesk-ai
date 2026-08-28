"""Tests for the "call can never complete when OpenAI is down" bug.

Found by manually running the live server and driving a real call through
/voice/incoming + /voice/collect while OpenAI was unreachable: even after
all three required fields (caller_name, service_requested, preferred_time)
were captured via the regex fallback extractors, the call kept asking
questions forever, because analyze_customer_turn()'s fallback paths
hardcoded enough_to_complete=False regardless of what had actually been
collected.

Two levels of coverage:
  - Unit-level, directly against analyze_customer_turn (patch_ai fixture).
  - Integration-level, driving the real /voice/incoming + /voice/collect
    routes with OpenAI failing on every turn, asserting a Lead row actually
    gets created — the same observable outcome I checked for manually.
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from twilio.request_validator import RequestValidator

from app.services import ai_service
from app.main import app
from app.config import settings
from app.db import engine
from app.models import Business, Lead
from sqlmodel import SQLModel


# --- unit-level: analyze_customer_turn in isolation -------------------------

def test_enough_to_complete_true_once_all_fields_known_despite_openai_exception(patch_ai):
    patch_ai(raises=RuntimeError("connection reset"))
    state = {"caller_name": "Marcus", "service_requested": "fade", "preferred_time": None}
    result = ai_service.analyze_customer_turn("Thursday at 2pm works", state)
    assert result["enough_to_complete"] is True


def test_enough_to_complete_false_when_a_field_still_missing_after_exception(patch_ai):
    patch_ai(raises=RuntimeError("connection reset"))
    state = {"caller_name": "Marcus", "service_requested": None, "preferred_time": None}
    result = ai_service.analyze_customer_turn("uh hello?", state)
    assert result["enough_to_complete"] is False


def test_enough_to_complete_true_once_all_fields_known_despite_malformed_json(patch_ai):
    patch_ai(returns="I can help you with that, sure thing")
    state = {"caller_name": "Priya", "service_requested": "beard trim", "preferred_time": None}
    result = ai_service.analyze_customer_turn("Thursday at 2pm", state)
    assert result["enough_to_complete"] is True


def test_enough_to_complete_still_trusts_ai_when_ai_succeeds(patch_ai):
    """A successful, valid-JSON AI response is unaffected by this fix — its
    own enough_to_complete value is still used as-is, even if it disagrees
    with what fields happen to be filled."""
    patch_ai(returns=(
        '{"intent":"booking","assistant_reply":"One more thing...",'
        '"caller_name":"Sam","service_requested":"fade",'
        '"preferred_barber":null,"preferred_time":"Thursday at 2pm",'
        '"enough_to_complete":false}'
    ))
    state = {"caller_name": "Sam", "service_requested": "fade", "preferred_time": "Thursday at 2pm"}
    result = ai_service.analyze_customer_turn("Thursday at 2pm", state)
    assert result["enough_to_complete"] is False


# --- integration-level: the real routes, OpenAI down the whole call ---------

@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _sign(url: str, params: dict) -> str:
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.compute_signature(url, params)


def _seeded_business(session: Session) -> Business:
    business = Business(
        name="Test Shop",
        twilio_number="+15559990001",
        owner_phone="+15555550000",
        booking_link="https://example.com/book",
    )
    session.add(business)
    session.commit()
    session.refresh(business)
    return business


def test_call_completes_even_when_openai_is_down_the_whole_time(client, patch_ai, monkeypatch):
    patch_ai(raises=RuntimeError("simulated OpenAI outage"))
    # SMS sending isn't the point of this test and has nothing to mock a
    # Twilio client against here — stub it out so it doesn't error.
    monkeypatch.setattr("app.routes.voice.send_sms", lambda *a, **k: type("M", (), {"sid": "SMfake"})())

    with Session(engine) as session:
        business = _seeded_business(session)
        business_id = business.id

    from_number = "+15550001111"
    to_number = business.twilio_number

    incoming_params = {"From": from_number, "To": to_number, "CallSid": "CAcompletiontest"}
    url = f"{settings.base_url}/voice/incoming"
    resp = client.post("/voice/incoming", data=incoming_params, headers={
        "X-Twilio-Signature": _sign(url, incoming_params)
    })
    assert resp.status_code == 200

    turns = [
        "my name is Jordan",
        "I need a haircut",
        "how about Thursday at 2pm",
    ]
    collect_url = f"{settings.base_url}/voice/collect"
    last_resp = None
    for text in turns:
        params = {"From": from_number, "SpeechResult": text}
        last_resp = client.post("/voice/collect", data=params, headers={
            "X-Twilio-Signature": _sign(collect_url, params)
        })
        assert last_resp.status_code == 200

    # The definitive proof the call actually completed, not just that the
    # last response was 200: a Lead row exists with the info the caller gave.
    with Session(engine) as session:
        lead = session.exec(
            select(Lead).where(Lead.business_id == business_id)
        ).first()

    assert lead is not None, "call never completed — enough_to_complete stayed stuck False"
    assert lead.caller_name == "Jordan"
    assert lead.service_requested == "haircut"
    assert lead.preferred_time is not None
