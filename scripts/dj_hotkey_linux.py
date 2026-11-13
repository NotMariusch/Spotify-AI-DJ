"""
dj_hotkey_linux.py — Global hotkey listener for SpotifyDJ on Linux.

Reads keyboard events directly from /dev/input/eventX using evdev.
Works globally on Wayland and X11, even in fullscreen games.

Requires user to be in the 'input' group:
    sudo usermod -aG input $USER  (then log out and back in)

Usage:
    python scripts/dj_hotkey_linux.py

Configure KEYBOARD_DEVICE to match your keyboard's event number.
Find it with: cat /proc/bus/input/devices
"""

import struct
import time
import os
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
KEYBOARD_DEVICE = "/dev/input/event8"   # change if your keyboard is different
BASE_DIR        = Path(__file__).resolve().parent.parent
INPUT_FILE      = BASE_DIR / "data" / "dj_input.txt"
# ────────────────────────────────────────────────────────────────────────────

# Linux input event format: timeval (8 bytes) + type (2) + code (2) + value (4)
EVENT_FORMAT = "llHHi"
EVENT_SIZE   = struct.calcsize(EVENT_FORMAT)

EV_KEY   = 1
KEY_DOWN = 1

# Key codes
KEY_F13      = 183
KEY_RIGHTCTRL = 97   # fallback if F13 doesn't register
KEY_1 = 2; KEY_2 = 3; KEY_3 = 4; KEY_4 = 5; KEY_5 = 6
KEY_6 = 7; KEY_7 = 8; KEY_8 = 9; KEY_9 = 10
KEY_V = 47

COMMANDS = {
    KEY_1: "5",
    KEY_2: "skip",
    KEY_3: "ban-artist",
    KEY_4: "ban",
    KEY_5: "quit",
    KEY_V: "voice",
}

def send_command(text):
    with open(INPUT_FILE, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  → {text}")

def main():
    print("SpotifyDJ Hotkeys (Linux)")
    print(f"Listening on {KEYBOARD_DEVICE}")
    print("F13+1: Standard | F13+2: skip | F13+3: ban artist | F13+4: ban track | F13+5: quit")
    print()

    f13_held = False

    with open(KEYBOARD_DEVICE, "rb") as dev:
        while True:
            data = dev.read(EVENT_SIZE)
            if not data:
                break
            _, _, ev_type, code, value = struct.unpack(EVENT_FORMAT, data)

            if ev_type != EV_KEY:
                continue

            if code == KEY_F13:
                f13_held = (value != 0)  # 1=down, 2=repeat, 0=up
                continue

            if f13_held and value == KEY_DOWN:
                if code in COMMANDS:
                    cmd = COMMANDS[code]
                    if cmd == "voice":
                        # Toggle voice via dj_voice.py's own mechanism
                        send_command("voice-toggle")
                    else:
                        send_command(cmd)

if __name__ == "__main__":
    try:
        main()
    except PermissionError:
        print(f"Cannot read {KEYBOARD_DEVICE}.")
        print("Add yourself to the input group:")
        print("  sudo usermod -aG input $USER")
        print("Then log out and back in.")
    except KeyboardInterrupt:
        print("\nHotkeys stopped.")
