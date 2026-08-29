"""Client intake form: turns a new business's answers into a Business + BusinessConfig row.

Protected by the same signed session-cookie auth as the dashboard (see app/auth.py).
This does NOT generate prompts with AI — it's a structured place to type in prompt
wording by hand so a new business's config lives in one form instead of being
hand-built in a Python shell.

Also includes /onboarding/edit/{business_id}, which reuses the same form to let an
already-live client's services, staff, prompts, and required fields be updated
without going back to a Python shell or the raw DB.
"""

import html
import json
import logging
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import Business, BusinessConfig
from app.auth import require_dashboard_auth
from app.rate_limit import limiter
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

VALID_REQUIRED_FIELDS = {"caller_name", "service_requested", "preferred_time", "preferred_barber"}


def esc(value):
    """HTML-escape a value for safe inline rendering, treating None as empty."""
    if value is None:
        return ""
    return html.escape(str(value))


def _joined(json_str: str | None) -> str:
    """Turn a JSON list column back into a comma-separated string for a text input."""
    if not json_str:
        return ""
    try:
        return ", ".join(json.loads(json_str))
    except Exception:
        return ""


def _checked(value: bool) -> str:
    return "checked" if value else ""


def render_form(
    business: Business | None = None,
    config: BusinessConfig | None = None,
    error: str | None = None,
    success: str | None = None,
) -> str:
    """Render the intake/edit form.

    Passing `business` (and optionally `config`) switches this into edit mode:
    the form pre-fills with that business's current values and posts to
    /onboarding/edit/{id} instead of /onboarding/create.

    `success` is trusted HTML (built server-side, not from user input) so links render;
    `error` is always escaped since it can echo back user-typed values.
    """
    edit_mode = business is not None
    config = config or BusinessConfig()

    error_html = f'<div class="banner error">{esc(error)}</div>' if error else ""
    success_html = f'<div class="banner success">{success}</div>' if success else ""

    action = f"/onboarding/edit/{business.id}" if edit_mode else "/onboarding/create"
    heading = f"Edit: {esc(business.name)}" if edit_mode else "New Client Intake"
    subheading = (
        f"Business id={business.id}. Changes apply immediately to live calls."
        if edit_mode
        else "Creates a Business + BusinessConfig row. Takes ~10 minutes per client."
    )
    submit_label = "Save changes" if edit_mode else "Create business"

    status_html = ""
    if edit_mode:
        status_label = "Active" if business.is_active else "Inactive"
        status_class = "active" if business.is_active else "inactive"
        status_html = f"""
            <div class="status-row">
                <span class="status-pill {status_class}">{status_label}</span>
                <span class="hint">
                    Subscription status: {esc(business.subscription_status or "none")}
                    (plan: {esc(business.plan or "none")}).
                    This normally flips automatically via the Stripe webhook when payment
                    completes or lapses — use the override below only to force it manually.
                </span>
            </div>
        """

    active_override_html = ""
    if edit_mode:
        active_override_html = f"""
            <div class="checkbox-row">
                <input type="checkbox" id="is_active" name="is_active" {_checked(business.is_active)}>
                <label for="is_active">Active (serving live calls)</label>
            </div>
        """

    required_fields_value = _joined(config.required_fields_json) or "caller_name, service_requested, preferred_time"
    services_value = _joined(config.services_json)
    staff_value = _joined(config.staff_json)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{heading}</title>
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
            .status-row {{ display: flex; align-items: baseline; gap: 10px; margin-bottom: 20px; }}
            .status-pill {{
                font-size: 12px; font-weight: 700; padding: 3px 10px; border-radius: 999px;
                text-transform: uppercase; letter-spacing: 0.03em;
            }}
            .status-pill.active {{ background: #e8f5e9; color: #2a7a3b; }}
            .status-pill.inactive {{ background: #fdecea; color: #a33; }}
        </style>
    </head>
    <body>
        <h1>{heading}</h1>
        <div class="sub">{subheading}</div>

        {status_html}
        {error_html}
        {success_html}

        <form method="post" action="{action}">
            <h2>Business basics</h2>

            <label>Business name</label>
            <input type="text" name="name" required placeholder="e.g. The Shaky Razor" value="{esc(business.name if business else '')}">

            <label>Twilio number</label>
            <input type="text" name="twilio_number" required placeholder="+15551234567" value="{esc(business.twilio_number if business else '')}">

            <label>Owner phone (receives lead notifications)</label>
            <input type="text" name="owner_phone" required placeholder="+15559876543" value="{esc(business.owner_phone if business else '')}">

            <label>Owner email (optional)</label>
            <input type="text" name="owner_email" value="{esc(business.owner_email if business else '')}">

            <label>Booking link (optional)</label>
            <input type="text" name="booking_link" value="{esc(business.booking_link if business else '')}">

            <label>Business type (optional)
                <div class="hint">e.g. barbershop, salon, dental — used to shape default prompt tone</div>
            </label>
            <input type="text" name="business_type" value="{esc(business.business_type if business else '')}">

            <label>Timezone
                <div class="hint">
                    IANA name, e.g. America/Denver, America/New_York, America/Los_Angeles.
                    This determines how the caller's "preferred time" gets interpreted — confirm
                    it explicitly with the client, don't leave the default.
                </div>
            </label>
            <input type="text" name="timezone" required value="{esc(business.timezone if business else '')}" placeholder="America/Denver">

            {active_override_html}

            <h2>Call script</h2>

            <label>Greeting</label>
            <textarea name="greeting" placeholder="Thanks for calling [Business], how can I help you today?">{esc(config.greeting)}</textarea>

            <label>Fallback message (used if something goes wrong mid-call)</label>
            <textarea name="fallback_message" placeholder="Sorry, something went wrong. Please call back in a few minutes.">{esc(config.fallback_message)}</textarea>

            <label>Completion message (said once all required info is collected)</label>
            <textarea name="completion_message" placeholder="Perfect, I've got everything I need. The shop will follow up with you soon.">{esc(config.completion_message)}</textarea>

            <label>Ask-for-name prompt</label>
            <input type="text" name="ask_name_prompt" value="{esc(config.ask_name_prompt or 'Can I get your name, please?')}">

            <label>Ask-for-service prompt</label>
            <input type="text" name="ask_service_prompt" value="{esc(config.ask_service_prompt or 'What service are you looking for today?')}">

            <label>Ask-for-time prompt</label>
            <input type="text" name="ask_time_prompt" value="{esc(config.ask_time_prompt or 'What specific time were you thinking?')}">

            <label>Ask-for-staff prompt</label>
            <input type="text" name="ask_staff_prompt" value="{esc(config.ask_staff_prompt or 'Do you have a preferred staff member?')}">

            <h2>Services &amp; staff</h2>

            <label>Services offered (comma-separated)
                <div class="hint">e.g. fade, haircut, lineup, beard trim</div>
            </label>
            <input type="text" name="services" placeholder="fade, haircut, lineup, beard trim" value="{esc(services_value)}">

            <label>Staff names (comma-separated, optional)</label>
            <input type="text" name="staff" placeholder="Tony, Marcus" value="{esc(staff_value)}">

            <label>Required fields before the call can complete (comma-separated)
                <div class="hint">Must be from: caller_name, service_requested, preferred_time, preferred_barber</div>
            </label>
            <input type="text" name="required_fields" value="{esc(required_fields_value)}">

            <h2>Notifications</h2>

            <div class="checkbox-row">
                <input type="checkbox" id="collect_notes" name="collect_notes" {_checked(config.collect_notes)}>
                <label for="collect_notes">Collect free-text notes from caller</label>
            </div>
            <div class="checkbox-row">
                <input type="checkbox" id="send_customer_sms" name="send_customer_sms" {_checked(config.send_customer_sms)}>
                <label for="send_customer_sms">Text the customer a booking link after the call</label>
            </div>
            <div class="checkbox-row">
                <input type="checkbox" id="send_owner_sms" name="send_owner_sms" {_checked(config.send_owner_sms)}>
                <label for="send_owner_sms">Text the owner a lead notification after the call</label>
            </div>

            <button type="submit">{submit_label}</button>
        </form>
    </body>
    </html>
    """


def _parse_required_fields(raw: str) -> list[str]:
    """Split, trim, and validate the required-fields input against the known field set."""
    fields = [f.strip() for f in raw.split(",") if f.strip()]
    unknown = [f for f in fields if f not in VALID_REQUIRED_FIELDS]
    if unknown:
        raise ValueError(f"Unknown required field(s): {', '.join(unknown)}")
    return fields


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
    timezone: str = Form(...),
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
    timezone = timezone.strip()

    if not name or not twilio_number or not owner_phone or not timezone:
        return HTMLResponse(
            content=render_form(error="Business name, Twilio number, owner phone, and timezone are required.")
        )

    existing = session.exec(
        select(Business).where(Business.twilio_number == twilio_number)
    ).first()
    if existing:
        return HTMLResponse(
            content=render_form(error=f"A business already uses Twilio number {twilio_number} (id={existing.id}).")
        )

    try:
        required_fields_list = _parse_required_fields(required_fields)
    except ValueError as e:
        return HTMLResponse(content=render_form(error=str(e)))

    business = Business(
        name=name,
        twilio_number=twilio_number,
        owner_phone=owner_phone,
        owner_email=owner_email.strip() or None,
        booking_link=booking_link.strip() or None,
        business_type=business_type.strip() or None,
        timezone=timezone,
        is_active=False,  # goes live once Stripe checkout completes (see /billing/webhook)
    )
    session.add(business)
    session.commit()
    session.refresh(business)

    services_list = [s.strip() for s in services.split(",") if s.strip()]
    staff_list = [s.strip() for s in staff.split(",") if s.strip()]

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
    basic_link = f"{base}/billing/checkout?business_id={business.id}&plan=basic"
    pro_link = f"{base}/billing/checkout?business_id={business.id}&plan=pro"
    edit_link = f"{base}/onboarding/edit/{business.id}"

    success_html = (
        f"Created '{esc(business.name)}' (business id={business.id}), currently <b>inactive</b> until they pay. "
        f"Send them one of these checkout links &mdash; the business auto-activates when payment completes:<br><br>"
        f"Basic: <a href=\"{basic_link}\">{basic_link}</a><br>"
        f"Pro: <a href=\"{pro_link}\">{pro_link}</a><br><br>"
        f"Also point their Twilio number's voice webhook at your /voice/incoming endpoint.<br><br>"
        f"To change services, staff, prompts, or required fields later: "
        f"<a href=\"{edit_link}\">{edit_link}</a>"
    )

    return HTMLResponse(content=render_form(success=success_html))


@router.get("/edit/{business_id}", response_class=HTMLResponse)
def edit_form(
    business_id: int,
    session: Session = Depends(get_session),
    _auth: None = Depends(require_dashboard_auth),
):
    """Render the intake form pre-filled with an existing business's current config."""
    business = session.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    config = session.exec(
        select(BusinessConfig).where(BusinessConfig.business_id == business_id)
    ).first()

    return HTMLResponse(content=render_form(business=business, config=config))


@router.post("/edit/{business_id}", response_class=HTMLResponse)
@limiter.limit("10/minute")
def update_business(
    request: Request,
    business_id: int,
    name: str = Form(...),
    twilio_number: str = Form(...),
    owner_phone: str = Form(...),
    owner_email: str = Form(""),
    booking_link: str = Form(""),
    business_type: str = Form(""),
    timezone: str = Form(...),
    is_active: bool = Form(False),
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
    """Update an existing Business + BusinessConfig row from submitted form data."""
    business = session.get(Business, business_id)
    if not business:
        raise HTTPException(status_code=404, detail="Business not found.")

    config = session.exec(
        select(BusinessConfig).where(BusinessConfig.business_id == business_id)
    ).first()
    if not config:
        # Shouldn't normally happen (create always writes one), but don't 500 on it.
        config = BusinessConfig(business_id=business_id)
        session.add(config)

    name = name.strip()
    twilio_number = twilio_number.strip()
    owner_phone = owner_phone.strip()
    timezone = timezone.strip()

    if not name or not twilio_number or not owner_phone or not timezone:
        return HTMLResponse(
            content=render_form(
                business=business,
                config=config,
                error="Business name, Twilio number, owner phone, and timezone are required.",
            )
        )

    # Twilio numbers must stay unique, but allow keeping this business's own number.
    conflict = session.exec(
        select(Business).where(
            Business.twilio_number == twilio_number, Business.id != business_id
        )
    ).first()
    if conflict:
        return HTMLResponse(
            content=render_form(
                business=business,
                config=config,
                error=f"Another business already uses Twilio number {twilio_number} (id={conflict.id}).",
            )
        )

    try:
        required_fields_list = _parse_required_fields(required_fields)
    except ValueError as e:
        return HTMLResponse(content=render_form(business=business, config=config, error=str(e)))

    business.name = name
    business.twilio_number = twilio_number
    business.owner_phone = owner_phone
    business.owner_email = owner_email.strip() or None
    business.booking_link = booking_link.strip() or None
    business.business_type = business_type.strip() or None
    business.timezone = timezone
    business.is_active = is_active

    services_list = [s.strip() for s in services.split(",") if s.strip()]
    staff_list = [s.strip() for s in staff.split(",") if s.strip()]

    config.greeting = greeting.strip() or None
    config.fallback_message = fallback_message.strip() or None
    config.completion_message = completion_message.strip() or None
    config.required_fields_json = json.dumps(required_fields_list)
    config.services_json = json.dumps(services_list)
    config.staff_json = json.dumps(staff_list)
    config.ask_name_prompt = ask_name_prompt.strip()
    config.ask_service_prompt = ask_service_prompt.strip()
    config.ask_time_prompt = ask_time_prompt.strip()
    config.ask_staff_prompt = ask_staff_prompt.strip()
    config.collect_notes = collect_notes
    config.send_customer_sms = send_customer_sms
    config.send_owner_sms = send_owner_sms

    session.add(business)
    session.add(config)
    session.commit()
    session.refresh(business)
    session.refresh(config)

    logger.info("Updated business id=%s name=%s is_active=%s", business.id, business.name, business.is_active)

    return HTMLResponse(
        content=render_form(
            business=business,
            config=config,
            success=f"Saved changes to '{esc(business.name)}'.",
        )
    )