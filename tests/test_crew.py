"""Three real SDK services on this machine, each with an Offband node, linked
over loopback peer streams. Drives what changes past two agents: messages
to everyone and to one, the board the crew shares, files to everyone, and
an agent that turns up after the board exists."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from offband import APP_ID
from offband.node import Node
from offband.rpc import LocalApi
from offband.service import ServiceSpec

from test_two_nodes import _exists, free_port, until


@pytest.fixture
async def spawn(tmp_path: Path):
    """Starts nodes one by one. Each pair is dialled from one side only, the
    newer node dialling the older ones (see README, Known issues)."""
    procs, nodes, listens = [], [], []

    async def start(name: str) -> Node:
        listen = f"127.0.0.1:{free_port()}"
        spec = ServiceSpec(tmp_path / f"{name.lower()}-node", transport="lan", listen=listen, peers=list(listens))
        listens.append(listen)
        procs.append(spec.spawn())
        node = Node(LocalApi(str(spec.socket_path), APP_ID), name, tmp_path / name.lower())
        node.workspace.mkdir(parents=True, exist_ok=True)
        await node.start()
        nodes.append(node)
        return node

    try:
        yield start
    finally:
        for node in nodes:
            await node.api.close()
        for proc in procs:
            proc.terminate()
        for proc in procs:
            proc.wait(20)


async def test_three_agents_share_one_board(spawn):
    ada, grace = await spawn("Ada"), await spawn("Grace")
    await ada.list_peers(wait_seconds=40)

    # The lead's first board edit creates the crew with the agent it hears.
    await ada.board_update(tasks=[{"id": "engine", "owner": "Ada", "status": "todo"}], notes={"plan": "Connect Four"})
    assert await until(lambda: _board_has(grace, {"engine"}), timeout=40)

    # A third agent turns up later: it is invited and sees the same board.
    linus = await spawn("Linus")
    peers = await ada.list_peers(wait_seconds=40, min_agents=2)
    assert sorted(p["name"] for p in peers if p["is_agent"]) == ["Grace", "Linus"]
    assert await until(lambda: _board_has(linus, {"engine"}), timeout=60)
    assert sorted(ada.crew_view()["members"]) == ["Ada", "Grace", "Linus"]
    assert ada.crew == grace.crew == linus.crew

    # Concurrent edits from all three merge on every replica.
    await asyncio.gather(
        ada.board_update(tasks=[{"id": "engine", "status": "doing"}]),
        grace.board_update(tasks=[{"id": "page", "owner": "Grace", "status": "doing"}]),
        linus.board_update(tasks=[{"id": "ui", "owner": "Linus", "status": "doing"}]),
    )
    for node in (ada, grace, linus):
        board = await until(lambda: _board_has(node, {"engine", "page", "ui"}), timeout=40)
        assert {t["id"]: t["status"] for t in board["tasks"]} == {"engine": "doing", "page": "doing", "ui": "doing"}
        assert board["notes"]["plan"] == "Connect Four"

    # With several agents nearby, no peer means everyone.
    sent = await ada.send(None, "propose", "engine Ada, page Grace, ui Linus")
    assert sent["to"] == "everyone"
    assert sorted(d["to"] for d in sent["deliveries"]) == ["Grace", "Linus"]
    assert all(d["status"] == "delivered" for d in sent["deliveries"])
    for node in (grace, linus):
        got = await _inbox_until(node, "propose")
        assert got["from"] == "Ada" and got["to"] == "everyone"

    # A named peer gets a direct message; the others never see it.
    direct = await grace.send("Linus", "question", "Which element holds the board?")
    assert direct["status"] == "delivered" and direct["to"] == "Linus"
    got = await _inbox_until(linus, "question")
    assert got["from"] == "Grace" and got["to"] == "you"
    assert not any(m["kind"] == "question" for m in (await ada.inbox(wait_seconds=2))["messages"])

    # A file goes to every agent, at the same path.
    src = ada.workspace / "game" / "engine.js"
    src.parent.mkdir(parents=True)
    src.write_text("window.Engine = {};\n" * 100)
    result = await ada.send_file(None, "game/engine.js")
    assert result["path"] == "game/engine.js" and len(result["deliveries"]) == 2
    for node in (grace, linus):
        landed = node.workspace / "game" / "engine.js"
        await until(lambda: _exists(landed), timeout=30)
        assert landed.read_text() == src.read_text()

    # A game is between two: the opponent is named, and both agree who starts.
    assert ada.game_info()["peer"] is None
    assert len(ada.game_info()["agents"]) == 2
    a_vs_l, l_vs_a = ada.game_info("Linus"), linus.game_info("Ada")
    assert a_vs_l["peer"]["name"] == "Linus" and a_vs_l["first"] != l_vs_a["first"]
    moves: list[dict] = []
    linus.add_game_listener(moves.append)
    await ada.game_send({"type": "move", "col": 2, "seq": 1}, "Linus")
    await until(lambda: _truthy(moves), timeout=15)
    assert moves[0]["address"] == ada.address


async def _board_has(node: Node, ids: set[str]):
    board = await node.board_get()
    return board if ids <= {t["id"] for t in board["tasks"]} else None


async def _inbox_until(node: Node, kind: str, timeout: float = 15.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for message in (await node.inbox(wait_seconds=deadline - loop.time()))["messages"]:
            if message["kind"] == kind:
                return message
    raise AssertionError(f"no {kind} message in time")


async def _truthy(value) -> bool:
    return bool(value)
