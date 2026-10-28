"""A client of the SDK's local API: JSON-RPC 2.0 over a WebSocket on the
service's Unix socket (docs/spec/local-api.md in the SDK).

One connection, one application id. Responses resolve the matching call;
events (notifications with ``method == "event"``) go to the ``on_event``
callback in the order the service sent them.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import unix_connect

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]

# send_file carries the bytes base64 inside one request. The service raises
# its own inbound limit; ours only has to fit the files Offband allows.
MAX_FRAME = 8 * 1024 * 1024


class RpcError(Exception):
    """A JSON-RPC error. ``variant`` is the engine's ProtocolError name."""

    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", "local API error"))
        self.code: int = error.get("code", 0)
        self.variant: str | None = (error.get("data") or {}).get("variant")


class LocalApi:
    def __init__(self, socket_path: str, app_id: str, client_name: str = "offband") -> None:
        self.socket_path = socket_path
        self.app_id = app_id
        self.client_name = client_name
        self.local_address: str | None = None
        self.on_event: EventHandler | None = None
        self._ws: Any = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()

    async def connect(self, timeout: float = 60.0) -> dict[str, Any]:
        """Connects, retrying while the service is still starting, and says hello."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                self._ws = await unix_connect(self.socket_path, uri="ws://localhost/", max_size=MAX_FRAME)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.5)
        self.closed.clear()
        self._reader = asyncio.create_task(self._read())
        hello = await self.call("hello", {"app_id": self.app_id, "client": self.client_name})
        self.local_address = hello.get("local_address")
        return hello

    async def _read(self) -> None:
        reason = "connection closed"
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                if "id" in message:
                    waiter = self._pending.pop(message["id"], None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                elif message.get("method") == "event" and self.on_event is not None:
                    try:
                        result = self.on_event(message["params"])
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:  # one bad handler must not stop the stream
                        logger.exception("event handler failed for %s", message["params"].get("type"))
        except Exception as exc:
            reason = f"connection closed: {exc}"
        finally:
            for waiter in self._pending.values():
                if not waiter.done():
                    waiter.set_exception(ConnectionError(reason))
            self._pending.clear()
            self.closed.set()

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        if self._ws is None:
            raise ConnectionError("not connected")
        request_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        await self._ws.send(json.dumps(payload))
        response = await asyncio.wait_for(future, timeout)
        if "error" in response:
            raise RpcError(response["error"])
        return response["result"]

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
