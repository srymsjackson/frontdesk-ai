"""Dashboard/onboarding authentication.

Replaces the old bare query-string `?key=...` scheme (which leaks into server
logs, browser history, and Referer headers — see the DASHBOARD_KEY exposure
that prompted this) with a login form that sets a signed, HttpOnly session
cookie. The shared secret (DASHBOARD_KEY) is only ever typed into a POST body
now, never carried in a URL.
"""

import os
import secrets

from fastapi import Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import settings

DASHBOARD_KEY = os.getenv("DASHBOARD_KEY", "")

SESSION_COOKIE_NAME = "dashboard_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Signing secret for the session cookie. Derived from DASHBOARD_KEY (the only
# secret this app currently has) with a distinct salt so a session token can
# never be replayed as the raw key or vice versa.
_serializer = URLSafeTimedSerializer(DASHBOARD_KEY or "dev-insecure-fallback-key", salt="dashboard-session")


class NotAuthenticated(Exception):
    """Raised by require_dashboard_auth; app.main registers a handler that
    turns this into a 303 redirect to /login instead of a bare 401, since
    these are browser-facing HTML pages, not an API."""


def key_matches(candidate: str) -> bool:
    """Constant-time comparison against the configured DASHBOARD_KEY."""
    if not DASHBOARD_KEY:
        return False
    return secrets.compare_digest(candidate, DASHBOARD_KEY)


def create_session_cookie_value() -> str:
    """Return a signed, timestamped token to store in the session cookie."""
    return _serializer.dumps({"authenticated": True})


def verify_session_token(token: str) -> bool:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    return bool(data.get("authenticated"))


def require_dashboard_auth(request: Request) -> None:
    """FastAPI dependency: raises NotAuthenticated unless the request carries
    a valid, unexpired session cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token or not verify_session_token(token):
        raise NotAuthenticated()


def set_session_cookie(response) -> None:
    """Attach a fresh session cookie to an outgoing response after login."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_cookie_value(),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        # Only require HTTPS transport in production — local/dev testing over
        # plain http would otherwise silently drop the cookie.
        secure=(settings.app_env == "production"),
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME)
