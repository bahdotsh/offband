"""What a page in the same browser can ask the daemon for."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from offband.daemon import Guard, build_app, host_name


@pytest.fixture
async def client(node):
    transport = httpx.ASGITransport(app=build_app(node))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as c:
        yield c


async def test_every_api_read_needs_the_header(client):
    assert (await client.get("/api/inbox")).status_code == 403
    assert (await client.get("/api/state")).status_code == 403
    assert (await client.get("/api/inbox", headers={"X-Offband": "1"})).status_code == 200


async def test_bad_numbers_are_a_client_error(client):
    r = await client.get("/api/inbox?wait=soon", headers={"X-Offband": "1"})
    assert r.status_code == 400 and "number" in r.json()["error"]


async def test_the_game_route_stays_inside_the_game_folder(client, node):
    (node.workspace / "game").mkdir()
    (node.workspace / "game" / "index.html").write_text("<p>game</p>")
    (node.workspace / "game-secret").mkdir()
    (node.workspace / "game-secret" / "x.txt").write_text("SECRET")
    async with game_client(node) as game:
        assert (await game.get("/game/")).text == "<p>game</p>"
    # httpx normalises "..", so hand the app the raw path, as curl --path-as-is does.
    status, body = await raw_get(build_app(node), "/game/../game-secret/x.txt", host=b"localhost:7777")
    assert status == 404 and b"SECRET" not in body


def game_client(node) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=build_app(node))
    return httpx.AsyncClient(transport=transport, base_url="http://localhost:7777")


async def raw_get(app, path: str, host: bytes = b"127.0.0.1:7777") -> tuple[int, bytes]:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": [(b"host", host)], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 7777)}
    out: dict[str, Any] = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")

    await app(scope, receive, send)
    return out["status"], out["body"]


async def test_the_fonts_are_served_and_nothing_else_is(client):
    r = await client.get("/fonts/JetBrainsMono.woff2")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2"
    assert (await client.get("/fonts/..%2Fradar.html")).status_code == 404


async def test_only_loopback_hosts_get_in():
    assert host_name("127.0.0.1:7777") == "127.0.0.1"
    assert host_name("[::1]:7777") == "[::1]"
    assert host_name("[::2]:7777") == "[::2]"

    async def ok(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=Guard(ok))) as c:
        for host, code in [("[::1]:7777", 200), ("[::2]:7777", 403), ("evil.example", 403)]:
            assert (await c.get("http://x/", headers={"Host": host})).status_code == code
        # The two event streams take no header: EventSource cannot send one.
        assert (await c.get("http://127.0.0.1/api/stream")).status_code == 200
        assert (await c.get("http://127.0.0.1/api/game/stream")).status_code == 200
        assert (await c.post("http://127.0.0.1/", content=b"")).status_code == 403


@pytest.mark.parametrize("wait", ["inf", "nan", "-inf"])
async def test_waits_must_be_finite(client, wait):
    r = await client.get(f"/api/peers?wait={wait}", headers={"X-Offband": "1"})
    assert r.status_code == 400 and "finite" in r.json()["error"]


async def test_a_game_page_cannot_reach_the_agent_api(node):
    """Peers write the game, so its origin offers the game channel only."""
    async with game_client(node) as game:
        for path in ("/api/inbox", "/api/state", "/api/send_file", "/api/stream", "/"):
            r = await game.get(path, headers={"X-Offband": "1"})
            assert r.status_code == 404, path
        r = await game.post("/api/send", json={"kind": "chat", "body": "hi"}, headers={"X-Offband": "1"})
        assert r.status_code == 404
        assert (await game.get("/api/game", headers={"X-Offband": "1"})).status_code == 200
        assert (await game.get("/offband.js")).status_code == 200


async def test_the_game_moves_off_the_control_origin(client):
    r = await client.get("/game/?vs=Grace")
    assert r.status_code == 307
    assert r.headers["location"] == "http://localhost:7777/game/?vs=Grace"
    assert (await client.get("/game")).headers["location"] == "http://localhost:7777/game"


async def test_a_wait_too_large_for_a_float_is_a_client_error(client):
    r = await client.get("/api/peers?wait=" + "9" * 400, headers={"X-Offband": "1"})
    assert r.status_code == 400 and "finite" in r.json()["error"]


async def test_the_game_redirect_keeps_the_path_encoded(node):
    scope_path = "/game/a b/c%2Fd.js"
    app = build_app(node)
    out: dict[str, Any] = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]
            out["headers"] = dict(message["headers"])

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": scope_path, "raw_path": b"/game/a%20b/c%2Fd.js", "root_path": "",
             "query_string": b"", "headers": [(b"host", b"127.0.0.1:7777")], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 7777)}
    await app(scope, receive, send)
    assert out["status"] == 307
    assert out["headers"][b"location"] == b"http://localhost:7777/game/a%20b/c%2Fd.js"


async def test_a_cors_preflight_is_never_answered(client):
    r = await client.options("/api/send", headers={"Origin": "http://evil.example",
                                                   "Access-Control-Request-Method": "POST"})
    assert r.status_code == 403 and "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("path", ["/game/a\x00b", "/game/" + "x" * 5000])
async def test_a_game_path_that_cannot_be_resolved_is_not_found(node, path):
    (node.workspace / "game").mkdir()
    status, _ = await raw_get(build_app(node), path, host=b"localhost:7777")
    assert status == 404
