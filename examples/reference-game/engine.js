// Connect Four rules, as the contract in OFFBAND.md specifies. No DOM.
(function () {
  "use strict";
  const ROWS = 6;
  const COLS = 7;

  function newGame() {
    return {
      board: Array.from({ length: ROWS }, () => Array(COLS).fill(0)),
      turn: 1,
      moves: 0,
      last: null,
    };
  }

  function play(state, col) {
    if (!Number.isInteger(col) || col < 0 || col >= COLS) return { ok: false, reason: "no such column" };
    if (winner(state) !== 0) return { ok: false, reason: "the game is over" };
    let row = -1;
    for (let r = ROWS - 1; r >= 0; r--) {
      if (state.board[r][col] === 0) { row = r; break; }
    }
    if (row < 0) return { ok: false, reason: "that column is full" };
    const board = state.board.map((line) => line.slice());
    board[row][col] = state.turn;
    return {
      ok: true,
      row,
      state: { board, turn: state.turn === 1 ? 2 : 1, moves: state.moves + 1, last: { row, col } },
    };
  }

  function winner(state) {
    const b = state.board;
    const dirs = [[0, 1], [1, 0], [1, 1], [1, -1]];
    for (let r = 0; r < ROWS; r++) {
      for (let c = 0; c < COLS; c++) {
        const p = b[r][c];
        if (!p) continue;
        for (const [dr, dc] of dirs) {
          let n = 1;
          while (n < 4) {
            const rr = r + dr * n, cc = c + dc * n;
            if (rr < 0 || rr >= ROWS || cc < 0 || cc >= COLS || b[rr][cc] !== p) break;
            n++;
          }
          if (n === 4) return p;
        }
      }
    }
    return state.moves >= ROWS * COLS ? "draw" : 0;
  }

  window.Engine = { ROWS, COLS, newGame, play, winner };
})();
