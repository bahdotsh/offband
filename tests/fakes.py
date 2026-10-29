"""A fake of the SDK's local API, and helpers that feed a node events the
way the service would. No service runs."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

from offband.node import Node

ME = "off1" + "m" * 40
ADA = "off1" + "a" * 40
EVE = "off1" + "e" * 40
BOB = "off1" + "b" * 40


class FakeApi:
    """Answers the calls the node makes with what a quiet service would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.closed = asyncio.Event()

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0) -> Any:
        self.calls.append((method, params))
        return {
            "get_establishment_state": "SessionConfirmed",
            "send_message": f"msg-{len(self.calls)}",
            "list_groups": [],
            "get_topology": {"nodes": []},
        }.get(method)


def envelope(kind: str, body: Any, name: str = "Ada") -> str:
    return json.dumps({"ob": 1, "kind": kind, "from": name, "body": body})


async def receive(node: Node, sender: str, content: str, message_id: str | None = None) -> None:
    await node._on_event({"type": "message_received", "sender": sender, "content": content,
                          "message_id": message_id or os.urandom(8).hex(), "transport": "wifi_direct"})


async def receive_file(node: Node, sender: str, name: str, data: bytes | str) -> None:
    encoded = data if isinstance(data, str) else base64.b64encode(data).decode()
    await node._on_event({"type": "file_received", "sender": sender, "file_name": name,
                          "file_data": encoded, "file_id": os.urandom(8).hex()})


def last(node: Node):
    return node.log[-1]
