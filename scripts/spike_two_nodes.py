"""Spike: two services on one host over loopback peer streams. Checks hello,
neighbors, session, messages both ways, send_file, and 1:1 document sync."""
import asyncio, base64, json, sys, time
from pathlib import Path


from offband.rpc import LocalApi
from offband.service import ServiceSpec

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/offband-spike")

async def main():
    a = ServiceSpec(ROOT / "a", transport="lan", listen="127.0.0.1:7901", peers=["127.0.0.1:7902"])
    # Only one side dials: two nodes dialling each other at once can lose
    # the first key exchange (README, Known issues).
    b = ServiceSpec(ROOT / "b", transport="lan", listen="127.0.0.1:7902", peers=[])
    pa, pb = a.spawn(), b.spawn()
    try:
        ca, cb = LocalApi(str(a.socket_path), "offband"), LocalApi(str(b.socket_path), "offband")
        evs = {"a": [], "b": []}
        ca.on_event = lambda e: evs["a"].append(e)
        cb.on_event = lambda e: evs["b"].append(e)
        t0 = time.time()
        print("hello a", await ca.connect()); print("hello b", await cb.connect())
        A, B = ca.local_address, cb.local_address
        for _ in range(60):
            sa = await ca.call("get_establishment_state", {"peer_id": B})
            sb = await cb.call("get_establishment_state", {"peer_id": A})
            if sa == sb == "SessionConfirmed": break
            await asyncio.sleep(0.5)
        print("session", sa, sb, round(time.time()-t0,1), "s")
        print("topology a", json.dumps(await ca.call("get_topology"))[:400])
        print("transports a", await ca.call("get_active_transports"))
        mid = await ca.call("send_message", {"recipient": B, "content": json.dumps({"k":"chat","t":"hi"}), "priority": "Medium"})
        mid2 = await cb.call("send_message", {"recipient": A, "content": "pong", "priority": "Medium"})
        fid = await ca.call("send_file", {"recipient": B, "file_data": base64.b64encode(b"print('x')\n"*500).decode(), "file_name": "src__game.js"})
        await ca.call("data.create_doc", {"space_id": B, "doc_id": "board"})
        await ca.call("data.map_set", {"space_id": B, "doc_id": "board", "collection": "tasks", "key": "t1", "value_json": json.dumps({"kind":"text","value":"from a"})})
        await ca.call("data.flush", {"space_id": B, "doc_id": "board"})
        await cb.call("data.create_doc", {"space_id": A, "doc_id": "board"})
        await cb.call("data.map_set", {"space_id": A, "doc_id": "board", "collection": "tasks", "key": "t2", "value_json": json.dumps({"kind":"text","value":"from b"})})
        await cb.call("data.flush", {"space_id": A, "doc_id": "board"})
        await asyncio.sleep(8)
        print("doc a", await ca.call("data.doc_json", {"space_id": B, "doc_id": "board"}))
        print("doc b", await cb.call("data.doc_json", {"space_id": A, "doc_id": "board"}))
        for side in "ab":
            print(f"--- events {side}")
            for e in evs[side]:
                e2 = {k: (v[:60]+"…" if isinstance(v,str) and len(v)>60 else v) for k,v in e.items()}
                print(" ", json.dumps(e2)[:300])
        await ca.close(); await cb.close()
    finally:
        pa.terminate(); pb.terminate(); pa.wait(); pb.wait()

asyncio.run(main())
