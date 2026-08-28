"""Request-authentication dependencies shared across routers.

Currently: Twilio webhook signature verification. Twilio signs every request
it sends to a configured webhook URL with an X-Twilio-Signature header, an
HMAC-SHA1 over (exact URL Twilio POSTed to + sorted POST params) keyed by the
account's auth token. Verifying it is the only way to know a request to
/voice/* actually came from Twilio and not a scripted POST from anyone who
found the URL.
"""

import logging
from fastapi import Request, HTTPException
from twilio.request_validator import RequestValidator
from app.config import settings

logger = logging.getLogger(__name__)


async def verify_twilio_signature(request: Request) -> None:
    """FastAPI dependency: 403s any request without a valid Twilio signature.

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
