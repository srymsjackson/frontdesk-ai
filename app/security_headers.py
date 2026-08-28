"""Security headers middleware: CSP and other browser-hardening headers.

Applied globally via ASGI middleware so every response gets baseline
protection without each route remembering to set headers individually.

The one route that renders an inline <script> block (GET /demo — a public,
unauthenticated page that displays caller-supplied text transcribed from live
phone calls) pulls a per-request nonce from request.state.csp_nonce and
embeds it in that <script nonce="..."> tag, so script-src can stay
'self' + nonce instead of 'unsafe-inline'. That distinction matters: the
existing HTML-escaping fix on /demo (see app/routes/demo.py) is what actually
prevents injected markup from executing, but a nonce'd CSP is real
defense-in-depth against a *future* escaping gap — 'unsafe-inline' would
quietly allow any injected inline script or event handler to run anyway,
making the CSP decorative rather than protective. Inline <style> blocks (used
on every HTML route, not just /demo) stay under 'unsafe-inline' — CSS
injection has a much smaller blast radius than script injection, and none of
these pages interpolate caller-supplied text into a <style> block, so nonce'ing
every route's stylesheet wasn't judged worth the added complexity here.
"""

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import settings

# Kept in one place so app/routes/demo.py can reuse the exact same policy
# pieces (style-src, font-src) instead of the two files drifting apart.
_CSP_STYLE_SRC = "'self' 'unsafe-inline' https://fonts.googleapis.com"
_CSP_FONT_SRC = "'self' https://fonts.gstatic.com"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # One nonce per request, available to any route via
        # request.state.csp_nonce — currently only /demo uses it.
        request.state.csp_nonce = secrets.token_urlsafe(16)

        response = await call_next(request)

        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{request.state.csp_nonce}'; "
            f"style-src {_CSP_STYLE_SRC}; "
            f"font-src {_CSP_FONT_SRC}; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Content-Security-Policy"] = csp

        # MIME-sniffing protection: browsers must trust the Content-Type we
        # send rather than guessing from content.
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Don't leak the full referring URL (which can include query strings
        # / paths with details) to third-party destinations; still send the
        # origin for same-site navigation so nothing else breaks.
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Belt-and-suspenders alongside frame-ancestors 'none' above — older
        # browsers that don't parse CSP frame-ancestors still respect this.
        response.headers["X-Frame-Options"] = "DENY"

        # This app doesn't use any of these browser APIs; explicitly deny
        # them so an XSS gap can't invoke them either.
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        # HSTS only in production: enforcing HTTPS-only against a local dev
        # server (often plain http://localhost) would break local testing.
        if settings.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response
