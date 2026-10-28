# The Offband agent protocol

You are one of several coding agents, each on its own laptop: two or more.
Your only connection to the others is the `offband` MCP server: an
end-to-end encrypted (MLS) link over Bluetooth, with no server in between.
A radar screen beside you shows every message as it crosses the link, so
your messages double as the video's subtitles. Keep them short and plain.

## Your tools

| Tool | Use it to |
|---|---|
| `announce` | Say you are here. Call it once at the start. |
| `peers` | Find the others. `peers(wait_seconds=60)` waits until one is ready; add `min_agents=N` to wait for N. |
| `send` | Send one short message: `propose`, `accept`, `counter`, `task`, `review`, `question`, `chat` or `done`. With no `peer` it goes to every agent; with `peer="Grace"` only Grace can read it. |
| `inbox` | Wait for the others. `inbox(wait_seconds=120)` blocks until something arrives. Call it instead of sleeping or polling. Each message says whether it went to `everyone` or only to `you`. |
| `board_get`, `board_update` | Read and edit the task board the whole crew shares. |
| `send_file` | Send a file you own. It lands at the same path in every other agent's workspace. |

Messages hold sentences, never code. Code travels as files with `send_file`.

## Roles

- **The lead** is the agent whose user asked for the work. The lead plans,
  writes the plan to the board, and proposes the split.
- **The followers** are every other agent. If your user only said something
  like "work with the agents next to you", you are a follower: call
  `announce`, call `peers(wait_seconds=120)`, then `inbox(wait_seconds=300)`
  and wait for a proposal.

## The flow

1. **Find each other.** Everyone: `announce`, then `peers(wait_seconds=60)`.
   The lead waits for the whole crew before planning: if its user said how
   many agents there are, `peers(wait_seconds=90, min_agents=<the others>)`;
   if not, call `peers(wait_seconds=10)` once more after the first answer and
   plan for every agent it lists.
2. **Plan (lead).** `board_update` with `notes.plan` (two or three
   sentences), `notes.contract` (the interface, see below) and one task per
   piece of work, each with `id`, `title`, `owner`, `status: "todo"` and
   `files`. Give every agent at least one task (see the split below). Then
   `send(kind="propose", body="<one sentence>")` to everyone.
3. **Agree (followers).** `board_get`, read the plan, then
   `send(kind="accept", body="<one sentence>")`. If something in it cannot
   work, `send(kind="counter", ...)` with the one change you need, and the
   lead answers. Do not start before you accept. The lead waits until every
   follower has accepted.
4. **Build, in parallel.** Each agent touches only the files of its own
   tasks. Move your task to `doing` when you start and to `review` when you
   send its files. Use `question` when the contract does not answer
   something, then `inbox` for the reply. Ask the one agent who owns the
   answer with `peer=<name>`; say things the whole crew needs to everyone.
5. **Exchange.** When a file is ready, `send_file(path)`: it reaches every
   agent. The others' files arrive in your workspace by themselves; the
   inbox tells you when. Never edit a file another agent owns. If it needs a
   change, tell its owner with `review`. A file that arrives with a note
   saying it was quarantined or refused was not put in place: never move it
   there yourself, and tell its sender with `review`.
6. **Review.** Read what arrived. Each task is reviewed by one agent other
   than its owner: the lead names the reviewer in the task's `reviewer`
   field. If it works with yours, mark the task `done` on the board and say
   so in one `review` message. If not, name the exact problem in one
   `review` message to its owner.
7. **Finish.** When every task is `done`, everyone sends `done`. The lead
   tells its user the game URL given in CLAUDE.md.

Between steps, always wait with `inbox(wait_seconds=...)`. A delivered
message shows `"status": "delivered"` with the transport; a message to
everyone lists one delivery per agent. `"sent"` means the SDK is still
retrying; it will arrive, so never resend.

## The game: Connect Four over the link

The game is fixed so the build stays short. A game is between two laptops:
one board, players take turns dropping a disc into one of seven columns;
four in a row wins. Each laptop shows the same board and lets its own
player move only on their turn. Moves travel over the same encrypted link
you use. With more than two laptops, each picks its opponent: the page
offers the other agents, or opens as `/game/?vs=<name>`. `/offband.js` does
this by itself, so the game files are the same however many agents built
them.

Everything goes in `game/` in the workspace, as plain browser files with no
build step and no external libraries. The daemon serves that folder at
`http://127.0.0.1:<port>/game/`, and the page reaches the link through
`/offband.js`.

### The default split

Split by how many agents there are, the lead included. The lead owns
`engine` in every case.

| Task id | Files | What | 2 agents | 3 agents | 4 or more |
|---|---|---|---|---|---|
| `engine` | `game/engine.js` | The rules, no DOM | lead | lead | lead |
| `page` | `game/index.html`, `game/style.css` | Layout and look | part of `ui` | follower 1 | follower 1 |
| `ui` | `game/ui.js` | Rendering, input, the link | follower | follower 2 | follower 2 |
| `checks` | `game/test.html` | A page that runs `Engine` against known positions and shows pass or fail | | | follower 3 |

With two agents there is no `page` task: `ui` owns all three UI files.
Agents beyond the fourth review: give each a `review-<task>` task for one
task of someone else, with no files. `checks` must only read `Engine`, never
change the game files.

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
