"""
dj_server.py — Dashboard server for SpotifyDJ.

Streams DJ output to the browser dashboard in real time via websockets.
Also handles commands from the dashboard (skip, ban, mode switches, AI requests).

Usage:
    python src/dj_server.py

Then open http://127.0.0.1:5001 in your browser.

Requirements:
    pip install flask flask-socketio spotipy python-dotenv
"""

import os
import sys
import subprocess
import threading
import queue
import time
import re
import socket
import json
import math
from dotenv import load_dotenv
from pathlib import Path
from flask import Flask, request
from flask_socketio import SocketIO, emit
from instance_lock import InstanceLock

BASE_DIR   = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "dj_input.txt"
DJ_SCRIPT  = BASE_DIR / "src" / "spotify_dj.py"

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = "spotifydj-dashboard"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

dj_log_queue    = queue.Queue()
voice_log_queue = queue.Queue()

state = {
    "current_artist": "—",
    "current_track":  "—",
    "mode":           "—",
    "recording":      False,
    "is_playing":     False,
    "status":         "Warte auf DJ",
    "playback_progress": None,
}

dj_process = None

# ── DJ process ──

def parse_dj_line(line: str):
    line = line.strip()
    if not line:
        return
    if line.startswith("PLAYBACK_PROGRESS:"):
        try:
            sample = json.loads(line[len("PLAYBACK_PROGRESS:"):])
            valid = (isinstance(sample, dict) and isinstance(sample.get("uri"), str)
                     and isinstance(sample.get("progress_ms"), (int, float))
                     and math.isfinite(sample["progress_ms"]) and sample["progress_ms"] >= 0
                     and isinstance(sample.get("duration_ms"), (int, float))
                     and math.isfinite(sample["duration_ms"]) and sample["duration_ms"] > 0
                     and isinstance(sample.get("is_playing"), bool))
            state["playback_progress"] = dict(
                uri=sample["uri"], progress_ms=min(sample["progress_ms"], sample["duration_ms"]),
                duration_ms=sample["duration_ms"], is_playing=sample["is_playing"],
                sample_id=time.monotonic_ns(), received_at=time.monotonic(),
            ) if valid else None
        except (ValueError, TypeError):
            state["playback_progress"] = None
        return
    if line.startswith("CURRENT_ARTIST:"):
        state["current_artist"] = line[len("CURRENT_ARTIST:"):]
    if line.startswith("DJ_STATUS:"):
        state["status"] = line[len("DJ_STATUS:"):]
    if line.startswith("MODE:"):
        mode = line[len("MODE:"):]
        state["mode"] = {"american_rap": "American Rap", "german_trap": "German Trap",
                         "kpop": "K-Pop", "jpop": "J-Pop", "global": "Global"}.get(mode, mode)
    if re.search(r"AI request:", line):
        state["current_artist"] = "AI Mode"
        state["mode"] = "AI"
    if line.startswith("NOW_PLAYING:"):
        state["current_track"] = line[len("NOW_PLAYING:"):]
    if line.startswith("IS_PLAYING:"):
        state["is_playing"] = line[len("IS_PLAYING:"):] == "true"


def handle_dj_line(line: str):
    """Update dashboard state without exposing its wire protocol as log spam."""
    line = line.strip()
    if not line:
        return
    parse_dj_line(line)
    if line.startswith("DJ_STATUS:"):
        message = line[len("DJ_STATUS:"):]
        # Routine activity is visible in the status header and track display.
        if message in ("DJ aktiv", "Start angefordert; warte auf Spotify-Bestaetigung"):
            return
        line = message
    elif line.startswith(("NOW_PLAYING:", "IS_PLAYING:", "CURRENT_ARTIST:", "MODE:", "PLAYBACK_PROGRESS:")):
        return
    dj_log_queue.put(line)


