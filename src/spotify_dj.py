import os
import sys
import time
import json
import random
import re
import math
import urllib.request
import urllib.parse
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
from collections import deque
from playback_state import PlaybackTracker
from instance_lock import InstanceLock
from ai_request import ask_claude, resolve_artists_to_ids
from ai_session import AISession, is_session_reset
from music_request import RequestValidationError, constrain_tracks, resolve_song_wishes, release_year

# =====================
# SPOTIFY CONFIG
# =====================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("SPOTIFY_REDIRECT_URI")

SCOPE = "user-modify-playback-state user-read-playback-state"

# =====================
# SETTINGS
# =====================

# AUTO_INTERVAL and SMOOTH_THRESHOLD removed -- smooth transition replaced
# by track_finished() which resumes immediately when a track ends naturally.

MEMORY_FILE        = os.path.join(BASE_DIR, "data", "dj_memory.json")
INPUT_FILE         = os.path.join(BASE_DIR, "data", "dj_input.txt")
CACHE_FILE         = os.path.join(BASE_DIR, "data", "track_cache.json")
DISCOVERED_FILE    = os.path.join(BASE_DIR, "data", "discovered_artists.json")
RECENT_FILE        = os.path.join(BASE_DIR, "data", "recent_tracks.json")
LOCK_FILE          = os.path.join(BASE_DIR, "data", "dj.lock")
BANNED_FILE        = os.path.join(BASE_DIR, "data", "banned_tracks.json")
BANNED_ARTISTS_FILE = os.path.join(BASE_DIR, "data", "banned_artists.json")
CACHE_TTL_DAYS     = 7

MEMORY_VERSION = 1

current_pool = None
auto_mode    = None
ai_pool      = []  # list of (name, artist_id) tuples — set by AI requests
ai_artists   = {}  # temporary artist ID lookup for AI-only artists — never added to permanent pools
ai_session_catalogs = {}  # Request-local catalog readiness; not taste preferences.
ai_artist_plays = {}  # Session rotation counters, never saved or learned from.
ai_last_artist = None
ai_request_plan = None
ai_request_tracks_ready = False
ai_song_queue = []  # Explicit wishes only; consume after confirmed playback.
ai_song_resolutions = {}  # Resume bounded lookups after a cooldown; request-local only.
ai_session = AISession()  # Conversation, exclusions and listening history; never saved.

tracker = PlaybackTracker()
now_playing = tracker.track
_pending_selection = None
_prepared_next = None
TRANSITION_LEAD_MS = 1500
PREPARE_LEAD_MS = 20000
_next_device_check = 0.0
_next_external_poll = 0.0
_last_status = None


def status(value):
    global _last_status
    if value != _last_status:
        print(f"DJ_STATUS:{value}")
        _last_status = value


def log_section(title):
    print(f"\n--- {title} ---")


track_disk_cache = {}

