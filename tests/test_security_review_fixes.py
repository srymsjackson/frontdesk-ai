"""Regression tests for the general code-review fixes:

- /leads/, /sms/leads, and /api/calls/* were unauthenticated duplicate/dead
  routes (the first two leaked every lead across every business with zero
  auth) — confirm they're gone entirely, not just now-authenticated.
- The public /ws/leads broadcast must never carry a full phone number —
  confirm the server-side masking helper actually strips it before it would
  reach that channel.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes.voice import _mask_phone


@pytest.fixture
def client():
    return TestClient(app)


def test_leads_route_is_gone(client):
    resp = client.get("/leads/")
    assert resp.status_code == 404


def test_sms_leads_route_is_gone(client):
    resp = client.get("/sms/leads")
    assert resp.status_code == 404


def test_api_calls_routes_are_gone(client):
    assert client.post("/api/calls/init", json={}).status_code == 404
    assert client.post("/api/calls/merge-state", json={}).status_code == 404
    assert client.post("/api/calls/complete", json={}).status_code == 404


def test_mask_phone_keeps_only_last_four_digits():
    assert _mask_phone("+15550001234") == "+1 (***) ***-1234"
    assert _mask_phone("5550001234") == "+1 (***) ***-1234"


def test_mask_phone_handles_missing_or_short_input():
    assert _mask_phone(None) == "(***) ***-****"
    assert _mask_phone("") == "(***) ***-****"
    assert _mask_phone("12") == "(***) ***-****"


def test_mask_phone_never_returns_more_than_last_four_digits_of_input():
    """However phone numbers get formatted upstream, no more than 4 raw
    digits from the actual number should ever be recoverable from the
    masked output (the leading '+1' is a fixed literal, not derived from
    the input, so it's excluded from this check)."""
    masked = _mask_phone("+1 (555) 000-1234")
    visible_digits = masked.rsplit("-", 1)[-1]
    assert visible_digits == "1234"
    assert masked.count("*") >= 6
