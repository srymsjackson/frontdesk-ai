"""Billing checkout link tests (Priority 6 of the hardening plan).

/billing/checkout used to take raw business_id + plan query params, directly
editable in the URL bar -- a link handed to one business could be altered to
activate a different business_id, or upgraded/downgraded to a different plan
than the one actually offered. These tests confirm the signed-token
replacement (app/billing_tokens.py) actually closes that: tokens can't be
minted without the secret, can't be edited without invalidating the
signature, and a bad token gets the same generic response as any other bad
token regardless of *why* it's bad.
"""

import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DBSession, SQLModel, select

from app.main import app
from app.db import engine
from app.models import Business
from app.billing_tokens import create_checkout_token, verify_checkout_token


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def business():
    """A real Business row so checkout can reach the point of calling Stripe."""
    with DBSession(engine) as db:
        b = Business(name="Token Test Co", twilio_number="+15555551234", owner_phone="+15555550001")
        db.add(b)
        db.commit()
        db.refresh(b)
        return b


@pytest.fixture(autouse=True)
def _fake_stripe(monkeypatch):
    """No real network calls to Stripe in this file -- only checkout-link
    validation is under test."""
    import app.routes.billing as billing_module

    def fake_create_checkout_session(**kwargs):
        class FakeSession:
            url = "https://checkout.stripe.com/fake-session"
        return FakeSession()

    monkeypatch.setattr(billing_module, "create_checkout_session", fake_create_checkout_session)


# --- Token round-trip ---------------------------------------------------


def test_valid_token_round_trips_business_id_and_plan():
    token = create_checkout_token(business_id=42, plan="pro")
    decoded = verify_checkout_token(token)
    assert decoded == (42, "pro")


def test_garbage_token_is_rejected():
    assert verify_checkout_token("not-a-real-token") is None


def test_empty_token_is_rejected():
    assert verify_checkout_token("") is None


# --- Tampering: the actual vulnerability this closes ---------------------


def test_editing_business_id_in_a_valid_token_string_breaks_the_signature():
    """A signed token isn't just base64 of {"business_id": N} -- flipping a
    character invalidates the whole thing, unlike the old raw query param
    which could be edited freely."""
    token = create_checkout_token(business_id=5, plan="basic")
    # Corrupt a character in the middle of the token (not just re-encoding
    # different JSON -- this simulates literally editing the URL by hand).
    tampered = token[:10] + ("X" if token[10] != "X" else "Y") + token[11:]
    assert verify_checkout_token(tampered) is None


def test_token_minted_for_one_business_cannot_be_reused_for_another(client, business):
    """The scenario the whole change exists to prevent: someone takes a
    legitimate checkout link and tries to redirect it at a different
    business_id by hand-editing the query string (simulated here by minting
    a token for a *different* business than the one this link is meant for,
    then attempting to pass a raw business_id override -- which the route no
    longer accepts as a parameter at all)."""
    real_token = create_checkout_token(business_id=business.id, plan="basic")

    # There's no business_id query param anymore for an attacker to even try
    # overriding -- confirm the route ignores one if supplied alongside a
    # valid token for a *different* business, rather than trusting it.
    resp = client.get(f"/billing/checkout?token={real_token}&business_id=999999", follow_redirects=False)
    assert resp.status_code == 303
    # The Stripe session (faked above) doesn't reveal which business_id was
    # used server-side from this response alone, but the route only ever
    # reads business_id out of the verified token -- there is no code path
    # that reads the business_id query param at all anymore (see
    # app/routes/billing.py: start_checkout's signature has no such param).


def test_plan_cannot_be_changed_via_query_string(client, business):
    """Old API: ?business_id=5&plan=basic could become ?plan=pro by hand.
    New API: plan is inside the signed token; a `plan` query param, if
    supplied, is simply not read by the route at all."""
    basic_token = create_checkout_token(business_id=business.id, plan="basic")

    resp = client.get(f"/billing/checkout?token={basic_token}&plan=pro", follow_redirects=False)
    assert resp.status_code == 303
    # (No direct way to assert "pro" wasn't used without inspecting the fake
    # Stripe call's kwargs -- covered more directly in the next test.)