def launch_dj():
    global dj_process
    python = sys.executable
    print(f"[server] Python: {python}")
    print(f"[server] Launching DJ: {DJ_SCRIPT}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    env["PYTHONUNBUFFERED"] = "1"
    dj_process = subprocess.Popen(
        [python, str(DJ_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
        env=env,
    )
    print(f"[server] DJ PID: {dj_process.pid}")

    def reader():
        for line in dj_process.stdout:
            handle_dj_line(line)
        dj_process.wait()
        state.update(is_playing=False, current_artist="—", current_track="—", status="DJ beendet",
                     playback_progress=None)
        dj_log_queue.put("[DJ process exited]")

    threading.Thread(target=reader, daemon=True).start()


# ── Broadcast loop ──

def dashboard_state():
    snapshot = dict(state)
    sample = snapshot.get("playback_progress")
    if sample is not None:
        sample = dict(sample)
        sample["age_ms"] = max(0, (time.monotonic() - sample.pop("received_at")) * 1000)
        snapshot["playback_progress"] = sample
    return snapshot


def broadcast_loop():
    while True:
        try:
            # DJ log
            lines = []
            while not dj_log_queue.empty():
                try:
                    lines.append(dj_log_queue.get_nowait())
                except queue.Empty:
                    break
            if lines:
                socketio.emit("dj_log", {"lines": lines}, namespace="/")

            # Voice log
            vlines = []
            while not voice_log_queue.empty():
                try:
                    vlines.append(voice_log_queue.get_nowait())
                except queue.Empty:
                    break
            if vlines:
                socketio.emit("voice_log", {"lines": vlines}, namespace="/")

            socketio.emit("state", dashboard_state(), namespace="/")
        except Exception as e:
            print(f"[broadcast] error: {e}")

        time.sleep(0.25)


# ── Routes ──

@app.route("/")
def index():
    with open(BASE_DIR / "src" / "dj_dashboard.html", encoding="utf-8") as f:
        return f.read()

@app.route("/voice_log", methods=["POST"])
def voice_log_endpoint():
    data = request.get_json(silent=True) or {}
    msg  = data.get("msg", "")
    if isinstance(data.get("recording"), bool):
        state["recording"] = data["recording"]
    if msg:
        voice_log_queue.put(msg)
    return "", 204


# ── Websocket events ──

def send_command(text: str):
    with open(INPUT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


@socketio.on("command")
def handle_command(data):
    cmd = data.get("cmd", "").strip()
    if not cmd:
        return

    mode_labels = {"1": "American Rap", "2": "German Trap", "3": "K-Pop", "4": "J-Pop", "5": "Global"}

    if cmd == "ai-mode":
        return  # UI-only, no action

    if cmd in ("skip", "ban", "ban-artist", "quit") or cmd in ("1", "2", "3", "4", "5"):
        send_command(cmd)
        if cmd in mode_labels:
            state["mode"]           = mode_labels[cmd]
            state["current_artist"] = "—"
            state["current_track"]  = "—"
            voice_log_queue.put(f"→ Switched to {mode_labels[cmd]} mode")
        elif cmd == "ban-artist":
            voice_log_queue.put(f"→ Banning current artist...")
        else:
            voice_log_queue.put(f"→ Command sent: {cmd}")

    elif cmd.startswith("ai:"):
        req = cmd[3:].strip()
        send_command(cmd)
        state["mode"]           = "AI"
        state["current_artist"] = "AI Mode"
        voice_log_queue.put(f"→ AI request: \"{req}\"")

    else:
        send_command(f"ai:{cmd}")
        state["mode"]           = "AI"
        state["current_artist"] = "AI Mode"
        voice_log_queue.put(f"→ AI request: \"{cmd}\"")


@socketio.on("connect")
def on_connect():
    emit("state", dashboard_state())


# ── Entry point ──

if __name__ == "__main__":
    server_lock = InstanceLock(str(BASE_DIR / "data" / "server.lock"))
    if not server_lock.acquire():
        print("[server] Dashboard already running at http://127.0.0.1:5001")
        sys.exit(0)
    try:
        # Check before launching the child, including older servers which do
        # not yet hold server.lock. Keep Werkzeug as the actual HTTP server.
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", 5001))
            except OSError:
                print("[server] Port 5001 already in use; no second DJ launched.")
                sys.exit(0)
        launch_dj()
        print("[server] Dashboard at http://127.0.0.1:5001")
        threading.Thread(target=broadcast_loop, daemon=True).start()
        socketio.run(app, host="127.0.0.1", port=5001, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
    finally:
        if dj_process and dj_process.poll() is None:
            dj_process.terminate()
            dj_process.wait(timeout=10)
        server_lock.release()
