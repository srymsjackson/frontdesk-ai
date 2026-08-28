"""Rate limiting tests (Priority 2C of the hardening plan).

Confirms each newly-limited endpoint actually returns 429 once its cap is
exceeded — not just that the decorator is present in the code. Each test
resets the shared in-memory limiter state first so results don't depend on
what other tests happened to hit earlier in the session.
"""

import os
import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.main import app
from app.config import settings
from app.rate_limit import limiter
from app.db import engine
from sqlmodel import SQLModel


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Every test starts with a clean rate-limit counter, regardless of what
    other tests (in this file or others) hit before it."""
    limiter.reset()
    yield


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def _twilio_signature(url: str, params: dict) -> str:
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.compute_signature(url, params)


def test_voice_incoming_rate_limited_after_30_per_minute(client):
    url = f"{settings.base_url}/voice/incoming"
    statuses = []
    for i in range(35):
        params = {"From": "+15550001111", "To": "+15559998888", "CallSid": f"CA{i:04d}"}
        sig = _twilio_signature(url, params)
        resp = client.post("/voice/incoming", data=params, headers={"X-Twilio-Signature": sig})
        statuses.append(resp.status_code)

    # First 30 should all pass Twilio auth and reach the route (200 TwiML,
    # "business not configured" since no matching Business row exists).
    assert all(s == 200 for s in statuses[:30]), statuses[:30]
    # Everything past the 30/minute cap should be rejected by the limiter.
    assert all(s == 429 for s in statuses[30:]), statuses[30:]


def test_voice_collect_rate_limited_after_60_per_minute(client):
    url = f"{settings.base_url}/voice/collect"
    statuses = []
    for i in range(65):
        params = {"From": "+15550001111", "SpeechResult": f"turn {i}"}
        sig = _twilio_signature(url, params)
        resp = client.post("/voice/collect", data=params, headers={"X-Twilio-Signature": sig})
        statuses.append(resp.status_code)

    assert all(s != 429 for s in statuses[:60]), statuses[:60]
    assert all(s == 429 for s in statuses[60:]), statuses[60:]


def test_onboarding_create_rate_limited_after_5_per_minute(client):
    # Log in first — /onboarding/create sits behind require_dashboard_auth.
    login = client.post("/login", data={"key": settings_dashboard_key()})
    assert login.status_code == 303

    statuses = []
    for i in range(8):
        resp = client.post(
            "/onboarding/create",
            data={
                "name": f"Test Biz {i}",
                "twilio_number": f"+1555000{i:04d}",
                "owner_phone": "+15555550000",
            },
        )
        statuses.append(resp.status_code)

    assert all(s != 429 for s in statuses[:5]), statuses[:5]
    assert all(s == 429 for s in statuses[5:]), statuses[5:]


def test_billing_checkout_rate_limited_after_10_per_minute(client, monkeypatch):
    # Avoid a real network call to Stripe — we're only testing the rate
    # limiter here, not checkout session creation itself.
    import app.routes.billing as billing_module

    def fake_create_checkout_session(**kwargs):
        class FakeSession:
            url = "https://checkout.stripe.com/fake-session"
        return FakeSession()

    monkeypatch.setattr(billing_module, "create_checkout_session", fake_create_checkout_session)

    statuses = []
    for _ in range(13):
        resp = client.get("/billing/checkout?business_id=1&plan=basic", follow_redirects=False)
        statuses.append(resp.status_code)

    assert all(s != 429 for s in statuses[:10]), statuses[:10]
    assert all(s == 429 for s in statuses[10:]), statuses[10:]


def settings_dashboard_key() -> str:
    return os.environ["DASHBOARD_KEY"]
