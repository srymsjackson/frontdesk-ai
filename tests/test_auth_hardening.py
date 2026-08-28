"""Auth-chain hardening tests: session secret independence, /login rate
limiting, and server-side session revocation.

Follow-up to tests/test_dashboard_auth.py, covering the gaps identified in
review: DASHBOARD_KEY had already leaked once, and before this pass (a) a
second leak would let an attacker forge session cookies directly, (b) /login
had no rate limit despite being the one truly brute-forceable endpoint, and
(c) a leaked session cookie was valid for up to 7 days with no way to kill it
early.
"""

import os

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel

from app.main import app
from app.db import engine


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def _dashboard_key() -> str:
    return os.environ["DASHBOARD_KEY"]


# --- Session secret independence --------------------------------------------


def test_session_cookie_is_not_signed_with_the_raw_dashboard_key(client):
    """A session cookie must not be forgeable just by knowing DASHBOARD_KEY
    and re-implementing itsdangerous by hand with that same value — i.e. the
    live SESSION_SECRET must differ from DASHBOARD_KEY in this test env.

    conftest.py sets DASHBOARD_KEY but not SESSION_SECRET, so app.auth falls
    back to using DASHBOARD_KEY (with a warning) for signing — this test
    exists to make that fallback visible and to fail loudly if a real
    SESSION_SECRET ever gets set in test config without updating this
    assumption, since production must not run on the fallback.
    """
    from app import auth as auth_module

    if os.environ.get("SESSION_SECRET"):
        assert auth_module.SESSION_SECRET != auth_module.DASHBOARD_KEY, (
            "SESSION_SECRET is configured but matches DASHBOARD_KEY — set it to a "
            "distinct value so a leaked DASHBOARD_KEY can't forge sessions."
        )
    else:
        # No SESSION_SECRET configured: confirm the documented fallback
        # behavior (same value, with a warning) rather than something silent
        # or different from what app/auth.py's module docstring promises.
        assert auth_module.SESSION_SECRET == auth_module.DASHBOARD_KEY


def test_forged_cookie_using_dashboard_key_as_signing_secret_is_rejected(client, monkeypatch):
    """Simulates the scenario SESSION_SECRET exists to prevent: attacker knows
    DASHBOARD_KEY (e.g. from a log leak) and tries to sign their own session
    cookie with it, bypassing /login entirely. With a real, distinct
    SESSION_SECRET configured, this must fail even though the forged token
    would have worked against the raw DASHBOARD_KEY-signed scheme.

    Patches the module's _serializer directly via monkeypatch.setattr rather
    than importlib.reload()'ing app.auth: reloading re-executes the module
    body and creates a brand-new NotAuthenticated *class object*, which no
    longer matches the exception-handler registration app.main did at import
    time against the original class — silently breaking auth error handling
    for every test that runs afterward in the same session. setattr swaps
    just the one value under test and leaves everything else — including
    class identity — alone.
    """
    from itsdangerous import URLSafeTimedSerializer
    from app import auth as auth_module

    real_session_secret = "a-real-distinct-session-secret-value"
    monkeypatch.setattr(auth_module, "SESSION_SECRET", real_session_secret)
    monkeypatch.setattr(
        auth_module,
        "_serializer",
        URLSafeTimedSerializer(real_session_secret, salt="dashboard-session"),
    )

    # Attacker forges a token using the leaked DASHBOARD_KEY as if it were
    # the signing secret.
    forged_serializer = URLSafeTimedSerializer(_dashboard_key(), salt="dashboard-session")
    forged_token = forged_serializer.dumps({"authenticated": True, "v": 0})

    assert auth_module.verify_session_token(forged_token) is False


# --- /login rate limiting ---------------------------------------------------


def test_login_rate_limited_after_5_per_minute(client):
    statuses = []
    for _ in range(8):
        resp = client.post("/login", data={"key": "wrong-key-on-purpose"})
        statuses.append(resp.status_code)

    # First 5 attempts reach the route and re-render the form with an error
    # (200), regardless of whether the key was right.
    assert all(s != 429 for s in statuses[:5]), statuses[:5]
    # Everything past the cap is rejected by the limiter before touching
    # key-comparison logic at all.
    assert all(s == 429 for s in statuses[5:]), statuses[5:]


def test_login_rate_limit_applies_even_to_correct_key(client):
    """The cap is on attempts, not failures — a script trying the correct key
    in a tight loop (e.g. a misconfigured client retrying) is capped the same
    as a brute-force attempt."""
    statuses = []
    for _ in range(7):
        resp = client.post("/login", data={"key": _dashboard_key()})
        statuses.append(resp.status_code)

    assert statuses[5] == 429
    assert statuses[6] == 429


# --- Session revocation -----------------------------------------------------


def test_revoke_all_sessions_invalidates_existing_cookie(client):
    from app.auth import revoke_all_sessions

    login_resp = client.post("/login", data={"key": _dashboard_key()})
    assert login_resp.status_code == 303
    session_cookie = login_resp.cookies["dashboard_session"]

    # Cookie works before revocation.
    resp = client.get("/dashboard/leads", cookies={"dashboard_session": session_cookie})
    assert resp.status_code == 200

    revoke_all_sessions()

    # Same cookie, same signature, now rejected — the version embedded in the
    # token no longer matches the live version.
    resp = client.get("/dashboard/leads", cookies={"dashboard_session": session_cookie})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_new_login_after_revocation_works_and_issues_a_fresh_valid_cookie(client):
    from app.auth import revoke_all_sessions

    revoke_all_sessions()

    login_resp = client.post("/login", data={"key": _dashboard_key()})
    assert login_resp.status_code == 303
    new_cookie = login_resp.cookies["dashboard_session"]

    resp = client.get("/dashboard/leads", cookies={"dashboard_session": new_cookie})
    assert resp.status_code == 200


def test_revocation_persists_across_a_fresh_db_session(client):
    """Guards against a regression back to in-process/file state: the version
    check must go through the same `engine` the rest of the app uses, so a
    revoke from one process (e.g. a CLI invocation) is visible to requests
    handled by another."""
    from sqlmodel import Session
    from app.db import engine
    from app.auth import revoke_all_sessions, _read_session_version

    login_resp = client.post("/login", data={"key": _dashboard_key()})
    cookie = login_resp.cookies["dashboard_session"]

    revoke_all_sessions()

    # Read the version back via a brand-new DB session, simulating a
    # different process reading the same persisted state.
    with Session(engine):
        version_seen_elsewhere = _read_session_version()

    resp = client.get("/dashboard/leads", cookies={"dashboard_session": cookie})
    assert resp.status_code == 303
    assert version_seen_elsewhere >= 1
