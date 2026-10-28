"""The Offband daemon: owns the node and serves it on loopback.

    /                 the radar page
    /game/            the game the agents build (served from the workspace)
    /offband.js       the game bridge (send and receive moves over the link)
    /api/...          the JSON API the MCP server and the pages call
    /api/stream       radar events, as server-sent events
    /api/game/stream  game moves from the peer, as server-sent events

Loopback only. Every POST must carry ``X-Offband: 1`` and every request a
loopback Host header: a header a cross-site form cannot set forces a CORS
preflight this server never answers, and the Host check stops DNS
rebinding. Together they keep a web page in the same browser from making
the agent say things.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Callable

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
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class Guard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            host = headers.get("host", "").rsplit(":", 1)[0] if not headers.get("host", "").startswith("[") else "[::1]"
            if host not in LOOPBACK_HOSTS:
                await PlainTextResponse("forbidden host", status_code=403)(scope, receive, send)
                return
            if scope["method"] not in ("GET", "HEAD") and headers.get("x-offband") != "1":
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

    async def game(request: Request) -> Response:
        root = game_dir or (node.workspace / "game" if node.workspace else None)
        if root is None:
            return PlainTextResponse("no game directory", status_code=404)
        relative = request.path_params.get("path") or "index.html"
        target = (root / relative).resolve()
        if not str(target).startswith(str(root.resolve())):
            return PlainTextResponse("not found", status_code=404)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if relative == "index.html":
                return FileResponse(WEB / "waiting.html")
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(target, headers={"Cache-Control": "no-store"})

    async def game_redirect(_: Request) -> Response:
        return Response(status_code=307, headers={"Location": "/game/"})

    async def state(_: Request) -> Response:
        return JSONResponse(node.state())

    async def stream(_: Request) -> Response:
        return _sse(node.add_listener, initial=[{"t": "state", "state": node.state()}])

    async def peers(request: Request) -> Response:
        try:
            wait = float(request.query_params.get("wait", "0"))
            min_agents = int(request.query_params.get("min_agents", "1"))
            return JSONResponse({"me": {"name": node.name, "address": node.address},
                                 "peers": await node.list_peers(wait, min_agents),
                                 "crew": node.crew_view()})
        except Exception as exc:
            return _error(exc)

    async def send(request: Request) -> Response:
        try:
            body = await _json(request)
            return JSONResponse(await node.send(body.get("peer"), str(body.get("kind", "chat")), body.get("body")))
        except Exception as exc:
            return _error(exc)

    async def inbox(request: Request) -> Response:
        try:
            wait = float(request.query_params.get("wait", "0"))
            peek = request.query_params.get("peek") == "1"
            return JSONResponse(await node.inbox(wait, peek))
        except Exception as exc:
            return _error(exc)

    async def board(request: Request) -> Response:
        try:
            if request.method == "GET":
                return JSONResponse(await node.board_get())
            body = await _json(request)
            return JSONResponse(await node.board_update(body.get("tasks"), body.get("notes"), body.get("remove")))
        except Exception as exc:
            return _error(exc)

    async def send_file(request: Request) -> Response:
        try:
            body = await _json(request)
            return JSONResponse(await node.send_file(body.get("peer"), str(body.get("path", ""))))
        except Exception as exc:
            return _error(exc)

    async def announce(request: Request) -> Response:
        try:
            body = await _json(request)
            caps = body.get("capabilities") or {}
            if not isinstance(caps, dict):
                raise NodeError("capabilities must be an object")
            return JSONResponse(await node.announce(caps))
        except Exception as exc:
            return _error(exc)

    async def game_info(request: Request) -> Response:
        return JSONResponse(node.game_info(request.query_params.get("vs")))

    async def game_send(request: Request) -> Response:
        try:
            body = await _json(request)
            payload = body.get("payload")
            if not isinstance(payload, dict) or not body.get("peer"):
                raise NodeError("Send {\"peer\": <opponent address>, \"payload\": {...}}.")
            return JSONResponse(await node.game_send(payload, str(body["peer"])))
        except Exception as exc:
            return _error(exc)

    async def game_stream(_: Request) -> Response:
        return _sse(node.add_game_listener)

    routes = [
        Route("/", radar),
        Route("/offband.js", bridge),
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
