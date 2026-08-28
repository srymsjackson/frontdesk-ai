"""FastAPI application entrypoint.

This module wires routes, loads settings, and ensures the database schema exists
when the app starts.
"""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from urllib.parse import quote
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.config import settings
from app.db import create_db_and_tables
from app.auth import NotAuthenticated
from app.rate_limit import limiter
from app.routes.voice import router as voice_router
from app.routes.dashboard import router as dashboard_router
from app.routes.demo import router as demo_router
from app.routes.onboarding import router as onboarding_router
from app.routes.billing import router as billing_router
from app.routes.auth_routes import router as auth_router

app = FastAPI(title=settings.app_name)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(NotAuthenticated)
def not_authenticated_handler(request, exc):
    """Dashboard/onboarding pages redirect to /login instead of a bare 401 —
    these are browser-facing HTML pages, not an API."""
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={quote(next_path, safe='')}", status_code=303)


@app.on_event("startup")
def on_startup() -> None:
    # Create tables if they do not exist yet.
    create_db_and_tables()


@app.get("/health")
def health():
    """Simple uptime check used by local/dev monitoring."""
    return {"status": "ok", "app": settings.app_name}


app.include_router(voice_router)
app.include_router(dashboard_router)
app.include_router(demo_router)
app.include_router(onboarding_router)
app.include_router(billing_router)
app.include_router(auth_router)