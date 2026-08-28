"""Twilio replay-window tests (Priority 8, the last item of the hardening
plan).

Twilio's signature scheme (see app/security.py's module docstring) has no
nonce or timestamp -- the same URL + params always produce the same valid
signature. These tests confirm the replay-window mitigation actually does
two things at once, since either one alone would be a real regression:

  1. Rejects an exact-duplicate signed request that shows up again *outside*
     the window (the actual replay-attack scenario this exists to catch).
  2. Still accepts an exact-duplicate signed request that shows up again
     *within* the window (a legitimate Twilio retry after a timeout/5xx --
     breaking this would mean flaky network conditions start dropping real
     calls, which is worse than the vulnerability being fixed).
"""

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


@pytest.fixture
def client():
    return TestClient(app)


def _sign(url: str, params: dict) -> str:
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.compute_signature(url, params)


def test_duplicate_signed_request_within_window_is_allowed_twice(client):
    """Simulates a Twilio retry: the exact same signed request arrives
    twice in quick succession. Both must succeed -- rejecting the second
    would mean a real call gets dropped whenever Twilio's own delivery is
    briefly slow or flaky."""
    params = {"From": "+15550002222", "To": "+15559998888", "CallSid": "CAretry001"}
    url = f"{settings.base_url}/voice/incoming"
    signature = _sign(url, params)

    first = client.post("/voice/incoming", data=params, headers={"X-Twilio-Signature": signature})
    second = client.post("/voice/incoming", data=params, headers={"X-Twilio-Signature": signature})

    assert first.status_code == 200
    assert second.status_code == 200


def test_duplicate_signed_request_outside_window_is_rejected(client, monkeypatch):
    """The actual replay scenario: an identical signed request shows up
    again well after the retry window has passed. This must now be
    rejected -- accepting it is exactly the gap this feature closes."""
    import app.security as security_module

    params = {"From": "+15550003333", "To": "+15559998888", "CallSid": "CAreplay001"}
    url = f"{settings.base_url}/voice/incoming"
    signature = _sign(url, params)

    fake_now = [1_000_000.0]
    monkeypatch.setattr(security_module.time, "time", lambda: fake_now[0])

    first = client.post("/voice/incoming", data=params, headers={"X-Twilio-Signature": signature})
    assert first.status_code == 200

    # Jump time forward past the replay window.
    fake_now[0] += security_module.REPLAY_WINDOW_SECONDS + 1

    second = client.post("/voice/incoming", data=params, headers={"X-Twilio-Signature": signature})
    assert second.status_code == 403
    assert "already been processed" in second.text


def test_request_just_inside_the_window_boundary_is_still_allowed(client, monkeypatch):
    """Boundary check: one second *before* the window closes should still be
    treated as a retry, not a replay."""
    import app.security as security_module

    params = {"From": "+15550004444", "To": "+15559998888", "CallSid": "CAboundary001"}
    url = f"{settings.base_url}/voice/incoming"
    signature = _sign(url, params)

    fake_now = [2_000_000.0]
    monkeypatch.setattr(security_module.time, "time", lambda: fake_now[0])

    first = client.post("/voice/incoming", data=params, headers={"X-Twilio-Signature": signature})
    assert first.status_code == 200

    fake_now[0] += security_module.REPLAY_WINDOW_SECONDS - 1

    second = client.post("/voice/incoming", data=params, headers={"X-Twilio-Signature": signature})
    assert second.status_code == 200


def test_different_params_are_not_treated_as_a_replay_of_each_other(client):
    """Two different turns of the same call (different SpeechResult, same
    CallSid) must not collide -- only a byte-for-byte identical request
    counts as a duplicate."""
    url = f"{settings.base_url}/voice/collect"

    params_a = {"CallSid": "CAmultiturn001", "SpeechResult": "I need a haircut"}
    params_b = {"CallSid": "CAmultiturn001", "SpeechResult": "Tomorrow at 3pm"}

    resp_a = client.post(
        "/voice/collect", data=params_a, headers={"X-Twilio-Signature": _sign(url, params_a)}
    )
    resp_b = client.post(
        "/voice/collect", data=params_b, headers={"X-Twilio-Signature": _sign(url, params_b)}
    )

    assert resp_a.status_code != 403
    assert resp_b.status_code != 403


def test_replayed_request_to_a_different_route_is_independent(client, monkeypatch):
    """The same params replayed against a different endpoint shouldn't be
    confused with each other -- the request identity includes the URL."""
    import app.security as security_module

    params = {"CallSid": "CAcrossroute001", "CallStatus": "completed"}

    fake_now = [3_000_000.0]
    monkeypatch.setattr(security_module.time, "time", lambda: fake_now[0])

    status_url = f"{settings.base_url}/voice/status"
    status_sig = _sign(status_url, params)
    resp = client.post("/voice/status", data=params, headers={"X-Twilio-Signature": status_sig})
    assert resp.status_code == 204

    # Even with an identical params dict, signing it for the *other* route
    # produces a different signature and a different request identity --
    # this must be treated as a brand-new request, not a replay of the
    # /voice/status one above.
    collect_url = f"{settings.base_url}/voice/collect"
    collect_sig = _sign(collect_url, params)
    resp2 = client.post("/voice/collect", data=params, headers={"X-Twilio-Signature": collect_sig})
    assert resp2.status_code != 403


def test_prune_expired_removes_old_entries_but_keeps_recent_ones(monkeypatch):
    """Directly exercises the cache-cleanup logic, since the HTTP-level tests
    above don't have visibility into whether old entries actually get
    dropped (only whether behavior is correct) -- an unbounded dict is a
    slow memory leak that wouldn't show up as a test failure otherwise."""
    import app.security as security_module

    fake_now = [4_000_000.0]
    monkeypatch.setattr(security_module.time, "time", lambda: fake_now[0])

    security_module._seen_requests["old-entry"] = fake_now[0]
    fake_now[0] += (security_module.REPLAY_WINDOW_SECONDS * 2) + 10
    security_module._seen_requests["recent-entry"] = fake_now[0]

    security_module._prune_expired(fake_now[0])

    assert "old-entry" not in security_module._seen_requests
    assert "recent-entry" in security_module._seen_requests
