import asyncio
import json
import logging
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.debug(f"WebSocket connected. Active connections: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        logger.debug(f"WebSocket disconnected. Active connections: {len(self.active)}")

    async def broadcast(self, msg_type: str, data: dict):
        msg = json.dumps({"type": msg_type, "data": data})
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)

    async def send_personal(self, ws: WebSocket, msg_type: str, data: dict):
        msg = json.dumps({"type": msg_type, "data": data})
        try:
            await ws.send_text(msg)
        except Exception as e:
            logger.debug(f"Failed to send personal message: {e}")
            self.disconnect(ws)


manager = ConnectionManager()
