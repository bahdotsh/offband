# Offband

**AI agents that talk to each other directly over Bluetooth, end-to-end
encrypted, with no server in between.**

Offband lets coding agents on different machines find each other nearby and
work together with no shared network. Each agent gets a small set of MCP
tools: see who's nearby, send an encrypted message, wait for a reply, share
files, and keep a shared task board. Underneath, the
[Offline Protocol SDK](https://github.com/Offline-Protocol/offline-protocol-sdk)
carries it all over Bluetooth with MLS end-to-end encryption, so no server
sees what the agents say to each other. In the demo, two agents on two
laptops split a job, build a two-player game together, and then the two
laptops play it against each other over the same link.

## How it fits together

On each laptop:

```
Claude Code (or any MCP agent)
   |  MCP over stdio
offband mcp          the agent's tools, a thin front over the daemon
   |  HTTP on 127.0.0.1
offband daemon       peers, message log, inbox, board, files, game channel
   |                 also serves the radar page and the game
   |  local API: JSON-RPC over a Unix socket
offline-protocol-service    the SDK: identity, MLS sessions, Bluetooth
   |  Bluetooth LE
the other laptop
```

`offband up` starts the bottom three layers. Claude Code starts `offband mcp`
from the workspace's `.mcp.json`.

- **Messages** are JSON envelopes inside ordinary 1:1 SDK messages, so they
  ride the MLS session the SDK forms by itself when the two laptops hear each
  other.
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

## The tools

| Tool | What it does |
|---|---|
| `announce(capabilities?)` | Advertises this agent nearby. |
| `peers(wait_seconds?)` | Lists nearby agents with transport, signal and session state. Can wait until one is ready. |
| `send(kind, body, peer?)` | Sends one encrypted message and reports delivery, transport and latency. |
| `inbox(wait_seconds?)` | Returns new messages once each. Blocks until one arrives, and flags board changes. |
| `board_get(peer?)` | Reads the shared board. |
| `board_update(tasks?, notes?, remove?, peer?)` | Merges task patches by id and sets notes. |
| `send_file(path, peer?)` | Sends one file of up to 256 KiB. |

The agent protocol both agents follow is
[`offband/agent/OFFBAND.md`](offband/agent/OFFBAND.md). It fixes the roles,
the message kinds, the board conventions and the Connect Four contract, so a
take stays short and predictable.

## Install

Python 3.10 or later on macOS (Apple silicon) or Linux.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

This pulls `offline-protocol-sdk` 0.31.0 from PyPI, which ships the
`offline-protocol-service` command.

## Security notes

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
