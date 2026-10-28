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

## Rehearse on one machine

Two nodes on one machine, linked over a loopback stream instead of
Bluetooth:

```bash
.venv/bin/offband rehearse --root ./rehearsal
```

It prints both workspaces and both radar URLs (ports 7777 and 7778). Open
each radar in a browser. Then either drive two agents by hand, one terminal
per workspace:

```bash
cd rehearsal/ada   && claude    # "Find the agent next to you and build a two-player game with it."
cd rehearsal/grace && claude    # "Work with the agent next to you. It will propose the plan."
```

or headless, timed, with logs:

```bash
scripts/headless_rehearsal.sh ./rehearsal
```

One headless run on 2026-10-28, over loopback: the agents went from first
prompt to both sending `done` in 237 seconds, with every message delivered.
They followed the protocol step for step: propose, accept, one file each
way, a review each way, and both board tasks done. Their game was then
played to a win across the two pages. The two agents together cost about
3.3 USD at list price.

When the game exists, open `http://127.0.0.1:7777/game/` and
`http://127.0.0.1:7778/game/` and play.

## The demo on two Macs

On each laptop, with a different name and workspace:

```bash
.venv/bin/offband init ~/offband-ada --name Ada
.venv/bin/offband up   --name Ada --workspace ~/offband-ada     # Bluetooth only by default
```

Then open the radar at `http://127.0.0.1:7777/` and start `claude` in the
workspace.

Things that matter on real hardware:

- **Start `offband up` from a Terminal window on the Mac itself, never over
  ssh.** macOS grants Bluetooth only to a process in the GUI session. A
  service started over ssh is denied, and a denied peripheral can hang its
  start.
- **Keep the default `--transport ble`.** It turns the SDK's LAN transport
  off. With Bluetooth on, macOS can bring up a Wi-Fi link between the
  laptops (AWDL) and a LAN link would form by itself, which would make the
  Bluetooth claim false. The radar's link badge shows the carrier of every
  delivered message.
- **No shared network.** Each laptop needs the internet for its own model
  calls. Put each on a different phone hotspot so nothing connects the two
  except Bluetooth. Never caption this "offline" or "airplane mode": the
  agents need the internet, they just never use it to reach each other.
- **Pair once before filming.** Identities and sessions persist under
  `~/.offband/<name>`, so after the first pairing a restart reconnects in
  about a second.
- **Restart both sides together.** In earlier Mac to Mac runs, restarting one
  side's service sometimes left the other side's Bluetooth link stale for
  minutes, with messages lost one way. If you restart one, restart both, and
  check that a message delivers each way before you roll.

## Filming checklist

1. Each laptop on its own hotspot. The Wi-Fi menu is on screen once.
2. `offband up` on both, from Terminal. Radar shows the other agent with
   "Encrypted session up" and "over Bluetooth".
3. Claude Code beside the radar on each screen, in the workspace.
4. One prompt on laptop A: "Find the agent next to you and build a
   two-player game with it." Laptop B: "Work with the agent next to you."
5. Time-lapse the build. The subtitles on the radar carry the story.
6. Open `/game/` on both and play.
7. End card: "No server. No shared network. End-to-end encrypted (MLS)."

## Tests

```bash
.venv/bin/python -m pytest
```

The suite starts two real SDK services linked over loopback and drives the
whole exchange: discovery and naming, propose and accept, concurrent board
edits merging, a file landing at the same path, game moves reaching the game
and not the inbox, and the daemon's request guard.

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

## Known issues

- **The first key exchange can be lost when both sides dial at once (SDK
  0.31.0).** With static peer streams in both directions, one stream
  supersedes the other, and the key package sent on the superseded stream is
  lost. Both sides then report `NoKeyPackage` until a queued message makes the
  engine retry about 30 seconds later. Three of eight fresh pairings hit it,
  every one with a supersede in the log, and none without. `offband
  rehearse` dials one way and paired ten of ten. Offband also sends its hello
  after 4 seconds without a session, which starts that retry. It is not yet
  known whether two Bluetooth centrals that connect to each other at once
  hit the same race.
- **Bluetooth needs the GUI session on macOS.** See the demo section.

## Layout

```
offband/            the package: node, daemon, MCP server, CLI, service launcher
offband/web/        the radar, the game bridge (offband.js), the waiting page
offband/agent/      OFFBAND.md, the protocol the agents follow
examples/reference-game/   a Connect Four that follows the contract, as a fallback
scripts/            the two-node spike and the headless rehearsal
tests/              the two-node integration test
```
