"""The ``offband`` command.

    offband up    --name Ada --workspace ./ada        SDK service + daemon + radar
    offband init  ./ada --name Ada                    write .mcp.json, OFFBAND.md, CLAUDE.md
    offband mcp                                       the MCP server (Claude Code runs this)
    offband rehearse --names Ada Grace Linus          linked nodes on this machine, over loopback
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import APP_ID, __version__
from .service import ServiceSpec, default_home

AGENT_DIR = Path(__file__).parent / "agent"
MARK_START = "<!-- offband -->"
MARK_END = "<!-- /offband -->"


def _add_up_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", required=True, help="this agent's display name, e.g. Ada")
    p.add_argument("--workspace", default=".", help="the agent's working directory (received files land here)")
    p.add_argument("--port", type=int, default=7777, help="loopback port for the radar and the agent API")
    p.add_argument("--home", help="node state directory (default ~/.offband/<name>)")
    p.add_argument("--transport", choices=["ble", "lan", "both"], default="ble",
                   help="ble (the demo: Bluetooth only), lan (rehearsals), or both")
    p.add_argument("--listen", help="lan: HOST:PORT to accept peer streams on")
    p.add_argument("--peer", action="append", default=[], help="lan: HOST:PORT of the other node (repeatable)")
    p.add_argument("--lan-discovery", action="store_true", help="lan: find the other node over DNS-SD")
    p.add_argument("--game-dir", help="serve the game from here instead of <workspace>/game")
    p.add_argument("--allow", action="append", default=[], metavar="ADDRESS",
                   help="only talk to this peer address (off1...); repeat once per peer. Default: anyone nearby")
    p.add_argument("--fresh", action="store_true",
                   help="start with a new identity, no crew and no board: deletes the node home first")
    p.add_argument("--log-level", default="INFO")


async def _run_node(args: argparse.Namespace, stop: asyncio.Event) -> int:
    from .daemon import serve
    from .node import Node
    from .rpc import LocalApi

    home = Path(args.home).expanduser() if args.home else default_home(args.name.lower())
    if args.fresh:
        reset_home(home)
    spec = ServiceSpec(home=home, transport=args.transport, listen=args.listen, peers=args.peer,
                       lan_discovery=args.lan_discovery, log_level=args.log_level)
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    proc = spec.spawn()
    log = logging.getLogger("offband")
    log.info("SDK service starting (%s), log: %s", args.transport, home / "service.log")
    try:
        node = Node(LocalApi(str(spec.socket_path), APP_ID, f"offband/{__version__}"), args.name, workspace,
                    allow=args.allow or None)
        start = asyncio.create_task(node.start())
        exited = asyncio.create_task(asyncio.to_thread(proc.wait))
        done, _ = await asyncio.wait({start, exited}, return_when=asyncio.FIRST_COMPLETED)
        if exited in done:
            log.error("the SDK service exited (code %s); see %s", proc.returncode, home / "service.log")
            return 1
        start.result()
        log.info("%s is %s", args.name, node.address)
        log.info("radar: http://127.0.0.1:%d/   game: http://127.0.0.1:%d/game/", args.port, args.port)
        game_dir = Path(args.game_dir).resolve() if args.game_dir else None
        web = asyncio.create_task(serve(node, args.port, game_dir))
        lost = asyncio.create_task(node.api.closed.wait())
        stopped = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait({web, exited, lost, stopped}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if exited in done or lost in done:
            log.error("lost the SDK service; see %s", home / "service.log")
            return 1
        return 0
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(proc.wait, 15)
            except subprocess.TimeoutExpired:
                proc.kill()


def reset_home(home: Path) -> None:
    """Deletes a node home, so the node starts with a new identity and none
    of the last run's crew or board. Refuses a directory that does not look
    like a node home, so a mistyped --home never deletes anything else."""
    if not home.exists():
        return
    markers = ("config.json", "store.key", "keys", "state")
    if not home.is_dir() or not any((home / m).exists() for m in markers):
        raise SystemExit(f"--fresh: {home} is not an Offband node home; not deleting it")
    unexpected = [p.name for p in home.iterdir() if p.name not in markers + ("service.log", "service.log.1")]
    if unexpected:
        raise SystemExit(f"--fresh: {home} holds files Offband did not make ({', '.join(unexpected)}); not deleting it")
    shutil.rmtree(home)


def _check_allow(addresses: list[str]) -> str | None:
    for address in addresses:
        if not address.startswith("off1") or len(address) < 20:
            return f"--allow takes a full peer address (off1...), not {address!r}"
    return None


def _main_async(coro_factory) -> int:
    async def runner() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        return await coro_factory(stop)

    return asyncio.run(runner())


def cmd_up(args: argparse.Namespace) -> int:
    problem = _check_allow(args.allow)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    return _main_async(lambda stop: _run_node(args, stop))


def cmd_rehearse(args: argparse.Namespace) -> int:
    """Two or more nodes on this machine, linked over loopback peer streams."""
    root = Path(args.root).expanduser().resolve()
    names = args.names
    if len(names) < 2 or len({n.lower() for n in names}) != len(names):
        print("rehearse needs two or more different names", file=sys.stderr)
        return 2
    ports = [args.port + i for i in range(len(names))]
    links = [f"127.0.0.1:{args.link_port + i}" for i in range(len(names))]

    async def run_all(stop: asyncio.Event) -> int:
        runs = []
        for i, name in enumerate(names):
            ns = argparse.Namespace(
                name=name, workspace=str(root / name.lower()), port=ports[i], home=str(root / f".{name.lower()}-node"),
                # Each pair is dialled from one side only, the later node
                # dialling the earlier ones: two nodes dialling each other at
                # once can lose the first key exchange (README, Known issues).
                transport="lan", listen=links[i], peer=links[:i], lan_discovery=False, game_dir=None,
                allow=[], fresh=args.fresh, log_level=args.log_level,
            )
            init_workspace(Path(ns.workspace), name, ports[i], quiet=True)
            runs.append(asyncio.create_task(_run_node(ns, stop)))
        for i, name in enumerate(names):
            print(f"{name}: workspace {root / name.lower()}  radar http://127.0.0.1:{ports[i]}/")
        results = await asyncio.gather(*runs)
        return max(results)

    return _main_async(run_all)


def init_workspace(workspace: Path, name: str, port: int, quiet: bool = False) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    offband = shutil.which("offband", path=str(Path(sys.executable).parent)) or shutil.which("offband")
    command = [offband, "mcp"] if offband else [sys.executable, "-m", "offband.mcp_server"]
    mcp = {
        "mcpServers": {
            "offband": {
                "command": command[0],
                "args": command[1:],
                "env": {"OFFBAND_URL": f"http://127.0.0.1:{port}"},
            }
        }
    }
    (workspace / ".mcp.json").write_text(json.dumps(mcp, indent=2) + "\n")
    shutil.copyfile(AGENT_DIR / "OFFBAND.md", workspace / "OFFBAND.md")
    claude_md = workspace / "CLAUDE.md"
    block = (
        f"{MARK_START}\n"
        f"You are {name}, an Offband agent. Read OFFBAND.md and follow it.\n\n"
        f"- Your radar: http://127.0.0.1:{port}/\n"
        f"- The game, once built: http://127.0.0.1:{port}/game/\n"
        f"{MARK_END}\n"
    )
    text = claude_md.read_text() if claude_md.exists() else ""
    if MARK_START in text and MARK_END in text:
        head, rest = text.split(MARK_START, 1)
        text = head + block + rest.split(MARK_END, 1)[1].lstrip("\n")
    else:
        text = (text + "\n" if text and not text.endswith("\n") else text) + block
    claude_md.write_text(text)
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(exist_ok=True)
    settings = settings_dir / "settings.json"
    if not settings.exists():
        settings.write_text(json.dumps({
            "enabledMcpjsonServers": ["offband"],
            "permissions": {"allow": ["mcp__offband"]},
        }, indent=2) + "\n")
    if not quiet:
        print(f"Workspace ready: {workspace}")
        print(f"  .mcp.json points Claude Code at the daemon on port {port}")
        print("  OFFBAND.md is the agent protocol; CLAUDE.md points at it")


def cmd_init(args: argparse.Namespace) -> int:
    init_workspace(Path(args.workspace).expanduser().resolve(), args.name, args.port)
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    from .mcp_server import main as mcp_main

    mcp_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="offband", description="Agents that work together over Bluetooth.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="run the SDK service, the daemon and the radar")
    _add_up_args(up)
    up.set_defaults(fn=cmd_up)

    init = sub.add_parser("init", help="prepare a workspace for an agent")
    init.add_argument("workspace")
    init.add_argument("--name", required=True)
    init.add_argument("--port", type=int, default=7777)
    init.set_defaults(fn=cmd_init)

    mcp = sub.add_parser("mcp", help="the MCP server over stdio")
    mcp.set_defaults(fn=cmd_mcp)

    rehearse = sub.add_parser("rehearse", help="two or more linked nodes on this machine, over loopback")
    rehearse.add_argument("--root", default="./rehearsal")
    rehearse.add_argument("--names", nargs="+", default=["Ada", "Grace"], help="one node per name (two or more)")
    rehearse.add_argument("--port", type=int, default=7777, help="radar port of the first node; the rest count up")
    rehearse.add_argument("--link-port", type=int, default=7911, help="loopback link port of the first node")
    rehearse.add_argument("--fresh", action="store_true",
                          help="start every node with a new identity, no crew and no board")
    rehearse.add_argument("--log-level", default="INFO")
    rehearse.set_defaults(fn=cmd_rehearse)

    args = parser.parse_args(argv)
    if args.command != "mcp":
        logging.basicConfig(level=getattr(logging, str(getattr(args, "log_level", "INFO")).upper(), logging.INFO),
                            format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
