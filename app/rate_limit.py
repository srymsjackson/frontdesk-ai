"""Rate limiting: caps request volume on public-facing / abuse-prone routes.

On /voice/*, Twilio's own signature validation (app/security.py) is the
*primary* defense — an unsigned or forged request never reaches the route at
all. Rate limiting here is defense-in-depth on top of that: it caps damage if
a genuinely valid signature is ever replayed (Twilio's signature scheme has
no nonce/timestamp, so a captured valid request could technically be resent),
and it caps runaway cost if something upstream misbehaves and hammers us with
otherwise-legitimate-looking traffic.

On /onboarding/create and /billing/checkout, it's a more direct defense:
onboarding is behind the dashboard login now (see app/auth.py), so this is a
backstop against a compromised session rather than the primary control;
checkout has no auth at all (customers reach it via a link in an SMS), so
this is the main thing standing between it and someone scripting repeated
Stripe Checkout Session creation against a guessed/enumerated business_id.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def client_ip_key(request: Request) -> str:
    """Identify the caller for rate-limiting purposes.

    This app deploys on Railway, which sits behind a single trusted edge
    proxy that sets X-Forwarded-For with the real client IP as the leftmost
    entry (Railway's own guidance: that header is proxy-controlled and safe
    to rely on for this deployment shape). Locally, with no proxy in front,
    there's no X-Forwarded-For, so this falls back to the direct connection.

    NOTE: slowapi ships a built-in get_ipaddr() that looks the header up as
    "X_FORWARDED_FOR" (underscores) — that never matches the real
    "X-Forwarded-For" HTTP header name, so it silently always falls through
    to the direct-connection IP. Don't swap this out for that one.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip_key)
