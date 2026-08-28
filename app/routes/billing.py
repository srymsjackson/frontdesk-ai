"""Stripe checkout + webhook endpoints.

/billing/checkout   - GET, redirects the browser to a Stripe-hosted Checkout page
                       for a given business_id + plan.
/billing/success     - GET, plain confirmation page Stripe redirects back to.
/billing/cancel       - GET, plain "you didn't complete checkout" page.
/billing/webhook     - POST, Stripe calls this on subscription lifecycle events.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select
import stripe

from app.db import get_session
from app.models import Business
from app.config import settings
from app.rate_limit import limiter
from app.billing_tokens import verify_checkout_token
from app.services.stripe_service import create_checkout_session, construct_webhook_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

VALID_PLANS = {"basic", "pro"}


def simple_page(title: str, message: str) -> str:
    """Minimal styled HTML page, matching the look of the other plain-HTML routes."""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{title}</title>
        <style>
            body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 60px 24px;
                    text-align: center; color: #222; }}
            h1 {{ font-size: 22px; }}
            p {{ color: #666; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        <p>{message}</p>
    </body>
    </html>
    """


@router.get("/checkout")
@limiter.limit("10/minute")
def start_checkout(
    request: Request,
    token: str = Query(..., description="Signed token from /onboarding, binds one business_id to one plan"),
    session: Session = Depends(get_session),
):
    """Redirect to a Stripe Checkout page for the business + plan bound to `token`.

    Takes a signed token (see app/billing_tokens.py) rather than raw
    business_id/plan query params -- those were directly editable in the URL
    bar, so a link handed to one business could be altered to activate a
    different business_id, or to pay for a different plan than the one
    actually offered. A tampered or expired token fails validation the same
    way a garbage one does: one generic error, not "invalid" vs "not found",
    so a bad guess doesn't confirm anything about what does exist.
    """
    decoded = verify_checkout_token(token)
    if decoded is None:
        raise HTTPException(status_code=400, detail="This checkout link is invalid or has expired.")
    business_id, plan = decoded

    if plan not in VALID_PLANS:
        # Defense-in-depth: tokens are only ever minted by /onboarding with a
        # hardcoded plan value, so this shouldn't be reachable in practice.
        raise HTTPException(status_code=400, detail=f"Unknown plan '{plan}'. Valid plans: {sorted(VALID_PLANS)}")

    business = session.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=400, detail="This checkout link is invalid or has expired.")

    base = settings.base_url.rstrip("/")
    try:
        checkout_session = create_checkout_session(
            business_id=business_id,
            plan=plan,
            success_url=f"{base}/billing/success?business_id={business_id}",
            cancel_url=f"{base}/billing/cancel?business_id={business_id}",
        )
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except stripe.error.StripeError as e:
        logger.error("Stripe API error creating checkout session for business_id=%s: %s", business_id, e)
        raise HTTPException(status_code=502, detail="Could not reach Stripe. Please try again shortly.")

    return RedirectResponse(url=checkout_session.url, status_code=303)


@router.get("/success", response_class=HTMLResponse)
def checkout_success(business_id: int = Query(...)):
    """Landing page after a successful Stripe Checkout. is_active flips via webhook,
    not here — Stripe's redirect can fire before the webhook lands, so this page
    never trusts query params to grant access."""
    return HTMLResponse(content=simple_page(
        "You're all set!",
        "Thanks for subscribing. Your line will be activated within a minute or two once we confirm payment.",
    ))


@router.get("/cancel", response_class=HTMLResponse)
def checkout_cancel(business_id: int = Query(...)):
    return HTMLResponse(content=simple_page(
        "Checkout canceled",
        "No charge was made. Reach out any time if you'd like to try again.",
    ))


@router.post("/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_session)):
    """Handle Stripe subscription lifecycle events and keep Business rows in sync."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = construct_webhook_event(payload, sig_header)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Rejected Stripe webhook: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event["type"]
    data = event["data"]["object"]
    logger.info("Stripe webhook received: %s", event_type)

    def _business_from(obj) -> Business | None:
        """Pull business_id out of metadata and look up the row."""
        business_id = (obj.get("metadata") or {}).get("business_id") or obj.get("client_reference_id")
        if not business_id:
            logger.warning("Stripe event %s missing business_id metadata", event_type)
            return None
        return session.get(Business, int(business_id))

    if event_type == "checkout.session.completed":
        business = _business_from(data)
        if business:
            business.stripe_customer_id = data.get("customer")
            business.stripe_subscription_id = data.get("subscription")
            business.plan = (data.get("metadata") or {}).get("plan") or business.plan
            business.is_active = True
            business.subscription_status = "active"
            session.add(business)
            session.commit()
            logger.info("Activated business id=%s via checkout.session.completed", business.id)

    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        sub_id = data.get("id")
        business = session.exec(
            select(Business).where(Business.stripe_subscription_id == sub_id)
        ).first()
        if business:
            status = data.get("status")  # active, past_due, canceled, unpaid, etc.
            business.subscription_status = status
            business.is_active = status == "active"
            session.add(business)
            session.commit()
            logger.info("Updated business id=%s subscription_status=%s is_active=%s", business.id, status, business.is_active)
        else:
            logger.warning("No business found for Stripe subscription id=%s", sub_id)

    else:
        logger.debug("Ignoring unhandled Stripe event type: %s", event_type)

    return {"received": True}
