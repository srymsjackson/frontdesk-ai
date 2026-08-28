"""Request-authentication dependencies shared across routers.

Currently: Twilio webhook signature verification, plus a replay-window check.

Twilio signs every request it sends to a configured webhook URL with an
X-Twilio-Signature header, an HMAC-SHA1 over (exact URL Twilio POSTed to +
sorted POST params) keyed by the account's auth token. Verifying it is the
only way to know a request to /voice/* actually came from Twilio and not a
scripted POST from anyone who found the URL.

That signature scheme has no nonce or timestamp, though -- the same URL and
params always produce the same signature, forever. So a request captured
once (a compromised proxy, a debugging tool that logged a raw body, a bug
that echoed one back) stays replayable indefinitely: resending the exact
same bytes months later still passes validate(). See _check_not_replayed()
below for how this is mitigated without breaking Twilio's own legitimate
retries, which are exact-duplicate resends of the same signed request.
"""

import hashlib
import logging
import time
from urllib.parse import urlencode

from fastapi import Request, HTTPException
from twilio.request_validator import RequestValidator
from app.config import settings

logger = logging.getLogger(__name__)

# How long an exact-duplicate signed request is still accepted as a
# legitimate retry before being treated as a replay. Twilio retries a
# webhook that times out or returns 5xx with the identical signed request,
# with backoff -- this needs to comfortably outlast that retry window so a
# real (slow, flaky) legitimate delivery never gets rejected as a replay.
# Chosen generously for that reason; the cost of getting this wrong in the
# permissive direction is a longer window an attacker's captured request
# would work in, not a broken legitimate call.
REPLAY_WINDOW_SECONDS = 10 * 60  # 10 minutes

# request_key -> first-seen unix timestamp. In-process memory, matching the
# same tradeoff already made for CALL_STATE in app/routes/voice.py (see its
# comment, and the README's "known limitations" section): fine for a single
# Railway instance, wouldn't survive a restart or share state across
# horizontally-scaled instances. A Redis-backed store would be the natural
# upgrade alongside CALL_STATE's, if this app ever needs to scale beyond one
# instance.
_seen_requests: dict[str, float] = {}


def _request_identity(url: str, params: dict) -> str:
    """A stable, fixed-size identifier for "this exact signed request" --
    same url + same params (order-independent) always hashes the same way,
    so a byte-for-byte resend (a replay, or a legitimate Twilio retry) is
    recognized as the same request rather than a new one."""
    canonical = url + "?" + urlencode(sorted(params.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prune_expired(now: float) -> None:
    """Drop entries older than twice the replay window. Runs on every call
    rather than on a schedule -- cheap for the request volumes this app
    handles (already capped by rate limiting), and means no background task
    or extra dependency just to keep the dict from growing unboundedly."""
    cutoff = now - (REPLAY_WINDOW_SECONDS * 2)
    expired = [key for key, seen_at in _seen_requests.items() if seen_at < cutoff]
    for key in expired:
        del _seen_requests[key]


def _check_not_replayed(url: str, params: dict) -> None:
    """Raise if this exact signed request was already seen more than
    REPLAY_WINDOW_SECONDS ago. Within the window, a repeat is treated as a
    legitimate Twilio retry and allowed silently -- only a repeat *outside*
    the window is rejected as a replay.
    """
    now = time.time()
    _prune_expired(now)

    key = _request_identity(url, params)
    first_seen = _seen_requests.get(key)

    if first_seen is None:
        _seen_requests[key] = now
        return

    if now - first_seen > REPLAY_WINDOW_SECONDS:
        logger.warning(
            "Rejected replayed Twilio request: identical signed request first seen "
            "%.0fs ago (window is %ds)",
            now - first_seen,
            REPLAY_WINDOW_SECONDS,
        )
        raise HTTPException(status_code=403, detail="This request has already been processed")
    # Within the window: treat as a legitimate retry, don't reset the
    # first-seen timestamp (the window is measured from the original
    # request, not extended by each retry).


async def verify_twilio_signature(request: Request) -> None:
    """FastAPI dependency: 403s any request without a valid Twilio signature,
    and 403s a valid signature replayed outside the retry window.

    The signature covers the *exact* URL Twilio requested, so we rebuild it
    from settings.base_url (the trusted, configured value) rather than
    trusting request.url or the Host header — an attacker-controlled Host
    header must never influence what URL we validate against.
    """
    if not settings.twilio_auth_token:
        # An empty auth token means the HMAC key is a known, non-secret value —
        # any caller could compute a "valid" signature themselves. Fail closed
        # instead of silently accepting everything.
        logger.error("TWILIO_AUTH_TOKEN is not configured; rejecting request to %s", request.url.path)
        raise HTTPException(status_code=500, detail="Server misconfiguration")

    signature = request.headers.get("X-Twilio-Signature", "")

    form = await request.form()
    params = dict(form)

    url = f"{settings.base_url.rstrip('/')}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    validator = RequestValidator(settings.twilio_auth_token)

    if not signature or not validator.validate(url, params, signature):
        logger.warning(
            "Rejected unsigned/invalid Twilio request path=%s has_sig=%s",
            request.url.path,
            bool(signature),
        )
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    _check_not_replayed(url, params)
