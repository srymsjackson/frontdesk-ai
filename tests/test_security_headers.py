"""Security headers tests (Priority 5 of the hardening plan).

Confirms the CSP and related browser-hardening headers are actually present
on responses -- not just that the middleware is registered -- and that the
/demo page's inline <script> nonce genuinely matches what the CSP header
allows (a mismatch would either break the page or silently make the nonce
pointless).
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import engine
from sqlmodel import SQLModel


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


EXPECTED_HEADERS = {
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "X-Frame-Options",
    "Permissions-Policy",
}


@pytest.mark.parametrize("path", ["/demo", "/health", "/login"])
def test_security_headers_present_on_html_and_json_routes(client, path):
    resp = client.get(path)
    # httpx/Starlette Headers are case-insensitive containers, but comparing
    # against resp.headers.keys() directly compares exact-case strings (the
    # keys are normalized lowercase) -- use "in resp.headers" per header
    # instead, which goes through the case-insensitive __contains__.
    missing = {h for h in EXPECTED_HEADERS if h not in resp.headers}
    assert not missing, f"{path} is missing headers: {missing}"


def test_x_content_type_options_is_nosniff(client):
    resp = client.get("/demo")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_x_frame_options_is_deny(client):
    resp = client.get("/demo")
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_csp_frame_ancestors_is_none(client):
    resp = client.get("/demo")
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_csp_has_no_unsafe_inline_or_unsafe_eval_for_scripts(client):
    """The whole point of the nonce is that script-src doesn't fall back to
    'unsafe-inline' -- assert that directly so a future edit can't quietly
    reintroduce it."""
    csp = client.get("/demo").headers["Content-Security-Policy"]
    script_src = next(part for part in csp.split(";") if part.strip().startswith("script-src"))
    assert "unsafe-inline" not in script_src
    assert "unsafe-eval" not in script_src


def test_hsts_absent_in_non_production(client):
    """conftest.py doesn't set APP_ENV, so settings.app_env defaults to
    "development" -- HSTS must not be sent, since it would force HTTPS on a
    local dev server that's normally served over plain http."""
    resp = client.get("/demo")
    assert "Strict-Transport-Security" not in resp.headers


def test_hsts_present_when_app_env_is_production(monkeypatch):
    """Exercises the production branch directly against the middleware
    class, rather than the shared `client` fixture's already-imported app
    (settings.app_env is read once at import time via a module-level
    Settings() singleton, so flipping the env var after import wouldn't
    retroactively change the running app's behavior)."""
    from app import security_headers as sh_module

    monkeypatch.setattr(sh_module.settings, "app_env", "production")

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient as StarletteTestClient

    async def homepage(request):
        return PlainTextResponse("ok")

    test_app = Starlette(routes=[Route("/", homepage)])
    test_app.add_middleware(sh_module.SecurityHeadersMiddleware)

    resp = StarletteTestClient(test_app).get("/")
    assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"


def test_demo_script_nonce_matches_csp_header(client):
    """The nonce embedded in the <script> tag must be exactly the value the
    CSP header allows for this same response -- if these ever drift apart,
    the browser silently refuses to run the script and the /demo page's
    live-feed JS goes dead with no visible error."""
    resp = client.get("/demo")
    csp = resp.headers["Content-Security-Policy"]

    header_nonce_match = re.search(r"nonce-([A-Za-z0-9_-]+)", csp)
    assert header_nonce_match, f"No nonce found in CSP header: {csp}"
    header_nonce = header_nonce_match.group(1)

    body_nonce_match = re.search(r'<script nonce="([^"]+)"', resp.text)
    assert body_nonce_match, "No nonced <script> tag found in /demo response body"
    body_nonce = body_nonce_match.group(1)

    assert header_nonce == body_nonce


def test_nonce_is_different_on_each_request(client):
    """A per-request nonce that never changes provides no protection --
    confirm two separate requests get two different values."""
    resp_a = client.get("/demo")
    resp_b = client.get("/demo")

    nonce_a = re.search(r"nonce-([A-Za-z0-9_-]+)", resp_a.headers["Content-Security-Policy"]).group(1)
    nonce_b = re.search(r"nonce-([A-Za-z0-9_-]+)", resp_b.headers["Content-Security-Policy"]).group(1)

    assert nonce_a != nonce_b


def test_websocket_route_unaffected_by_security_headers_middleware(client):
    """BaseHTTPMiddleware should only intercept HTTP-scope requests; the
    live-feed WebSocket must keep working with the middleware installed."""
    with client.websocket_connect("/ws/leads") as ws:
        ws.send_text("ping")
        msg = ws.receive_text()
        assert msg == "pong"
