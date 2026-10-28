// Offband game bridge. Load it from the game page with
//   <script src="/offband.js"></script>
// then:
//   const link = await Offband.connect();
//   link.me, link.peer        -> {name, address}
//   link.first                -> true on exactly one of the two laptops
//   link.onMessage((msg, from) => { ... })
//   await link.send({type: "move", col: 3, seq: 1})
//
// Messages travel as end-to-end encrypted SDK messages over the same link
// the agents use (Bluetooth in the demo). They arrive in order, once.
(function () {
  "use strict";

  async function info() {
    const r = await fetch("/api/game");
    if (!r.ok) throw new Error("Offband daemon not reachable");
    return r.json();
  }

  async function connect(options) {
    const waitMs = (options && options.waitMs) || 120000;
    const deadline = Date.now() + waitMs;
    let state = await info();
    while (!state.peer && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1000));
      state = await info();
    }
    if (!state.peer) throw new Error("No Offband peer nearby");

    const handlers = [];
    const source = new EventSource("/api/game/stream");
    source.onmessage = (e) => {
      const data = JSON.parse(e.data);
      for (const fn of handlers) {
        try { fn(data.payload, data.from); } catch (err) { console.error(err); }
      }
    };

    return {
      me: state.me,
      peer: state.peer,
      first: !!state.first,
      onMessage(fn) { handlers.push(fn); },
      async send(payload) {
        const r = await fetch("/api/game/send", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Offband": "1" },
          body: JSON.stringify(payload),
        });
        const body = await r.json();
        if (!r.ok) throw new Error(body.error || "send failed");
        return body;
      },
      close() { source.close(); },
    };
  }

  window.Offband = { connect };
})();
