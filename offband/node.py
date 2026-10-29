"""The Offband node: everything one agent's laptop knows and does.

It is one client of the SDK's local API under the application id
``offband``. It keeps the peers it can hear, a log of every message in and
out (which feeds the agent's inbox and the radar page), the shared task
board, received files, and the game channel.

Every message between agents is one JSON envelope inside an ordinary 1:1
SDK message, so it travels over the MLS session the engine sets up with the
peer. A message to everyone is one such message per agent. Nothing here uses
service request bodies for content: those are signed plaintext, and services
are used for discovery only.

    {"ob": 1, "kind": "propose", "from": "Ada", "to": "all", "body": ...}

The agents nearby form a crew: one MLS group whose space holds the shared
board, so every member edits the same replicated document however many
there are. The agent with the lowest address creates it shortly after it
hears another agent, unless an agent it hears already reports a crew; after
that the member with the lowest address among those nearby invites any
agent that turns up. Two agents that each thought they were lowest (each had
heard only part of the room) make two crews; every node then picks the same
one, and moves what it wrote into it.

Everything that arrives from a peer is untrusted: a peer may run any code
on top of the SDK. Envelopes are validated, inbound files are capped, files
that would land on the agent's own instructions or settings are
quarantined, and a name a peer claims never replaces the one already known
for its address. ``allow`` restricts the node to a fixed set of addresses.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import itertools
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import APP_ID, files
from .files import clean_relative, protected_path
from .rpc import LocalApi, RpcError

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = 1
SERVICE_ID = "offband-agent"
BOARD_DOC = "board"
CREW_NAME = "offband-crew"
EVERYONE = ("all", "everyone", "crew")

# What an agent may say. "hello" and "game" are the node's own; the rest are
# the agent protocol (see offband/agent/OFFBAND.md).
AGENT_KINDS = ("propose", "accept", "counter", "task", "review", "done", "chat", "question")
# What a peer may put in an envelope's "kind". "file" is not one: file
# entries come only from the SDK's own file transfer events.
INBOUND_KINDS = AGENT_KINDS + ("hello", "game")

# BLE is slow and the local API caps a request near the frame limit. Source
# files of tens of KiB are the intended size. The same limit applies to
# files that arrive: anything larger is refused, not written.
MAX_FILE_BYTES = 256 * 1024
MAX_FILE_CHARS = (MAX_FILE_BYTES + 2) // 3 * 4  # the same limit, as base64
MAX_BODY_CHARS = 16 * 1024
MAX_INBOUND_CHARS = 4 * MAX_BODY_CHARS  # the whole envelope, as received
MAX_NAME_CHARS = 40
# Deeper bodies are refused: the radar, the inbox and asdict all recurse.
MAX_BODY_DEPTH = 32
HELLO_SESSION_WAIT = 4.0
CREW_WAIT = 20.0
CREW_SETTLE = 2.5  # lets the room fill in before the creator picks it
LOG_LIMIT = 2000
SEEN_LIMIT = 4096
# With --allow, how long a crew check stays good before the next use of the
# crew asks the service again. Group events clear it at once.
CREW_CHECK_TTL = 2.0



class _Recent:
    """A set that forgets its oldest members past ``limit``."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._items: dict[str, None] = {}

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def add(self, item: str) -> None:
        self._items[item] = None
        while len(self._items) > self.limit:
            self._items.pop(next(iter(self._items)))


def _not_json(token: str) -> Any:
    raise ValueError(f"{token} is not JSON")


def peer_json(text: str) -> Any:
    """Parses JSON a peer wrote. Python's parser also takes NaN and
    Infinity, which no JSON encoder downstream accepts: the API would answer
    500 and the radar could not parse its state. They are refused here."""
    return json.loads(text, parse_constant=_not_json)