def test_route_only_ever_uses_business_id_and_plan_from_the_verified_token(client, business, monkeypatch):
    """Directly confirms the plan/business_id actually passed to Stripe come
    from the token, not from any query string, by capturing the real
    arguments create_checkout_session was called with."""
    import app.routes.billing as billing_module

    captured = {}

    def capturing_create_checkout_session(**kwargs):
        captured.update(kwargs)
        class FakeSession:
            url = "https://checkout.stripe.com/fake-session"
        return FakeSession()

    monkeypatch.setattr(billing_module, "create_checkout_session", capturing_create_checkout_session)

    token = create_checkout_token(business_id=business.id, plan="basic")
    client.get(
        f"/billing/checkout?token={token}&business_id=999999&plan=pro",
        follow_redirects=False,
    )

    assert captured["business_id"] == business.id
    assert captured["plan"] == "basic"


# --- Response behavior: no distinguishing signal between failure modes ---


def test_invalid_token_and_nonexistent_business_get_identical_error_response(client):
    """A garbage token and a well-formed-but-for-a-deleted-business token
    should look the same from the outside -- neither should let someone
    distinguish "this token is fake" from "this business doesn't exist"."""
    garbage_resp = client.get("/billing/checkout?token=garbage-token-value", follow_redirects=False)

    valid_token_deleted_business = create_checkout_token(business_id=999999, plan="basic")
    deleted_business_resp = client.get(
        f"/billing/checkout?token={valid_token_deleted_business}", follow_redirects=False
    )

    assert garbage_resp.status_code == deleted_business_resp.status_code == 400


def test_missing_token_param_is_a_client_error_not_a_500(client):
    resp = client.get("/billing/checkout", follow_redirects=False)
    assert resp.status_code == 422  # FastAPI's standard missing-required-query-param response


# --- Onboarding actually generates token-based links now -----------------


def test_onboarding_generates_token_links_not_raw_business_id(client):
    """/onboarding/create's success page should hand back /billing/checkout
    links using ?token=..., never the old ?business_id=&plan= form."""
    login = client.post("/login", data={"key": os.environ["DASHBOARD_KEY"]})
    assert login.status_code == 303

    # Unique per run: /onboarding/create rejects a reused Twilio number, and
    # the on-disk test DB can survive between runs.
    unique_number = "+1555" + str(uuid.uuid4().int)[:7]
    resp = client.post(
        "/onboarding/create",
        data={
            "name": "Onboarding Link Test Co",
            "twilio_number": unique_number,
            "owner_phone": "+15555550002",
            "timezone": "America/Denver",
        },
    )
    assert resp.status_code == 200
    assert "billing/checkout?token=" in resp.text
    assert "business_id=" not in resp.text
    assert "&plan=" not in resp.text

    # The links must actually be accepted by /billing/checkout, not just
    # look right -- this is the exact path a paying customer clicks.
    # Each link appears twice (href + visible text), so dedupe.
    tokens = set(re.findall(r"billing/checkout\?token=([^\"<]+)", resp.text))
    assert len(tokens) == 2
    for tok in tokens:
        checkout = client.get(f"/billing/checkout?token={tok}")
        assert checkout.status_code == 303, checkout.text


def test_edit_page_shows_fresh_checkout_links_while_inactive(client):
    """Tokens expire and the create page shows them once. The edit page must
    re-mint working links for any business that hasn't paid yet, so a
    prospect who asks for the link later can still be sent one."""
    with DBSession(engine) as db:
        b = Business(
            name="Not Paid Yet Co",
            twilio_number="+15555554444",
            owner_phone="+15555550004",
            is_active=False,  # what /onboarding/create sets; the model default is True
        )
        db.add(b)
        db.commit()
        db.refresh(b)

    login = client.post("/login", data={"key": os.environ["DASHBOARD_KEY"]})
    assert login.status_code == 303

    resp = client.get(f"/onboarding/edit/{b.id}")
    assert resp.status_code == 200
    tokens = set(re.findall(r"billing/checkout\?token=([^\"<]+)", resp.text))
    assert len(tokens) == 2
    for tok in tokens:
        assert verify_checkout_token(tok) is not None
        assert client.get(f"/billing/checkout?token={tok}").status_code == 303


def test_edit_page_hides_checkout_links_once_active(client):
    """No point offering a paid business a link to pay again."""
    with DBSession(engine) as db:
        b = Business(
            name="Already Paid Co",
            twilio_number="+15555553333",
            owner_phone="+15555550003",
            is_active=True,
            subscription_status="active",
            plan="basic",
        )
        db.add(b)
        db.commit()
        db.refresh(b)

    login = client.post("/login", data={"key": os.environ["DASHBOARD_KEY"]})
    assert login.status_code == 303

    resp = client.get(f"/onboarding/edit/{b.id}")
    assert resp.status_code == 200
    assert "billing/checkout?token=" not in resp.text
