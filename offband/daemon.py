"""The Offband daemon: owns the node and serves it on loopback.

    /                 the radar page
    /fonts/...        the radar's fonts, bundled so the page works offline
    /game/            the game the agents build (served from the workspace)
    /offband.js       the game bridge (send and receive moves over the link)
    /api/...          the JSON API the MCP server and the pages call
    /api/stream       radar events, as server-sent events
    /api/game/stream  game moves from the peer, as server-sent events

Loopback only. Every request needs a loopback Host header, which stops DNS
rebinding. The game is written by peers, so it is served on its own origin,
``http://localhost:<port>``, which has only the game, ``/offband.js`` and
the ``/api/game`` routes; ``/game/`` on 127.0.0.1 redirects there. The
radar and the rest of the API stay on ``http://127.0.0.1:<port>``, where a
game page cannot reach them. Every /api/ request also needs ``X-Offband: 1``, a header a
cross-site page can only send after a CORS preflight this server never
answers; that covers GETs too, because some of them have effects (reading
the inbox consumes it). The two event streams are exempt, because
EventSource cannot set headers: they only read, and a cross-site page cannot
read their responses.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .node import Node, NodeError
from .rpc import RpcError

logger = logging.getLogger(__name__)

WEB = Path(__file__).parent / "web"
# The radar and the agent's API live on the control origin. The game, which
# is written by peers, lives on the game origin: a different host on the
# same port is a different origin, so a game page cannot call the API.
CONTROL_HOSTS = {"127.0.0.1", "[::1]"}
GAME_HOST = "localhost"
LOOPBACK_HOSTS = CONTROL_HOSTS | {GAME_HOST}
GAME_API = {"/api/game", "/api/game/send", "/api/game/stream"}
UNGUARDED_API = {"/api/stream", "/api/game/stream"}
FONTS = {"FamiljenGrotesk.woff2", "JetBrainsMono.woff2"}


def is_game_page(path: str) -> bool:
    return path == "/game" or path.startswith("/game/")


def on_game_origin(path: str) -> bool:
    """What the game origin serves: the game, its bridge and its channel."""
    return is_game_page(path) or path == "/offband.js" or path in GAME_API


def host_name(host: str) -> str:
    """The host part of a Host header, without the port."""
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end > 0 else host
    return host.rsplit(":", 1)[0]


class Guard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            host = host_name(headers.get("host", ""))
            if host not in LOOPBACK_HOSTS:
                await PlainTextResponse("forbidden host", status_code=403)(scope, receive, send)
                return
            path = scope.get("path", "")
            if host == GAME_HOST and not on_game_origin(path):
                await PlainTextResponse("not on the game origin", status_code=404)(scope, receive, send)
                return
            if host != GAME_HOST and is_game_page(path):
                port = (scope.get("server") or ("", 0))[1]
                # The path as the client sent it, still percent-encoded: the
                # decoded one would put raw spaces in Location and turn an
                # encoded slash into a real one.
                raw_path = (scope.get("raw_path") or path.encode("utf-8")).decode("latin-1")
                query = scope.get("query_string", b"").decode("latin-1")
                location = f"http://{GAME_HOST}:{port}{raw_path}" + (f"?{query}" if query else "")
                await Response(status_code=307, headers={"Location": location})(scope, receive, send)
                return
            needs_header = path.startswith("/api/") and path not in UNGUARDED_API
            if (needs_header or scope["method"] not in ("GET", "HEAD")) and headers.get("x-offband") != "1":
                await PlainTextResponse("missing X-Offband header", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, NodeError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, RpcError):
        return JSONResponse({"error": str(exc), "variant": exc.variant}, status_code=502)
    if isinstance(exc, ConnectionError):
        return JSONResponse({"error": f"the SDK service is not reachable: {exc}"}, status_code=503)
    logger.exception("request failed")
    return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)


def api(handler: Callable[[Request], Awaitable[Any]]) -> Callable[[Request], Awaitable[Response]]:
    """Wraps a handler that returns JSON-able data: every failure becomes a
    JSON error with the right status."""

    @functools.wraps(handler)
    async def wrapped(request: Request) -> Response:
        try:
            result = await handler(request)
        except Exception as exc:
            return _error(exc)
        return result if isinstance(result, Response) else JSONResponse(result)

    return wrapped


def _number(request: Request, name: str, default: float, kind: type = float) -> Any:
    raw = request.query_params.get(name)
    if raw is None:
        return kind(default)
    try:
        value = kind(raw)
    except ValueError:
        raise NodeError(f"{name} must be a number.") from None
    try:
        finite = math.isfinite(value)  # "inf" would hold a request open for good
    except OverflowError:  # an int too large for a float
        finite = False
    if not finite:
        raise NodeError(f"{name} must be a finite number.")
    return value


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise NodeError("The request body must be JSON.") from None
    if not isinstance(body, dict):
        raise NodeError("The request body must be a JSON object.")
    return body


def _sse(subscribe: Callable[[Callable[[dict[str, Any]], None]], Callable[[], None]],
         initial: list[dict[str, Any]] | None = None) -> StreamingResponse:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)

    def push(event: dict[str, Any]) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # a stalled page loses events, never the node

    async def stream() -> AsyncIterator[str]:
        unsubscribe = subscribe(push)
        try:
            for event in initial or []:
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            unsubscribe()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def build_app(node: Node, game_dir: Path | None = None) -> Starlette:
    async def radar(_: Request) -> Response:
        return FileResponse(WEB / "radar.html")

    async def bridge(_: Request) -> Response:
        return FileResponse(WEB / "offband.js", media_type="text/javascript")

    async def font(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in FONTS:
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(WEB / "fonts" / name, media_type="font/woff2",
                            headers={"Cache-Control": "max-age=86400"})

    async def game(request: Request) -> Response:
        root = game_dir or (node.workspace / "game" if node.workspace else None)
        if root is None:
            return PlainTextResponse("no game directory", status_code=404)
        root = root.resolve()
        relative = request.path_params.get("path") or "index.html"
        try:
            target = (root / relative).resolve()
            target.relative_to(root)
            if target.is_dir():
                target = target / "index.html"
            found = target.is_file()
        except (ValueError, OSError):  # outside the root, a NUL, or a name too long
            return PlainTextResponse("not found", status_code=404)
        if not found:
            if relative == "index.html":
                return FileResponse(WEB / "waiting.html")
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target, headers={"Cache-Control": "no-store"})

    async def game_redirect(_: Request) -> Response:
        return Response(status_code=307, headers={"Location": "/game/"})

    @api
    async def state(_: Request) -> Any:
        return node.state()

    async def stream(_: Request) -> Response:
        return _sse(node.add_listener, initial=[{"t": "state", "state": node.state()}])

    @api
    async def peers(request: Request) -> Any:
        wait = _number(request, "wait", 0)
        min_agents = _number(request, "min_agents", 1, int)
        return {"me": {"name": node.name, "address": node.address},
                "peers": await node.list_peers(wait, min_agents),
                "crew": node.crew_view()}

    @api
    async def send(request: Request) -> Any:
        body = await _json(request)
        return await node.send(body.get("peer"), str(body.get("kind", "chat")), body.get("body"))

    @api
    async def inbox(request: Request) -> Any:
        return await node.inbox(_number(request, "wait", 0), request.query_params.get("peek") == "1")

    @api
    async def board(request: Request) -> Any:
        if request.method == "GET":
            return await node.board_get()
        body = await _json(request)
        return await node.board_update(body.get("tasks"), body.get("notes"), body.get("remove"))

    @api
    async def send_file(request: Request) -> Any:
        body = await _json(request)
        return await node.send_file(body.get("peer"), str(body.get("path", "")))

    @api
    async def announce(request: Request) -> Any:
        body = await _json(request)
        caps = body.get("capabilities") or {}
        if not isinstance(caps, dict):
            raise NodeError("capabilities must be an object")
        return await node.announce(caps)

    @api
    async def game_info(request: Request) -> Any:
        return node.game_info(request.query_params.get("vs"))

    @api
    async def game_send(request: Request) -> Any:
        body = await _json(request)
        payload = body.get("payload")
        if not isinstance(payload, dict) or not body.get("peer"):
            raise NodeError("Send {\"peer\": <opponent address>, \"payload\": {...}}.")
        return await node.game_send(payload, str(body["peer"]))

    async def game_stream(_: Request) -> Response:
        return _sse(node.add_game_listener)

    routes = [
        Route("/", radar),
        Route("/offband.js", bridge),
        Route("/fonts/{name}", font),
        Route("/game", game_redirect),
        Route("/game/", game),
        Route("/game/{path:path}", game),
        Route("/api/state", state),
        Route("/api/stream", stream),
        Route("/api/peers", peers),
        Route("/api/send", send, methods=["POST"]),
        Route("/api/inbox", inbox),
        Route("/api/board", board, methods=["GET", "POST"]),
        Route("/api/send_file", send_file, methods=["POST"]),
        Route("/api/announce", announce, methods=["POST"]),
        Route("/api/game", game_info),
        Route("/api/game/send", game_send, methods=["POST"]),
        Route("/api/game/stream", game_stream),
    ]
    return Starlette(routes=routes, middleware=[Middleware(Guard)])


async def serve(node: Node, port: int, game_dir: Path | None = None) -> None:
    import uvicorn

    config = uvicorn.Config(
        build_app(node, game_dir), host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    await server.serve()
