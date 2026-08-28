"""Twilio webhook signature verification tests (Priority 2A of the hardening plan).

Uses a real FastAPI TestClient against the actual app + dependency (not a
mocked stand-in) so the test exercises the real HMAC check. No dependency
override / test-mode bypass exists in application code at all — the only
way these tests get a passing signature is by computing one correctly with
twilio.request_validator.RequestValidator, the same way Twilio itself does.
That keeps the bypass entirely in test code, never shippable to prod.
"""

import os

# NOTE: the required env vars (TWILIO_AUTH_TOKEN, BASE_URL, DATABASE_URL, ...)
# are set in tests/conftest.py, which loads before any test module — including
# this one — so app.config.settings (a module-level singleton) is already
# correctly populated by the time anything here imports app.*.

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.main import app
from app.config import settings
from app.db import engine
from sqlmodel import SQLModel


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(engine)
    yield
    # Clean up the on-disk test DB file so repeated runs start fresh.
    db_path = settings.database_url.replace("sqlite:///", "")
    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def client():
    return TestClient(app)


def _sign(url: str, params: dict) -> str:
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.compute_signature(url, params)


def test_incoming_call_rejected_without_signature(client):
    """No X-Twilio-Signature header at all -> 403, not processed."""
    resp = client.post(
        "/voice/incoming",
        data={"From": "+15550001111", "To": "+15559998888", "CallSid": "CA123"},
    )
    assert resp.status_code == 403


def test_incoming_call_rejected_with_wrong_signature(client):
    """A signature that doesn't match the params -> 403."""
    resp = client.post(
        "/voice/incoming",
        data={"From": "+15550001111", "To": "+15559998888", "CallSid": "CA123"},
        headers={"X-Twilio-Signature": "not-a-real-signature=="},
    )
    assert resp.status_code == 403


def test_incoming_call_accepted_with_valid_signature(client):
    """A correctly computed signature for the exact URL+params -> passes auth
    (falls through to the route's own 'business not configured' logic, which
    is a 200 TwiML response since no Business row matches in the test DB —
    the point here is it's NOT rejected at the auth layer)."""
    params = {"From": "+15550001111", "To": "+15559998888", "CallSid": "CA123"}
    url = f"{settings.base_url}/voice/incoming"
    signature = _sign(url, params)

    resp = client.post(
        "/voice/incoming",
        data=params,
        headers={"X-Twilio-Signature": signature},
    )
    assert resp.status_code == 200
    assert "not configured" in resp.text


def test_voice_status_rejected_without_signature(client):
    resp = client.post("/voice/status", data={"CallStatus": "completed", "CallSid": "CA999"})
    assert resp.status_code == 403


def test_voice_status_accepted_with_valid_signature(client):
    """204 No Content is this route's normal success response — the point
    is a valid signature reaches the handler at all instead of getting 403'd."""
    params = {"CallStatus": "completed", "CallSid": "CA999"}
    url = f"{settings.base_url}/voice/status"
    signature = _sign(url, params)

    resp = client.post(
        "/voice/status",
        data=params,
        headers={"X-Twilio-Signature": signature},
    )
    assert resp.status_code == 204
