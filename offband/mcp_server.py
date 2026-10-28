"""offband-mcp: the agent's tools, over MCP stdio.

A thin front over the daemon's loopback API. The daemon owns the SDK
connection, the message log and the radar, so restarting the agent loses
nothing and the radar keeps running.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer

INSTRUCTIONS = """\
Offband connects you to the other coding agents nearby, however many there
are, over Bluetooth, end-to-end encrypted (MLS), with no server in between.
A message goes to every agent unless you name one peer; then only that peer
can read it.

Typical flow: announce, then peers(wait_seconds=30) to find the others.
Plan on the board every agent shares (board_update), agree with propose/accept messages,
send the files you own with send_file, and wait for replies with
inbox(wait_seconds=...) instead of polling in a loop. Keep messages short:
they show on the radar screen as they fly between the machines.
Read the Offband agent protocol (OFFBAND.md in the workspace) for the roles,
message kinds and the game contract.
"""

server = MCPServer(name="offband", instructions=INSTRUCTIONS)


def _base() -> str:
    return os.environ.get("OFFBAND_URL", "http://127.0.0.1:7777").rstrip("/")


def _request(method: str, path: str, body: dict[str, Any] | None, timeout: float) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_base() + path, data=data, method=method)
    req.add_header("X-Offband", "1")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error")
        except ValueError:
            detail = exc.reason
        return {"error": detail}
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        return {"error": f"The Offband daemon at {_base()} is not reachable ({exc}). Start it with `offband up`."}


async def _call(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
    return await asyncio.to_thread(_request, method, path, body, timeout)


@server.tool()
async def peers(wait_seconds: float = 0, min_agents: int = 1) -> dict[str, Any]:
    """List nearby devices and agents: name, address, transport (BLE or LAN),
    signal strength, and whether the encrypted session is up
    (session == "SessionConfirmed"), plus the crew that shares the board.
    With wait_seconds > 0, waits until at least min_agents agents are ready."""
    wait = max(0.0, min(float(wait_seconds), 120.0))
    count = max(1, int(min_agents))
    return await _call("GET", f"/api/peers?wait={wait}&min_agents={count}", timeout=wait + 15)


@server.tool()
async def send(kind: str, body: Any, peer: str | None = None) -> dict[str, Any]:
    """Send an end-to-end encrypted message to a nearby agent.

    kind: propose | accept | counter | task | review | question | chat | done
    body: a short string, or a small JSON object (max 16 KiB). Code goes in
          files, with send_file, never in a message.
    peer: one agent's name or address for a direct message; omit (or "all")
          to send to every agent nearby.

    Waits a few seconds for each acknowledgement and reports status
    "delivered" with the transport and latency, or "sent" when the SDK is
    still retrying (it keeps retrying; no need to resend). Sent to several
    agents, the result lists one delivery per agent."""
    return await _call("POST", "/api/send", {"peer": peer, "kind": kind, "body": body})


@server.tool()
async def inbox(wait_seconds: float = 60) -> dict[str, Any]:
    """New messages from other agents since the last call (each returned
    once). Blocks up to wait_seconds for the first one, so call it instead of
    sleeping. Each message says who it is from and whether it went to
    "everyone" or only to "you". Also reports board_changed when another
    agent edited the board. File transfers appear as kind "file" with the
    path they were saved to."""
    wait = max(0.0, min(float(wait_seconds), 300.0))
    return await _call("GET", f"/api/inbox?wait={wait}", timeout=wait + 15)


@server.tool()
async def board_get() -> dict[str, Any]:
    """Read the task board every agent in the crew shares: notes (plan,
    contract) and tasks (id, title, owner, status, files). Each agent holds a
    replica and edits merge over the link."""
    return await _call("GET", "/api/board")


@server.tool()
async def board_update(
    tasks: list[dict[str, Any]] | None = None,
    notes: dict[str, str] | None = None,
    remove: list[str] | None = None,
) -> dict[str, Any]:
    """Edit the shared board. tasks: a list of task patches merged by id,
    e.g. [{"id": "engine", "title": "Game rules", "owner": "Ada",
    "status": "doing", "files": ["game/engine.js"]}]. Status is one of
    todo | doing | review | done. notes: named text blocks such as "plan" or
    "contract" (whole value replaced). remove: task ids to delete.
    Edit only tasks you own, except to claim one the plan gave you.
    The first edit creates the crew: every agent nearby joins the board, and
    agents that turn up later join it by themselves."""
    return await _call("POST", "/api/board", {"tasks": tasks, "notes": notes, "remove": remove})


@server.tool()
async def send_file(path: str, peer: str | None = None) -> dict[str, Any]:
    """Send one file (max 256 KiB), encrypted, to every agent nearby, or to
    the one peer named. A path relative to the workspace keeps its place:
    each agent receives it at the same relative path in its workspace. Only
    files inside the workspace can be sent, never dot files such as .env or
    .claude/, nor CLAUDE.md or OFFBAND.md. Send only files you own."""
    return await _call("POST", "/api/send_file", {"peer": peer, "path": path}, timeout=60)


@server.tool()
async def announce(capabilities: dict[str, str] | None = None) -> dict[str, Any]:
    """Advertise this agent to nearby devices, with optional capabilities
    such as {"role": "lead", "skills": "game logic, UI"}. These are visible
    on the air in plaintext (signed, not encrypted): names and skills only,
    never content."""
    return await _call("POST", "/api/announce", {"capabilities": capabilities or {}})


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
