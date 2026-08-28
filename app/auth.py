"""Dashboard/onboarding authentication.

Replaces the old bare query-string `?key=...` scheme (which leaks into server
logs, browser history, and Referer headers — see the DASHBOARD_KEY exposure
that prompted this) with a login form that sets a signed, HttpOnly session
cookie. The shared secret (DASHBOARD_KEY) is only ever typed into a POST body
now, never carried in a URL.

Session cookies are signed with SESSION_SECRET, a separate value from
DASHBOARD_KEY. This matters because DASHBOARD_KEY has already leaked once
(into Railway logs, before the /login rework). If it ever leaks again,
signing sessions with it would let an attacker forge a valid session cookie
directly — bypassing /login, and any rate limit on /login, entirely. Keeping
the two secrets independent means a leaked login key only grants what
/login's rate limit allows: guessing the key, not manufacturing sessions.
"""

import logging
import os
import secrets

from fastapi import Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import AppSetting

logger = logging.getLogger(__name__)

DASHBOARD_KEY = os.getenv("DASHBOARD_KEY", "")

# Independent secret for signing session cookies. Falls back to DASHBOARD_KEY
# with a loud warning (not silently) so local/dev setups that predate this
# still boot, but any real deployment should set its own SESSION_SECRET.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not SESSION_SECRET:
    if DASHBOARD_KEY:
        logger.warning(
            "SESSION_SECRET is not set; falling back to DASHBOARD_KEY for session "
            "signing. Set a separate SESSION_SECRET so a leaked DASHBOARD_KEY can't "
            "be used to forge session cookies directly."
        )
        SESSION_SECRET = DASHBOARD_KEY
    else:
        SESSION_SECRET = "dev-insecure-fallback-key"

SESSION_COOKIE_NAME = "dashboard_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days

_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="dashboard-session")


class NotAuthenticated(Exception):
    """Raised by require_dashboard_auth; app.main registers a handler that
    turns this into a 303 redirect to /login instead of a bare 401, since
    these are browser-facing HTML pages, not an API."""


def key_matches(candidate: str) -> bool:
    """Constant-time comparison against the configured DASHBOARD_KEY."""
    if not DASHBOARD_KEY:
        return False
    return secrets.compare_digest(candidate, DASHBOARD_KEY)


# --- Session revocation -----------------------------------------------------
#
# A signed cookie can't be individually revoked (there's no server-side state
# to delete) — normally that means a leaked session cookie stays valid until
# it expires, up to 7 days, no matter what you do. SESSION_VERSION closes that
# gap cheaply: every issued token embeds the current version number, and
# every check compares it against the live value in the database. Bumping the
# version (via revoke_all_sessions()) invalidates every outstanding session in
# one write, without needing a full session table or Redis for an app this size.
#
# This lives in the database (AppSetting), not a local file or in-memory
# value: Railway's app container filesystem/process isn't guaranteed to
# survive a redeploy, but the configured database is a separate managed
# service and does — a file-based counter would silently un-revoke every
# session on the next deploy, defeating the whole point.
_SESSION_VERSION_KEY = "dashboard_session_version"


def _read_session_version() -> int:
    with Session(engine) as db:
        row = db.exec(select(AppSetting).where(AppSetting.key == _SESSION_VERSION_KEY)).first()
        if row is None:
            return 0
        try:
            return int(row.value)
        except ValueError:
            return 0


def revoke_all_sessions() -> int:
    """Invalidate every outstanding session cookie. Returns the new version.

    Call this any time DASHBOARD_KEY is rotated, or if a session is ever
    suspected compromised (lost device, etc.) — there's no per-session
    granularity, but for a single-operator dashboard "log everyone out" is
    the right blunt instrument.
    """
    with Session(engine) as db:
        row = db.exec(select(AppSetting).where(AppSetting.key == _SESSION_VERSION_KEY)).first()
        new_version = (int(row.value) if row else 0) + 1
        if row is None:
            row = AppSetting(key=_SESSION_VERSION_KEY, value=str(new_version))
        else:
            row.value = str(new_version)
        db.add(row)
        db.commit()
    logger.warning("All dashboard sessions revoked (version now %d)", new_version)
    return new_version


def create_session_cookie_value() -> str:
    """Return a signed, timestamped token to store in the session cookie."""
    return _serializer.dumps({"authenticated": True, "v": _read_session_version()})


def verify_session_token(token: str) -> bool:
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    if not data.get("authenticated"):
        return False
    # A token signed before the last revoke_all_sessions() call carries a
    # stale version number and must be rejected even though its signature is
    # still cryptographically valid.
    return data.get("v") == _read_session_version()


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


if __name__ == "__main__":
    # `python -m app.auth` — revoke every outstanding dashboard session.
    # Useful right after rotating DASHBOARD_KEY, or if a device with a live
    # session is lost. Run against the same DATABASE_URL the app uses (e.g.
    # `railway run python -m app.auth` in production) so it revokes the
    # sessions that actually matter, not a local dev database.
    from app.db import create_db_and_tables

    create_db_and_tables()
    new_version = revoke_all_sessions()
    print(f"All dashboard sessions revoked. New session version: {new_version}")
