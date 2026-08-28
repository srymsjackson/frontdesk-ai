"""Login form that exchanges the shared DASHBOARD_KEY for a signed session
cookie, so the key is only ever submitted once via POST body — never
embedded in a URL, logged, cached in browser history, or leaked via Referer.
"""

import html
from fastapi import APIRouter, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import key_matches, set_session_cookie, clear_session_cookie

router = APIRouter(tags=["auth"])


def esc(value):
    if value is None:
        return ""
    return html.escape(str(value))


def render_login(error: str | None = None, next_path: str = "/dashboard/leads") -> str:
    error_html = f'<div class="banner error">{esc(error)}</div>' if error else ""
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Sign in</title>
        <style>
            body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 80px 24px;
                    display: flex; justify-content: center; color: #222; }}
            .card {{ width: 100%; max-width: 360px; }}
            h1 {{ font-size: 20px; margin-bottom: 20px; }}
            label {{ display: block; font-size: 13px; font-weight: 600; color: #444; margin-bottom: 6px; }}
            input[type=password] {{
                width: 100%; padding: 10px 12px; font-size: 14px; border: 1px solid #ccc;
                border-radius: 6px; box-sizing: border-box; font-family: inherit;
            }}
            button {{
                margin-top: 16px; width: 100%; padding: 10px 20px; font-size: 14px; font-weight: 600;
                background: #222; color: #fff; border: none; border-radius: 6px; cursor: pointer;
            }}
            button:hover {{ background: #444; }}
            .banner {{ padding: 12px 16px; border-radius: 6px; margin-bottom: 20px; font-size: 14px; }}
            .banner.error {{ background: #fdecea; color: #a33; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>Sign in</h1>
            {error_html}
            <form method="post" action="/login">
                <input type="hidden" name="next" value="{esc(next_path)}">
                <label for="key">Access key</label>
                <input type="password" id="key" name="key" required autofocus>
                <button type="submit">Sign in</button>
            </form>
        </div>
    </body>
    </html>
    """


def _safe_next(next_path: str) -> str:
    """Only ever redirect to a same-site path, never an absolute/external URL,
    so /login?next=... can't be used as an open redirect."""
    if next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/dashboard/leads"


@router.get("/login", response_class=HTMLResponse)
def login_form(next: str = Query(default="/dashboard/leads")):
    return HTMLResponse(content=render_login(next_path=_safe_next(next)))


@router.post("/login", response_class=HTMLResponse)
def login_submit(key: str = Form(...), next: str = Form(default="/dashboard/leads")):
    safe_next = _safe_next(next)
    if not key_matches(key):
        return HTMLResponse(content=render_login(error="Incorrect key.", next_path=safe_next))

    response = RedirectResponse(url=safe_next, status_code=303)
    set_session_cookie(response)
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response
