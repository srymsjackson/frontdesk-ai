"""Simple HTML dashboard for viewing leads. Protected by a signed session
cookie set at /login — see app/auth.py."""

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select, func
from datetime import datetime, timedelta
from app.db import get_session
from app.models import Lead, Business
from app.config import settings
from app.auth import require_dashboard_auth
import html

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def esc(value):
    """HTML-escape a value for safe inline rendering."""
    if value is None:
        return "&mdash;"
    return html.escape(str(value))


@router.get("/leads", response_class=HTMLResponse)
def leads_dashboard(
    session: Session = Depends(get_session),
    _auth: None = Depends(require_dashboard_auth),
):
    """Render a plain HTML page with lead stats and a table of recent leads."""
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = now - timedelta(days=7)

    total_count = session.exec(select(func.count(Lead.id))).one()
    today_count = session.exec(
        select(func.count(Lead.id)).where(Lead.created_at >= today_start)
    ).one()
    week_count = session.exec(
        select(func.count(Lead.id)).where(Lead.created_at >= week_start)
    ).one()

    leads = session.exec(
        select(Lead).order_by(Lead.created_at.desc()).limit(50)
    ).all()

    rows = ""
    for lead in leads:
        rows += f"""
        <tr>
            <td>{esc(lead.created_at.strftime('%Y-%m-%d %H:%M'))}</td>
            <td>{esc(lead.caller_name)}</td>
            <td>{esc(lead.phone_number)}</td>
            <td>{esc(lead.service_requested)}</td>
            <td>{esc(lead.preferred_time)}</td>
            <td>{esc(lead.preferred_barber)}</td>
            <td style="max-width:300px;font-size:13px;color:#555;">{esc(lead.notes)}</td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="7" style="text-align:center;color:#888;padding:24px;">No leads yet.</td></tr>'

    page = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{esc(settings.app_name)} — Leads</title>
        <style>
            body {{ font-family: -apple-system, system-ui, sans-serif; margin: 32px; color: #222; }}
            h1 {{ margin-bottom: 8px; }}
            .sub {{ color: #777; margin-bottom: 24px; }}
            .stats {{ display: flex; gap: 16px; margin-bottom: 24px; }}
            .stat {{ background: #f4f4f4; padding: 16px 20px; border-radius: 8px; flex: 1; }}
            .stat .label {{ font-size: 13px; color: #777; }}
            .stat .value {{ font-size: 28px; font-weight: 600; margin-top: 4px; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 14px; }}
            th {{ background: #fafafa; font-weight: 600; color: #555; }}
            tr:hover td {{ background: #fafbfc; }}
        </style>
    </head>
    <body>
        <h1>{esc(settings.app_name)}</h1>
        <div class="sub">Lead dashboard &middot; refreshed {esc(now.strftime('%Y-%m-%d %H:%M UTC'))}</div>

        <div class="stats">
            <div class="stat"><div class="label">Today</div><div class="value">{today_count}</div></div>
            <div class="stat"><div class="label">Last 7 days</div><div class="value">{week_count}</div></div>
            <div class="stat"><div class="label">All time</div><div class="value">{total_count}</div></div>
        </div>

        <table>
            <thead>
                <tr>
                    <th>When</th>
                    <th>Name</th>
                    <th>Phone</th>
                    <th>Service</th>
                    <th>Time</th>
                    <th>Barber</th>
                    <th>Notes</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
    </body>
    </html>
    """
    return HTMLResponse(content=page)