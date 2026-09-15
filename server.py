import asyncio
import json
import os
import threading
import time
from pathlib import Path
from aiohttp import web, WSMsgType

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "10000"))
BASE_DIR = Path(__file__).resolve().parent

PALETTE = ['#e04f5f', '#4fa3e0', '#e0b04f', '#6fbf7f', '#b06fe0', '#e0824f', '#4fd6c8', '#e04fd0']
CURSOR_TTL = 3.0
PLAYER_TTL = 12.0
REVEAL_DURATION = 2.5

STATE = {
    "board": {"cells": {f"{ci}_{vi}": {"answered": False} for ci in range(5) for vi in range(5)}, "activeClue": None},
    "players": {},
    "phase": "lobby",
    "cursors": {},
}
LOCK = threading.Lock()
CLIENTS = set()


def cleanup_stale(now=None):
    now = now or time.time()
    stale_players = [pid for pid, p in STATE["players"].items() if now - p.get("lastSeen", 0) > PLAYER_TTL]
    for pid in stale_players:
        STATE["players"].pop(pid, None)
        STATE["cursors"].pop(pid, None)
    stale_cursors = [pid for pid, c in STATE["cursors"].items() if now - c.get("ts", 0) >= CURSOR_TTL or pid not in STATE["players"]]
    for pid in stale_cursors:
        STATE["cursors"].pop(pid, None)


def snapshot():
    with LOCK:
        cleanup_stale()
        return {
            "board": STATE["board"],
            "players": {k: {"name": v["name"], "score": v["score"], "color": v["color"]} for k, v in STATE["players"].items()},
            "phase": STATE["phase"],
            "cursors": {k: dict(v) for k, v in STATE["cursors"].items()},
        }


async def broadcast():
    if not CLIENTS:
        return
    msg = json.dumps({"type": "state", "state": snapshot()}, separators=(",", ":"))
    dead = []
    for ws in list(CLIENTS):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CLIENTS.discard(ws)