# Raised by fetch_artist_tracks_by_id when Spotify returns 429, so callers
# can react immediately instead of sleeping the full Retry-After inline.
class RateLimitError(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after

# Tracks when a rate limit ban expires so track_finished() can skip
# API calls while banned rather than hammering the endpoint.
_rate_limit_until = 0.0

# =====================
# DISK CACHE
# =====================

def load_track_cache():
    global track_disk_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                track_disk_cache = json.load(f)
        except Exception:
            track_disk_cache = {}

def save_track_cache():
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(track_disk_cache, f, indent=2)

# Startup loading is performed in main(), never on import.

# =====================
# FILTER SETTINGS
# =====================

TRACK_KEYWORDS_WORD = [
    "remix", "live", "edit", "clean", "demo", "acoustic",
    "instrumental", "karaoke", "nightcore", "extended",
    "censored", "remaster", "remastered", "reverb",
    "interlude", "skit", "intro", "outro",
]

TRACK_KEYWORDS_SUBSTR = [
    "sped up", "sped-up", "speed up", "spedup",
    "slowed reverb", "slowed",
    "radio edit",
]

ALBUM_KEYWORDS_WORD = [
    "live", "tour", "concert", "arena", "stadium", "festival", "anniversary", "soundtrack", "vocaloid",
]

LANGUAGE_VERSION_PATTERN = re.compile(
    r"(japanese|english|chinese|mandarin|spanish|french|german|thai|vietnamese)"
    r"\s*(ver\.?|version|edit)",
    re.IGNORECASE
)

ALBUM_KEYWORDS_SUBSTR = [
    "tokyo dome",
    "world tour",
    "original soundtrack",
    "サウンドトラック",
    "sound track",
]

BLOCKED_ARTIST_IDS = {
    "6pNgnvzBa6Bthsv8SrZJYl",  # Hatsune Miku (main)
    "3hZDdhpZycP7DbqHV58RD6",  # Hatsune Miku (alternate profile)
    "2XEx6N3gknSmtshM0PVuxu",  # GUMI
    "7wZ6E8iVwjGYRGwSfkIAjP",  # Kagamine Rin
    "2GXfF7OsKmE87OBfZ69cFY",  # Kagamine Len
    "2rh169c2dYzbplqrEwDqsk",  # Kagamine Rin & Kagamine Len (combined profile)
    "7CWdEBRTF2C44GfX1GmSc2",  # KAITO
    "2IkmuwBBKvBWsJjzyUsvhp",  # MEIKO
    "1oz1HYOyJhjshPi5Nvs3MX",  # IA
    "7HwMt9qpoww4yfE3D8886I",  # Megurine Luka
    "4JX0GdKx8EduY2Ck7qac4H",  # Kasane Teto
    "7Al85ZdCo2ZoQiBghDSj7o",  # 重音テト (Kasane Teto Japanese profile)
}

recent_titles  = deque(maxlen=30)
recent_artists = deque(maxlen=10)
# Spontaneous AI listening stays in process-local session history.
ai_recent_titles = deque(maxlen=30)

def load_recent():
    """Restore recent_titles from disk so repeats are avoided across restarts."""
    if os.path.exists(RECENT_FILE):
        try:
            with open(RECENT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for t in data.get("titles", []):
                recent_titles.append(t)
            for a in data.get("artists", []):
                recent_artists.append(a)
        except Exception:
            pass  # corrupt file — start fresh, not a big deal

def save_recent():
    """Persist recent_titles to disk."""
    with open(RECENT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "titles":  list(recent_titles),
            "artists": list(recent_artists),
        }, f)


# =====================
# BAN SYSTEM
# =====================

banned_track_ids = set()

def load_banned():
    """Load banned track IDs from disk into the in-memory set."""
    global banned_track_ids
    if os.path.exists(BANNED_FILE):
        try:
            with open(BANNED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            banned_track_ids = set(data.get("track_ids", []))
            print(f"  Loaded {len(banned_track_ids)} banned track(s).")
        except Exception:
            banned_track_ids = set()

def save_banned():
    with open(BANNED_FILE, "w", encoding="utf-8") as f:
        json.dump({"track_ids": list(banned_track_ids)}, f, indent=2)

def ban_current_track():
    """
    Ban whatever is currently playing:
    1. Look up the current track via Spotify API
    2. Add its ID to banned_track_ids and save
    3. Skip to the next track immediately
    """
    try:
        pb = sp.current_playback()
        if not pb or not pb.get("item"):
            print("  Ban: nothing is currently playing.")
            return

        track    = pb["item"]
        track_id = track["id"]
        name     = track["name"]

        if track_id in banned_track_ids:
            print(f"  Ban: '{name}' is already banned.")
        else:
            banned_track_ids.add(track_id)
            save_banned()
            print(f"  Banned: '{name}' (ID: {track_id})")

        # Skip immediately — play next track in current mode.
        # Explicit track bans are neutral for artist weights.
        if ai_pool:
            play_artist(random.choice(ai_pool)[0], "ai", no_judge=True, interrupted=False, banned=True)
        elif auto_mode == "global":
            play_global_mix(interrupted=False, banned=True)
        elif current_pool:
            play_from_pool(current_pool, auto_mode, interrupted=False, banned=True)

    except Exception as e:
        print(f"  Ban failed: {e}")


# =====================
# BAN ARTIST SYSTEM
# =====================

banned_artist_names = set()

def load_banned_artists():
    """Load banned artist names from disk."""
    global banned_artist_names
    if os.path.exists(BANNED_ARTISTS_FILE):
        try:
            with open(BANNED_ARTISTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            banned_artist_names = set(data.get("artist_names", []))
            if banned_artist_names:
                print(f"  Loaded {len(banned_artist_names)} banned artist(s).")
        except Exception:
            banned_artist_names = set()

def save_banned_artists():
    with open(BANNED_ARTISTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"artist_names": list(banned_artist_names)}, f, indent=2)

def ban_current_artist():
    """
    Ban the currently playing artist if they are a discovered artist.
    Permanent pool artists cannot be banned this way.
    Removes them from all pools, weights, track cache, and discovered_artists.
    Adds them to banned_artist_names so they can never be re-discovered.
    """
    artist = now_playing.get("artist")
    if not artist:
        print("  Ban artist: nothing is currently playing.")
        return

    # Only allow banning discovered artists, not permanent pool artists
    if artist not in discovered_artists:
        print(f"  Ban artist: '{artist}' is a permanent pool artist and cannot be banned this way.")
        return

    print(f"  Banning artist: '{artist}'")

    # Remove from discovered_artists and save
    del discovered_artists[artist]
    save_discovered(discovered_artists)

    # Remove from ARTISTS dict
    ARTISTS.pop(artist, None)

    # Remove from all mode pools and global pool
    for pool in MODE_POOLS.values():
        if artist in pool:
            pool.remove(artist)
    if artist in GLOBAL_POOL:
        GLOBAL_POOL.remove(artist)

    # Remove from all weight modes in memory
    for mode_weights in memory["modes"].values():
        mode_weights.pop(artist, None)
    save_memory(memory)

    # Remove from track cache
    if artist in track_disk_cache:
        del track_disk_cache[artist]
        save_track_cache()
    artist_cache.pop(artist, None)

    # Add to banned artists so discovery never re-adds them
    banned_artist_names.add(artist)
    save_banned_artists()

    print(f"  '{artist}' permanently banned and removed from all pools.")

    # Skip to next track immediately
    if ai_pool:
        play_artist(random.choice(ai_pool)[0], "ai", no_judge=True, interrupted=False, banned=True)
    elif auto_mode == "global":
        play_global_mix(interrupted=False, banned=True)
    elif current_pool:
        play_from_pool(current_pool, auto_mode, interrupted=False, banned=True)


# =====================
# INSTANCE LOCK
# =====================

_instance_lock = InstanceLock(LOCK_FILE)


def acquire_lock():
    if not _instance_lock.acquire():
        print("DJ is already running. Exiting.")
        sys.exit(0)


def release_lock():
    _instance_lock.release()


# =====================
# ARTISTS
# =====================

ARTISTS = {
    "Juice WRLD":    "4MCBfE4596Uoi2O4DtmEMz",
    "XXXTENTACION":  "15UsOTVnJzReFVN1VCnxy4",
    "Ski Mask":      "2rhFzFmezpnW82MNqEKVry",
    "A Boogie":      "31W5EY0aAly4Qieq6OFu6I",
    "tj_beastboy":   "7l8dcABCTyZKrkskt53Z2u",
    "Sierra Kidd":   "0U7ti3mwGrBNlKNE4YlbfT",
    "LE SSERAFIM":   "4SpbR6yFEvexJuaBpgAU5p",
    "BLACKPINK":     "41MozSoPIsD1dJM0CLPjZF",
    "NewJeans":      "6HvZYsbFfjnjFrWF950C9d",
    "K/DA":          "4gOc8TsQed9eqnqJct2c5v",
    "aespa":         "6YVMFz59CuY7ngCxTxjpxE",
    "Ado":           "6mEQK9m2krja6X1cfsAjfl",
    "YOASOBI":       "64tJ2EAv1R6UaZqc4iOCyj",
    "Eve":           "58oPVy7oihAEXE0Ott6JOf",
    "BABYMETAL":     "630wzNP2OL7fl4Xl0GnMWq",
    "Aimer":         "0bAsR2unSRpn6BQPEnNlZm",
    "Joji":          "3MZsBdqDrRTJihTHQrO6Dq",
}

AMERICAN_RAP_POOL = ["Juice WRLD", "XXXTENTACION", "Ski Mask", "A Boogie"]
GERMAN_TRAP_POOL  = ["tj_beastboy", "Sierra Kidd"]
KPOP_POOL         = ["LE SSERAFIM", "BLACKPINK", "NewJeans", "K/DA", "aespa"]
JPOP_POOL         = ["Ado", "YOASOBI", "Eve", "BABYMETAL", "Aimer"]

GLOBAL_POOL = list(ARTISTS.keys())

# Pool lookup by mode name — used by the discovery system to know
# which pool to add a graduated artist into.
MODE_POOLS = {
    "american_rap": AMERICAN_RAP_POOL,
    "german_trap":  GERMAN_TRAP_POOL,
    "kpop":         KPOP_POOL,
    "jpop":         JPOP_POOL,
    "global":       GLOBAL_POOL,
}

# =====================
# MEMORY SYSTEM
# =====================

def default_memory():
    return {
        "version":    MEMORY_VERSION,
        "last_decay": None,
        "modes": {
            "american_rap":  {},
            "german_trap":   {},
            "kpop":          {},
            "jpop":          {},
            "global":        {},
        }
    }

def upgrade_memory(data):
    if "version" not in data:
        data["version"] = MEMORY_VERSION
    if data["version"] < MEMORY_VERSION:
        print("Upgrading DJ memory...")
        data["version"] = MEMORY_VERSION
    # Migration: add last_decay if missing (existing files won't have it)
    if "last_decay" not in data:
        data["last_decay"] = None
    return data

def save_memory(mem):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2)

# How much of the distance to 1.0 decays per week (10%).
# A weight of 3.0 becomes 3.0 - (3.0 - 1.0) * 0.10 = 2.8 after one week.
# A weight of 0.3 becomes 0.3 + (1.0 - 0.3) * 0.10 = 0.37 after one week.
DECAY_RATE_PER_WEEK = 0.10

def apply_weight_decay(mem):
    """
    Proportionally decay all weights toward 1.0 based on time elapsed
    since the last decay. Called once at startup.

    Uses DECAY_RATE_PER_WEEK scaled by actual days elapsed, so the decay
    is accurate regardless of how long the DJ has been off.
    """
    now = time.time()
    last = mem.get("last_decay")

    if last is None:
        # First run after adding this feature — just record the timestamp,
        # don't apply any decay yet since we don't know how much time passed.
        mem["last_decay"] = now
        save_memory(mem)
        print("  Weight decay: initialized (no decay applied on first run).")
        return

    days_elapsed = (now - last) / 86400
    if days_elapsed < 1:
        # Less than a day since last decay — not worth applying.
        return

    weeks_elapsed = days_elapsed / 7
    decay_factor  = DECAY_RATE_PER_WEEK * weeks_elapsed

    total_changes = 0
    for mode, weights in mem["modes"].items():
        for artist in list(weights.keys()):
            current = weights[artist]
            if abs(current - 1.0) < 0.001:
                continue  # already at neutral, skip
            distance  = current - 1.0
            new_weight = current - (distance * decay_factor)
            new_weight = round(max(0.2, min(3.0, new_weight)), 3)
            if new_weight != current:
                weights[artist] = new_weight
                total_changes += 1

    mem["last_decay"] = now
    save_memory(mem)

    if total_changes > 0:
        print(f"  Weight decay: applied over {days_elapsed:.1f} days "
              f"({decay_factor * 100:.1f}% toward neutral), "
              f"{total_changes} artist(s) adjusted.")
    else:
        print(f"  Weight decay: nothing to adjust ({days_elapsed:.1f} days elapsed).")

def load_memory():
    if not os.path.exists(MEMORY_FILE):
        mem = default_memory()
        save_memory(mem)
        return mem
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data = upgrade_memory(data)
        apply_weight_decay(data)
        return data
    except Exception:
        raise RuntimeError("Lerndaten nicht lesbar; Datei bleibt unveraendert.") from None

memory = default_memory()

def get_mode_memory(mode):
    return memory["modes"].setdefault(mode, {})

# =====================
# WEIGHT SYSTEM
# =====================

WEIGHT_BOOST  =  0.15  # reward for playing 80%+ of a track
WEIGHT_PUNISH = -0.10  # penalty for switching away in first 25%
WEIGHT_MAX    =  3.0   # ceiling — stops one artist dominating forever
WEIGHT_MIN    =  0.2   # floor — keeps every artist in the rotation

# Weight threshold that triggers a discovery search for a mode.
# Only artists at the ceiling (3.0) are eligible as discovery seeds.
DISCOVERY_TRIGGER_WEIGHT = 3.0

# Probability of actually queuing a discovery when the threshold is hit.
# Prevents constant searches as the pool grows large.
DISCOVERY_CHANCE = 0.25

def update_weight(artist, mode, delta):
    weights = get_mode_memory(mode)
    current = weights.get(artist, 1.0)
    updated = max(WEIGHT_MIN, min(WEIGHT_MAX, current + delta))
    weights[artist] = round(updated, 3)
    save_memory(memory)
    print(f"  weight [{mode}] {artist}: {current:.3f} -> {updated:.3f}")

    # Check if this boost pushed the artist past the discovery threshold
    if delta > 0 and updated >= DISCOVERY_TRIGGER_WEIGHT:
        if random.random() < DISCOVERY_CHANCE:
            queue_discovery(artist, mode)

def judge_last_track(interrupted=False, mode_switch=False, banned=False):
    verdict = tracker.verdict(interrupted=interrupted, neutral=mode_switch or banned)
    if verdict:
        artist, mode = now_playing["artist"], now_playing["mode"]
        update_weight(artist, mode, WEIGHT_BOOST if verdict == "positive" else WEIGHT_PUNISH)
        if verdict == "positive" and now_playing.get("is_trial"):
            record_trial_play(artist)


# =====================
# DISCOVERY SYSTEM
# =====================

# Pending discoveries: list of (seed_artist, mode) tuples to process
# at the next track boundary so we never interrupt playback.
_discovery_queue = []

# How many clean tracks a candidate needs to pass the quality bar
DISCOVERY_MIN_TRACKS = 5

# Trial plays needed before an artist graduates to permanent
TRIAL_GRADUATION = 5

def load_discovered():
    """
    Load discovered_artists.json.
    Structure:
    {
      "artist_name": {
        "id":          "spotify_artist_id",
        "mode":        "kpop",
        "trial_plays": 3,
        "graduated":   false
      },
      ...
    }
    """
    if not os.path.exists(DISCOVERED_FILE):
        return {}
    try:
        with open(DISCOVERED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_discovered(data):
    with open(DISCOVERED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

discovered_artists = {}

def all_known_ids():
    """Return a set of all artist IDs we already know about (permanent + discovered)."""
    ids = set(ARTISTS.values())
    for entry in discovered_artists.values():
        ids.add(entry["id"])
    return ids

def inject_discovered_into_pools():
    """
    At startup, add any previously discovered (including graduated) artists
    into the live pools and ARTISTS dict so they work like normal artists.
    """
    for name, entry in discovered_artists.items():
        if name not in ARTISTS:
            ARTISTS[name] = entry["id"]
        mode = entry.get("mode", "global")
        pool = MODE_POOLS.get(mode, GLOBAL_POOL)
        if name not in pool:
            pool.append(name)
        if name not in GLOBAL_POOL:
            GLOBAL_POOL.append(name)
    print(f"  Loaded {len(discovered_artists)} discovered artist(s) into pools.")

def queue_discovery(seed_artist, mode):
    """Queue a discovery search to run at the next track boundary."""
    entry = (seed_artist, mode)
    if entry not in _discovery_queue:
        print(f"  Discovery queued: find artists similar to {seed_artist} for [{mode}]")
        _discovery_queue.append(entry)

def run_pending_discoveries():
    """
    Process one queued discovery per call so we don't stall playback.
    Called at each track boundary before the new track starts.
    """
    if not _discovery_queue:
        return
    seed_artist, mode = _discovery_queue.pop(0)
    try:
        discover_new_artist(seed_artist, mode)
    except Exception as e:
        print(f"  Discovery error: {e}")

# Last.fm API key for artist similarity lookups.
# Replaces Spotify's deprecated /recommendations endpoint (removed Nov 2024).
# Set LASTFM_API_KEY in .env or it falls back to the value below.
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

def discover_new_artist(seed_artist, mode):
    """
    Use Last.fm's artist.getSimilar to find a new artist, then resolve
    their Spotify ID via sp.search(). Replaces the deprecated Spotify
    recommendations endpoint.
    """
    if not LASTFM_API_KEY:
        print("  Discovery unavailable: LASTFM_API_KEY not configured.")
        return
    known_names = set(ARTISTS.keys()) | set(discovered_artists.keys())

    print(f"  Discovery: searching Last.fm for artists similar to {seed_artist}...")

    # Step 1: ask Last.fm for similar artists
    try:
        params = urllib.parse.urlencode({
            "method":      "artist.getSimilar",
            "artist":      seed_artist,
            "api_key":     LASTFM_API_KEY,
            "format":      "json",
            "limit":       30,
            "autocorrect": 1,
        })
        url = f"{LASTFM_API_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "SpotifyDJ/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Discovery: Last.fm request failed: {e}")
        return

    similar = data.get("similarartists", {}).get("artist", [])
    if not similar:
        print(f"  Discovery: Last.fm returned no similar artists for {seed_artist}.")
        return

    # Shuffle so we don't always try the same top result
    random.shuffle(similar)

    # Step 2: for each candidate, resolve to a Spotify ID and quality check
    for entry in similar:
        name = entry.get("name", "").strip()
        if not name or name in known_names:
            continue
        if name in banned_artist_names:
            print(f"  Discovery: '{name}' is banned, skipping.")
            continue

        print(f"  Discovery: trying candidate '{name}'...")

        # Resolve artist name -> Spotify ID via search
        try:
            results = sp.search(q=f"artist:{name}", type="artist", limit=1, market="DE")
            items = results.get("artists", {}).get("items", [])
            if not items:
                print(f"    Not found on Spotify, skipping.")
                continue
            artist_obj = items[0]
            # Verify the name matches reasonably (Spotify search can return wrong artists)
            if artist_obj["name"].lower() != name.lower():
                print(f"    Spotify returned '{artist_obj['name']}' instead of '{name}', skipping.")
                continue
            artist_id = artist_obj["id"]
        except Exception as e:
            print(f"    Spotify search failed: {e}")
            continue

        # Skip if we already know this Spotify ID under a different name
        if artist_id in all_known_ids():
            print(f"    Already in pool under a different name, skipping.")
            continue

        # Quality check: must have enough clean tracks in DE
        tracks = fetch_artist_tracks_by_id(name, artist_id)
        filtered = select_best_tracks(tracks)
        if len(filtered) < DISCOVERY_MIN_TRACKS:
            print(f"    Only {len(filtered)} clean tracks, skipping.")
            continue

        # Candidate passes — add to discovered pool
        discovered_artists[name] = {
            "id":          artist_id,
            "mode":        mode,
            "trial_plays": 0,
            "graduated":   False,
        }
        save_discovered(discovered_artists)

        # Add to live pools immediately so they can be picked
        ARTISTS[name] = artist_id
        pool = MODE_POOLS.get(mode, GLOBAL_POOL)
        if name not in pool:
            pool.append(name)
        if name not in GLOBAL_POOL:
            GLOBAL_POOL.append(name)

        # Initialise weight at 1.0 in the mode memory
        weights = get_mode_memory(mode)
        weights.setdefault(name, 1.0)
        save_memory(memory)

        print(f"  Discovery: added '{name}' to [{mode}] pool for trial "
              f"({len(filtered)} clean tracks).")
        return

    print(f"  Discovery: all Last.fm candidates failed quality check for {seed_artist}.")

def record_trial_play(artist):
    """
    Called when a discovered (non-graduated) artist's track plays through 80%+.
    Increments trial_plays and graduates the artist if the threshold is reached.
    """
    if artist not in discovered_artists:
        return
    entry = discovered_artists[artist]
    if entry.get("graduated"):
        return

    entry["trial_plays"] += 1
    print(f"  Trial play {entry['trial_plays']}/{TRIAL_GRADUATION} for '{artist}'")

    if entry["trial_plays"] >= TRIAL_GRADUATION:
        entry["graduated"] = True
        save_discovered(discovered_artists)
        print(f"  '{artist}' has GRADUATED — permanently added to [{entry['mode']}] pool.")
    else:
        save_discovered(discovered_artists)

# =====================
# SPOTIFY CONNECT
# =====================

def retry_seconds(error):
    headers = getattr(error, "headers", None) or {}
    try:
        value = float(headers.get("Retry-After", 30))
        return max(1, value) if math.isfinite(value) and value > 0 else 30
    except (ValueError, TypeError):
        return 30


class SpotifyGateway:
    """Share one cooldown across all Spotify endpoints; no SDK retry sleeps."""
    def __init__(self, client):
        self.client = client

    def __getattr__(self, name):
        method = getattr(self.client, name)
        def call(*args, **kwargs):
            global _rate_limit_until
            if time.time() < _rate_limit_until:
                raise RateLimitError(_rate_limit_until - time.time())
            try:
                return method(*args, **kwargs)
            except spotipy.exceptions.SpotifyException as error:
                if error.http_status == 429:
                    wait = retry_seconds(error)
                    _rate_limit_until = time.time() + wait
                    status(f"Rate-Limit: {wait:.0f}s warten")
                    raise RateLimitError(wait) from None
                raise
        return call


def connect():
    auth_manager = SpotifyOAuth(
        client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI, scope=SCOPE, open_browser=False,
        cache_path=os.path.join(BASE_DIR, ".cache-dj"),
    )
    return SpotifyGateway(spotipy.Spotify(
        auth_manager=auth_manager, retries=0, status_retries=0,
        backoff_factor=0, requests_timeout=8,
    ))


class DeviceUnavailable(Exception):
    pass


def get_active_device_id():
    devices = sp.devices().get("devices", [])
    preferred = os.getenv("SPOTIFY_DEVICE_NAME", "").strip()
    if preferred:
        chosen = next((d for d in devices if d.get("name", "").casefold() == preferred.casefold()
                       and not d.get("is_restricted")), None)
    else:
        chosen = next((d for d in devices if d.get("is_active") and not d.get("is_restricted")), None)
    if not chosen or not chosen.get("id"):
        raise DeviceUnavailable("Spotify-Geraet nicht bereit; Spotify oeffnen oder Geraet aktivieren.")
    return chosen["id"]


sp = None
DEBUG_MODE = os.getenv("DJ_DEBUG", "0") == "1"


def safe_play(_device_id=None, **kwargs):
    """Request once, then confirm the exact track and device in four polls."""
    if DEBUG_MODE:
        status("Debug: kein Playback angefordert")
        return None
    # Automatic transitions already have a just-confirmed owned device.
    device_id = _device_id if _device_id is not None else get_active_device_id()
    expected_uri = kwargs["offset"]["uri"]
    status("Start angefordert; warte auf Spotify-Bestaetigung")
    print("IS_PLAYING:false")
    publish_progress(None)
    sp.start_playback(device_id=device_id, **kwargs)
    for delay in (1, 2, 2, 3):
        time.sleep(delay)
        try:
            pb = sp.current_playback()
        except RateLimitError:
            raise
        except Exception:
            continue
        if (pb and pb.get("is_playing") and (pb.get("item") or {}).get("uri") == expected_uri
                and (pb.get("device") or {}).get("id") == device_id
                and (pb.get("context") or {}).get("uri") == kwargs["context_uri"]):
            return pb
    status("Start nicht bestaetigt; neuer DJ-Befehl erforderlich")
    return None


# =====================
# FILTER FUNCTIONS
# =====================

def is_alternate_version(track):
    track_text = track["name"].lower()
    track_text = track_text.replace("–", "-").replace("_", " ").replace(".", " ")
    track_text = " ".join(track_text.split())

    album_text = (track.get("album") or {}).get("name", "").lower()
    album_text = album_text.replace("–", "-").replace("_", " ").replace(".", " ")
    album_text = " ".join(album_text.split())

    if LANGUAGE_VERSION_PATTERN.search(track["name"]):
        return True
    for kw in TRACK_KEYWORDS_WORD:
        if re.search(rf"\b{re.escape(kw)}\b", track_text):
            return True
    for kw in TRACK_KEYWORDS_SUBSTR:
        if kw in track_text:
            return True
    for kw in ALBUM_KEYWORDS_WORD:
        if re.search(rf"\b{re.escape(kw)}\b", album_text):
            return True
    for kw in ALBUM_KEYWORDS_SUBSTR:
        if kw in album_text:
            return True
    for kw in ALBUM_KEYWORDS_WORD:
        if re.search(rf"\b{re.escape(kw)}\b", track_text):
            return True
    return False

def normalize_title(title):
    title = title.lower()
    title = re.sub(r"\(.*?\)", "", title)
    title = re.sub(r"\[.*?\]", "", title)
    title = title.split("-")[0]
    return title.strip()

# =====================
# SELECTION LOGIC
# =====================

def weighted_choice(pool, mode):
    weights = get_mode_memory(mode)
    scored  = [(a, weights.get(a, 1.0)) for a in pool]
    total   = sum(w for _, w in scored)
    r       = random.uniform(0, total)
    upto    = 0
    for artist, w in scored:
        upto += w
        if upto >= r:
            return artist
    return pool[-1]

# =====================
# TRACK FETCHING + CACHE
# =====================

artist_cache = {}

def fetch_artist_tracks_by_id(artist_name, artist_id):
    entry = track_disk_cache.get(artist_name)
    if entry and (time.time() - entry.get("fetched_at", 0)) / 86400 < CACHE_TTL_DAYS:
        if entry.get("catalog_version") == 2 or select_best_tracks(entry.get("tracks", [])):
            return entry["tracks"]
        print(f"  {artist_name}: legacy cache has no eligible tracks; refreshing catalog.")
    if artist_name in artist_cache:
        return artist_cache[artist_name]

    tracks, seen = [], set()
    # Installed Spotipy's public method still sends 'country' and defaults to
    # 20. Spotify now documents 'market' and a maximum of 10 albums per page.
    # Pass flat kwargs: _get(params={...}) sends the wrong nested query key.
    response = sp._get(f"artists/{artist_id}/albums",
                       include_groups="album,single", market="DE", limit=10)
    for page in range(20):
        for album in response.get("items", []):
            if album.get("id") in seen:
                continue
            seen.add(album.get("id"))
            # Avoid spending one request per live album when none of its tracks
            # can pass our existing album filters.
            if is_alternate_version({"name": "", "album": album}):
                continue
            track_page = sp.album_tracks(album["id"], limit=50, market="DE")
            for track_page_number in range(20):
                for item in track_page.get("items", []):
                    if artist_id not in {a.get("id") for a in item.get("artists", [])}:
                        continue
                    t = dict(item, album=album)
                    t.setdefault("popularity", 0)
                    tracks.append(t)
                if not track_page.get("next"):
                    break
                if track_page_number == 19:
                    raise ValueError("Track pagination limit reached; cache preserved")
                track_page = sp.next(track_page)
        if not response.get("next"):
            break
        if page == 19:
            raise ValueError("Album pagination limit reached; cache preserved")
        response = sp.next(response)
    # Publish only a complete fetch. Failures leave the old cache intact.
    artist_cache[artist_name] = tracks
    track_disk_cache[artist_name] = {
        "fetched_at": time.time(), "tracks": tracks, "catalog_version": 2,
    }
    save_track_cache()
    return tracks


def get_artist_tracks(artist_name):
    """Fetch tracks for a permanent artist (looks up ID from ARTISTS dict)."""
    artist_id = ARTISTS.get(artist_name) or ai_artists.get(artist_name)
    if not artist_id:
        # Artist is in a pool but not in ARTISTS — stale cache entry from a
        # discovery that was later removed. Skip it cleanly instead of crashing.
        print(f"  {artist_name} not found in ARTISTS dict, skipping.")
        return []
    return fetch_artist_tracks_by_id(artist_name, artist_id)

# =====================
# TRACK SELECTION
# =====================

def select_best_tracks(tracks, min_duration_ms=90000, allow_compilations=False):
    grouped = {}
    for t in tracks:
        if not isinstance(t, dict):
            continue
        album = t.get("album") or {}
        if not isinstance(album, dict):
            continue
        album_uri, track_uri = album.get("uri"), t.get("uri")
        if not (isinstance(album_uri, str) and isinstance(track_uri, str)
                and re.fullmatch(r"spotify:album:[A-Za-z0-9]+", album_uri)
                and re.fullmatch(r"spotify:track:[A-Za-z0-9]+", track_uri)
                and isinstance(t.get("name"), str) and isinstance(album.get("name"), str)):
            continue
        if album.get("album_type") in ["compilation", "appears_on"] and not allow_compilations:
            continue
        # Word boundaries are handled by is_alternate_version. Substring
        # matching the word 'live' would also reject a studio album 'Believe'.
        if is_alternate_version(t):
            continue

        track_artist_ids = {a.get("id") for a in t.get("artists", [])}
        if track_artist_ids & BLOCKED_ARTIST_IDS:
            continue

        # Skip tracks shorter than 90 seconds (intros, skits, talking tracks)
        if (t.get("is_playable") is False or not isinstance(t.get("duration_ms"), (int, float))
                or t["duration_ms"] < min_duration_ms):
            continue

        title     = normalize_title(t["name"])
        score_new = (1 if t.get("explicit", False) else 0) * 3 \
                  + (1 if t["album"].get("album_type") == "single" else 0) * 2 \
                  + t.get("popularity", 0)

        if title not in grouped:
            grouped[title] = (t, score_new)
            continue

        _, score_old = grouped[title]
        if score_new > score_old:
            grouped[title] = (t, score_new)

    return [t for t, _ in grouped.values()]

# =====================
# PLAY FUNCTIONS
# =====================

def start_selected(name, mode, chosen, no_judge, device_id=None):
    global _prepared_next, ai_last_artist
    _prepared_next = None
    pb = safe_play(
        _device_id=device_id,
        context_uri=chosen["album"]["uri"],
        offset={"uri": chosen["uri"]}, position_ms=0,
    )
    if not pb:
        tracker.control = False
        return False
    ai_session.record_play(name, chosen)
    tracker.start(name, mode, chosen, pb, time.monotonic(), ai=no_judge,
                  trial=name in discovered_artists and not discovered_artists[name].get("graduated", False))
    if no_judge:
        ai_recent_titles.append(normalize_title(chosen["name"]))
        ai_artist_plays[name] = ai_artist_plays.get(name, 0) + 1
        ai_last_artist = name
        if ai_song_queue and ai_song_queue[0][1]["uri"] == chosen["uri"]:
            ai_song_queue.pop(0)
    else:
        recent_titles.append(normalize_title(chosen["name"]))
        recent_artists.append(name)
        save_recent()
    log_section("Neuer Song")
    print(f"{'Global DJ' if mode == 'global' else 'DJ'} -> {name}")
    print(f"  {name} — {chosen['name']}")
    if no_judge and ai_request_plan is not None and ai_request_plan.year_start is not None:
        year = chosen.get("_dj_source_year", release_year(chosen))
        print(f"  AI year evidence: {year} ({chosen.get('_dj_era_basis', 'Spotify-Katalogdatum')})")
    print(f"CURRENT_ARTIST:{name}")
    print(f"NOW_PLAYING:{chosen['name']}")
    print("IS_PLAYING:true")
    publish_progress(pb)
    status("DJ aktiv")
    if not no_judge:
        # Discovery must not delay the outgoing song's critical end window.
        run_pending_discoveries()
    return True


def play_artist(name, mode, pool=None, interrupted=True, mode_switch=False, banned=False, no_judge=False):
    global _pending_selection, _next_device_check, _prepared_next
    _pending_selection = None
    _prepared_next = None
    if mode_switch and not no_judge:
        label = {"american_rap": "American Rap", "german_trap": "German Trap",
                 "kpop": "K-Pop", "jpop": "J-Pop", "global": "Global"}.get(mode, mode)
        log_section(f"Modus: {label}")
    print(f"MODE:{'AI' if no_judge else mode}")
    # Refresh before explicit skips, so a manual switch/device change cannot
    # accidentally penalize the formerly owned artist.
    if interrupted and not mode_switch and tracker.control:
        track_finished()
    judge_last_track(interrupted=interrupted,
                     mode_switch=mode_switch or no_judge or not tracker.control, banned=banned)
    tracker.control = False
    publish_progress(None)
    if now_playing:
        now_playing["confirmed"] = False
    # An AI fallback is always restricted to the active request, irrespective
    # of a caller passing the global pool. Each artist is tried at most once.
    allowed = [n for n, _ in ai_pool] if no_judge else list(pool or [name])
    if no_judge and not ensure_ai_catalogs():
        return False
    if no_judge and explicit_songs_pending():
        if ai_song_queue:
            name, chosen = ai_song_queue[0]
            if chosen.get("id") in banned_track_ids or name in banned_artist_names:
                status("Der naechste Songwunsch ist gesperrt; Anfrage bitte anpassen")
                return False
            if not ai_session.allows(ai_request_plan, name, chosen, explicit=True):
                status("Der Songwunsch widerspricht deinen Session-Ausschluessen")
                return False
            try:
                return start_selected(name, "ai", chosen, True)
            except DeviceUnavailable:
                _pending_selection = (name, "ai", chosen, True)
                _next_device_check = time.monotonic() + 15
                status("Warte auf Spotify-Geraet fuer den Songwunsch")
                return False
            except Exception as error:
                status(f"Songwunsch nicht gestartet ({type(error).__name__}); erneut versuchen")
                return False
        status("Songwuensche abgeschlossen; neuer DJ-Befehl erforderlich")
        return False
    candidates = (ai_artist_order(allowed, preferred=name) if no_judge else
                  ([name] if name in allowed else []) + [n for n in allowed if n != name])
    candidates = list(dict.fromkeys(candidates))
    for candidate in candidates:
        try:
            tracks = ai_session_catalogs[candidate] if no_judge else get_artist_tracks(candidate)
            filtered = ai_track_versions(tracks) if no_judge else select_best_tracks(tracks)
            print(f"  {candidate}: fetched={len(tracks)}, eligible={len(filtered)}")
            available = [t for t in filtered if t.get("id") not in banned_track_ids
                         and normalize_title(t["name"]) not in recent_titles
                         and normalize_title(t["name"]) not in ai_recent_titles
                         and (not no_judge or ai_session.allows(ai_request_plan, candidate, t))]
            if not available:
                print(f"  {candidate}: keine passenden, nicht kuerzlich gehoerten Tracks.")
                continue
            chosen = random.choice(available)
            try:
                return start_selected(candidate, mode, chosen, no_judge)
            except DeviceUnavailable:
                _pending_selection = (candidate, mode, chosen, no_judge)
                _next_device_check = time.monotonic() + 15
                status("Warte auf Spotify-Geraet; erneute Pruefung in 15s")
                print("IS_PLAYING:false")
                return False
        except RateLimitError:
            status("Spotify Rate-Limit; nach Wartezeit mit DJ-Befehl fortsetzen")
            return False
        except Exception as error:
            # A playback failure must not silently start a different artist.
            status(f"Spotify-Fehler ({type(error).__name__}); neuer DJ-Befehl erforderlich")
            print("IS_PLAYING:false")
            return False
    status("Keine passenden Tracks im angefragten Pool; neuer DJ-Befehl erforderlich")
    print("IS_PLAYING:false")
    return False


def service_pending_start():
    global _pending_selection, _next_device_check
    if not _pending_selection or time.monotonic() < _next_device_check or time.time() < _rate_limit_until:
        return
    args = _pending_selection
    _next_device_check = time.monotonic() + 15
    try:
        start_selected(*args)
        _pending_selection = None
    except DeviceUnavailable:
        pass  # A waiting state, not repeated start_playback requests.
    except RateLimitError:
        _pending_selection = None
        status("Rate-Limit beim Start; nach Wartezeit neuen DJ-Befehl senden")
    except Exception as error:
        _pending_selection = None
        status(f"Startfehler ({type(error).__name__}); neuer DJ-Befehl erforderlich")


def publish_progress(pb):
    """Display-only telemetry from an existing API response; never learning input."""
    item = (pb or {}).get("item") or {}
    position, duration = (pb or {}).get("progress_ms"), item.get("duration_ms")
    sample = None
    if (item.get("uri") and isinstance(position, (int, float)) and math.isfinite(position)
            and position >= 0 and isinstance(duration, (int, float))
            and math.isfinite(duration) and duration > 0):
        sample = dict(uri=item["uri"], progress_ms=min(position, duration),
                      duration_ms=duration, is_playing=bool(pb.get("is_playing")))
    print("PLAYBACK_PROGRESS:" + json.dumps(sample, ensure_ascii=False))


def publish_playback(pb):
    if pb and pb.get("item"):
        if pb.get("is_playing"):
            names = ', '.join(a.get('name', '') for a in pb['item'].get('artists', []))
            ai_session.record_play(names, pb['item'])
        print(f"NOW_PLAYING:{pb['item'].get('name', '—')}")
        print(f"CURRENT_ARTIST:{', '.join(a.get('name', '') for a in pb['item'].get('artists', []))}")
    else:
        print("NOW_PLAYING:—")
        print("CURRENT_ARTIST:—")
    print(f"IS_PLAYING:{str(bool(pb and pb.get('is_playing') and pb.get('item'))).lower()}")
    publish_progress(pb)


def observe_external_playback():
    """Keep the dashboard current after release, without reclaiming control."""
    global _next_external_poll
    if (tracker.control or _pending_selection or not auto_mode
            or time.monotonic() < _next_external_poll or time.time() < _rate_limit_until):
        return
    _next_external_poll = time.monotonic() + 15
    try:
        publish_playback(sp.current_playback())
    except Exception:
        publish_playback(None)


def play_from_pool(pool, mode, interrupted=True, mode_switch=False, banned=False):
    play_artist(weighted_choice(pool, mode), mode, pool, interrupted=interrupted, mode_switch=mode_switch, banned=banned)

def play_global_mix(interrupted=True, mode_switch=False, banned=False):
    artist = weighted_choice(GLOBAL_POOL, "global")
    play_artist(artist, "global", GLOBAL_POOL, interrupted=interrupted, mode_switch=mode_switch, banned=banned)


def active_request():
    if auto_mode == "ai":
        return "ai", tuple(n for n, _ in ai_pool), True
    if auto_mode == "global":
        return "global", tuple(GLOBAL_POOL), False
    return auto_mode, tuple(current_pool or ()), False


def ai_artist_order(pool, preferred=None):
    """Random ties, less-played artists first, current artist as last fallback."""
    names = list(dict.fromkeys(n for n in pool if n not in banned_artist_names))
    random.shuffle(names)
    names.sort(key=lambda n: (n == ai_last_artist, ai_artist_plays.get(n, 0),
                              n != preferred if preferred is not None else False))
    return names


def ai_track_versions(tracks):
    targeted = ai_request_plan is not None and (ai_request_plan.kind == "songs" or ai_request_plan.selection == "hits")
    return select_best_tracks(tracks, min_duration_ms=1000 if targeted else 90000,
                              allow_compilations=targeted)


def ensure_ai_catalogs():
    """Load the requested pool before playing, avoiding cache-biased transitions.

    Calls are sequential and reuse valid caches. A technical failure stops this
    request instead of silently reducing it to whichever artist was cached first.
    """
    if ai_request_plan is not None:
        return ensure_structured_request()
    for name, _ in ai_pool:
        if name in ai_session_catalogs or name in banned_artist_names:
            continue
        print(f"  AI: preparing catalog for {name}...")
        try:
            tracks = get_artist_tracks(name)
        except RateLimitError:
            status("AI-Katalogvorbereitung durch Rate-Limit pausiert; nach Wartezeit erneut starten")
            return False
        except Exception as error:
            status(f"AI-Katalog fuer {name} nicht ladbar ({type(error).__name__}); erneut versuchen")
            return False
        ai_session_catalogs[name] = tracks
        print(f"  AI: {name}: {len(select_best_tracks(tracks))} passende Titel")
    return True


def explicit_songs_pending():
    return (auto_mode == "ai" and ai_request_plan is not None and ai_request_plan.kind == "songs"
            and (bool(ai_song_queue) or not ai_request_plan.continue_similar))


def ensure_structured_request():
    global ai_request_tracks_ready
    if ai_request_tracks_ready:
        return True
    plan = ai_request_plan
    raw = {}
    try:
        for name, _ in ai_pool:
            if name in banned_artist_names:
                raw[name] = []
            elif plan.kind == "songs" and not plan.continue_similar:
                # A single song wish does not require downloading a whole catalog.
                raw[name] = artist_cache.get(name, track_disk_cache.get(name, {}).get("tracks", []))
            else:
                print(f"  AI: preparing constrained catalog for {name}...")
                raw[name] = get_artist_tracks(name)
        catalogs = {name: [t for t in select_best_tracks(constrain_tracks(tracks, plan, tracks))
                           if ai_session.allows(plan, name, t)] for name, tracks in raw.items()}
        if plan.songs:
            qualify = lambda ts: [t for t in select_best_tracks(ts, min_duration_ms=1000, allow_compilations=True)
                                  if t.get("id") not in banned_track_ids
                                  and not ({a.get('name') for a in t.get('artists', [])} & banned_artist_names)
                                  and ai_session.allows(plan, '', t, explicit=plan.kind == 'songs')]
            found, missing = resolve_song_wishes(sp, plan, ai_pool, raw, qualify, ai_song_resolutions)
            for title in missing:
                print(f"  AI: nicht eindeutig/passend auf Spotify aufgeloest: {title}")
            if plan.kind == "songs" and missing:
                status("Songwunsch nicht vollstaendig aufloesbar: " + "; ".join(missing) + ". Bitte konkretisieren.")
                return False
            if plan.kind == "songs":
                ai_song_queue.extend(found)
            elif plan.selection == "hits":
                catalogs = {name: [] for name, _ in ai_pool}
                for name, chosen in found:
                    if chosen["uri"] not in {t["uri"] for t in catalogs[name]}:
                        catalogs[name].append(chosen)
            if plan.selection == "hits" and not found:
                status("Keine passenden, auf Spotify geprueften Hits gefunden; Anfrage bitte anpassen")
                return False
        ai_session_catalogs.update(catalogs)
        ai_request_tracks_ready = True
        print(f"  AI: {sum(len(ts) for ts in catalogs.values())} passende Titel; {len(ai_song_queue)} konkrete Songwuensche")
        return True
    except RequestValidationError as error:
        ai_session.note_problem(ai_session.turns[-1] if ai_session.turns else "", str(error))
        status(f"AI: {error}")
        return False
    except RateLimitError:
        status("AI-Aufloesung durch Rate-Limit pausiert; nach Wartezeit erneut starten")
        return False
    except Exception as error:
        status(f"AI-Aufloesung fehlgeschlagen ({type(error).__name__}); erneut versuchen")
        return False


def finish_song_request(device_id, reason="Songwuensche abgeschlossen"):
    """Do not let an explicit finite list spill into unrelated album tracks."""
    tracker.control = False
    now_playing["confirmed"] = False
    try:
        sp.pause_playback(device_id=device_id)
        for _ in range(2):
            time.sleep(.5)
            pb = sp.current_playback()
            if pb and (pb.get("device") or {}).get("id") == device_id and not pb.get("is_playing"):
                publish_playback(pb)
                status(reason + "; Wiedergabe pausiert")
                return
        status(reason + "; Pause nicht bestaetigt, bitte Spotify pruefen")
    except Exception:
        publish_playback(None)
        status(reason + "; Spotify bitte manuell pausieren")


def prepared_next_is_current():
    return bool(_prepared_next and _prepared_next["owner"] == now_playing.get("generation")
                and _prepared_next["request"] == active_request())


def prepare_next_track():
    """Select once from available caches; never perform I/O in the end window."""
    global _prepared_next
    t = now_playing
    if (not tracker.control or not t.get("confirmed") or not t.get("playing")
            or t.get("preparation_attempted")
            or not 0 <= t["duration"] - t["progress_ms"] <= PREPARE_LEAD_MS):
        return
    t["preparation_attempted"] = True
    request = active_request()
    mode, pool, ai = request
    if not pool or mode != t["mode"] or ai != t["is_ai_play"]:
        return
    if ai and explicit_songs_pending():
        if ai_song_queue:
            name, chosen = ai_song_queue[0]
            _prepared_next = dict(owner=t["generation"], request=request, selection=(name, "ai", chosen, True), explicit=True)
        else:
            _prepared_next = dict(owner=t["generation"], request=request, stop=True)
        return
    if ai:
        candidates = ai_artist_order(pool)
    else:
        first = weighted_choice(pool, mode)
        candidates = list(dict.fromkeys((first,) + pool))
    for name in candidates:
        if name in banned_artist_names:
            continue
        tracks = ai_session_catalogs.get(name) if ai else None
        if tracks is None:
            tracks = artist_cache.get(name)
        if tracks is None:
            entry = track_disk_cache.get(name, {})
            tracks = (entry.get("tracks", []) if
                      (time.time() - entry.get("fetched_at", 0)) / 86400 < CACHE_TTL_DAYS else [])
        versions = ai_track_versions(tracks) if ai else select_best_tracks(tracks)
        available = [t for t in versions
                     if t.get("id") not in banned_track_ids
                     and normalize_title(t["name"]) not in recent_titles
                     and normalize_title(t["name"]) not in ai_recent_titles
                     and (not ai or ai_session.allows(ai_request_plan, name, t))]
        if available:
            _prepared_next = dict(owner=now_playing["generation"], request=request,
                                  selection=(name, mode, random.choice(available), ai))
            return
    if ai and ai_request_plan is not None and ai_request_tracks_ready:
        _prepared_next = dict(owner=t["generation"], request=request, stop=True,
                              reason="Passende AI-Titel fuer diese Anfrage ausgeschoepft")


def advance_to_next_track():
    global _prepared_next
    if prepared_next_is_current():
        if _prepared_next.get("stop"):
            reason = _prepared_next.get("reason", "Songwuensche abgeschlossen")
            _prepared_next = None
            finish_song_request(now_playing["device_id"], reason)
            return
        explicit = _prepared_next.get("explicit", False)
        selection = _prepared_next["selection"]
        _prepared_next = None
        tracker.control = False
        chosen = selection[2]
        if (chosen.get("id") in banned_track_ids or selection[0] in banned_artist_names
                or (selection[3] and not ai_session.allows(ai_request_plan, selection[0], chosen, explicit=explicit))
                or (not explicit and (normalize_title(chosen["name"]) in recent_titles
                                      or normalize_title(chosen["name"]) in ai_recent_titles))):
            status("Vorbereiteter Titel nicht mehr gueltig; neuer DJ-Befehl erforderlich")
            return
        try:
            start_selected(*selection, device_id=now_playing["device_id"])
        except RateLimitError:
            status("Rate-Limit beim Uebergang; nach Wartezeit neuen DJ-Befehl senden")
        except Exception as error:
            status(f"Uebergang fehlgeschlagen ({type(error).__name__}); neuer DJ-Befehl erforderlich")
            publish_playback(None)
        return  # A failed start must never trigger a second fallback start.
    _prepared_next = None
    if explicit_songs_pending() and not ai_song_queue:
        finish_song_request(now_playing["device_id"])
        return
    if ai_pool:
        play_artist(random.choice(ai_pool)[0], "ai", no_judge=True, interrupted=False)
    elif auto_mode == "global":
        play_global_mix(interrupted=False)
    elif current_pool:
        play_from_pool(current_pool, auto_mode, interrupted=False)


def playback_poll_interval():
    t = now_playing
    if (prepared_next_is_current() and tracker.control and t.get("confirmed") and t.get("playing")
            and t.get("sampled_at") is not None and t.get("stable_since") is not None
            and time.monotonic() - t["stable_since"] >= 3):
        age = max(0, time.monotonic() - t["sampled_at"])
        remaining = (t["duration"] - t["progress_ms"] - TRANSITION_LEAD_MS) / 1000 - age
        return max(.25, min(5, remaining))
    return 5

# =====================
# TRANSITION CHECK
# =====================

def track_finished(allow_early=False):
    global _prepared_next
    if not tracker.control or not now_playing.get("confirmed"):
        return False
    if time.time() < _rate_limit_until:
        tracker.gap()
        publish_progress(None)
        return False
    try:
        pb = sp.current_playback()
    except Exception as error:
        tracker.gap()
        status("Rate-Limit; Playback unbekannt" if isinstance(error, RateLimitError)
               else "Playback-Daten voruebergehend unbekannt")
        print("IS_PLAYING:false")
        publish_progress(None)
        return False
    event = tracker.observe(pb, time.monotonic())
    early = False
    if event == "playing" and allow_early:
        prepare_next_track()
        early = (prepared_next_is_current()
                 and (pb.get("context") or {}).get("uri") == now_playing["album_uri"]
                 and tracker.can_finish_early(time.monotonic(), TRANSITION_LEAD_MS))
    if event == "finished" or early:
        if early:
            print("Track near end -- prepared DJ transition")
        judge_last_track()
        now_playing["confirmed"] = False
        publish_progress(None)
        return True
    if event == "manual":
        _prepared_next = None
        judge_last_track(mode_switch=True)
        now_playing["confirmed"] = False
        status("Manueller oder unklarer Wechsel; DJ freigegeben bis zum naechsten DJ-Befehl")
    elif event == "paused":
        status("Pausiert; auf Fortsetzen in Spotify warten")
    elif event == "unknown":
        status("Playback-Daten fehlen; keine Bewertung und kein Neustart")
    elif event == "playing":
        status("DJ aktiv")
    publish_playback(pb)
    return False


# =====================
# MAIN DJ LOOP
# =====================

def run_dj():
    global current_pool, auto_mode, _pending_selection, _prepared_next, ai_last_artist
    global ai_request_plan, ai_request_tracks_ready

    while True:
        if os.path.exists(INPUT_FILE):
            try:
                with open(INPUT_FILE, encoding="utf-8-sig") as f:
                    choice = f.read().strip()
                os.remove(INPUT_FILE)
            except Exception:
                time.sleep(1)
                continue

            if choice in ("1", "2", "3", "4", "5"):
                ai_session.leave_ai()

            if choice == "quit":
                print("DJ shutting down via hotkey.")
                sys.exit(0)

            elif choice == "skip":
                log_section("Skip")
                print("Skip hotkey -- playing next track.")
                if ai_pool:
                    name, artist_id = random.choice(ai_pool)
                    play_artist(name, "ai", [n for n, _ in ai_pool], interrupted=True, no_judge=True)
                elif auto_mode == "global":
                    play_global_mix(interrupted=True)
                elif current_pool:
                    play_from_pool(current_pool, auto_mode, interrupted=True)

            elif choice == "ban":
                log_section("Song sperren")
                ban_current_track()

            elif choice == "ban-artist":
                log_section("Kuenstler sperren")
                ban_current_artist()

            elif choice == "1":
                auto_mode    = "american_rap"
                current_pool = AMERICAN_RAP_POOL
                ai_pool.clear()
                ai_artists.clear()
                play_from_pool(current_pool, "american_rap", mode_switch=True)
            elif choice == "2":
                auto_mode    = "german_trap"
                current_pool = GERMAN_TRAP_POOL
                ai_pool.clear()
                ai_artists.clear()
                play_from_pool(current_pool, "german_trap", mode_switch=True)
            elif choice == "3":
                auto_mode    = "kpop"
                current_pool = KPOP_POOL
                ai_pool.clear()
                ai_artists.clear()
                play_from_pool(current_pool, "kpop", mode_switch=True)
            elif choice == "4":
                auto_mode    = "jpop"
                current_pool = JPOP_POOL
                ai_pool.clear()
                ai_artists.clear()
                play_from_pool(current_pool, "jpop", mode_switch=True)
            elif choice == "5":
                auto_mode = "global"
                current_pool = None
                ai_pool.clear()
                ai_artists.clear()
                play_global_mix(mode_switch=True)

            elif choice.startswith("ai:"):
                log_section("AI-Anfrage")
                _pending_selection = None
                _prepared_next = None
                auto_mode = "ai"
                current_pool = None
                judge_last_track(mode_switch=True)
                tracker.control = False
                if now_playing:
                    now_playing["confirmed"] = False
                ai_pool.clear()
                ai_artists.clear()
                ai_session_catalogs.clear()
                ai_artist_plays.clear()
                ai_last_artist = None
                ai_request_plan = None
                ai_request_tracks_ready = False
                ai_song_queue.clear()
                ai_song_resolutions.clear()
                user_request = choice[3:].strip()
                if is_session_reset(user_request):
                    ai_session.clear()
                    ai_recent_titles.clear()
                    publish_progress(None)
                    status("AI-Session zurueckgesetzt; Steuerung freigegeben, neue Musikanfrage eingeben")
                    continue
                if not user_request:
                    print("  AI: empty request, ignoring.")
                else:
                    print(f"  AI request: '{user_request}'")
                    try:
                        ai_request_plan = ai_session.reconcile(ask_claude(user_request, ai_session.context()))
                        ai_session.accept(ai_request_plan, user_request)
                        artist_names = ai_request_plan.artists
                        print("  AI interpretation: " + ai_request_plan.summary())
                        print("  AI session: " + ("Folgeanfrage" if ai_request_plan.context_action == 'refine' else "Neue Musikauswahl")
                              + f"; {len(ai_request_plan.excluded_artists)} Kuenstler-/{len(ai_request_plan.excluded_songs)} Song-Ausschluesse"
                              + ("; keine Session-Wiederholungen" if ai_request_plan.avoid_session_repeats else ""))
                        print(f"  AI suggested: {artist_names}")

                        # Build artist dict for ID resolution only
                        all_artists = dict(ARTISTS)
                        for name, entry in discovered_artists.items():
                            if name not in all_artists:
                                all_artists[name] = entry["id"]

                        resolved = resolve_artists_to_ids(sp, artist_names, all_artists)
                        if not resolved:
                            status("AI: keine angefragten Kuenstler gefunden; neuer DJ-Befehl erforderlich")
                        else:
                            # Store as the active AI pool and start playing
                            ai_pool.clear()
                            ai_artists.clear()
                            ai_pool.extend(resolved)

                            # Temporary lookup only; no preference/pool changes.
                            for name, artist_id in resolved:
                                if name not in ARTISTS and name not in discovered_artists:
                                    ai_artists[name] = artist_id
                                    print(f"  AI: registered temporary artist '{name}'")
                            
                            name, artist_id = random.choice(ai_pool)
                            auto_mode    = "ai"
                            current_pool = None
                            play_artist(name, "ai", [n for n, _ in ai_pool], interrupted=False, mode_switch=True, no_judge=True)

                    except RequestValidationError as error:
                        ai_session.note_problem(user_request, str(error))
                        status(f"AI: {error}")
                    except Exception as e:
                        status(f"AI-Anfrage fehlgeschlagen ({type(e).__name__}); neuer DJ-Befehl erforderlich")

            # Clear ai_pool when any mode hotkey is pressed (1-5)

        # A fresh owned sample permits the agreed 1.5s early transition.
        service_pending_start()
        observe_external_playback()
        if track_finished(allow_early=True):
            advance_to_next_track()

        time.sleep(playback_poll_interval())

def main():
    global sp, memory, discovered_artists
    import atexit
    acquire_lock()
    atexit.register(release_lock)
    load_track_cache()
    load_recent()
    load_banned()
    load_banned_artists()
    memory = load_memory()
    discovered_artists = load_discovered()
    inject_discovered_into_pools()
    sp = connect()
    status("Bereit; Modus waehlen. Spotify darf beim Autostart noch geschlossen sein.")
    # Fetch catalogs on demand rather than flooding Spotify at Windows startup.
    while True:
        try:
            run_dj()
        except Exception as error:
            tracker.gap()
            tracker.control = False
            status(f"DJ-Fehler ({type(error).__name__}); auf neuen DJ-Befehl warten")
            time.sleep(5)


if __name__ == "__main__":
    main()
