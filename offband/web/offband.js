// Offband game bridge. Load it from the game page with
//   <script src="/offband.js"></script>
// then:
//   const link = await Offband.connect();
//   link.me, link.peer        -> {name, address}
//   link.first                -> true on exactly one of the two laptops
//   link.onMessage((msg, from) => { ... })
//   await link.send({type: "move", col: 3, seq: 1})
//
// A game is between two laptops. With one other agent nearby it is the
// opponent; with several, open the page as /game/?vs=Grace or pick one from
// the list the bridge shows. Moves from anyone but the opponent are dropped.
//
// Messages travel as end-to-end encrypted SDK messages over the same link
// the agents use (Bluetooth in the demo). They arrive in order, once.
(function () {
  "use strict";

  async function info(vs) {
    const r = await fetch("/api/game" + (vs ? "?vs=" + encodeURIComponent(vs) : ""),
      { headers: { "X-Offband": "1" } });
    if (!r.ok) throw new Error("Offband daemon not reachable");
    return r.json();
  }

  // Shown only while several agents are nearby and none was chosen.
  function picker(agents) {
    let el = document.getElementById("offband-picker");
    if (!el) {
      el = document.createElement("div");
      el.id = "offband-picker";
      el.style.cssText = "position:fixed;inset:0;display:flex;align-items:center;justify-content:center;" +
        "background:rgba(10,20,30,.82);z-index:9999;font:600 22px/1.4 system-ui,sans-serif;color:#fff";
      document.body.appendChild(el);
    }
    el.innerHTML = "";
    const box = document.createElement("div");
    box.style.cssText = "display:flex;flex-direction:column;gap:12px;padding:24px;max-width:90vw";
    const title = document.createElement("div");
    title.textContent = "Play against";
    box.appendChild(title);
    for (const a of agents) {
      const link = document.createElement("a");
      link.href = "?vs=" + encodeURIComponent(a.name);
      link.textContent = a.name;
      link.style.cssText = "display:block;padding:12px 20px;border-radius:8px;background:#fff;color:#142230;text-decoration:none";
      box.appendChild(link);
    }
    el.appendChild(box);
  }

  async function connect(options) {
    const waitMs = (options && options.waitMs) || 120000;
    const vs = (options && options.peer) || new URLSearchParams(location.search).get("vs");
    const deadline = Date.now() + waitMs;
    let state = await info(vs);
    while (!state.peer && (vs || state.agents.length < 2) && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1000));
      state = await info(vs);
    }
    if (!state.peer && state.agents.length > 1) {
      picker(state.agents);
      return new Promise(() => {}); // the choice reloads the page
    }
    if (!state.peer) throw new Error("No Offband peer nearby");

    const opponent = state.peer.address;
    const handlers = [];
    const source = new EventSource("/api/game/stream");
    source.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.address !== opponent) return;
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
          body: JSON.stringify({ peer: opponent, payload }),
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
