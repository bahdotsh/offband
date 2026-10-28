// Connect Four UI over the Offband link, as the contract in OFFBAND.md specifies.
(async function () {
  "use strict";
  const boardEl = document.getElementById("board");
  const statusEl = document.getElementById("status");
  const again = document.getElementById("again");
  let state = Engine.newGame();
  let link;
  let mine;

  function name(player) {
    if (!link) return player === 1 ? "Player 1" : "Player 2";
    return player === mine ? link.me.name : link.peer.name;
  }

  function render(dropAt) {
    boardEl.innerHTML = "";
    const result = Engine.winner(state);
    const myTurn = link && result === 0 && state.turn === mine;
    for (let r = 0; r < Engine.ROWS; r++) {
      for (let c = 0; c < Engine.COLS; c++) {
        const cell = document.createElement("button");
        const v = state.board[r][c];
        cell.className = "cell" + (v ? " p" + v : "");
        if (state.last && state.last.row === r && state.last.col === c) cell.classList.add("last");
        if (dropAt && dropAt.row === r && dropAt.col === c) cell.classList.add("drop");
        cell.setAttribute("aria-label", `row ${r + 1}, column ${c + 1}, ${v ? name(v) : "empty"}`);
        cell.disabled = !myTurn;
        cell.addEventListener("click", () => move(c));
        boardEl.appendChild(cell);
      }
    }
    statusEl.classList.toggle("go", !!myTurn);
    if (!link) statusEl.textContent = "Connecting to the other laptop";
    else if (result === "draw") statusEl.textContent = "Draw";
    else if (result) statusEl.textContent = `${name(result)} wins`;
    else statusEl.textContent = myTurn ? "Your move" : `Waiting for ${link.peer.name}`;
    again.hidden = result === 0;
  }

  async function move(col) {
    const next = Engine.play(state, col);
    if (!next.ok) return;
    state = next.state;
    render(state.last);
    await link.send({ type: "move", col, seq: state.moves });
  }

  function receive(msg) {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "reset") {
      state = Engine.newGame();
      render();
    } else if (msg.type === "move" && msg.seq === state.moves + 1) {
      const next = Engine.play(state, msg.col);
      if (next.ok) {
        state = next.state;
        render(state.last);
      }
    }
  }

  again.addEventListener("click", async () => {
    state = Engine.newGame();
    render();
    await link.send({ type: "reset" });
  });

  render();
  link = await Offband.connect();
  mine = link.first ? 1 : 2;
  document.getElementById("p1").textContent = name(1);
  document.getElementById("p2").textContent = name(2);
  link.onMessage(receive);
  render();
})();
