"""Client intake form: turns a new business's answers into a Business + BusinessConfig row.

Protected by the same signed session-cookie auth as the dashboard (see app/auth.py).
This does NOT generate prompts with AI — it's a structured place to type in prompt
wording by hand so a new business's config lives in one form instead of being
hand-built in a Python shell.
"""

import html
import json
import logging
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import Business, BusinessConfig
from app.auth import require_dashboard_auth
from app.rate_limit import limiter
from app.config import settings
from app.billing_tokens import create_checkout_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


def esc(value):
    """HTML-escape a value for safe inline rendering, treating None as empty."""
    if value is None:
        return ""
    return html.escape(str(value))


def render_form(error: str | None = None, success: str | None = None) -> str:
    """Render the intake form.

    `success` is trusted HTML (built server-side, not from user input) so links render;
    `error` is always escaped since it can echo back user-typed values."""
    error_html = f'<div class="banner error">{esc(error)}</div>' if error else ""
    success_html = f'<div class="banner success">{success}</div>' if success else ""

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>New Client Intake</title>
        <style>
            body {{ font-family: -apple-system, system-ui, sans-serif; margin: 32px; color: #222; max-width: 720px; }}
            h1 {{ margin-bottom: 4px; }}
            h2 {{ margin-top: 32px; margin-bottom: 8px; font-size: 16px; color: #555; border-bottom: 1px solid #eee; padding-bottom: 6px; }}
            .sub {{ color: #777; margin-bottom: 24px; }}
            label {{ display: block; font-size: 13px; font-weight: 600; color: #444; margin-top: 14px; margin-bottom: 4px; }}
            .hint {{ font-size: 12px; color: #888; font-weight: 400; margin-top: 2px; }}
            input[type=text], textarea {{
                width: 100%; padding: 8px 10px; font-size: 14px; border: 1px solid #ccc;
                border-radius: 6px; box-sizing: border-box; font-family: inherit;
            }}
            textarea {{ resize: vertical; min-height: 60px; }}
            .checkbox-row {{ display: flex; align-items: center; gap: 8px; margin-top: 10px; }}
            .checkbox-row label {{ margin: 0; font-weight: 500; }}
            button {{
                margin-top: 28px; padding: 10px 20px; font-size: 14px; font-weight: 600;
                background: #222; color: #fff; border: none; border-radius: 6px; cursor: pointer;
            }}
            button:hover {{ background: #444; }}
            .banner {{ padding: 12px 16px; border-radius: 6px; margin-bottom: 20px; font-size: 14px; }}
            .banner.error {{ background: #fdecea; color: #a33; }}
            .banner.success {{ background: #e8f5e9; color: #2a7a3b; }}
        </style>
    </head>
    <body>
        <h1>New Client Intake</h1>
        <div class="sub">Creates a Business + BusinessConfig row. Takes ~10 minutes per client.</div>

        {error_html}
        {success_html}

        <form method="post" action="/onboarding/create">
            <h2>Business basics</h2>

            <label>Business name</label>
            <input type="text" name="name" required placeholder="e.g. The Shaky Razor">

            <label>Twilio number this business receives calls on
                <div class="hint">Format: +1XXXXXXXXXX</div>
            </label>
            <input type="text" name="twilio_number" required placeholder="+14352654742">

            <label>Owner's phone (gets lead notifications)</label>
            <input type="text" name="owner_phone" required placeholder="+15155551234">

            <label>Owner's email (optional)</label>
            <input type="text" name="owner_email" placeholder="owner@shop.com">

            <label>Booking link (optional)
                <div class="hint">Sent to the customer via SMS if set. Leave blank if there's no online booking yet.</div>
            </label>
            <input type="text" name="booking_link" placeholder="https://bookingsite.com">

            <label>Business type</label>
            <input type="text" name="business_type" placeholder="e.g. barbershop">

            <label>Timezone</label>
            <input type="text" name="timezone" value="America/Denver">

            <h2>Call script (write these by hand)</h2>

            <label>Greeting
                <div class="hint">First thing the caller hears.</div>
            </label>
            <textarea name="greeting" placeholder="Shaky Razor, how can I help you?"></textarea>

            <label>Fallback message
                <div class="hint">Said when something goes wrong mid-call.</div>
            </label>
            <textarea name="fallback_message" placeholder="Sorry, something went wrong. Please call back in a few minutes."></textarea>

            <label>Completion message
                <div class="hint">Said once all required info is collected.</div>
            </label>
            <textarea name="completion_message" placeholder="Perfect, I've got everything I need. The shop will follow up with you soon."></textarea>

            <label>Ask-for-name prompt</label>
            <textarea name="ask_name_prompt">Can I get your name, please?</textarea>

            <label>Ask-for-service prompt</label>
            <textarea name="ask_service_prompt">What service are you looking for today?</textarea>

            <label>Ask-for-time prompt</label>
            <textarea name="ask_time_prompt">What specific time were you thinking?</textarea>

            <label>Ask-for-staff prompt</label>
            <textarea name="ask_staff_prompt">Do you have a preferred staff member?</textarea>

            <h2>Services & staff</h2>

            <label>Services offered (comma-separated)
                <div class="hint">e.g. fade, haircut, lineup, beard trim</div>
            </label>
            <input type="text" name="services" placeholder="fade, haircut, lineup, beard trim">

            <label>Staff names (comma-separated, optional)</label>
            <input type="text" name="staff" placeholder="Tony, Marcus">

            <label>Required fields before the call can complete (comma-separated)
                <div class="hint">Must be from: caller_name, service_requested, preferred_time, preferred_barber</div>
            </label>
            <input type="text" name="required_fields" value="caller_name, service_requested, preferred_time">

            <h2>Notifications</h2>

            <div class="checkbox-row">
                <input type="checkbox" id="collect_notes" name="collect_notes" checked>
                <label for="collect_notes">Collect free-text notes from caller</label>
            </div>
            <div class="checkbox-row">
                <input type="checkbox" id="send_customer_sms" name="send_customer_sms" checked>
                <label for="send_customer_sms">Text the customer a booking link after the call</label>
            </div>
            <div class="checkbox-row">
                <input type="checkbox" id="send_owner_sms" name="send_owner_sms" checked>
                <label for="send_owner_sms">Text the owner a lead notification after the call</label>
            </div>

            <button type="submit">Create business</button>
        </form>
    </body>
    </html>
    """


@router.get("", response_class=HTMLResponse)
def intake_form(_auth: None = Depends(require_dashboard_auth)):
    """Render the blank intake form."""
    return HTMLResponse(content=render_form())


@router.post("/create", response_class=HTMLResponse)
@limiter.limit("5/minute")
def create_business(
    request: Request,
    name: str = Form(...),
    twilio_number: str = Form(...),
    owner_phone: str = Form(...),
    owner_email: str = Form(""),
    booking_link: str = Form(""),
    business_type: str = Form(""),
    timezone: str = Form("America/Denver"),
    greeting: str = Form(""),
    fallback_message: str = Form(""),
    completion_message: str = Form(""),
    ask_name_prompt: str = Form("Can I get your name, please?"),
    ask_service_prompt: str = Form("What service are you looking for today?"),
    ask_time_prompt: str = Form("What specific time were you thinking?"),
    ask_staff_prompt: str = Form("Do you have a preferred staff member?"),
    services: str = Form(""),
    staff: str = Form(""),
    required_fields: str = Form("caller_name, service_requested, preferred_time"),
    collect_notes: bool = Form(False),
    send_customer_sms: bool = Form(False),
    send_owner_sms: bool = Form(False),
    session: Session = Depends(get_session),
    _auth: None = Depends(require_dashboard_auth),
):
    """Create the Business + BusinessConfig rows from submitted form data."""
    name = name.strip()
    twilio_number = twilio_number.strip()
    owner_phone = owner_phone.strip()

    if not name or not twilio_number or not owner_phone:
        return HTMLResponse(
            content=render_form(error="Business name, Twilio number, and owner phone are required.")
        )

    existing = session.exec(
        select(Business).where(Business.twilio_number == twilio_number)
    ).first()
    if existing:
        return HTMLResponse(
            content=render_form(error=f"A business already uses Twilio number {twilio_number} (id={existing.id}).")
        )

    business = Business(
        name=name,
        twilio_number=twilio_number,
        owner_phone=owner_phone,
        owner_email=owner_email.strip() or None,
        booking_link=booking_link.strip() or None,
        business_type=business_type.strip() or None,
        timezone=timezone.strip() or "UTC",
        is_active=False,  # goes live once Stripe checkout completes (see /billing/webhook)
    )
    session.add(business)
    session.commit()
    session.refresh(business)

    services_list = [s.strip() for s in services.split(",") if s.strip()]
    staff_list = [s.strip() for s in staff.split(",") if s.strip()]
    required_fields_list = [f.strip() for f in required_fields.split(",") if f.strip()]

    config = BusinessConfig(
        business_id=business.id,
        greeting=greeting.strip() or None,
        fallback_message=fallback_message.strip() or None,
        completion_message=completion_message.strip() or None,
        required_fields_json=json.dumps(required_fields_list),
        services_json=json.dumps(services_list),
        staff_json=json.dumps(staff_list),
        ask_name_prompt=ask_name_prompt.strip(),
        ask_service_prompt=ask_service_prompt.strip(),
        ask_time_prompt=ask_time_prompt.strip(),
        ask_staff_prompt=ask_staff_prompt.strip(),
        collect_notes=collect_notes,
        send_customer_sms=send_customer_sms,
        send_owner_sms=send_owner_sms,
    )
    session.add(config)
    session.commit()

    logger.info("Onboarded new business id=%s name=%s", business.id, business.name)

    base = settings.base_url.rstrip("/")
    basic_token = create_checkout_token(business_id=business.id, plan="basic")
    pro_token = create_checkout_token(business_id=business.id, plan="pro")
    basic_link = f"{base}/billing/checkout?token={basic_token}"
    pro_link = f"{base}/billing/checkout?token={pro_token}"

    success_html = (
        f"Created '{esc(business.name)}' (business id={business.id}), currently <b>inactive</b> until they pay. "
        f"Send them one of these checkout links &mdash; the business auto-activates when payment completes:<br><br>"
        f"Basic: <a href=\"{basic_link}\">{basic_link}</a><br>"
        f"Pro: <a href=\"{pro_link}\">{pro_link}</a><br><br>"
        f"Also point their Twilio number's voice webhook at your /voice/incoming endpoint."
    )

    return HTMLResponse(content=render_form(success=success_html))
