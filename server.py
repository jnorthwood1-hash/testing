"""
SPQR Jeopardy - local multiplayer server
-----------------------------------------
No installs needed - uses only the Python standard library.
 
Run it:
    python3 server.py
 
Then:
    - You open:                 http://localhost:8000
    - Friends on your Wi-Fi open the "Friends on your network" address
      printed below when the server starts.
 
Everyone who opens that address is in the SAME game, live - the
server keeps the board, scores, and cursor positions in memory.
Press Ctrl+C to stop the server.
"""
import json
import os
import socket
import threading
import time
import webbrowser
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
 
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))
 
LOCK = threading.Lock()
PALETTE = ['#e04f5f', '#4fa3e0', '#e0b04f', '#6fbf7f', '#b06fe0', '#e0824f', '#4fd6c8', '#e04fd0']
CURSOR_TTL = 6.0          # seconds before a stale cursor is hidden
PLAYER_TTL = 8.0          # seconds before an inactive player is removed
REVEAL_DURATION = 2.5     # seconds the answer stays up after reveal
 
 
def default_board():
    return {
        "cells": {f"{ci}_{vi}": {"answered": False} for ci in range(5) for vi in range(5)},
        "activeClue": None,
    }
 
 
STATE = {
    "board": default_board(),
    "players": {},   # id -> {name, score, color, lastSeen}
    "phase": "lobby",
    "cursors": {},   # id -> {name, color, x, y, ts}
}

def cleanup_stale(now=None):
    now = now or time.time()
    stale_players = [pid for pid, p in STATE["players"].items() if now - p.get("lastSeen", 0) > PLAYER_TTL]
    for pid in stale_players:
        STATE["players"].pop(pid, None)
        STATE["cursors"].pop(pid, None)

    stale_cursors = [pid for pid, c in STATE["cursors"].items() if now - c.get("ts", 0) >= CURSOR_TTL or pid not in STATE["players"]]
    for pid in stale_cursors:
        STATE["cursors"].pop(pid, None)
 
 
