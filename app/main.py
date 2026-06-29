"""FastAPI application entrypoint.

This module wires routes, loads settings, and ensures the database schema exists
when the app starts.
"""

from fastapi import FastAPI
from app.config import settings
from app.db import create_db_and_tables
from app.routes.voice import router as voice_router
from app.routes.sms import router as sms_router
from app.routes.leads import router as leads_router
from app.routes.calls import router as calls_router
from app.routes.dashboard import router as dashboard_router

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
def on_startup() -> None:
    # Create tables if they do not exist yet.
    create_db_and_tables()


@app.get("/health")
def health():
    """Simple uptime check used by local/dev monitoring."""
    return {"status": "ok", "app": settings.app_name}


app.include_router(voice_router)
app.include_router(sms_router)
app.include_router(leads_router)
app.include_router(calls_router)