def nesting_depth(value: Any) -> int:
    """How deeply lists and objects nest in ``value``, without recursion."""
    deepest, stack = 0, [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            item = list(item.values())
        if isinstance(item, list):
            deepest = max(deepest, depth)
            stack.extend((child, depth + 1) for child in item)
    return deepest


def short(address: str | None) -> str:
    if not address:
        return "?"
    return f"{address[:8]}…{address[-4:]}" if len(address) > 14 else address


def transport_label(raw: str | None) -> str | None:
    """The engine spells transports two ways (docs/spec/local-api.md)."""
    if not raw:
        return None
    key = raw.lower()
    if key == "ble":
        return "BLE"
    if key in ("wifidirect", "wifi_direct"):
        return "LAN"
    if key == "none":
        return None
    return raw.upper() if len(raw) <= 4 else raw.capitalize()


@dataclass
class Peer:
    address: str
    name: str | None = None
    transport: str | None = None
    rssi: int | None = None
    nearby: bool = True
    is_agent: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    session: str | None = None
    last_seen: float = field(default_factory=time.time)
    crew: str | None = None  # the crew this agent said it is in
    hello_sent: bool = False

    def view(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("hello_sent")
        data["label"] = self.name or short(self.address)
        return data


@dataclass
class Entry:
    """One message in the log, in or out."""

    seq: int
    direction: str  # "in" | "out"
    peer: str
    kind: str
    body: Any
    ts: float
    message_id: str | None = None
    status: str = "received"  # out: queued | sent | delivered | failed
    transport: str | None = None
    latency_ms: int | None = None
    sender_name: str | None = None
    cipher: str | None = None  # what went over the air, as the SDK reported it
    audience: str | None = None  # "all" when it went to every agent

    def view(self) -> dict[str, Any]:
        return asdict(self)


class NodeError(Exception):
    """A request the node refuses, with a message written for the agent."""


class Node:
    def __init__(
        self,
        api: LocalApi,
        name: str,
        workspace: Path | None = None,
        files_dir: Path | None = None,
        allow: list[str] | None = None,
    ) -> None:
        self.api = api
        self.name = name
        self.workspace = workspace.resolve() if workspace else None
        # The quarantine for files a peer may not write into the workspace,
        # and the only destination when the node has no workspace.
        self.files_dir = (files_dir or (self.workspace / ".offband" / "received" if self.workspace else None))
        # With an allowlist, every event from any other address is dropped.
        self.allow: frozenset[str] | None = frozenset(allow) if allow else None
        self.address: str | None = None
        self.peers: dict[str, Peer] = {}
        self.log: list[Entry] = []
        self.capabilities: dict[str, Any] = {}
        self._seq = itertools.count(1)
        self._inbox_cursor = 0
        self._seen_ids = _Recent(SEEN_LIMIT)
        self._by_message_id: dict[str, Entry] = {}
        self._file_entries: dict[str, Entry] = {}
        self._ciphers: dict[str, str] = {}
        self._introduced: set[str] = set()
        self._board_dirty = False
        self._changed = asyncio.Condition()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._game_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self.link_transport: str | None = None
        self.crew: str | None = None  # the crew's group id
        self.crew_members: list[str] = []
        self._crew_lock = asyncio.Lock()
        self._left_groups: set[str] = set()
        self._crew_checked: float | None = None  # loop time of the last crew load
        self._name_conflicts: set[str] = set()  # addresses already warned about

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        self.api.on_event = self._on_event
        # Larger files are refused anyway: let the reader drop their bytes.
        self.api.max_file_chars = MAX_FILE_CHARS
        self.api.max_content_chars = MAX_INBOUND_CHARS
        hello = await self.api.connect()
        self.address = hello.get("local_address")
        await self.api.call("subscribe", {"types": "all"})
        await self.announce({})
        await self._snapshot_neighbors()
        await self._load_crew()

    async def _snapshot_neighbors(self) -> None:
        """Peers the engine already knows about before our events started."""
        try:
            topology = await self.api.call("get_topology")
        except RpcError:
            return
        for node in (topology or {}).get("nodes", []):
            if not isinstance(node, dict):
                continue
            address = node.get("id") or node.get("peer_id") or node.get("address")
            if (isinstance(address, str) and address.startswith("off1") and address != self.address
                    and self.admitted(address)):
                self._touch_peer(address, None)

    def admitted(self, address: str) -> bool:
        return self.allow is None or address in self.allow

    async def announce(self, capabilities: dict[str, Any]) -> dict[str, Any]:
        """Registers this agent as a mesh service, for discovery only. The
        capabilities are signed plaintext on the air: names and skills,
        never content."""
        self.capabilities = {**self.capabilities, **capabilities}
        caps = {"name": self.name, "v": str(ENVELOPE_VERSION)}
        caps.update({k: str(v) for k, v in self.capabilities.items()})
        try:
            await self.api.call("services.unregister_service", {"service_id": SERVICE_ID})
        except RpcError:
            pass
        try:
            await self.api.call(
                "services.register_service", {"service_id": SERVICE_ID, "version": "1", "capabilities": caps}
            )
            await self.api.call("services.discover_services", {"service_id": SERVICE_ID})
        except RpcError as exc:
            logger.warning("service announce failed: %s", exc)
        # Peers already linked learn the new capabilities over the session.
        for peer in list(self.peers.values()):
            if peer.is_agent:
                await self._send_hello(peer.address, want_reply=False)
        return {"name": self.name, "address": self.address, "capabilities": caps}

    # ------------------------------------------------------------- listeners

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def add_game_listener(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._game_listeners.append(fn)
        return lambda: self._game_listeners.remove(fn)

    def _emit(self, event: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:
                logger.exception("radar listener failed")

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ peers

    def _touch_peer(self, address: str, transport: str | None, rssi: int | None = None) -> Peer:
        peer = self.peers.get(address)
        is_new = peer is None
        if peer is None:
            peer = self.peers[address] = Peer(address=address)
        peer.last_seen = time.time()
        peer.nearby = True
        if transport:
            peer.transport = transport
        if rssi is not None:
            peer.rssi = rssi
        if is_new:
            self._emit({"t": "peer", "peer": peer.view()})
        if not peer.hello_sent:
            peer.hello_sent = True
            self._spawn(self._send_hello(address, want_reply=True))
        return peer

    async def _send_hello(self, address: str, want_reply: bool) -> None:
        # Give the engine a moment to form the session itself, which it does
        # as soon as the two devices hear each other (usually under 1 s).
        # If it has not after a few seconds the key exchange was lost (seen
        # when both sides dial at once and one stream supersedes the other);
        # a queued message then makes the engine retry it, about 30 s later.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HELLO_SESSION_WAIT
        while loop.time() < deadline:
            try:
                state = await self.api.call("get_establishment_state", {"peer_id": address})
            except (RpcError, ConnectionError):
                state = None
            if state == "SessionConfirmed":
                break
            await asyncio.sleep(0.3)
        # The crew id tells a newcomer that a crew exists, so it waits for the
        # invitation instead of making a second one.
        body = {"name": self.name, "capabilities": self.capabilities, "want_reply": want_reply, "crew": self.crew}
        try:
            await self._post(address, "hello", body)
        except (RpcError, ConnectionError) as exc:
            logger.info("hello to %s not sent yet: %s", short(address), exc)
            peer = self.peers.get(address)
            if peer:
                peer.hello_sent = False

    async def list_peers(self, wait_seconds: float = 0.0, min_agents: int = 1) -> list[dict[str, Any]]:
        """Nearby peers, agents first. With ``wait_seconds``, waits until at
        least ``min_agents`` agents are known with their sessions confirmed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(wait_seconds, 600.0))
        while True:
            await self._refresh_sessions()
            ready = [p for p in self.peers.values() if p.is_agent and p.session == "SessionConfirmed"]
            remaining = deadline - loop.time()
            if len(ready) >= max(1, min_agents) or remaining <= 0:
                break
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), min(remaining, 1.0))
                except asyncio.TimeoutError:
                    pass
        peers = sorted(self.peers.values(), key=lambda p: (not p.is_agent, not p.nearby, p.address))
        return [p.view() for p in peers]

    async def _refresh_sessions(self) -> None:
        for peer in self.peers.values():
            try:
                state = await self.api.call("get_establishment_state", {"peer_id": peer.address})
            except (RpcError, ConnectionError):
                continue
            if state != peer.session:
                peer.session = state
                self._emit({"t": "peer", "peer": peer.view()})

    def agents(self) -> list[Peer]:
        return sorted((p for p in self.peers.values() if p.is_agent), key=lambda p: p.address)

    def resolve_recipients(self, peer: str | None) -> list[str]:
        """Every agent for nothing or "all"; otherwise the one peer named."""
        if peer and peer.lower() not in EVERYONE + ("partner", "peer"):
            return [self.resolve_peer(peer)]
        agents = self.agents()
        if agents:
            return [p.address for p in agents]
        if len(self.peers) == 1:
            return [next(iter(self.peers))]
        raise NodeError("No agent is nearby yet. Call peers(wait_seconds=30) and try again.")

    def resolve_peer(self, peer: str | None) -> str:
        """An address from a name, an address, a prefix, or nothing (the one
        agent nearby)."""
        agents = self.agents()
        if not peer or peer in ("partner", "peer"):
            if len(agents) == 1:
                return agents[0].address
            if not agents and len(self.peers) == 1:
                return next(iter(self.peers))
            if not agents:
                raise NodeError("No agent is nearby yet. Call peers(wait_seconds=30) and try again.")
            names = ", ".join(p.name or p.address for p in agents)
            raise NodeError(f"Several agents are nearby ({names}); name one.")
        if peer.startswith("off1") and peer in self.peers:
            return peer
        lowered = peer.lower()
        named = [p.address for p in self.peers.values() if p.name and p.name.lower() == lowered]
        if len(named) == 1:
            return named[0]
        if len(named) > 1:
            # Names are what peers claim, not who they are: never guess.
            raise NodeError(f"Several peers call themselves {peer!r} ({', '.join(named)}); give the address.")
        matches = [a for a in self.peers if a.startswith(peer)]
        if len(matches) == 1:
            return matches[0]
        if peer.startswith("off1"):
            if not self.admitted(peer):
                raise NodeError(f"{short(peer)} is not on this node's --allow list.")
            return peer  # not heard yet; the engine holds the message until it is
        raise NodeError(f"No nearby peer called {peer!r}.")

    def peer_name(self, address: str) -> str:
        peer = self.peers.get(address)
        return (peer.name if peer and peer.name else None) or short(address)

    # --------------------------------------------------------------- messages

    async def _post(self, address: str, kind: str, body: Any, audience: str | None = None) -> str:
        """Sends one envelope and returns the SDK's message id."""
        envelope = {"ob": ENVELOPE_VERSION, "kind": kind, "from": self.name, "body": body}
        if audience:
            envelope["to"] = audience
        content = json.dumps(envelope)
        return await self.api.call(
            "send_message", {"recipient": address, "content": content, "priority": "High" if kind == "game" else "Medium"}
        )

    async def _send_envelope(self, address: str, kind: str, body: Any, audience: str | None = None) -> Entry:
        """Sends one envelope and logs it."""
        message_id = await self._post(address, kind, body, audience)
        entry = Entry(
            seq=next(self._seq), direction="out", peer=address, kind=kind, body=body, ts=time.time(),
            message_id=message_id, status="sent", sender_name=self.name,
            cipher=self._ciphers.pop(message_id, None), audience=audience,
        )
        self._append(entry)
        self._by_message_id[message_id] = entry
        return entry

    async def send(self, peer: str | None, kind: str, body: Any, wait_delivery: float = 5.0) -> dict[str, Any]:
        if kind not in AGENT_KINDS:
            raise NodeError(f"kind must be one of {', '.join(AGENT_KINDS)}")
        if isinstance(body, str) and len(body) > MAX_BODY_CHARS:
            raise NodeError(f"Message too long ({len(body)} chars, max {MAX_BODY_CHARS}). Send code with send_file.")
        if not isinstance(body, str) and len(json.dumps(body)) > MAX_BODY_CHARS:
            raise NodeError(f"Message too long (max {MAX_BODY_CHARS} chars as JSON). Send code with send_file.")
        addresses = self.resolve_recipients(peer)
        audience = "all" if len(addresses) > 1 else None
        entries = []
        for address in addresses:
            entries.append(await self._send_envelope(address, kind, body, audience=audience))
        await asyncio.gather(*(self._await_status(e, wait_delivery) for e in entries))
        return self._outcomes(entries)

    def _outcomes(self, entries: list[Entry]) -> dict[str, Any]:
        """One recipient reads as before; several as one result per agent."""
        if len(entries) == 1:
            return self._outcome(entries[0])
        deliveries = []
        for entry in entries:
            result = self._outcome(entry)
            result.pop("kind", None)
            deliveries.append(result)
        return {"to": "everyone", "kind": entries[0].kind, "deliveries": deliveries}

    async def _await_status(self, entry: Entry, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while entry.status not in ("delivered", "failed"):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return

    def _outcome(self, entry: Entry) -> dict[str, Any]:
        result: dict[str, Any] = {
            "to": self.peer_name(entry.peer),
            "kind": entry.kind,
            "message_id": entry.message_id,
            "status": entry.status,
        }
        if entry.status == "delivered":
            result["transport"] = entry.transport
            result["latency_ms"] = entry.latency_ms
        elif entry.status == "sent":
            result["note"] = "Encrypted and queued. The SDK retries until the peer acknowledges it."
        return result

    def _append(self, entry: Entry) -> None:
        self.log.append(entry)
        if len(self.log) > LOG_LIMIT:
            dropped = self.log[: len(self.log) - LOG_LIMIT]
            self.log = self.log[-LOG_LIMIT:]
            for old in dropped:
                if old.message_id:
                    self._by_message_id.pop(old.message_id, None)
                    self._file_entries.pop(old.message_id, None)
        self._emit({"t": "msg", "msg": self._radar_view(entry)})

    def _radar_view(self, entry: Entry) -> dict[str, Any]:
        """Never raises, whatever the body holds: state() renders every
        recent entry, so one bad entry would break the radar for good."""
        view = entry.view()
        view["peer_name"] = self.peer_name(entry.peer)
        body = entry.body
        try:
            text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)
        except (TypeError, ValueError, RecursionError):
            text = "[a message the radar cannot show]"
        view["summary"] = text if len(text) <= 240 else text[:237] + "..."
        if entry.kind == "file" and isinstance(body, dict):
            view.pop("body", None)
            view["summary"] = f"{body.get('path')} ({body.get('bytes')} bytes)"
            if body.get("note"):
                view["summary"] += f", {body['note']}"
        return view

    async def inbox(self, wait_seconds: float = 0.0, peek: bool = False) -> dict[str, Any]:
        """New inbound agent messages since the last call. Blocks up to
        ``wait_seconds`` for the first one."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(wait_seconds, 600.0))
        while True:
            fresh = [
                e for e in self.log
                if e.seq > self._inbox_cursor and e.direction == "in" and e.kind != "game"
            ]
            remaining = deadline - loop.time()
            if fresh or self._board_dirty or remaining <= 0:
                break
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except asyncio.TimeoutError:
                    pass
        board_changed = self._board_dirty
        if not peek:
            if fresh:
                self._inbox_cursor = max(e.seq for e in fresh)
            else:
                self._inbox_cursor = max([self._inbox_cursor] + [e.seq for e in self.log])
            self._board_dirty = False
        messages = []
        for e in fresh:
            item = {
                "from": e.sender_name or self.peer_name(e.peer),
                "address": e.peer,  # who sent it; the name is only what it claims
                "to": "everyone" if e.audience == "all" else "you",
                "kind": e.kind,
                "body": e.body,
                "transport": e.transport,
                "at": time.strftime("%H:%M:%S", time.localtime(e.ts)),
            }
            messages.append(item)
        return {"messages": messages, "board_changed": board_changed}

    # ------------------------------------------------------------------ files

    async def send_file(self, peer: str | None, path: str) -> dict[str, Any]:
        addresses = self.resolve_recipients(peer)
        source = Path(path)
        if not source.is_absolute():
            if self.workspace is None:
                raise NodeError("Give an absolute path; this node has no workspace.")
            source = self.workspace / source
        source = source.resolve()
        if self.workspace is not None:
            # Only files of the workspace, and never its dot files or the
            # agent's own instructions: a page or prompt that talks the agent
            # into sending ~/.ssh/... or .env gets a refusal.
            try:
                inside = source.relative_to(self.workspace)
            except ValueError:
                raise NodeError(f"send_file sends only files inside the workspace ({self.workspace}).") from None
            blocked = protected_path(list(inside.parts))
            if blocked is not None:
                raise NodeError(f"{inside.as_posix()} is not sent: {blocked!r} is private to this agent.")
        if not source.is_file():
            raise NodeError(f"No such file: {source}")
        data = source.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise NodeError(f"{source.name} is {len(data)} bytes; the limit is {MAX_FILE_BYTES}. Send source, not assets.")
        relative = self._relative(source)
        encoded = base64.b64encode(data).decode()
        entries = []
        for address in addresses:
            file_id = await self.api.call(
                "send_file", {"recipient": address, "file_data": encoded, "file_name": quote(relative, safe="")}
            )
            entry = Entry(
                seq=next(self._seq), direction="out", peer=address, kind="file",
                body={"path": relative, "bytes": len(data)}, ts=time.time(), message_id=file_id,
                status="sent", sender_name=self.name, audience="all" if len(addresses) > 1 else None,
            )
            self._append(entry)
            self._file_entries[file_id] = entry
            entries.append(entry)
        await asyncio.gather(*(self._await_status(e, 8.0) for e in entries))
        result = self._outcomes(entries)
        result["path"] = relative
        result["bytes"] = len(data)
        return result

    def _relative(self, source: Path) -> str:
        if self.workspace is not None:
            try:
                return source.relative_to(self.workspace).as_posix()
            except ValueError:
                pass
        return source.name

    # ------------------------------------------------------------------- crew

    async def _load_crew(self) -> None:
        """Adopts the crew group this node belongs to. When there are two,
        every node picks the same one: the most members, then the lowest
        group id (creation times are local to each node)."""
        try:
            group_ids = await self.api.call("list_groups")
        except (RpcError, ConnectionError):
            return
        best: tuple[Any, ...] | None = None
        refused: set[str] = set()
        mine: set[str] = set()  # crews that list this node
        unsure: set[str] = set()  # groups whose info could not be read
        for group_id in group_ids or []:
            gid = group_id if isinstance(group_id, str) else (group_id or {}).get("group_id")
            if not gid:
                continue
            try:
                info = await self.api.call("get_group_info", {"group_id": gid})
            except (RpcError, ConnectionError):
                unsure.add(gid)
                continue
            if not info or info.get("name") != CREW_NAME or self.address not in (info.get("members") or []):
                continue
            mine.add(gid)
            members = list(info.get("members") or [])
            if not all(m == self.address or self.admitted(m) for m in members):
                refused.add(gid)  # made or joined by a peer outside the allowlist
                continue
            key = (-len(members), gid, members)
            if best is None or key[:2] < best[:2]:
                best = key
        # A leave is remembered only while the group still lists this node
        # (it may, a while after the leave). Once it does not, or once the
        # crew is clean, the next outsider or re-invitation is left again.
        self._left_groups = (self._left_groups & (mine | unsure)) - (mine - refused)
        self._crew_checked = asyncio.get_running_loop().time()
        for gid in refused - self._left_groups:
            # A crew that took in a peer outside the allowlist (another member
            # invited it): leave the MLS group, so this node neither writes
            # the board where the outsider reads nor stays on its roster.
            # Marked before the call, so a concurrent load does not leave twice.
            logger.warning("crew %s holds a peer outside --allow; leaving it", gid)
            self._left_groups.add(gid)
            try:
                await self.api.call("leave_group", {"group_id": gid})
            except (RpcError, ConnectionError) as exc:
                self._left_groups.discard(gid)
                logger.warning("could not leave crew %s: %s", gid, exc)
        if self.crew in refused:
            self.crew, self.crew_members = None, []
            self._emit({"t": "crew", "crew": None})
            await self._notify()
        if best is None:
            return
        _, gid, members = best
        changed = gid != self.crew or members != self.crew_members
        previous = self.crew
        self.crew, self.crew_members = gid, members
        if previous is not None and previous != gid:
            self._spawn(self._move_board(previous, gid))
        elif previous != gid:
            self._board_dirty = True
            self._spawn(self._push_board())
        if changed:
            self._emit({"t": "crew", "crew": self.crew_view()})
            self._spawn(self._grow_crew())  # we may be the one to invite
            await self._notify()

    async def _join_crew(self, group_id: str) -> None:
        """After an invitation the group can take a moment to list us."""
        for _ in range(10):
            await self._load_crew()
            if self.crew == group_id or (self.crew and group_id not in (await self._crew_ids())):
                return
            await asyncio.sleep(1.0)

    async def _crew_ids(self) -> list[str]:
        try:
            return [g if isinstance(g, str) else (g or {}).get("group_id") for g in await self.api.call("list_groups")]
        except (RpcError, ConnectionError):
            return []

    async def _move_board(self, old: str, new: str) -> None:
        """Copies what the old crew's board holds and the new one lacks. A
        task on both boards keeps the edit made last."""
        try:
            raw = json.loads(await self.api.call("data.doc_json", {"space_id": old, "doc_id": BOARD_DOC}))
            space = {"space_id": new, "doc_id": BOARD_DOC}
            await self.api.call("data.create_doc", space)
            current = json.loads(await self.api.call("data.doc_json", space))
            moved = 0
            for collection in ("tasks", "notes"):
                have = current.get(collection) or {}
                for key, value in (raw.get(collection) or {}).items():
                    if key in have and (collection != "tasks" or _stamp(value) <= _stamp(have[key])):
                        continue
                    await self.api.call("data.map_set", {**space, "collection": collection, "key": key,
                                                         "value_json": json.dumps({"kind": "text", "value": value})})
                    moved += 1
            if moved:
                await self.api.call("data.flush", space)
        except (RpcError, ConnectionError, ValueError, AttributeError, TypeError) as exc:
            # AttributeError, TypeError: a board a peer filled with non-objects.
            logger.info("board not moved to the new crew: %s", exc)
        self._board_dirty = True
        await self._push_board()
        await self._notify()

    def _inviter(self) -> bool:
        """The crew member with the lowest address among those nearby
        invites newcomers, so two members never commit the same add."""
        nearby = [a for a in self.crew_members if a == self.address or (a in self.peers and self.peers[a].nearby)]
        return bool(nearby) and min(nearby) == self.address

    async def _ensure_crew(self, create: bool) -> str | None:
        """The crew's group id. With ``create``, makes the crew when there
        is none (and raises NodeError when no agent is nearby to share it)."""
        async with self._crew_lock:
            # With an allowlist, the crew is checked again before use: another
            # member may have let an outsider in since it was adopted. Group
            # events force the check; otherwise it holds for CREW_CHECK_TTL,
            # so a burst of board reads is not a burst of group lookups.
            if self.crew is None or (self.allow is not None and self._crew_stale()):
                await self._load_crew()
            if self.crew is None:
                if not create:
                    return None
                if not self.agents():
                    raise NodeError("No agent is nearby yet. Call peers(wait_seconds=30) and try again.")
                info = await self.api.call("create_group", {"group_name": CREW_NAME})
                self.crew = info["group_id"]
                self.crew_members = list(info.get("members") or [self.address])
                self._emit({"t": "crew", "crew": self.crew_view()})
            if self._inviter():
                await self._invite_missing()
            return self.crew

    def _crew_stale(self) -> bool:
        checked = self._crew_checked
        return checked is None or asyncio.get_running_loop().time() - checked > CREW_CHECK_TTL

    async def _invite_missing(self) -> None:
        if self.crew is None:
            return
        added = False
        for peer in self.agents():
            if peer.address in self.crew_members:
                continue
            if peer.session != "SessionConfirmed":
                try:
                    peer.session = await self.api.call("get_establishment_state", {"peer_id": peer.address})
                except (RpcError, ConnectionError):
                    continue
                if peer.session != "SessionConfirmed":
                    continue  # invited once its session is up
            try:
                await self.api.call("invite_to_group", {"group_id": self.crew, "invitee_user_id": peer.address})
                self.crew_members.append(peer.address)
                added = True
            except (RpcError, ConnectionError) as exc:
                logger.info("crew invite to %s not sent yet: %s", short(peer.address), exc)
        if added:
            self._emit({"t": "crew", "crew": self.crew_view()})

    def _creator(self) -> bool:
        """Before there is a crew, the agent with the lowest address makes it.
        An agent that hears of a crew already formed waits to be invited,
        however low its address: a second crew would only have to merge."""
        agents = self.agents()
        if any(p.crew for p in agents):
            return False
        return all((self.address or "") < p.address for p in agents)

    async def _grow_crew(self) -> None:
        if self.crew is None:
            if not self._creator():
                return  # the creator invites us
            await asyncio.sleep(CREW_SETTLE)
            if self.crew is None and not self._creator():
                return
        try:
            await self._ensure_crew(create=True)
        except (RpcError, NodeError, ConnectionError) as exc:
            logger.info("crew not grown yet: %s", exc)

    async def _await_crew(self) -> None:
        """An edit before the crew's invitation arrives waits for it a
        while, then makes a crew itself (the creator may be gone)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CREW_WAIT
        while self.crew is None and not self._creator():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            async with self._changed:
                try:
                    await asyncio.wait_for(self._changed.wait(), min(remaining, 1.0))
                except asyncio.TimeoutError:
                    pass
            await self._load_crew()

    def crew_view(self) -> dict[str, Any] | None:
        if self.crew is None:
            return None
        return {"id": self.crew, "members": [self.name if a == self.address else self.peer_name(a)
                                             for a in self.crew_members]}

    # ------------------------------------------------------------------ board

    async def board_get(self) -> dict[str, Any]:
        crew = await self._ensure_crew(create=False)
        if crew is None:
            return {"crew": [], "notes": {}, "tasks": [],
                    "note": "No board yet. The lead's first board_update creates it for every agent nearby."}
        space = {"space_id": crew, "doc_id": BOARD_DOC}
        await self.api.call("data.create_doc", space)
        raw = json.loads(await self.api.call("data.doc_json", space))
        tasks = []
        tasks_raw = raw.get("tasks") if isinstance(raw, dict) else None
        notes_raw = raw.get("notes") if isinstance(raw, dict) else None
        for key, value in (tasks_raw if isinstance(tasks_raw, dict) else {}).items():
            # Every crew member writes the board, so a task may hold any
            # JSON at all: anything but an object reads as a bare title.
            try:
                task = peer_json(value) if isinstance(value, str) else value
            except (ValueError, RecursionError):  # not JSON, or NaN
                task = None
            if not isinstance(task, dict):
                task = {"title": str(value)[:200]}
            task["id"] = str(key)
            tasks.append(task)
        tasks.sort(key=lambda t: (str(t.get("order", "")).zfill(4), t["id"]))
        notes = {str(k): v if isinstance(v, str) else json.dumps(v)
                 for k, v in (notes_raw if isinstance(notes_raw, dict) else {}).items()}
        view = self.crew_view() or {}
        return {"crew": view.get("members", []), "notes": notes, "tasks": tasks}

    async def board_update(
        self,
        tasks: list[dict[str, Any]] | None = None,
        notes: dict[str, str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Merges task fields by id and sets notes. Each task id is its own
        key, so agents editing different tasks never collide; the same task
        edited on two sides resolves last write wins."""
        if tasks is not None and (not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks)):
            raise NodeError("tasks must be a list of objects, each with an id.")
        if notes is not None and not isinstance(notes, dict):
            raise NodeError("notes must be an object of named text blocks.")
        if remove is not None and not isinstance(remove, list):
            raise NodeError("remove must be a list of task ids.")
        for patch in tasks or []:
            if "id" not in patch or not str(patch["id"]).strip():
                raise NodeError("Every task needs an id, e.g. {\"id\": \"rules\", \"title\": ..., \"owner\": ...}.")
        await self._await_crew()
        crew = await self._ensure_crew(create=True)
        if crew is None:
            raise NodeError("No crew yet. Call peers(wait_seconds=30) and try again.")
        space = {"space_id": crew, "doc_id": BOARD_DOC}
        await self.api.call("data.create_doc", space)
        current = {t["id"]: t for t in (await self.board_get())["tasks"]}
        for patch in tasks or []:
            task_id = str(patch["id"])
            merged = {**current.get(task_id, {}), **patch}
            merged.pop("id", None)
            merged["updated_by"] = self.name
            merged["updated_at"] = round(time.time(), 3)  # lets a crew merge keep the newer edit
            await self.api.call(
                "data.map_set",
                {**space, "collection": "tasks", "key": task_id,
                 "value_json": json.dumps({"kind": "text", "value": json.dumps(merged)})},
            )
        for key, text in (notes or {}).items():
            await self.api.call(
                "data.map_set",
                {**space, "collection": "notes", "key": key,
                 "value_json": json.dumps({"kind": "text", "value": str(text)})},
            )
        for task_id in remove or []:
            await self.api.call("data.map_delete", {**space, "collection": "tasks", "key": str(task_id)})
        await self.api.call("data.flush", space)
        board = await self.board_get()
        self._emit({"t": "board", "board": board, "by": self.name})
        return board

    # ------------------------------------------------------------------- game

    async def game_send(self, payload: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        address = self.resolve_peer(peer)
        entry = await self._send_envelope(address, "game", payload)
        return {"message_id": entry.message_id}

    def game_info(self, vs: str | None = None) -> dict[str, Any]:
        """The opponent is the one named by ``vs``, or the only agent
        nearby; with several and none named, ``peer`` is null and the page
        offers ``agents`` to choose from."""
        try:
            opponent = self.resolve_peer(vs)
        except NodeError:
            opponent = None
        info: dict[str, Any] = {
            "me": {"name": self.name, "address": self.address},
            "peer": None,
            "agents": [{"name": self.peer_name(p.address), "address": p.address} for p in self.agents()],
        }
        if opponent:
            info["peer"] = {"name": self.peer_name(opponent), "address": opponent}
            # A fixed, symmetric rule both sides compute the same way.
            info["first"] = (self.address or "") < opponent
        return info

    # ----------------------------------------------------------------- events

    async def _on_event(self, event: dict[str, Any]) -> None:
        # The rpc dispatcher runs this off the socket reader, so handlers may
        # call the API; they still run one at a time, in the service's order.
        kind = event.get("type")
        source = next((event[k] for k in ("sender", "peer_id", "provider_peer_id") if isinstance(event.get(k), str)),
                      None)
        # Group events are about this node's own crews: an outsider added to
        # one is exactly what an allowlisted node must hear about.
        is_group = isinstance(kind, str) and kind.startswith("group_")
        if source and source != self.address and not is_group and not self.admitted(source):
            return  # a peer outside the allowlist: not even on the radar
        handler = getattr(self, f"_ev_{kind}", None)
        if handler is not None:
            await handler(event)
        self._emit({"t": "sdk", "event": _trim_event(event)})
        await self._notify()

    async def _ev_neighbor_discovered(self, event: dict[str, Any]) -> None:
        address = event.get("peer_id")
        if isinstance(address, str) and address != self.address:
            transport = transport_label(event.get("transport"))
            peer = self._touch_peer(address, transport, event.get("rssi"))
            self._set_link(transport)
            self._emit({"t": "peer", "peer": peer.view()})

    async def _ev_peer_rssi(self, event: dict[str, Any]) -> None:
        peer = self.peers.get(event.get("peer_id", ""))
        if peer:
            peer.rssi = event.get("rssi")

    async def _ev_neighbor_lost(self, event: dict[str, Any]) -> None:
        peer = self.peers.get(event.get("peer_id", ""))
        if peer:
            peer.nearby = False
            peer.hello_sent = False
            # Its crew claim lapses with it: a peer that left (or lied about
            # a crew) must not stop this node from ever making one. Its next
            # hello says again where it stands.
            peer.crew = None
            self._emit({"t": "peer", "peer": peer.view()})

    async def _ev_secure_session_established(self, event: dict[str, Any]) -> None:
        address = event.get("peer_id")
        if isinstance(address, str):
            peer = self._touch_peer(address, None)
            peer.session = "SessionConfirmed"
            self._emit({"t": "peer", "peer": peer.view()})
            self._emit({"t": "secure", "peer": peer.view()})
            if peer.is_agent:
                self._spawn(self._grow_crew())

    async def _ev_message_sent(self, event: dict[str, Any]) -> None:
        # Emitted inside send_message, before it returns the id, so the entry
        # may not exist yet; it picks the preview up when it is created.
        message_id = event.get("message_id", "")
        content = event.get("content") or ""
        if not content.startswith("__MLS_ENC__"):
            return
        preview = content[len("__MLS_ENC__"):][:48]
        entry = self._by_message_id.get(message_id)
        if entry is None:
            self._ciphers[message_id] = preview
            if len(self._ciphers) > 256:
                self._ciphers.pop(next(iter(self._ciphers)))
        else:
            entry.cipher = preview

    async def _ev_message_delivered(self, event: dict[str, Any]) -> None:
        entry = self._by_message_id.get(event.get("message_id", ""))
        transport = transport_label(event.get("transport"))
        self._set_link(transport)
        if entry is None:
            return
        entry.status = "delivered"
        entry.transport = transport
        entry.latency_ms = event.get("latency_ms")
        self._emit({"t": "status", "msg": self._radar_view(entry)})

    async def _ev_message_failed(self, event: dict[str, Any]) -> None:
        entry = self._by_message_id.get(event.get("message_id", ""))
        if entry is not None:
            entry.status = "failed"
            self._emit({"t": "status", "msg": self._radar_view(entry)})

    async def _ev_media_sent(self, event: dict[str, Any]) -> None:
        entry = self._file_entries.get(event.get("file_id", ""))
        if entry is not None:
            entry.status = "delivered"
            entry.transport = self.link_transport
            self._emit({"t": "status", "msg": self._radar_view(entry)})

    async def _ev_media_send_failed(self, event: dict[str, Any]) -> None:
        entry = self._file_entries.get(event.get("file_id", ""))
        if entry is not None:
            entry.status = "failed"
            self._emit({"t": "status", "msg": self._radar_view(entry)})

    async def _ev_file_progress(self, event: dict[str, Any]) -> None:
        entry = self._file_entries.get(event.get("file_id", ""))
        if entry is not None:
            self._emit({"t": "progress", "seq": entry.seq, "percentage": event.get("percentage")})

    async def _ev_message_received(self, event: dict[str, Any]) -> None:
        message_id = event.get("message_id")
        if not message_id or message_id in self._seen_ids:
            return  # replay after a reconnect is at least once
        self._seen_ids.add(message_id)
        sender = event.get("sender")
        if not isinstance(sender, str) or not sender:
            return
        transport = transport_label(event.get("transport"))
        self._set_link(transport)
        peer = self._touch_peer(sender, transport)
        content = event.get("content")
        content = content if isinstance(content, str) else ""
        # The rpc reader drops oversized content and keeps only its length.
        shed = event.get("content_chars")
        chars = shed if isinstance(shed, int) else len(content)
        if chars > MAX_INBOUND_CHARS:
            logger.warning("dropped a %d-char message from %s", chars, short(sender))
            kind, body, sender_name, audience = "chat", f"[a message of {chars} chars, too long to show]", None, None
        else:
            try:
                envelope = peer_json(content)
                if not isinstance(envelope, dict) or envelope.get("ob") != ENVELOPE_VERSION:
                    raise ValueError
                kind = str(envelope.get("kind"))
                body = envelope.get("body")
                sender_name = envelope.get("from")
                audience = "all" if envelope.get("to") == "all" else None
                if nesting_depth(body) > MAX_BODY_DEPTH:
                    raise ValueError
            except (ValueError, RecursionError):  # not JSON, NaN, or nested too deep
                kind, body, sender_name, audience = "chat", content, None, None
        if kind not in INBOUND_KINDS:
            kind = "chat"  # an unknown kind, or one only the node may make ("file")
        self._claim_name(peer, sender_name)
        if kind == "game" and not isinstance(body, dict):
            return  # the game page expects an object; drop anything else
        if kind == "hello":
            await self._on_hello(peer, body if isinstance(body, dict) else {})
            return
        entry = Entry(
            seq=next(self._seq), direction="in", peer=sender, kind=kind, body=body, ts=time.time(),
            message_id=message_id, transport=transport, sender_name=peer.name, audience=audience,
        )
        self._append(entry)
        if kind == "game":
            for fn in list(self._game_listeners):
                try:
                    fn({"from": peer.name, "address": sender, "payload": body})
                except Exception:
                    logger.exception("game listener failed")

    async def _on_hello(self, peer: Peer, body: dict[str, Any]) -> None:
        first_time = peer.address not in self._introduced
        self._introduced.add(peer.address)
        peer.is_agent = True
        if isinstance(body.get("capabilities"), dict):
            peer.capabilities = {str(k)[:40]: str(v)[:200] for k, v in list(body["capabilities"].items())[:16]}
        crew = body.get("crew")
        peer.crew = crew if isinstance(crew, str) and crew else None
        self._emit({"t": "peer", "peer": peer.view()})
        if body.get("want_reply"):
            self._spawn(self._send_hello(peer.address, want_reply=False))
        self._spawn(self._grow_crew())
        if first_time:
            entry = Entry(
                seq=next(self._seq), direction="in", peer=peer.address, kind="hello",
                body={"name": peer.name, "capabilities": peer.capabilities, "note": "An Offband agent is nearby."},
                ts=time.time(), sender_name=peer.name, transport=peer.transport,
            )
            self._append(entry)

    async def _ev_file_received(self, event: dict[str, Any]) -> None:
        file_id = event.get("file_id")
        if not isinstance(file_id, str) or not file_id or file_id in self._seen_ids:
            return  # no id to dedupe on, or a replay after a reconnect
        self._seen_ids.add(file_id)
        sender = event.get("sender")
        if not isinstance(sender, str) or not sender:
            return
        peer = self._touch_peer(sender, None)
        encoded = event.get("file_data")
        shed = event.get("file_data_chars")
        if not isinstance(encoded, str) and not isinstance(shed, int):
            # No payload at all: nothing to save, and no empty file.
            logger.warning("a file from %s arrived without data; dropped", short(sender))
            return
        encoded = encoded if isinstance(encoded, str) else ""
        name = event.get("file_name")
        name = name if isinstance(name, str) and name else "file"
        # The rpc reader drops the bytes of a file over the limit and keeps
        # only their length.
        chars = shed if isinstance(shed, int) else len(encoded)
        size = chars * 3 // 4
        saved: Path | None = None
        data: bytes | None = None
        refusal: str | None = None
        if chars <= MAX_FILE_CHARS:
            try:
                data = base64.b64decode(encoded, validate=True)
                size = len(data)
            except (binascii.Error, ValueError):
                # Never written: an empty file would blank the one already there.
                refusal = "refused: not valid base64, not saved"
        # The base64 check refuses most oversized files before decoding; the
        # byte check catches the last two bytes base64 padding hides.
        if refusal is None and (data is None or size > MAX_FILE_BYTES):
            refusal = f"refused: over the {MAX_FILE_BYTES}-byte limit, not saved"
        if refusal is not None:
            relative = "/".join(clean_relative(name))
            note: str | None = refusal
            logger.warning("refused a file from %s: %s", short(sender), refusal)
        else:
            try:
                stored = await asyncio.to_thread(files.store, self.workspace, self.files_dir, sender, name, data)
                relative, saved, note = stored.path, stored.saved_to, stored.note
            except (OSError, ValueError) as exc:  # ValueError: a NUL in the name
                reason = getattr(exc, "strerror", None) or exc
                relative, note = "/".join(clean_relative(name)), f"not saved: {reason}"
        body: dict[str, Any] = {"path": relative, "bytes": size, "saved_to": str(saved) if saved else None}
        if note:
            body["note"] = note
        entry = Entry(
            seq=next(self._seq), direction="in", peer=sender, kind="file", body=body,
            ts=time.time(), message_id=file_id, sender_name=peer.name, transport=self.link_transport,
        )
        self._append(entry)

    async def _ev_service_discovered(self, event: dict[str, Any]) -> None:
        if event.get("service_id") != SERVICE_ID:
            return
        address = event.get("provider_peer_id")
        if not isinstance(address, str) or address == self.address:
            return
        peer = self._touch_peer(address, None)
        caps = event.get("capabilities")
        peer.is_agent = True
        self._claim_name(peer, caps.get("name") if isinstance(caps, dict) else None)
        self._emit({"t": "peer", "peer": peer.view()})

    def _claim_name(self, peer: Peer, name: Any) -> None:
        """A peer's name is what it claims. The first claim for an address
        stands; a later different one, or our own name, is ignored."""
        if not isinstance(name, str) or not name.strip():
            return
        name = name.strip()[:MAX_NAME_CHARS]
        if peer.name is None:
            if name.lower() == self.name.lower():
                logger.warning("%s claims this node's own name %r; ignored", short(peer.address), name)
                return
            peer.name = name
            self._emit({"t": "peer", "peer": peer.view()})
        elif peer.name != name:
            # Once per address: a peer rotating names must not flood the log.
            log = logger.debug if peer.address in self._name_conflicts else logger.warning
            self._name_conflicts.add(peer.address)
            log("%s now claims the name %r; keeping %r", short(peer.address), name, peer.name)

    async def _ev_data_changed(self, event: dict[str, Any]) -> None:
        if event.get("doc_id") != BOARD_DOC:
            return
        if self.crew is not None and event.get("space_id") == self.crew:
            self._board_dirty = True
            # In a task of its own, so a burst of edits does not hold up
            # the events behind it.
            self._spawn(self._push_board())

    async def _ev_group_member_added(self, event: dict[str, Any]) -> None:
        # An invitation to a crew, or a newcomer in ours.
        self._crew_checked = None  # the next use of the crew checks it again
        group_id = event.get("group_id")
        if isinstance(group_id, str):
            self._spawn(self._join_crew(group_id))

    async def _ev_group_member_removed(self, event: dict[str, Any]) -> None:
        self._crew_checked = None
        self._spawn(self._load_crew())

    async def _push_board(self) -> None:
        # Best effort, in a task of its own: a failure is logged, never left
        # as an unretrieved task exception.
        try:
            board = await self.board_get()
        except (RpcError, NodeError, ConnectionError):
            return
        except Exception:
            logger.exception("board not pushed to the radar")
            return
        self._emit({"t": "board", "board": board})

    def _set_link(self, transport: str | None) -> None:
        if transport and transport != self.link_transport:
            self.link_transport = transport
            self._emit({"t": "link", "transport": transport})

    # ------------------------------------------------------------------ views

    def state(self) -> dict[str, Any]:
        return {
            "me": {"name": self.name, "address": self.address, "workspace": str(self.workspace) if self.workspace else None},
            "link": self.link_transport,
            "crew": self.crew_view(),
            "peers": [p.view() for p in self.peers.values()],
            "messages": [self._radar_view(e) for e in self.log[-300:]],
        }


def _stamp(value: Any) -> float:
    """When a board task was last edited, from its stored JSON; 0 if unknown."""
    try:
        task = json.loads(value) if isinstance(value, str) else value
        stamp = float(task.get("updated_at") or 0)
    except (ValueError, TypeError, AttributeError):
        return 0.0
    return stamp if math.isfinite(stamp) else 0.0


def _trim_event(event: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in event.items():
        if key in ("file_data", "key_package", "data"):
            continue
        if isinstance(value, str) and len(value) > 120:
            value = value[:117] + "..."
        out[key] = value
    return out
