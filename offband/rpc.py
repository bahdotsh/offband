"""A client of the SDK's local API: JSON-RPC 2.0 over a WebSocket on the
service's Unix socket (docs/spec/local-api.md in the SDK).

One connection, one application id. Responses resolve the matching call;
events (notifications with ``method == "event"``) go to the ``on_event``
callback in the order the service sent them.

The reader task only routes frames. Events are handed to a separate
dispatcher task, so an event handler may itself call the API: its response
is read by the reader while the handler waits on it.
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

# The service's own inbound frame limit: its 100 MiB file limit as base64,
# plus 1 MiB. Our limit must be at least as large, or one large file from
# any peer closes this connection. Offband enforces its own, much smaller,
# file limit in code (see ``max_file_chars``).
try:
    from offline_protocol_sdk.local_api import MAX_MESSAGE_SIZE as MAX_FRAME
except ImportError:  # the tests run without the SDK
    MAX_FRAME = 100 * 1024 * 1024 * 4 // 3 + 1024 * 1024

# The event queue is unbounded (the reader must never block), so a backlog
# this deep is logged once each time it is reached.
QUEUE_WARN = 5000

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
        self._dispatcher: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[dict[str, Any] | None] | None = None
        # When set, a file_received event whose base64 data is longer than
        # this is queued without the data, with its length in
        # ``file_data_chars``: the handler refuses it by size, and the queue
        # never holds a payload that will be thrown away.
        self.max_file_chars: int | None = None
        # The same for a message_received event's content, its length kept
        # in ``content_chars``.
        self.max_content_chars: int | None = None
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
        self._events = asyncio.Queue()
        self._dispatcher = asyncio.create_task(self._dispatch(self._events))
        self._reader = asyncio.create_task(self._read(self._events))
        hello = await self.call("hello", {"app_id": self.app_id, "client": self.client_name})
        self.local_address = hello.get("local_address")
        return hello

    async def _read(self, events: asyncio.Queue[dict[str, Any] | None]) -> None:
        reason = "connection closed"
        try:
            async for raw in self._ws:
                # Parsed inline: the C decoder holds the GIL, so a thread
                # would not free the loop, and even the largest frame the
                # service allows parses in about a tenth of a second.
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError):
                    logger.warning("dropped a frame that is not JSON")
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    waiter = self._pending.pop(message["id"], None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(message)
                elif message.get("method") == "event" and isinstance(message.get("params"), dict):
                    events.put_nowait(self._shed(message["params"]))
                    if events.qsize() == QUEUE_WARN:
                        logger.warning("%d events wait for their handlers; a peer may be flooding", QUEUE_WARN)
        except Exception as exc:
            reason = f"connection closed: {exc}"
        finally:
            for waiter in self._pending.values():
                if not waiter.done():
                    waiter.set_exception(ConnectionError(reason))
            self._pending.clear()
            events.put_nowait(None)
            self.closed.set()

    def _shed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Drops a file payload over ``max_file_chars``, or message content
        over ``max_content_chars``, before the event is queued. The reader
        must never block (it also routes responses), so the queue is
        unbounded; shedding here keeps it from holding large payloads."""
        kind = event.get("type")
        if kind == "file_received":
            field, limit = "file_data", self.max_file_chars
        elif kind == "message_received":
            field, limit = "content", self.max_content_chars
        else:
            return event
        value = event.get(field)
        if limit is not None and isinstance(value, str) and len(value) > limit:
            event = {**event, field: None, f"{field}_chars": len(value)}
        return event

    async def _dispatch(self, events: asyncio.Queue[dict[str, Any] | None]) -> None:
        while True:
            event = await events.get()
            if event is None:
                return
            if self.on_event is None:
                continue
            try:
                result = self.on_event(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # one bad handler must not stop the stream
                logger.exception("event handler failed for %s", event.get("type"))

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        if self._ws is None or self.closed.is_set():
            raise ConnectionError("not connected")
        request_id = next(self._ids)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        try:
            await self._ws.send(json.dumps(payload))
            response = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            raise RpcError(error if isinstance(error, dict) else {"message": str(error)})
        if "result" not in response:
            raise RpcError({"message": f"the service answered {method} without a result"})
        return response["result"]

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        tasks = [t for t in (self._reader, self._dispatcher) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
