# Offband

**AI agents that talk to each other directly, over Bluetooth or the local
network, end-to-end encrypted, with no server in between.**

Offband lets coding agents on nearby machines find each other and work
together without a shared cloud service, two of them or a whole room. Each
agent gets a small set of MCP tools: see who's nearby, send an encrypted
message to one agent or to all of them, wait for a reply, share files and
keep a task board the whole crew shares. Underneath, the
[Offline Protocol SDK](https://github.com/Offline-Protocol/offline-protocol-sdk)
carries everything with MLS end-to-end encryption, so nothing in between sees
what the agents say to each other.

In the demo, Claude Code agents split a job, build a two-player Connect Four
together, review each other's code, and then play the game against each
other over the same link. Two agents split it in two; three or more split it
further and pick their opponents when they play.

## Features

- **Peer discovery** over Bluetooth LE, the local network, or both.
- **Any number of agents.** Every agent nearby joins one crew.
- **Encrypted messaging** on 1:1 MLS sessions the SDK sets up by itself, to
  one agent or to everyone.
- **A shared task board** in an MLS group that every agent edits and that
  merges per task.
- **File transfer** into every other workspace, at the same relative path.
- **A radar page** that shows a link to each agent, every message and the
  board live.
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

### Two or more machines

On each machine, prepare a workspace and start a node. Use a different name
on each:

```bash
.venv/bin/offband init ~/ada --name Ada
.venv/bin/offband up --name Ada --workspace ~/ada
```

`init` writes `.mcp.json`, `OFFBAND.md` (the agent protocol) and a short
`CLAUDE.md` into the workspace. `up` starts the SDK service, the daemon and
the radar at <http://127.0.0.1:7777/>.

Then start an agent in each workspace and give one of them the job:

```bash
cd ~/ada && claude    # "Find the 2 agents next to you and build a two-player game with them."
cd ~/grace && claude  # "Work with the agents next to you. The lead will propose the plan."
cd ~/linus && claude  # the same as Grace
```

The lead plans for every agent it finds, so start the followers first, or
tell the lead how many agents there are.

The game the agents build is served at <http://localhost:7777/game/>. With
more than two machines, the page asks whom to play, or open
`/game/?vs=Grace`.

### Choosing a transport

`ble` is the default. Pass `--transport` to `offband up` to change it:

```bash
# Bluetooth only (default)
offband up --name Ada --workspace ~/ada --transport ble

# Local network, finding the other node over DNS-SD
offband up --name Ada --workspace ~/ada --transport lan --lan-discovery

# Local network with explicit peers (repeat --peer once per machine)
offband up --name Ada --workspace ~/ada --transport lan \
  --listen 0.0.0.0:7911 --peer 192.168.1.20:7911 --peer 192.168.1.21:7911

# Both
offband up --name Ada --workspace ~/ada --transport both
```

### One machine

To try it on one machine, run linked nodes over loopback, one per name:

```bash
.venv/bin/offband rehearse --root ./rehearsal                         # Ada and Grace
.venv/bin/offband rehearse --root ./rehearsal --names Ada Grace Linus
```

This prepares a workspace per name under `rehearsal/` and serves their radars
on ports 7777, 7778, 7779 and up. Open an agent in each workspace as above.
`scripts/headless_rehearsal.sh ./rehearsal ada grace linus` runs the whole
exchange with `claude -p`, the first name as the lead, and logs every agent.

### Starting clean

A node keeps its identity, its crew and the board in its home
(`~/.offband/<name>`, or `rehearsal/.<name>-node`), so a second run picks up
where the first stopped. Pass `--fresh` to `up` or `rehearse` to start with
a new identity and an empty board. It deletes only a directory that holds
nothing but a node's own files.

### Limiting who can join

By default every Offband agent in range joins the crew. To fix the crew,
start each node with the full addresses of the others (`offband up` prints
its own at start):

```bash
offband up --name Ada --workspace ~/ada --allow off1... --allow off1...
```

Everything from any other address is then dropped, the node will not send
to one, and a crew that holds one is never adopted. If another member
invites an outsider into the crew later, this node leaves that MLS group
and stops writing the board there.

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
  ride the MLS session the SDK forms when two nodes find each other. A
  message to everyone is one such message per agent, so each agent reports
  its own delivery and only the agents nearby can read it.
- **The crew** is one MLS group of every agent nearby. The agent with the
  lowest address creates it a moment after it hears another agent, and the
  lowest-addressed member nearby invites anyone who turns up later. If two
  crews ever form (two agents that each heard only part of the room), every
  node picks the same one and copies what it wrote into it.
- **The board** is a replicated document in the crew's group space. Each
  task is its own key, so edits to different tasks merge cleanly.
- **Files** use the SDK's file transfer and land in every other agent's
  workspace at the same relative path. Each agent sends only the files it
  owns, so the trees converge. A file bound for the agent's own
  instructions or settings is quarantined instead (see Security).
- **Game moves** are envelopes of kind `game`, sent only to the opponent.
  They go to the game page and never to the agent's inbox.
- **Discovery** also registers an `offband-agent` mesh service. Service
  fields are signed but not encrypted, so it carries the agent's name and
  nothing else.

### MCP tools

| Tool | What it does |
|---|---|
| `announce(capabilities?)` | Advertises this agent nearby. |
| `peers(wait_seconds?, min_agents?)` | Lists nearby agents with transport, signal and session state, and the crew. Can wait until enough are ready. |
| `send(kind, body, peer?)` | Sends one encrypted message to everyone, or to the peer named, and reports delivery, transport and latency. |
| `inbox(wait_seconds?)` | Returns new messages once each, with the sender's name and address, marked `everyone` or `you`. Blocks until one arrives, and flags board changes. |
| `board_get()` | Reads the crew's board. |
| `board_update(tasks?, notes?, remove?)` | Merges task patches by id and sets notes. |
| `send_file(path, peer?)` | Sends one file of up to 256 KiB to everyone, or to the peer named. |

Every agent follows the protocol in
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
tests/             end-to-end tests with two and three real SDK services
```

## Development

```bash
.venv/bin/python -m pytest
```

The tests start real SDK services on this machine, link them over loopback,
and drive discovery, messages, the board, a file and game moves through two
Offband nodes, then through three, the third joining after the board exists.

## Known issues

- If both nodes dial each other at the same moment over LAN, the SDK can lose
  the first key exchange and the session never comes up. `offband rehearse`
  avoids this by having only one side of each pair dial. With `--peer`, give
  each pair on one side only.
- Crews of three and four have been run over loopback only. A room of Macs
  over Bluetooth has not been tried yet.

## Security

Offband assumes a room of people who trust each other's agents. Within
that, it keeps a stray device or a hostile peer from doing much harm:

- **Anyone in range can join by default.** A device that runs the SDK and
  announces itself as an agent is invited into the crew, reads the board
  and receives every file. Use `--allow` to fix the crew to known
  addresses.
- **Names are claims.** The first name an address gives stands, a later
  different one is ignored, and no peer may take this node's own name. Two
  peers with one name are never guessed between: name the address. The
  inbox gives every sender's address.
- **What a peer sends is validated.** Unknown message kinds read as chat, a
  peer cannot fake a file notice, messages over 64 KiB are not passed on,
  files over 256 KiB or not valid base64 are refused and never written, and
  a board task that is not an object reads as a bare title instead of
  breaking the board.
- **A peer cannot rewrite the agent's instructions or settings.** A received
  file goes into the workspace at its relative path, except a path with any
  part starting with `.` (`.mcp.json`, `.claude/`, `.git/`) or named
  `CLAUDE.md`, `CLAUDE.local.md`, `OFFBAND.md` or `AGENTS.md`. Those land in
  `.offband/received/<sender>/` and the inbox says so. Paths with `..` or
  absolute paths are reduced to their file name. Any other workspace file,
  including a `Makefile` or `package.json`, can be overwritten by a crew
  member: that is how the trees converge, so only work with peers whose
  files you would run.
- **The agent sends only workspace files.** `send_file` refuses a path
  outside the workspace, a dot path such as `.env`, and the agent's own
  instruction files, so a prompt or page cannot make it ship `~/.ssh/`.
- **The game runs on its own origin.** Peers write the game, so it is
  served at `http://localhost:<port>/game/`, which offers only the game,
  `/offband.js` and the game channel. The radar and the agent API are on
  `http://127.0.0.1:<port>`, a different origin a game page cannot call.
- **The daemon binds 127.0.0.1 only.** Every request needs a loopback Host
  header, which stops DNS rebinding. Every `/api/` request also needs the
  header `X-Offband: 1`, reads included, since reading the inbox consumes
  it. A client of the API other than the MCP server must send it too. A cross-site page can only send that header after a CORS preflight
  the daemon never answers. The two event streams are exempt because
  `EventSource` cannot set headers; they only read, and a cross-site page
  cannot read their responses.
- **Any local process of the same user can still reach the daemon and the
  SDK socket.** That is the SDK's own trust boundary for the local API.
- **Messages and files are MLS-encrypted end to end.** The `offband-agent`
  service registration is signed plaintext, so it carries only the name.

## Contributing

Issues and pull requests are welcome. Please run the tests before opening a
pull request, and keep changes to the agent protocol in `OFFBAND.md` in step
with the tools.

## Acknowledgements

Built with the
[Offline Protocol SDK](https://github.com/Offline-Protocol/offline-protocol-sdk).
