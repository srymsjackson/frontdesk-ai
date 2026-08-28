"""Dashboard/onboarding auth tests (Priority 2B of the hardening plan).

Confirms the key never has to appear in a URL: /dashboard/leads and
/onboarding redirect unauthenticated visitors to /login, a POST with the
correct key sets a session cookie, and that cookie is what grants access
afterward — not any query string.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import engine
from sqlmodel import SQLModel


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    # follow_redirects=False so we can assert on the redirect itself, not
    # just where it eventually lands.
    return TestClient(app, follow_redirects=False)


def test_dashboard_redirects_to_login_when_unauthenticated(client):
    resp = client.get("/dashboard/leads")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_onboarding_redirects_to_login_when_unauthenticated(client):
    resp = client.get("/onboarding")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_login_with_wrong_key_does_not_set_cookie(client):
    resp = client.post("/login", data={"key": "totally-wrong-key"})
    assert resp.status_code == 200  # re-renders the form with an error
    assert "dashboard_session" not in resp.cookies


def test_login_with_correct_key_sets_cookie_and_redirects(client):
    resp = client.post("/login", data={"key": "test-dashboard-key-not-a-real-secret"})
    assert resp.status_code == 303
    assert "dashboard_session" in resp.cookies


def test_session_cookie_grants_dashboard_access(client):
    login_resp = client.post("/login", data={"key": "test-dashboard-key-not-a-real-secret"})
    session_cookie = login_resp.cookies["dashboard_session"]

    resp = client.get("/dashboard/leads", cookies={"dashboard_session": session_cookie})
    assert resp.status_code == 200
    assert "Leads" in resp.text


def test_garbage_cookie_value_is_rejected(client):
    resp = client.get("/dashboard/leads", cookies={"dashboard_session": "not-a-valid-signed-token"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_login_next_param_is_not_an_open_redirect(client):
    """?next=https://evil.example must not be honored as a redirect target."""
    resp = client.post(
        "/login",
        data={"key": "test-dashboard-key-not-a-real-secret", "next": "https://evil.example/phish"},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/leads"
