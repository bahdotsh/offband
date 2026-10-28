# Offband

**AI agents that talk to each other directly, over Bluetooth or the local
network, end-to-end encrypted, with no server in between.**

Offband lets coding agents on nearby machines find each other and work
together without a shared cloud service. Each agent gets a small set of MCP
tools: see who's nearby, send an encrypted message, wait for a reply, share
files and keep a shared task board. Underneath, the
[Offline Protocol SDK](https://github.com/Offline-Protocol/offline-protocol-sdk)
carries everything with MLS end-to-end encryption, so nothing in between sees
what the agents say to each other.

In the demo, two Claude Code agents split a job, build a two-player Connect
Four together, review each other's code, and then play the game against each
other over the same link.

## Features

- **Peer discovery** over Bluetooth LE, the local network, or both.
- **Encrypted messaging** on 1:1 MLS sessions the SDK sets up by itself.
- **A shared task board** that both agents edit and that merges per task.
- **File transfer** into the partner's workspace, at the same relative path.
- **A radar page** that shows the link, every message and the board live.
- **A game channel** so the finished game can be played across the link.
- **Works with any MCP agent.** Claude Code is wired up out of the box.

## Requirements

- Python 3.10 or later
- macOS (Apple silicon) or Linux
- Bluetooth LE for the `ble` transport, or a shared network for `lan`
- [Claude Code](https://docs.claude.com/en/docs/claude-code) or another MCP
  client to drive the agents

## Installation

```bash
git clone https://github.com/bahdotsh/offband.git
cd offband
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

This pulls `offline-protocol-sdk` from PyPI, which ships the
`offline-protocol-service` binary that Offband runs underneath.

## Quick start

### Two machines

On each machine, prepare a workspace and start a node. Use a different name
on each side:

```bash
.venv/bin/offband init ~/ada --name Ada
.venv/bin/offband up --name Ada --workspace ~/ada
```

`init` writes `.mcp.json`, `OFFBAND.md` (the agent protocol) and a short
`CLAUDE.md` into the workspace. `up` starts the SDK service, the daemon and
the radar at <http://127.0.0.1:7777/>.

Then start an agent in each workspace and give one of them the job:

```bash
cd ~/ada && claude    # "Find the agent next to you and build a two-player game with it."
cd ~/grace && claude  # "Work with the agent next to you. It will propose the plan."
```

The game the agents build is served at <http://127.0.0.1:7777/game/>.

### Choosing a transport

`ble` is the default. Pass `--transport` to `offband up` to change it:

```bash
# Bluetooth only (default)
offband up --name Ada --workspace ~/ada --transport ble

# Local network, finding the other node over DNS-SD
offband up --name Ada --workspace ~/ada --transport lan --lan-discovery

# Local network with an explicit peer
offband up --name Ada --workspace ~/ada --transport lan \
  --listen 0.0.0.0:7911 --peer 192.168.1.20:7911

# Both
offband up --name Ada --workspace ~/ada --transport both
```

### One machine

To try it without a second machine, run two linked nodes over loopback:

```bash
.venv/bin/offband rehearse --root ./rehearsal
```

This prepares `rehearsal/ada` and `rehearsal/grace` and serves their radars on
ports 7777 and 7778. Open an agent in each workspace as above.
`scripts/headless_rehearsal.sh` runs the whole exchange with `claude -p` and
logs both agents.

## How it works

On each machine:

```
Claude Code (or any MCP agent)
   |  MCP over stdio
offband mcp          the agent's tools, a thin front over the daemon
   |  HTTP on 127.0.0.1
offband daemon       peers, message log, inbox, board, files, game channel
   |                 also serves the radar page and the game
   |  local API: JSON-RPC over a Unix socket
offline-protocol-service    the SDK: identity, MLS sessions, transports
   |  Bluetooth LE or LAN
the other machine
```

`offband up` starts the bottom three layers. Claude Code starts `offband mcp`
from the workspace's `.mcp.json`.

- **Messages** are JSON envelopes inside ordinary 1:1 SDK messages, so they
  ride the MLS session the SDK forms when the two nodes find each other.
- **The board** is a replicated document in the 1:1 space the two agents
  share. Each task is its own key, so edits to different tasks merge cleanly.
- **Files** use the SDK's file transfer and land in the partner's workspace
  at the same relative path. Each agent sends only the files it owns, so the
  two trees converge.
- **Game moves** are envelopes of kind `game`. They go to the game page and
  never to the agent's inbox.
- **Discovery** also registers an `offband-agent` mesh service. Service
  fields are signed but not encrypted, so it carries the agent's name and
  nothing else.

### MCP tools

| Tool | What it does |
|---|---|
| `announce(capabilities?)` | Advertises this agent nearby. |
| `peers(wait_seconds?)` | Lists nearby agents with transport, signal and session state. Can wait until one is ready. |
| `send(kind, body, peer?)` | Sends one encrypted message and reports delivery, transport and latency. |
| `inbox(wait_seconds?)` | Returns new messages once each. Blocks until one arrives, and flags board changes. |
| `board_get(peer?)` | Reads the shared board. |
| `board_update(tasks?, notes?, remove?, peer?)` | Merges task patches by id and sets notes. |
| `send_file(path, peer?)` | Sends one file of up to 256 KiB. |

Both agents follow the protocol in
[`offband/agent/OFFBAND.md`](offband/agent/OFFBAND.md). It fixes the roles,
the message kinds, the board conventions and the Connect Four contract.

## Project layout

```
offband/
  cli.py           offband up | init | mcp | rehearse
  daemon.py        HTTP API, radar and game server
  mcp_server.py    the MCP tools
  node.py          peers, messages, board, files on top of the SDK
  rpc.py           client for the SDK's local API
  service.py       starts offline-protocol-service
  agent/           OFFBAND.md, the agent protocol
  web/             the radar page
examples/
  reference-game/  a reference Connect Four built to the protocol's contract
scripts/           headless rehearsal and a two-node spike
tests/             end-to-end test with two real SDK services
```

## Development

```bash
.venv/bin/python -m pytest
```

The test starts two real SDK services on this machine, links them over
loopback, and drives discovery, messages, the board, a file and game moves
through two Offband nodes.

## Known issues

- If both nodes dial each other at the same moment over LAN, the SDK can lose
  the first key exchange and the session never comes up. `offband rehearse`
  avoids this by having only one node dial. With `--peer`, give it on one
  side only.

## Security

- The daemon binds 127.0.0.1 only. Every POST needs the header
  `X-Offband: 1` and every request a loopback Host header. Together they stop
  a web page in the same browser from driving the agent, by cross-site
  request or by DNS rebinding.
- Any local process of the same user can still reach the daemon and the SDK
  socket. That is the SDK's own trust boundary for the local API.
- Messages and files are MLS-encrypted end to end. The `offband-agent`
  service registration is signed plaintext, so it carries only the name.
- A received file is written only inside the workspace. Paths with `..` or
  absolute paths are reduced to their file name.

## Contributing

Issues and pull requests are welcome. Please run the tests before opening a
pull request, and keep changes to the agent protocol in `OFFBAND.md` in step
with the tools.

## Acknowledgements

Built with the
[Offline Protocol SDK](https://github.com/Offline-Protocol/offline-protocol-sdk).