def get_local_ips():
    """Return usable local IPv4 addresses, excluding loopback/link-local."""
    found = []

    def add(ip):
        if not ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip not in found:
            found.append(ip)

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            add(info[4][0])
    except Exception:
        pass

    # Also ask the OS which interface it would use for an outbound route.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        add(probe.getsockname()[0])
        probe.close()
    except Exception:
        pass

    return found or ["127.0.0.1"]
 
 
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console quiet
 
    # ---------- helpers ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
 
    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}
 
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
 
    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
 
        if path == "/api/health":
            self._send_json({"ok": True, "transport": "http-tcp"})
            return
 
        if path == "/api/state":
            with LOCK:
                now = time.time()
                query = parse_qs(urlparse(self.path).query)
                pid = (query.get("id") or [None])[0]
                if pid in STATE["players"]:
                    STATE["players"][pid]["lastSeen"] = now
                cleanup_stale(now)
                cursors = {k: dict(v) for k, v in STATE["cursors"].items()}
                players = {
                    k: {"name": v["name"], "score": v["score"], "color": v["color"]}
                    for k, v in STATE["players"].items()
                }
                self._send_json({
                    "board": STATE["board"],
                    "players": players,
                    "phase": STATE["phase"],
                    "cursors": cursors,
                })
            return
 
        # static files (index.html and anything else placed in this folder)
        if path == "/":
            path = "/index.html"
        safe_path = os.path.normpath(path).lstrip(os.sep)
        file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), safe_path)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.isfile(file_path) and os.path.commonpath([file_path, base_dir]) == base_dir:
            ext = os.path.splitext(file_path)[1]
            ctype = {
                ".html": "text/html", ".js": "application/javascript",
                ".css": "text/css", ".json": "application/json",
            }.get(ext, "application/octet-stream")
            self.send_response(200)
            if ctype.startswith("text/") or ctype == "application/javascript":
                ctype += "; charset=utf-8"
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            with open(file_path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.end_headers()
 
    # ---------- POST ----------
    def do_POST(self):
        data = self._read_json()
        path = self.path.split("?")[0]
 
        with LOCK:
            if path == "/api/health":
                self._send_json({"ok": True, "transport": "http-tcp"})
                return

            if path == "/api/join":
                pid = data.get("id")
                name = (data.get("name") or "Citizen").strip()[:18] or "Citizen"
                if not pid:
                    self._send_json({"error": "missing id"}, 400)
                    return
                used = {p["color"] for p in STATE["players"].values()}
                color = next((c for c in PALETTE if c not in used), PALETTE[len(STATE["players"]) % len(PALETTE)])
                existing = STATE["players"].get(pid)
                STATE["players"][pid] = {
                    "name": name,
                    "score": existing["score"] if existing else 0,
                    "color": existing["color"] if existing else color,
                    "lastSeen": time.time(),
                }
                self._send_json({"ok": True, "color": STATE["players"][pid]["color"]})
                return
 
            if path == "/api/cursor":
                pid = data.get("id")
                if pid in STATE["players"]:
                    STATE["players"][pid]["lastSeen"] = time.time()
                    player = STATE["players"][pid]
                    try:
                        x = max(0.0, min(1.0, float(data.get("x", 0))))
                        y = max(0.0, min(1.0, float(data.get("y", 0))))
                    except (TypeError, ValueError):
                        x, y = 0.0, 0.0
                    now = time.time()
                    STATE["cursors"][pid] = {
                        "name": player["name"],
                        "color": player["color"],
                        "x": x,
                        "y": y,
                        "ts": now,
                    }
                self._send_json({"ok": True})
                return

            if path == "/api/leave":
                pid = data.get("id")
                if pid:
                    STATE["players"].pop(pid, None)
                    STATE["cursors"].pop(pid, None)
                self._send_json({"ok": True})
                return
 
            if path == "/api/open_clue":
                try:
                    ci, vi = int(data.get("ci")), int(data.get("vi"))
                except (TypeError, ValueError):
                    self._send_json({"error": "invalid clue"}, 400)
                    return
                key = f"{ci}_{vi}"
                cell = STATE["board"]["cells"].get(key)
                if STATE["phase"] == "playing" and cell is not None and not cell["answered"] and STATE["board"]["activeClue"] is None:
                    STATE["board"]["activeClue"] = {
                        "ci": ci, "vi": vi, "buzzedId": None, "buzzedName": None,
                        "lockedOut": [], "revealAnswer": False,
                    }
                self._send_json({"ok": True})
                return
 
            if path == "/api/buzz":
                ac = STATE["board"]["activeClue"]
                pid = data.get("id")
                if ac and pid in STATE["players"] and not ac["buzzedId"] and pid not in ac["lockedOut"]:
                    STATE["players"][pid]["lastSeen"] = time.time()
                    ac["buzzedId"] = pid
                    ac["buzzedName"] = STATE["players"][pid]["name"]
                self._send_json({"ok": True})
                return
 
            if path == "/api/judge":
                ac = STATE["board"]["activeClue"]
                correct = bool(data.get("correct"))
                if ac and ac["buzzedId"] in STATE["players"]:
                    value = (ac["vi"] + 1) * 100
                    pid = ac["buzzedId"]
                    if pid in STATE["players"]:
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
 
                            def clear_later(clue_ref=ac):
                                time.sleep(REVEAL_DURATION)
                                with LOCK:
                                    if STATE["board"]["activeClue"] is clue_ref:
                                        STATE["board"]["activeClue"] = None
                            threading.Thread(target=clear_later, daemon=True).start()
                self._send_json({"ok": True})
                return
 
            if path == "/api/skip":
                ac = STATE["board"]["activeClue"]
                if ac:
                    STATE["board"]["cells"][f"{ac['ci']}_{ac['vi']}"] = {"answered": True}
                    ac["revealAnswer"] = True
                    def clear_skipped(clue_ref=ac):
                        time.sleep(REVEAL_DURATION)
                        with LOCK:
                            if STATE["board"]["activeClue"] is clue_ref:
                                STATE["board"]["activeClue"] = None
                    threading.Thread(target=clear_skipped, daemon=True).start()
                self._send_json({"ok": True})
                return
 
            if path == "/api/start":
                cleanup_stale()
                if not STATE["players"]:
                    self._send_json({"error": "no players"}, 400)
                    return
                STATE["phase"] = "playing"
                self._send_json({"ok": True})
                return
 
            if path == "/api/reset":
                STATE["board"] = default_board()
                for p in STATE["players"].values():
                    p["score"] = 0
                STATE["phase"] = "lobby"
                STATE["cursors"] = {}
                self._send_json({"ok": True})
                return
 
        self._send_json({"error": "not found"}, 404)
 
 
def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    ips = get_local_ips()
    print("=" * 64)
    print("  SPQR JEOPARDY - local multiplayer server running")
    print("=" * 64)
    print(f"  Host computer:        http://localhost:{PORT}")
    print()
    print("  FRIEND CONNECTION ADDRESSES:")
    for ip in ips:
        print(f"    http://{ip}:{PORT}")
    print()
    print("  IMPORTANT:")
    print("    Friends must use one of the addresses above, NOT localhost.")
    print("    First test one of those addresses on THIS computer.")
    print("    If it works here but not on another school computer,")
    print("    the school network/firewall is blocking device-to-device traffic.")
    print("=" * 64)
    print("  Press Ctrl+C to stop the server.")
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
 
 
if __name__ == "__main__":
    main()