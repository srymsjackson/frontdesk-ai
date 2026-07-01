"""
websocket_manager.py
---
In-memory WebSocket broadcast manager. Keeps track of all connected
clients (demo page viewers) and fans out events as they happen:
  - call_started  → when Twilio hits /voice/incoming
  - new_lead      → when a lead is committed to DB
  - call_ended    → when Twilio posts a status callback

Single instance via module-level `manager`. Railway runs one process,
so in-memory is fine. If you ever scale horizontally, swap this for a
Redis pub/sub broadcast.
"""

import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(f"[ws] client connected — {len(self._connections)} total")

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(f"[ws] client disconnected — {len(self._connections)} remaining")

    async def broadcast(self, payload: dict) -> None:
        """Fan out a JSON payload to every connected demo viewer."""
        if not self._connections:
            return

        message = json.dumps(payload)

        # Snapshot so we can mutate the set while iterating
        async with self._lock:
            connections = set(self._connections)

        dead: Set[WebSocket] = set()
        for ws in connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                self._connections -= dead
            logger.info(f"[ws] pruned {len(dead)} dead connection(s)")


# Module-level singleton — import this everywhere
manager = ConnectionManager()