def json_response(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


async def health(request):
    return json_response({"ok": True, "transport": "websocket+http"})


async def state(request):
    pid = request.query.get("id")
    with LOCK:
        if pid in STATE["players"]:
            STATE["players"][pid]["lastSeen"] = time.time()
    return json_response(snapshot())


async def apply_action(path, data):
    changed = False
    with LOCK:
        now = time.time()
        if path == "/api/join":
            pid = data.get("id")
            name = (data.get("name") or "Citizen").strip()[:18] or "Citizen"
            if not pid:
                return {"error": "missing id"}, 400, False
            used = {p["color"] for p in STATE["players"].values()}
            color = next((c for c in PALETTE if c not in used), PALETTE[len(STATE["players"]) % len(PALETTE)])
            existing = STATE["players"].get(pid)
            STATE["players"][pid] = {"name": name, "score": existing["score"] if existing else 0,
                                     "color": existing["color"] if existing else color, "lastSeen": now}
            return {"ok": True, "color": STATE["players"][pid]["color"]}, 200, True

        if path == "/api/cursor":
            pid = data.get("id")
            if pid in STATE["players"]:
                try:
                    x = max(0.0, min(1.0, float(data.get("x", 0))))
                    y = max(0.0, min(1.0, float(data.get("y", 0))))
                except (TypeError, ValueError):
                    x, y = 0.0, 0.0
                p = STATE["players"][pid]
                p["lastSeen"] = now
                STATE["cursors"][pid] = {"name": p["name"], "color": p["color"], "x": x, "y": y, "ts": now}
                changed = True
            return {"ok": True}, 200, changed

        if path == "/api/leave":
            pid = data.get("id")
            if pid:
                STATE["players"].pop(pid, None)
                STATE["cursors"].pop(pid, None)
            return {"ok": True}, 200, True

        if path == "/api/open_clue":
            try:
                ci, vi = int(data.get("ci")), int(data.get("vi"))
            except (TypeError, ValueError):
                return {"error": "invalid clue"}, 400, False
            key = f"{ci}_{vi}"
            cell = STATE["board"]["cells"].get(key)
            if STATE["phase"] == "playing" and cell is not None and not cell["answered"] and STATE["board"]["activeClue"] is None:
                STATE["board"]["activeClue"] = {"ci": ci, "vi": vi, "buzzedId": None, "buzzedName": None, "lockedOut": [], "revealAnswer": False}
                changed = True
            return {"ok": True}, 200, changed

        if path == "/api/buzz":
            ac = STATE["board"]["activeClue"]
            pid = data.get("id")
            if ac and pid in STATE["players"] and not ac["buzzedId"] and pid not in ac["lockedOut"]:
                STATE["players"][pid]["lastSeen"] = now
                ac["buzzedId"] = pid
                ac["buzzedName"] = STATE["players"][pid]["name"]
                changed = True
            return {"ok": True}, 200, changed

        if path == "/api/judge":
            ac = STATE["board"]["activeClue"]
            correct = bool(data.get("correct"))
            if ac and ac["buzzedId"] in STATE["players"]:
                value = (ac["vi"] + 1) * 100
                pid = ac["buzzedId"]
                STATE["players"][pid]["score"] += value if correct else -value
                if correct:
                    STATE["board"]["cells"][f"{ac['ci']}_{ac['vi']}"] = {"answered": True}
                    STATE["board"]["activeClue"] = None
                else:
                    ac["lockedOut"].append(pid)
                    ac["buzzedId"] = None
                    ac["buzzedName"] = None
                    if len(ac["lockedOut"]) >= max(len(STATE["players"]), 1):
                        ac["revealAnswer"] = True
                        STATE["board"]["cells"][f"{ac['ci']}_{ac['vi']}"] = {"answered": True}
                        clue_ref = ac
                        async def clear_later():
                            await asyncio.sleep(REVEAL_DURATION)
                            with LOCK:
                                if STATE["board"]["activeClue"] is clue_ref:
                                    STATE["board"]["activeClue"] = None
                            await broadcast()
                        asyncio.create_task(clear_later())
                changed = True
            return {"ok": True}, 200, changed

        if path == "/api/skip":
            ac = STATE["board"]["activeClue"]
            if ac:
                STATE["board"]["cells"][f"{ac['ci']}_{ac['vi']}"] = {"answered": True}
                ac["revealAnswer"] = True
                clue_ref = ac
                async def clear_skipped():
                    await asyncio.sleep(REVEAL_DURATION)
                    with LOCK:
                        if STATE["board"]["activeClue"] is clue_ref:
                            STATE["board"]["activeClue"] = None
                    await broadcast()
                asyncio.create_task(clear_skipped())
                changed = True
            return {"ok": True}, 200, changed

        if path == "/api/start":
            cleanup_stale()
            if not STATE["players"]:
                return {"error": "no players"}, 400, False
            STATE["phase"] = "playing"
            return {"ok": True}, 200, True

        if path == "/api/reset":
            STATE["board"] = {"cells": {f"{ci}_{vi}": {"answered": False} for ci in range(5) for vi in range(5)}, "activeClue": None}
            for p in STATE["players"].values():
                p["score"] = 0
            STATE["phase"] = "lobby"
            STATE["cursors"] = {}
            return {"ok": True}, 200, True

    return {"error": "not found"}, 404, False


async def api_action(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    result, status, changed = await apply_action(request.path, data)
    if changed:
        await broadcast()
    return json_response(result, status)


async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    CLIENTS.add(ws)
    await ws.send_str(json.dumps({"type": "state", "state": snapshot()}, separators=(",", ":")))
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                if data.get("type") == "cursor":
                    pid = data.get("id")
                    with LOCK:
                        if pid in STATE["players"]:
                            p = STATE["players"][pid]
                            p["lastSeen"] = time.time()
                            try:
                                x = max(0.0, min(1.0, float(data.get("x", 0))))
                                y = max(0.0, min(1.0, float(data.get("y", 0))))
                            except (TypeError, ValueError):
                                x, y = 0.0, 0.0
                            STATE["cursors"][pid] = {"name": p["name"], "color": p["color"], "x": x, "y": y, "ts": time.time()}
                    # Broadcast cursor-only updates immediately; no polling needed.
                    await broadcast()
                elif data.get("type") == "action":
                    result, status, changed = await apply_action(data.get("path", ""), data.get("data") or {})
                    await ws.send_str(json.dumps({"type": "actionResult", "id": data.get("requestId"), "result": result, "status": status}))
                    if changed:
                        await broadcast()
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                break
    finally:
        CLIENTS.discard(ws)
    return ws


async def index(request):
    return web.FileResponse(BASE_DIR / "index.html", headers={"Cache-Control": "public, max-age=60"})


async def on_startup(app):
    app["cleanup_task"] = asyncio.create_task(cleanup_loop())


async def on_cleanup(app):
    task = app.get("cleanup_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def cleanup_loop():
    while True:
        await asyncio.sleep(2)
        before = json.dumps(STATE.get("cursors", {}), sort_keys=True)
        with LOCK:
            cleanup_stale()
        after = json.dumps(STATE.get("cursors", {}), sort_keys=True)
        if before != after:
            await broadcast()


app = web.Application()
app.router.add_get("/api/health", health)
app.router.add_get("/api/state", state)
app.router.add_get("/ws", websocket_handler)
for route in ["/api/join", "/api/cursor", "/api/leave", "/api/open_clue", "/api/buzz", "/api/judge", "/api/skip", "/api/start", "/api/reset"]:
    app.router.add_post(route, api_action)
app.router.add_get("/", index)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host=HOST, port=PORT, access_log=None)
