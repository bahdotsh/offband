# The Offband agent protocol

You are one of two coding agents on two laptops. Your only connection to the
other agent is the `offband` MCP server: an end-to-end encrypted (MLS) link
over Bluetooth, with no server in between. A radar screen beside you shows
every message as it crosses the link, so your messages double as the video's
subtitles. Keep them short and plain.

## Your tools

| Tool | Use it to |
|---|---|
| `announce` | Say you are here. Call it once at the start. |
| `peers` | Find your partner. `peers(wait_seconds=60)` waits until one is ready. |
| `send` | Send one short message: `propose`, `accept`, `counter`, `task`, `review`, `question`, `chat` or `done`. |
| `inbox` | Wait for your partner. `inbox(wait_seconds=120)` blocks until something arrives. Call it instead of sleeping or polling. |
| `board_get`, `board_update` | Read and edit the task board you share with your partner. |
| `send_file` | Send a file you own. It lands at the same path in your partner's workspace. |

Messages hold sentences, never code. Code travels as files with `send_file`.

## Roles

- **The lead** is the agent whose user asked for the work. The lead plans,
  writes the plan to the board, and proposes the split.
- **The follower** is the other agent. If your user only said something like
  "work with the agent next to you", you are the follower: call `announce`,
  call `peers(wait_seconds=120)`, then `inbox(wait_seconds=300)` and wait for
  a proposal.

## The flow

1. **Find each other.** Both: `announce`, then `peers(wait_seconds=60)`.
2. **Plan (lead).** `board_update` with `notes.plan` (two or three
   sentences), `notes.contract` (the interface, see below) and one task per
   piece of work, each with `id`, `title`, `owner`, `status: "todo"` and
   `files`. Then `send(kind="propose", body="<one sentence>")`.
3. **Agree (follower).** `board_get`, read the plan, then
   `send(kind="accept", body="<one sentence>")`. If something in it cannot
   work, `send(kind="counter", ...)` with the one change you need, and the
   lead answers. Do not start before you accept.
4. **Build, in parallel.** Each agent touches only the files of its own
   tasks. Move your task to `doing` when you start and to `review` when you
   send its files. Use `question` when the contract does not answer
   something, then `inbox` for the reply.
5. **Exchange.** When a file is ready, `send_file(path)`. Your partner's
   files arrive in your workspace by themselves; the inbox tells you when.
   Never edit a file your partner owns. If it needs a change, say so with
   `review`.
6. **Review.** Read what arrived. If it works with yours, mark your partner's
   task `done` on the board and say so in one `review` message. If not, name
   the exact problem in one `review` message.
7. **Finish.** When every task is `done`, both send `done`. The lead tells
   its user the game URL given in CLAUDE.md.

Between steps, always wait with `inbox(wait_seconds=...)`. A delivered
message shows `"status": "delivered"` with the transport. `"sent"` means the
SDK is still retrying; it will arrive, so never resend.

## The game: Connect Four over the link

The game is fixed so the build stays short. Two laptops, one board, players
take turns dropping a disc into one of seven columns; four in a row wins.
Each laptop shows the same board and lets its own player move only on their
turn. Moves travel over the same encrypted link you use.

Everything goes in `game/` in the workspace, as plain browser files with no
build step and no external libraries. The daemon serves that folder at
`http://127.0.0.1:<port>/game/`, and the page reaches the link through
`/offband.js`.

### The default split

| Task id | Owner | Files | What |
|---|---|---|---|
| `engine` | lead | `game/engine.js` | The rules, no DOM |
| `ui` | follower | `game/index.html`, `game/style.css`, `game/ui.js` | Rendering, input, the link |

### The contract (copy it into `notes.contract`)

`game/engine.js` defines one global, `window.Engine`:

```js
Engine.ROWS === 6; Engine.COLS === 7;
Engine.newGame()          // -> state
Engine.play(state, col)   // -> {ok: true, state, row} or {ok: false, reason}
                          //    never mutates the state it was given
Engine.winner(state)      // -> 0 (none yet), 1, 2, or "draw"
// state = {board: number[6][7] (row 0 is the top; 0 empty, 1 or 2),
//          turn: 1 | 2, moves: number, last: {row, col} | null}
```

`game/index.html` loads, in order: `/offband.js`, `engine.js`, `ui.js`.
`ui.js` does:

```js
const link = await Offband.connect();   // link.me, link.peer, link.first
const mine = link.first ? 1 : 2;         // player 1 moves first
link.onMessage((msg) => { /* apply the peer's move */ });
await link.send({type: "move", col, seq});   // seq = state.moves after the move
```

The wire messages are exactly these:

| Message | Meaning |
|---|---|
| `{"type": "move", "col": 0-6, "seq": n}` | A move. Apply it when `seq === state.moves + 1`; ignore a repeat. |
| `{"type": "reset"}` | Start a new game. Either side may send it after a game ends. |

The UI shows whose turn it is, both names (`link.me.name`, `link.peer.name`),
the winner, and a "Play again" button that sends `reset`. Keep it readable on
a screen recording: a large board, high contrast, player 1 and player 2 in
two clearly different colors. Do not name the carrier (Bluetooth, Wi-Fi) in
the page: the radar shows the real one, and the page cannot know it.
