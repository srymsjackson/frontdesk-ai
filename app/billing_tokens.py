"""Signed tokens for billing checkout links.

Checkout links are generated once (by the operator, via /onboarding) and
handed to a business owner outside the app -- SMS, email, however. Before
this module existed, the link was just /billing/checkout?business_id=5&plan=basic:
a raw sequential integer plus a plan name, both directly editable in the
browser's URL bar. That meant two separate problems:

  - business_id was trivially enumerable (1, 2, 3, ...), and a 404 vs a
    redirect told an attacker which IDs existed -- low-sensitivity, but real
    information disclosure about how many businesses are on the platform.
  - Nothing stopped anyone holding a legitimate link from just editing it:
    changing ?plan=basic to ?plan=pro on a link meant for someone else, or
    changing ?business_id=5 to ?business_id=6 so a completed payment
    activates a *different* business than the one the payer intended to pay
    for. Rate limiting (already in place) slows down brute-force guessing
    but does nothing about someone editing a link they already have.

Signing business_id + plan together as one token closes both: the token
can't be forged or edited without the signing secret, so a tampered token
fails validation outright instead of the route silently trusting whatever
business_id happens to be in the query string.
"""

import logging
import os

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logger = logging.getLogger(__name__)

# Falls back through SESSION_SECRET, then DASHBOARD_KEY, with a warning at
# each fallback level -- same layered pattern as app/auth.py's
# SESSION_SECRET. A dedicated secret keeps checkout-link forgery and
# dashboard-session forgery as separate blast radii: leaking one doesn't
# imply the other is compromised too.
CHECKOUT_LINK_SECRET = os.getenv("CHECKOUT_LINK_SECRET", "")
if not CHECKOUT_LINK_SECRET:
    _session_secret = os.getenv("SESSION_SECRET", "")
    _dashboard_key = os.getenv("DASHBOARD_KEY", "")
    if _session_secret:
        logger.warning(
            "CHECKOUT_LINK_SECRET is not set; falling back to SESSION_SECRET for "
            "signing checkout links. Set a dedicated CHECKOUT_LINK_SECRET so a "
            "leaked SESSION_SECRET can't be used to forge checkout links too."
        )
        CHECKOUT_LINK_SECRET = _session_secret
    elif _dashboard_key:
        logger.warning(
            "Neither CHECKOUT_LINK_SECRET nor SESSION_SECRET is set; falling back "
            "to DASHBOARD_KEY for signing checkout links."
        )
        CHECKOUT_LINK_SECRET = _dashboard_key
    else:
        CHECKOUT_LINK_SECRET = "dev-insecure-fallback-key"

# Checkout links may sit unclicked in an SMS/email for a while, so this is
# generous compared to the 7-day dashboard session -- but it's not
# unbounded: a link from a business that churned or was onboarded by mistake
# months ago shouldn't still work today.
CHECKOUT_LINK_MAX_AGE_SECONDS = 30 * 24 * 60 * 60  # 30 days

_serializer = URLSafeTimedSerializer(CHECKOUT_LINK_SECRET, salt="billing-checkout-link")


def create_checkout_token(business_id: int, plan: str) -> str:
    """Return a signed, timestamped token binding one business_id to one plan."""
    return _serializer.dumps({"business_id": business_id, "plan": plan})


def verify_checkout_token(token: str) -> tuple[int, str] | None:
    """Return (business_id, plan) if the token is valid and unexpired, else None.

    Deliberately returns None (not an exception with a distinguishable
    message) for every failure mode -- bad signature, expired, malformed
    payload -- so the route can give one generic response regardless of
    which way the token is bad, rather than leaking which.
    """
    try:
        data = _serializer.loads(token, max_age=CHECKOUT_LINK_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None

    business_id = data.get("business_id")
    plan = data.get("plan")
    if not isinstance(business_id, int) or not isinstance(plan, str):
        return None
    return business_id, plan
