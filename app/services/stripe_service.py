"""Stripe subscription checkout + webhook verification helpers.

Flow:
  1. Onboarding creates a Business with is_active=False (not paid yet).
  2. Owner is sent a /billing/checkout link for their chosen plan.
  3. They pay via Stripe's hosted Checkout page.
  4. Stripe calls our webhook -> we flip Business.is_active = True and store
     the Stripe customer/subscription ids so future events (cancellation,
     failed payment) can flip it back off.
"""

import logging
import stripe
from app.config import settings

logger = logging.getLogger(__name__)

stripe.api_key = settings.stripe_secret_key

PLAN_PRICE_IDS = {
    "basic": settings.stripe_price_basic,
    "pro": settings.stripe_price_pro,
}


def price_id_for_plan(plan: str) -> str | None:
    """Look up the Stripe Price ID for a plan slug, or None if unknown/unconfigured."""
    return PLAN_PRICE_IDS.get(plan) or None


def create_checkout_session(business_id: int, plan: str, success_url: str, cancel_url: str):
    """Create a Stripe Checkout session for a business's subscription.

    Stashes business_id and plan in metadata (and client_reference_id) so the
    webhook handler can map the completed session back to our Business row
    without relying on email matching.
    """
    price_id = price_id_for_plan(plan)
    if not price_id:
        raise ValueError(f"No Stripe price configured for plan '{plan}'")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(business_id),
        metadata={"business_id": str(business_id), "plan": plan},
        subscription_data={"metadata": {"business_id": str(business_id), "plan": plan}},
    )
    logger.info("Created Stripe checkout session id=%s business_id=%s plan=%s", session.id, business_id, plan)
    return session


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verify and parse an incoming Stripe webhook payload. Raises on bad signature."""
    return stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
