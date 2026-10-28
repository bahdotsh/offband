"""The local API client against a small WebSocket server on a Unix socket."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from websockets.asyncio.server import unix_serve

from fakes import ME
from offband.rpc import LocalApi


async def test_an_event_handler_may_call_the_api():
    """With handlers on the reader task, this call would wait forever for a
    response only the blocked reader could read."""
    path = Path(tempfile.mkdtemp(prefix="ob-")) / "s.sock"

    async def service(ws):
        async for raw in ws:
            req = json.loads(raw)
            if req["method"] == "hello":
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"local_address": ME}}))
                await ws.send(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {"type": "ping"}}))
            else:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": "pong"}))

    async with unix_serve(service, str(path)):
        api = LocalApi(str(path), "offband")
        answers: list[str] = []

        async def on_event(_: dict) -> None:
            answers.append(await api.call("echo", timeout=5))

        api.on_event = on_event
        await api.connect(timeout=5)
        for _ in range(50):
            if answers:
                break
            await asyncio.sleep(0.05)
        assert answers == ["pong"]
        with pytest.raises(asyncio.TimeoutError):
            await api.call("hello", timeout=0)  # answered, but too late
        assert not api._pending
        await api.close()


def test_the_frame_limit_is_the_services_own():
    from offband.rpc import MAX_FRAME
    sdk = pytest.importorskip("offline_protocol_sdk.local_api")
    assert MAX_FRAME == sdk.MAX_MESSAGE_SIZE


def test_a_file_over_the_limit_is_shed_before_it_is_queued():
    api = LocalApi("/nowhere", "offband")
    api.max_file_chars = 8
    big = api._shed({"type": "file_received", "file_data": "A" * 12, "file_id": "f"})
    assert big["file_data"] is None and big["file_data_chars"] == 12
    small = {"type": "file_received", "file_data": "AAAA"}
    assert api._shed(small) is small
    other = {"type": "message_received", "file_data": "A" * 12}
    assert api._shed(other) is other


def test_message_content_over_the_limit_is_shed_before_it_is_queued():
    api = LocalApi("/nowhere", "offband")
    api.max_content_chars = 8
    big = api._shed({"type": "message_received", "content": "x" * 12})
    assert big["content"] is None and big["content_chars"] == 12
    small = {"type": "message_received", "content": "hi"}
    assert api._shed(small) is small
