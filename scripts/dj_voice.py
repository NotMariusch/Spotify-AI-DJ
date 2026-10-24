"""
dj_voice.py — Global push-to-talk voice input for SpotifyDJ.

Press F13+V (Right Ctrl + V) once to start listening, again to stop.
Works globally — even in fullscreen games.
Uses Google Speech Recognition (free, no API key needed).

Run this alongside spotify_dj.py or dj_server.py in a separate terminal.

Usage:
    python scripts/dj_voice.py

Requirements:
    pip install SpeechRecognition sounddevice numpy keyboard
"""

import sys
import os
import time
import threading
import tempfile
import wave
import json as _json
import urllib.request
import platform
import numpy as np
import sounddevice as sd
import speech_recognition as sr
from pathlib import Path

IS_LINUX = platform.system() == "Linux"
if not IS_LINUX:
    import keyboard

BASE_DIR   = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "dj_input.txt"
sys.path.insert(0, str(BASE_DIR / "src"))
from instance_lock import InstanceLock
from shutdown_signal import ShutdownNotice

SAMPLE_RATE = 16000
CHANNELS    = 1

recording    = False
frames       = []
stream       = None
frames_lock  = threading.Lock()
toggle_lock  = threading.Lock()
processing   = False
shutdown_notice = None
closing = threading.Event()


def stopping():
    if shutdown_notice is not None and shutdown_notice.changed():
        closing.set()
    return closing.is_set()


def send_command(text: str):
    if stopping():
        return
    with open(INPUT_FILE, "w", encoding="utf-8") as f:
        f.write(text)


def log_to_server(msg: str, recording_state=None):
    """Send a log message to the dashboard server (best-effort, silent on failure)."""
    try:
        data = {"msg": msg}
        if recording_state is not None:
            data["recording"] = recording_state
        payload = _json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:5001/voice_log",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=1):
            pass
    except Exception:
        pass  # server not running — just print to terminal


def audio_to_wav(audio: np.ndarray) -> str:
    # Close the temporary handle before reopening the path on Windows.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        return wav_path
    except Exception:
        os.unlink(wav_path)
        raise


def transcribe(wav_path: str):
    r = sr.Recognizer()
    r.operation_timeout = 15
    with sr.AudioFile(wav_path) as source:
        audio = r.record(source)
    try:
        return r.recognize_google(audio)
    except sr.UnknownValueError:
        print("  Could not understand audio.")
        log_to_server("Keine Sprache erkannt. Bitte erneut versuchen.")
        return None
    except sr.RequestError as e:
        print(f"  Speech recognition error: {e}")
        log_to_server("Spracherkennung nicht erreichbar. Internetverbindung prüfen.")
        return None


def start_recording():
    global recording, frames, stream
    frames    = []

    def callback(indata, frame_count, time_info, status):
        if recording:
            with frames_lock:
                frames.append(indata.copy())

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", callback=callback,
        )
        stream.start()
        recording = True
    except Exception:
        recording = False
        if stream:
            stream.close()
            stream = None
        raise
    print("  Recording... (press F13+V again to stop)")
    log_to_server("🎙 Recording...", recording_state=True)


def stop_recording():
    global recording, stream
    recording = False
    log_to_server("Aufnahme beendet.", recording_state=False)
    if stream:
        try:
            stream.stop()
        finally:
            stream.close()
            stream = None

    with frames_lock:
        captured = list(frames)

    if not captured:
        print("  No audio captured.")
        log_to_server("  No audio captured.")
        return

    audio    = np.concatenate(captured, axis=0)
    wav_path = audio_to_wav(audio)

    print("  Transcribing...")
    log_to_server("  Transcribing...")
    try:
        text = transcribe(wav_path)
    finally:
        os.unlink(wav_path)

    if text:
        print(f'  You said: "{text}"')
        log_to_server(f'You said: "{text}"')
        send_command(f"ai:{text}")
        print("  -> Sent to AI DJ.")
        log_to_server("→ Sent to AI DJ.")
    print()


def finish_recording():
    global processing
    try:
        stop_recording()
    except Exception as error:
        print(f"  Voice processing failed: {type(error).__name__}")
        log_to_server(f"Voice-Verarbeitung fehlgeschlagen ({type(error).__name__}).", recording_state=False)
    finally:
        with toggle_lock:
            processing = False


def on_hotkey():
    if stopping():
        return
    global recording, processing
    with toggle_lock:
        if processing:
            return  # Do not replace the audio while it is being transcribed.
        if not recording:
            try:
                start_recording()
            except Exception as error:
                print(f"  Microphone failed: {type(error).__name__}")
                log_to_server("Mikrofon nicht verfügbar. Windows-Mikrofonzugriff und Standard-Eingabegerät prüfen.", recording_state=False)
        else:
            recording = False
            processing = True
            threading.Thread(target=finish_recording, daemon=True).start()


def run_voice():
    print("=" * 50)
    print("  SpotifyDJ - Voice Mode")
    print("=" * 50)
    print("  F13+V (Right Ctrl + V) - toggle recording")
    print()
    print("  Works globally in games and other apps.")
    print("  Press Ctrl+C in this window to exit.")
    print("=" * 50)
    print()

    if IS_LINUX:
        # On Linux, keyboard library requires root. Instead we watch
        # dj_input.txt for "voice-toggle" written by dj_hotkey_linux.py
        print("  Listening for voice-toggle command (via dj_hotkey_linux.py)...")
        print()
        try:
            while not stopping():
                if INPUT_FILE.exists():
                    try:
                        content = INPUT_FILE.read_text().strip()
                        if content == "voice-toggle":
                            INPUT_FILE.unlink()
                            on_hotkey()
                    except Exception:
                        pass
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nClosing voice mode.")
            sys.exit(0)
    else:
        hotkey = keyboard.add_hotkey("f13+v", on_hotkey, suppress=True)
        log_to_server("Voice bereit: F13 + V startet/stoppt die Aufnahme.", recording_state=False)
        print("  Listening for F13+V...")
        print()
        try:
            while not stopping():
                closing.wait(.25)
        except KeyboardInterrupt:
            print("\nClosing voice mode.")
            sys.exit(0)
        finally:
            keyboard.remove_hotkey(hotkey)


def main():
    global shutdown_notice
    lock = InstanceLock(str(BASE_DIR / "data" / "voice.lock"))
    if not lock.acquire():
        print("Voice mode is already running.")
        return
    try:
        closing.clear()
        shutdown_notice = ShutdownNotice(BASE_DIR / 'data' / 'shutdown.signal')
        run_voice()
    finally:
        closing.set()
        if stream:
            stream.close()
        log_to_server("Voice beendet.", recording_state=False)
        lock.release()


if __name__ == "__main__":
    main()
