"""Two real SDK services on this machine, linked over loopback peer streams,
each with an Offband node. Drives the exchange the agents use: discovery,
hello, propose and accept, the shared board, a file, and game moves."""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from offband import APP_ID
from offband.node import Node
from offband.rpc import LocalApi
from offband.service import ServiceSpec


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(predicate, timeout: float = 30.0, every: float = 0.2):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(every)


@pytest.fixture
async def pair(tmp_path: Path):
    la, lb = f"127.0.0.1:{free_port()}", f"127.0.0.1:{free_port()}"
    specs = [
        ServiceSpec(tmp_path / "a-node", transport="lan", listen=la, peers=[lb]),
        # One side dials. When both dial at once the SDK can lose the first
        # key exchange on the superseded stream (see README, Known issues).
        ServiceSpec(tmp_path / "b-node", transport="lan", listen=lb, peers=[]),
    ]
    procs = [spec.spawn() for spec in specs]
    nodes = [
        Node(LocalApi(str(specs[0].socket_path), APP_ID), "Ada", tmp_path / "ada"),
        Node(LocalApi(str(specs[1].socket_path), APP_ID), "Grace", tmp_path / "grace"),
    ]
    try:
        for node in nodes:
            node.workspace.mkdir(parents=True, exist_ok=True)
        await asyncio.gather(*(node.start() for node in nodes))
        yield nodes
    finally:
        for node in nodes:
            await node.api.close()
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(20)


async def test_agents_find_each_other_and_collaborate(pair):
    ada, grace = pair

    # Discovery and the hello exchange: each side learns the other's name.
    peers = await ada.list_peers(wait_seconds=40)
    partner = next(p for p in peers if p["is_agent"])
    assert partner["name"] == "Grace"
    assert partner["session"] == "SessionConfirmed"
    assert partner["transport"] == "LAN"
    await until(lambda: _named(grace, "Ada"))
    first = await grace.inbox(wait_seconds=5)
    assert any(m["kind"] == "hello" and m["from"] == "Ada" for m in first["messages"])

    # Propose and accept, by name and by default partner.
    sent = await ada.send("Grace", "propose", {"split": "Ada: engine, Grace: UI"})
    assert sent["status"] == "delivered" and sent["transport"] == "LAN"
    got = await grace.inbox(wait_seconds=10)
    assert [m["kind"] for m in got["messages"]] == ["propose"]
    assert got["messages"][0]["body"] == {"split": "Ada: engine, Grace: UI"}
    assert got["messages"][0]["from"] == "Ada"
    await grace.send(None, "accept", "deal")
    back = await ada.inbox(wait_seconds=10)
    assert any(m["kind"] == "accept" and m["body"] == "deal" for m in back["messages"])

    # The outbound entry shows what crossed the air: ciphertext, not the plan.
    out = next(e for e in ada.log if e.kind == "propose")
    assert out.cipher and "engine" not in out.cipher

    # The board: concurrent edits to different tasks merge on both replicas.
    await asyncio.gather(
        ada.board_update(tasks=[{"id": "engine", "title": "Rules", "owner": "Ada", "status": "doing"}],
                         notes={"plan": "Connect Four"}),
        grace.board_update(tasks=[{"id": "ui", "title": "Board UI", "owner": "Grace", "status": "doing"}]),
    )

    async def merged(node):
        board = await node.board_get()
        ids = sorted(t["id"] for t in board["tasks"])
        return board if ids == ["engine", "ui"] and board["notes"].get("plan") == "Connect Four" else None

    board_a = await until(lambda: merged(ada), timeout=40)
    board_g = await until(lambda: merged(grace), timeout=40)
    assert board_a["tasks"] == board_g["tasks"]
    assert (await grace.inbox(wait_seconds=1))["board_changed"] is True

    # A file keeps its relative path in the partner's workspace.
    src = ada.workspace / "game" / "engine.js"
    src.parent.mkdir(parents=True)
    src.write_text("export const ROWS = 6;\n" * 200)
    result = await ada.send_file(None, "game/engine.js")
    assert result["path"] == "game/engine.js"
    landed = grace.workspace / "game" / "engine.js"
    await until(lambda: _exists(landed), timeout=30)
    assert landed.read_text() == src.read_text()
    files = await grace.inbox(wait_seconds=5)
    assert any(m["kind"] == "file" and m["body"]["path"] == "game/engine.js" for m in files["messages"])

    # Game moves reach the game listeners and stay out of the agent inbox.
    moves: list[dict] = []
    grace.add_game_listener(moves.append)
    await ada.game_send({"type": "move", "col": 3, "seq": 1})
    await until(lambda: _truthy(moves), timeout=15)
    assert moves[0]["payload"] == {"type": "move", "col": 3, "seq": 1}
    assert (await grace.inbox(wait_seconds=0))["messages"] == []

    # Both sides agree who moves first.
    assert ada.game_info()["first"] != grace.game_info()["first"]


async def test_daemon_api_and_guard(pair, monkeypatch):
    from offband import mcp_server
    from offband.daemon import serve

    ada, grace = pair
    port = free_port()
    web = asyncio.create_task(serve(ada, port))
    try:
        base = f"http://127.0.0.1:{port}"
        monkeypatch.setenv("OFFBAND_URL", base)

        async def up():
            try:
                return await asyncio.to_thread(_get, base + "/api/state")
            except OSError:
                return None

        await until(up, timeout=10)
        peers = await mcp_server.peers(wait_seconds=40)
        assert any(p["name"] == "Grace" for p in peers["peers"])
        sent = await mcp_server.send("chat", "hello from the tool", None)
        assert sent["status"] in ("delivered", "sent")
        assert (await mcp_server.send("bogus", "x", None))["error"].startswith("kind must be")

        # A POST without the header is refused, as is a foreign Host.
        req = urllib.request.Request(base + "/api/send", data=b"{}", method="POST")
        assert await asyncio.to_thread(_status, req) == 403
        req = urllib.request.Request(base + "/api/state", headers={"Host": "evil.example:80"})
        assert await asyncio.to_thread(_status, req) == 403

        # The radar page and the game bridge are served.
        assert "<html" in (await asyncio.to_thread(_get_text, base + "/")).lower()
        assert "Offband" in await asyncio.to_thread(_get_text, base + "/offband.js")
    finally:
        web.cancel()
        await asyncio.gather(web, return_exceptions=True)


async def _named(node: Node, name: str) -> bool:
    return any(p.name == name for p in node.peers.values())


async def _exists(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


async def _truthy(value) -> bool:
    return bool(value)


def _get(url: str):
    req = urllib.request.Request(url, headers={"X-Offband": "1"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _status(req: urllib.request.Request) -> int:
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode()
