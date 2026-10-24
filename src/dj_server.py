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
import signal
import json
import math
from dotenv import load_dotenv
from pathlib import Path
from flask import Flask, request
from flask_socketio import SocketIO, emit
from instance_lock import InstanceLock
from shutdown_signal import ShutdownNotice
from werkzeug.serving import make_server

BASE_DIR   = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "dj_input.txt"
DJ_SCRIPT  = BASE_DIR / "src" / "spotify_dj.py"
SHUTDOWN_FILE = BASE_DIR / "data" / "shutdown.signal"

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
    "shutting_down": False,
}

dj_process = None
shutdown_requested = threading.Event()
shutdown_guard = threading.Lock()

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
        if message in ("DJ aktiv", "Start angefordert; warte auf Spotify-Bestätigung"):
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
        start_new_session=os.name != 'nt',
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
    while not shutdown_requested.is_set():
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

        shutdown_requested.wait(0.25)


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
    if shutdown_requested.is_set() and text != 'quit':
        return
    with open(INPUT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def request_shutdown(reason='Beenden angefordert'):
    """One stop path for dashboard, hotkey, child exit and server teardown."""
    with shutdown_guard:
        if shutdown_requested.is_set():
            return
        shutdown_requested.set()
        state.update(shutting_down=True, is_playing=False, recording=False,
                     playback_progress=None, status='DJ und Dashboard werden beendet')
        print(f'[server] {reason}; beende DJ und Dashboard.', flush=True)
        try:
            send_command('quit')
        except OSError as error:
            print(f'[server] Quit-Datei nicht schreibbar ({type(error).__name__}); Prozessende wird erzwungen.')
        try:
            ShutdownNotice.broadcast(SHUTDOWN_FILE)
        except OSError as error:
            print(f'[server] Voice-Stoppsignal nicht schreibbar ({type(error).__name__}).')
        try:
            socketio.emit('state', dashboard_state(), namespace='/')
        except Exception as error:
            print(f'[server] Abschiedsmeldung nicht gesendet ({type(error).__name__}).')


def watch_shutdown():
    # Independent of the DJ loop, even while Claude/Spotify calls are blocked.
    while not shutdown_requested.is_set():
        try:
            if INPUT_FILE.read_text(encoding='utf-8-sig').strip() == 'quit':
                request_shutdown('Quit über Hotkey/Chat')
                return
        except (OSError, UnicodeError):
            pass
        if dj_process is not None and dj_process.poll() is not None:
            request_shutdown('DJ-Prozess beendet')
            return
        shutdown_requested.wait(0.25)


def stop_dj(process, timeout=8):
    if process is None or process.poll() is not None:
        return
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        print('[server] DJ reagiert nicht rechtzeitig; beende eigenen Prozessbaum.', flush=True)
    if os.name == 'nt':
        # Windows venv python.exe is a redirector with another Python child.
        # Terminating only Popen would leave that child running and locked.
        result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                capture_output=True, timeout=5,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode and process.poll() is None:
            raise RuntimeError('Eigener DJ-Prozessbaum konnte nicht beendet werden')
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait(timeout=3)


def clear_stale_quit():
    # Only a control command; preserve all other pending commands and user data.
    try:
        if INPUT_FILE.read_text(encoding='utf-8-sig').strip() == 'quit':
            INPUT_FILE.unlink()
    except (OSError, UnicodeError):
        pass


@socketio.on("command")
def handle_command(data):
    cmd = data.get("cmd", "").strip()
    if not cmd:
        return
    if cmd == 'quit':
        request_shutdown('Quit über Dashboard')
        return
    if shutdown_requested.is_set():
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

def main(port=5001, shutdown_timeout=8):
    global dj_process
    server_lock = InstanceLock(str(BASE_DIR / "data" / "server.lock"))
    if not server_lock.acquire():
        print("[server] Dashboard already running at http://127.0.0.1:5001")
        return
    http_server = None
    watcher = None
    dj_process = None
    try:
        # Keep Werkzeug/threading and SocketIO middleware, but own the listener
        # so quit can stop it cleanly instead of leaving app.run blocked.
        http_server = make_server('127.0.0.1', port, app, threaded=True)
        http_server.timeout = .25
        shutdown_requested.clear()
        state['shutting_down'] = False
        clear_stale_quit()
        launch_dj()
        print(f"[server] Dashboard at http://127.0.0.1:{http_server.server_port}", flush=True)
        threading.Thread(target=broadcast_loop, daemon=True).start()
        watcher = threading.Thread(target=watch_shutdown, daemon=True)
        watcher.start()
        while not shutdown_requested.is_set():
            http_server.handle_request()
    except KeyboardInterrupt:
        request_shutdown('Server unterbrochen')
    finally:
        try:
            if dj_process is not None:
                request_shutdown('Server wird beendet')
                stop_dj(dj_process, timeout=shutdown_timeout)
            shutdown_requested.set()
            if watcher:
                watcher.join(timeout=1)
            clear_stale_quit()
        finally:
            try:
                if http_server is not None:
                    http_server.server_close()
            finally:
                server_lock.release()


if __name__ == '__main__':
    main()
