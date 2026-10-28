"""The Offband node: everything one agent's laptop knows and does.

It is one client of the SDK's local API under the application id
``offband``. It keeps the peers it can hear, a log of every message in and
out (which feeds the agent's inbox and the radar page), the shared task
board, received files, and the game channel.

Every message between agents is one JSON envelope inside an ordinary 1:1
SDK message, so it travels over the MLS session the engine sets up with the
peer. Nothing here uses service request bodies for content: those are signed
plaintext, and services are used for discovery only.

    {"ob": 1, "kind": "propose", "from": "Ada", "body": ...}
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from . import APP_ID
from .rpc import LocalApi, RpcError

logger = logging.getLogger(__name__)

ENVELOPE_VERSION = 1
SERVICE_ID = "offband-agent"
BOARD_DOC = "board"

# What an agent may say. "hello" and "game" are the node's own; the rest are
# the agent protocol (see offband/agent/OFFBAND.md).
AGENT_KINDS = ("propose", "accept", "counter", "task", "review", "done", "chat", "question")
SYSTEM_KINDS = ("hello", "game", "file")

# BLE is slow and the local API caps a request near the frame limit. Source
# files of tens of KiB are the intended size.
MAX_FILE_BYTES = 256 * 1024
MAX_BODY_CHARS = 16 * 1024
HELLO_SESSION_WAIT = 4.0
LOG_LIMIT = 2000


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
    ) -> None:
        self.api = api
        self.name = name
        self.workspace = workspace.resolve() if workspace else None
        self.files_dir = (files_dir or (self.workspace / ".offband" / "received" if self.workspace else None))
        self.address: str | None = None
        self.peers: dict[str, Peer] = {}
        self.log: list[Entry] = []
        self.capabilities: dict[str, Any] = {}
        self._seq = itertools.count(1)
        self._inbox_cursor = 0
        self._seen_ids: set[str] = set()
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

    # ------------------------------------------------------------------ setup

    async def start(self) -> None:
        self.api.on_event = self._on_event
        hello = await self.api.connect()
        self.address = hello.get("local_address")
        await self.api.call("subscribe", {"types": "all"})
        await self.announce({})
        await self._snapshot_neighbors()

    async def _snapshot_neighbors(self) -> None:
        """Peers the engine already knows about before our events started."""
        try:
            topology = await self.api.call("get_topology")
        except RpcError:
            return
        for node in topology.get("nodes", []):
            address = node.get("id") or node.get("peer_id") or node.get("address")
            if isinstance(address, str) and address.startswith("off1") and address != self.address:
                self._touch_peer(address, None)

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
        body = {"name": self.name, "capabilities": self.capabilities, "want_reply": want_reply}
        try:
            await self._send_envelope(address, "hello", body, log=False)
        except (RpcError, ConnectionError) as exc:
            logger.info("hello to %s not sent yet: %s", short(address), exc)
            peer = self.peers.get(address)
            if peer:
                peer.hello_sent = False

    async def list_peers(self, wait_seconds: float = 0.0) -> list[dict[str, Any]]:
        """Nearby peers, agents first. With ``wait_seconds``, waits until at
        least one agent is known and its session is confirmed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait_seconds)
        while True:
            await self._refresh_sessions()
            ready = [p for p in self.peers.values() if p.is_agent and p.session == "SessionConfirmed"]
            remaining = deadline - loop.time()
            if ready or remaining <= 0:
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

    def resolve_peer(self, peer: str | None) -> str:
        """An address from a name, an address, a prefix, or nothing (the one
        agent nearby)."""
        agents = [p for p in self.peers.values() if p.is_agent]
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
        for p in self.peers.values():
            if p.name and p.name.lower() == lowered:
                return p.address
        matches = [a for a in self.peers if a.startswith(peer)]
        if len(matches) == 1:
            return matches[0]
        if peer.startswith("off1"):
            return peer  # not heard yet; the engine holds the message until it is
        raise NodeError(f"No nearby peer called {peer!r}.")

    def peer_name(self, address: str) -> str:
        peer = self.peers.get(address)
        return (peer.name if peer and peer.name else None) or short(address)

    # --------------------------------------------------------------- messages

    async def _send_envelope(self, address: str, kind: str, body: Any, log: bool = True) -> Entry | None:
        content = json.dumps({"ob": ENVELOPE_VERSION, "kind": kind, "from": self.name, "body": body})
        message_id = await self.api.call(
            "send_message", {"recipient": address, "content": content, "priority": "High" if kind == "game" else "Medium"}
        )
        if not log:
            return None
        entry = Entry(
            seq=next(self._seq), direction="out", peer=address, kind=kind, body=body, ts=time.time(),
            message_id=message_id, status="sent", sender_name=self.name,
            cipher=self._ciphers.pop(message_id, None),
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
        address = self.resolve_peer(peer)
        entry = await self._send_envelope(address, kind, body)
        assert entry is not None
        await self._await_status(entry, wait_delivery)
        return self._outcome(entry)

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
        self._emit({"t": "msg", "msg": self._radar_view(entry)})

    def _radar_view(self, entry: Entry) -> dict[str, Any]:
        view = entry.view()
        view["peer_name"] = self.peer_name(entry.peer)
        body = entry.body
        if isinstance(body, (dict, list)):
            text = json.dumps(body)
        else:
            text = str(body)
        view["summary"] = text if len(text) <= 240 else text[:237] + "..."
        if entry.kind == "file":
            view.pop("body", None)
            view["summary"] = f"{body.get('path')} ({body.get('bytes')} bytes)"
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
                "kind": e.kind,
                "body": e.body,
                "transport": e.transport,
                "at": time.strftime("%H:%M:%S", time.localtime(e.ts)),
            }
            messages.append(item)
        return {"messages": messages, "board_changed": board_changed}

    # ------------------------------------------------------------------ files

    async def send_file(self, peer: str | None, path: str) -> dict[str, Any]:
        address = self.resolve_peer(peer)
        source = Path(path)
        if not source.is_absolute():
            if self.workspace is None:
                raise NodeError("Give an absolute path; this node has no workspace.")
            source = self.workspace / source
        source = source.resolve()
        if not source.is_file():
            raise NodeError(f"No such file: {source}")
        data = source.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise NodeError(f"{source.name} is {len(data)} bytes; the limit is {MAX_FILE_BYTES}. Send source, not assets.")
        relative = self._relative(source)
        file_id = await self.api.call(
            "send_file",
            {"recipient": address, "file_data": base64.b64encode(data).decode(), "file_name": quote(relative, safe="")},
        )
        entry = Entry(
            seq=next(self._seq), direction="out", peer=address, kind="file",
            body={"path": relative, "bytes": len(data)}, ts=time.time(), message_id=file_id,
            status="sent", sender_name=self.name,
        )
        self._append(entry)
        self._file_entries[file_id] = entry
        await self._await_status(entry, 8.0)
        result = self._outcome(entry)
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

    def _store_received(self, sender: str, encoded_name: str, data: bytes) -> tuple[str, Path | None]:
        relative = unquote(encoded_name)
        parts = [p for p in Path(relative).parts if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts) or Path(relative).is_absolute():
            parts = [Path(relative).name or "received.bin"]
        relative = "/".join(parts)
        # Into the shared workspace at the same path, so both trees converge;
        # an agent only ever sends files it owns.
        root = self.workspace or self.files_dir
        if root is None:
            return relative, None
        target = (root / relative).resolve()
        if not str(target).startswith(str(root.resolve())):
            return relative, None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return relative, target

    # ------------------------------------------------------------------ board

    async def board_get(self, peer: str | None = None) -> dict[str, Any]:
        address = self.resolve_peer(peer)
        await self.api.call("data.create_doc", {"space_id": address, "doc_id": BOARD_DOC})
        raw = json.loads(await self.api.call("data.doc_json", {"space_id": address, "doc_id": BOARD_DOC}))
        tasks = []
        for key, value in (raw.get("tasks") or {}).items():
            try:
                task = json.loads(value) if isinstance(value, str) else dict(value)
            except (ValueError, TypeError):
                task = {"title": str(value)}
            task["id"] = key
            tasks.append(task)
        tasks.sort(key=lambda t: (str(t.get("order", "")).zfill(4), t["id"]))
        notes = {k: v for k, v in (raw.get("notes") or {}).items()}
        return {"with": self.peer_name(address), "notes": notes, "tasks": tasks}

    async def board_update(
        self,
        peer: str | None = None,
        tasks: list[dict[str, Any]] | None = None,
        notes: dict[str, str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Merges task fields by id and sets notes. Each task id is its own
        key, so two agents editing different tasks never collide; the same
        task edited on both sides resolves last write wins."""
        address = self.resolve_peer(peer)
        space = {"space_id": address, "doc_id": BOARD_DOC}
        await self.api.call("data.create_doc", space)
        current = {t["id"]: t for t in (await self.board_get(address))["tasks"]}
        for patch in tasks or []:
            if "id" not in patch or not str(patch["id"]).strip():
                raise NodeError("Every task needs an id, e.g. {\"id\": \"rules\", \"title\": ..., \"owner\": ...}.")
            task_id = str(patch["id"])
            merged = {**current.get(task_id, {}), **patch}
            merged.pop("id", None)
            merged["updated_by"] = self.name
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
            await self.api.call("data.map_delete", {**space, "collection": "tasks", "key": task_id})
        await self.api.call("data.flush", space)
        board = await self.board_get(address)
        self._emit({"t": "board", "board": board, "by": self.name})
        return board

    # ------------------------------------------------------------------- game

    async def game_send(self, payload: dict[str, Any], peer: str | None = None) -> dict[str, Any]:
        address = self.resolve_peer(peer)
        entry = await self._send_envelope(address, "game", payload)
        assert entry is not None
        return {"message_id": entry.message_id}

    def game_info(self) -> dict[str, Any]:
        try:
            partner = self.resolve_peer(None)
        except NodeError:
            partner = None
        info: dict[str, Any] = {"me": {"name": self.name, "address": self.address}, "peer": None}
        if partner:
            info["peer"] = {"name": self.peer_name(partner), "address": partner}
            # A fixed, symmetric rule both sides compute the same way.
            info["first"] = (self.address or "") < partner
        return info

    # ----------------------------------------------------------------- events

    async def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
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
            self._emit({"t": "peer", "peer": peer.view()})

    async def _ev_secure_session_established(self, event: dict[str, Any]) -> None:
        address = event.get("peer_id")
        if isinstance(address, str):
            peer = self._touch_peer(address, None)
            peer.session = "SessionConfirmed"
            self._emit({"t": "peer", "peer": peer.view()})
            self._emit({"t": "secure", "peer": peer.view()})

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
        sender = event.get("sender", "")
        transport = transport_label(event.get("transport"))
        self._set_link(transport)
        peer = self._touch_peer(sender, transport)
        try:
            envelope = json.loads(event.get("content") or "")
            if not isinstance(envelope, dict) or envelope.get("ob") != ENVELOPE_VERSION:
                raise ValueError
            kind = str(envelope.get("kind"))
            body = envelope.get("body")
            sender_name = envelope.get("from")
        except ValueError:
            kind, body, sender_name = "chat", event.get("content"), None
        if isinstance(sender_name, str) and sender_name:
            if peer.name != sender_name:
                peer.name = sender_name
                self._emit({"t": "peer", "peer": peer.view()})
        if kind == "hello":
            await self._on_hello(peer, body if isinstance(body, dict) else {})
            return
        entry = Entry(
            seq=next(self._seq), direction="in", peer=sender, kind=kind, body=body, ts=time.time(),
            message_id=message_id, transport=transport, sender_name=peer.name,
        )
        self._append(entry)
        if kind == "game":
            for fn in list(self._game_listeners):
                try:
                    fn({"from": peer.name, "payload": body})
                except Exception:
                    logger.exception("game listener failed")

    async def _on_hello(self, peer: Peer, body: dict[str, Any]) -> None:
        first_time = peer.address not in self._introduced
        self._introduced.add(peer.address)
        peer.is_agent = True
        if isinstance(body.get("capabilities"), dict):
            peer.capabilities = body["capabilities"]
        self._emit({"t": "peer", "peer": peer.view()})
        if body.get("want_reply"):
            self._spawn(self._send_hello(peer.address, want_reply=False))
        if first_time:
            entry = Entry(
                seq=next(self._seq), direction="in", peer=peer.address, kind="hello",
                body={"name": peer.name, "capabilities": peer.capabilities, "note": "An Offband agent is nearby."},
                ts=time.time(), sender_name=peer.name, transport=peer.transport,
            )
            self._append(entry)

    async def _ev_file_received(self, event: dict[str, Any]) -> None:
        file_id = event.get("file_id") or ""
        if file_id in self._seen_ids:
            return
        self._seen_ids.add(file_id)
        sender = event.get("sender", "")
        peer = self._touch_peer(sender, None)
        try:
            data = base64.b64decode(event.get("file_data") or "")
        except ValueError:
            data = b""
        relative, saved = self._store_received(sender, event.get("file_name") or "file", data)
        entry = Entry(
            seq=next(self._seq), direction="in", peer=sender, kind="file",
            body={"path": relative, "bytes": len(data), "saved_to": str(saved) if saved else None},
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
        caps = event.get("capabilities") or {}
        peer.is_agent = True
        if caps.get("name") and not peer.name:
            peer.name = caps["name"]
        self._emit({"t": "peer", "peer": peer.view()})

    async def _ev_data_changed(self, event: dict[str, Any]) -> None:
        if event.get("doc_id") != BOARD_DOC:
            return
        space = event.get("space_id")
        if isinstance(space, str) and space in self.peers:
            self._board_dirty = True
            # Never call the API from inside the event reader: the response
            # would be read by the task that is waiting for it.
            self._spawn(self._push_board(space))

    async def _push_board(self, space: str) -> None:
        try:
            board = await self.board_get(space)
        except (RpcError, NodeError, ConnectionError):
            return
        self._emit({"t": "board", "board": board, "by": self.peer_name(space)})

    def _set_link(self, transport: str | None) -> None:
        if transport and transport != self.link_transport:
            self.link_transport = transport
            self._emit({"t": "link", "transport": transport})

    # ------------------------------------------------------------------ views

    def state(self) -> dict[str, Any]:
        return {
            "me": {"name": self.name, "address": self.address, "workspace": str(self.workspace) if self.workspace else None},
            "link": self.link_transport,
            "peers": [p.view() for p in self.peers.values()],
            "messages": [self._radar_view(e) for e in self.log[-300:]],
        }


def _trim_event(event: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in event.items():
        if key in ("file_data", "key_package", "data"):
            continue
        if isinstance(value, str) and len(value) > 120:
            value = value[:117] + "..."
        out[key] = value
    return out
