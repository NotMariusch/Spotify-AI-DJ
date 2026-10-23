# Spotify AI DJ

A local automated Spotify DJ that runs as a background service on Windows. It selects artists from curated pools, filters tracks, learns your listening preferences over time, discovers new artists similar to ones you already enjoy, and supports natural language requests via an AI chat mode powered by Claude.

## Project Structure

```
SpotifyDJ/
├── src/
│   ├── spotify_dj.py           # Main DJ script
│   ├── ai_request.py           # Claude API integration for AI chat mode
│   ├── dj_server.py            # Flask/websockets dashboard server
│   └── dj_dashboard.html       # Browser dashboard UI
├── scripts/
│   ├── dj_hotkey.ahk           # AutoHotkey hotkey script
│   ├── dj_chat.py              # AI chat terminal interface (type requests)
│   └── dj_voice.py             # AI voice interface (F13+V to record)
├── data/                        # Runtime data — all gitignored except .gitkeep
│   ├── track_cache.json         # Cached artist tracks (7 day TTL)
│   ├── dj_memory.json           # Artist weights per mode
│   ├── recent_tracks.json       # Recently played titles (persists across restarts)
│   ├── discovered_artists.json  # Trial and graduated discovered artists
│   ├── dj.lock                  # Instance lock file (prevents duplicate processes)
│   ├── dj_input.txt             # Hotkey/chat input (written by AHK, dj_chat.py, or dj_voice.py)
│   ├── banned_tracks.json       # Permanently banned track IDs
│   ├── banned_artists.json      # Permanently banned discovered artist names
│   └── dj_crash.log            # Crash and exit log
├── .cache                       # Spotify OAuth token for dj_server.py (gitignored)
├── .cache-dj                    # Spotify OAuth token for spotify_dj.py (gitignored)
├── start_dj.bat                 # Launch with visible console (for testing)
├── start_dj_hidden.vbs          # Launch hidden in background (normal use)
├── start_voice_hidden.vbs       # Launch voice mode hidden in background
├── .env                         # API credentials — never committed
├── .gitignore
├── README.md
└── requirements.txt
```

## Features

- **5 modes** switchable via hotkeys at any time
- **AI chat mode** — type a natural language request and the DJ uses Claude to pick matching artists, searches Spotify for them, and plays continuously from that selection until you switch modes or send a new request. AI plays are fully isolated from the weight system
- **Web dashboard** — browser-based control panel at `http://127.0.0.1:5001`. Shows live DJ output and voice/chat logs in real time, displays current artist and track name, active mode indicator, playback status, and buttons for all controls (skip, ban, modes, AI requests)
- **AI voice mode** — press F13+V (Right Ctrl + V) to start recording, press again to stop. Works globally even in fullscreen games. Transcribed via Google Speech Recognition and sent to the AI DJ
- **Smart track filtering** — removes remixes, live versions, sped-up/slowed, language alternate versions, concert recordings, and other alternates automatically
- **Weight system** — tracks play-through rate per artist per mode and adjusts selection probability over time. Artists you consistently listen through get picked more often; artists you skip get picked less
- **Weight decay** — artist weights drift back toward neutral (1.0) over time so old boosts and penalties don't last forever. Applied automatically on each startup based on days elapsed since last run
- **Artist discovery** — when an artist's weight crosses a threshold, the DJ queries Last.fm for similar artists, resolves each candidate on Spotify, quality-checks their catalog, and adds passing candidates to the pool for a trial period. Artists that earn enough play-throughs are permanently saved
- **Continuous playback** — plays songs back to back automatically, picking a new weighted-random artist from the active mode's pool after each track. Pausing in Spotify stops the DJ without auto-resuming — resume manually via your keyboard, Spotify app, or switch modes to start a new track
- **Persistent track cache** — fetches each artist's catalog once and caches it for 7 days, keeping API calls near zero during normal use
- **Persistent recent history** — remembers recently played tracks across restarts to avoid immediate repeats
- **Skip hotkey** — F13+6 skips the current track, applies the correct weight penalty, and immediately picks the next track from the active mode's pool
- **Ban track** — F13+8 permanently bans the current track. It will never be played again. Does not affect artist weight
- **Ban artist** — F13+7 permanently bans the current discovered artist. Removes them from all pools, weights, and track cache and prevents re-discovery. Only works on discovered artists, not permanent pool artists
- **Duplicate instance protection** — lock file prevents two instances running simultaneously
- **Crash recovery** — automatically restarts after unhandled exceptions and logs all crashes and exits to `data/dj_crash.log`

## Setup

**1. Clone the repo:**
```
git clone https://github.com/NotMariusch/spotify-ai-dj
cd spotify-ai-dj
```

**2. Create the data folder placeholder** (if it doesn't exist after cloning):
```
type nul > data\.gitkeep
```

**3. Install dependencies:**
```
pip install -r requirements.txt
```

**4. Create a `.env` file in the project root:**
```
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
LASTFM_API_KEY=your_lastfm_api_key
ANTHROPIC_API_KEY=your_anthropic_api_key
```

**5. Register a Last.fm API key** (free, required for artist discovery):
- Create an account at [last.fm](https://www.last.fm) (use mobile app if the website registration is broken)
- Go to [last.fm/api/account/create](https://www.last.fm/api/account/create) and fill in any application name
- Copy the API key into your `.env` as `LASTFM_API_KEY`

**6. Register a Spotify app:**
- Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
- Create an app and add `http://127.0.0.1:8888/callback` as a redirect URI
- Copy the Client ID and Secret into your `.env`

**7. Get a Claude API key** (required for AI chat/voice mode):
- Sign up at [console.anthropic.com](https://console.anthropic.com)
- Add credits (a small amount like $5 lasts hundreds of requests at this usage level)
- Copy the API key into your `.env` as `ANTHROPIC_API_KEY`

**8. Install AutoHotkey v2** and run `scripts/dj_hotkey.ahk` (can be set to run on startup)

**9. Authorize Spotify for the DJ script (one-time setup):**

`spotify_dj.py` uses a separate token cache (`.cache-dj`) from the dashboard server. You need to authorize it once manually before running it through the dashboard, otherwise it will crash silently because it has no console to prompt you in.

Open a terminal in the project root and run:
```
python src\spotify_dj.py
```
It will print an authorization URL. Open it in your browser, authorize the app, then paste the redirect URL back into the terminal. Once done, press Ctrl+C to stop the script. The `.cache-dj` file is now created and the DJ will authorize silently from this point forward.

**10. Open Spotify and start playing anything on your target device, then start the dashboard:**
```
start_dj.bat
```
The first run fetches and caches all artists (~1 minute with rate-limit delays). After that, subsequent startups load from cache instantly.

> **Note:** Spotify enforces strict API rate limits. If you hit a limit during the first fetch (startup will print a warning and stop early), the DJ will still run using whichever artists were cached successfully. The missing artists will be retried automatically on the next startup once the ban window expires (can be several hours). Do not delete `track_cache.json` between runs unless necessary.

**11. For normal background use:**
```
start_dj_hidden.vbs
```
This launches the DJ with no visible window. The VBS also prevents duplicate instances from starting.

## AI Chat Mode

With the DJ running, open a second terminal and run:
```
python scripts/dj_chat.py
```

Type any natural language request and press Enter:
```
You: play some current pop hits
You: I want chill anime music
You: give me hype rap
You: something dark and moody
You: Ado                          ← plays only Ado
You: Ado and Eve                  ← plays only those two artists
```

When you name one or more specific artists, the DJ plays only those artists. When you describe a vibe, genre, or mood, the DJ asks Claude to pick 5–8 matching artists.

Claude interprets the request into validated fields for artists, songs, years, style, mood, and hit selection. Spotify supplies all actual artist/track IDs. AI plays never affect your weight system or permanent artist pools.

Artist lookup checks up to ten candidates using an artist-filtered search, with at most one plain-name fallback. Accents and punctuation are normalized; `Pink` explicitly resolves as `P!nk`, never as `PinkPantheress`. Ambiguous exact-name profiles are skipped rather than guessed. The search size follows the [current Spotify Search API limit](https://developer.spotify.com/documentation/web-api/reference/search).

Before the first AI track starts, catalogs for the resolved request pool are prepared sequentially, reusing valid caches. This can make a new request slower to start. Rate limits stop preparation instead of silently reducing the session to the first cached artist; retry after the indicated cooldown. No catalog requests run in the automatic transition's end window.

Rotation chooses randomly among the less-played requested artists and avoids immediately repeating an artist whenever another requested artist has an eligible track. One-artist requests remain one-artist requests. These counters update only after confirmed playback, live only in memory, and reset on a new AI request. They are rotation bookkeeping, not a learned preference profile; they never change normal weights, artist pools, trial progress, or persistent recent history. The catalog cache stores Spotify metadata only.

A request such as `2000s pop` restricts tracks to 2000–2009. For hits/bangers/nostalgia, Claude supplies concrete song suggestions and the DJ uses only suggestions resolved to the correct Spotify artist and title, within the requested years. Genre, mood, and how recognizable a song is remain model recommendations; Spotify metadata verifies identity and dates, not subjective fit. Invalid or missing suggestions do not fall back to arbitrary catalog tracks.

Era checks use Spotify album release dates and the earliest observed matching catalog release. Later reissues require an identical ISRC with a known release in the requested period; unknown dates are excluded. This is conservative catalog evidence, not a guarantee of the original worldwide release date. Some valid songs may be unavailable when that evidence is missing.

Concrete song wishes and lists are played in order (for example, `Play Complicated by Avril Lavigne, then Toxic by Britney Spears`). A missing or materially ambiguous requested song stops resolution with an explanation instead of silently replacing it or starting a partial list. Only an explicit request for similar music enables continuation after the list. Otherwise playback pauses after the final song, using the agreed approximately 1.5-second early transition to prevent album playback from spilling into an unrelated song. A curated hits pool also pauses when its non-recent matching titles are exhausted; it does not automatically make another paid model request.

Resolution reuses catalogs and makes at most two track searches per song suggestion (up to 40 suggestions). A simple finite song list does not download complete artist catalogs. Preparation is sequential; a rate limit stops work, and a DJ skip after the cooldown resumes completed lookups from temporary memory. Every new AI request resets that lookup state. No temporary wishes are automatically added to permanent taste preferences.

To return to a normal mode, use any hotkey (F13+1 through F13+5) or send a new AI request.

You can also send regular commands through the chat window:
```
You: skip
You: ban
You: 3        ← switches to K-Pop mode
```

## AI Voice Mode

With the DJ running, open a second terminal and run:
```
python scripts/dj_voice.py
```

Press **F13+V** (Right Ctrl + V) once to start recording, press again to stop. The voice input is transcribed by Google Speech Recognition (free, no API key needed) and sent to Claude as an AI request — same as typing in the chat window.

Works globally even when you are tabbed into a fullscreen game. The terminal window just needs to stay open in the background.

**Tips for best results:**
- Describe the vibe rather than naming specific artists — "play some German trap" works better than naming artists directly, since speech recognition may mishear artist names
- Claude will pick the right artists based on your description regardless

Voice mode requires two additional packages:
```
pip install sounddevice numpy keyboard
```

## Hotkeys

Requires AutoHotkey v2 running `scripts/dj_hotkey.ahk`. Right Ctrl is remapped to F13 to avoid conflicts in games.

| Hotkey | Action |
|--------|--------|
| F13 + 1 | American Rap mode (Juice WRLD, XXXTENTACION, Ski Mask, A Boogie) |
| F13 + 2 | German Trap mode (tj_beastboy, Sierra Kidd) |
| F13 + 3 | K-Pop mode (LE SSERAFIM, BLACKPINK, NewJeans, K/DA, aespa) |
| F13 + 4 | J-Pop mode (Ado, YOASOBI, Eve, BABYMETAL, Aimer) |
| F13 + 5 | Global mode (all artists, weighted random) |
| F13 + 6 | Skip current track |
| F13 + 7 | Ban current artist permanently (discovered artists only) |
| F13 + 8 | Ban current track permanently |
| F13 + 9 | Quit DJ cleanly |
| F13 + V | Toggle voice recording (via dj_voice.py) |

## How the Weight System Works

Each artist starts at weight `1.0` per mode. At every track boundary the DJ judges the previous track:

| Condition | Weight change |
|-----------|--------------|
| Played 80%+ naturally | +0.15 |
| Skipped in first 25% | -0.10 |
| Switched between 25–80% | No change |
| Mode switch (any point) | No change |
| AI mode play (any point) | No change |
| Track banned (any point) | No change |
| Artist banned | Removed entirely |

Weights are clamped between `0.2` (floor) and `3.0` (ceiling). `weighted_choice()` uses these as probabilities so higher-weight artists get picked more often without ever fully excluding lower-weight ones.

**Weight decay:** On each startup, all weights drift 10% of the distance to 1.0 per week elapsed since the last run. A weight of 3.0 becomes ~2.8 after one week, and a weight of 0.3 becomes ~0.37. This prevents old boosts and penalties from becoming permanent over time.

**To reset all weights:** delete `data/dj_memory.json`

## How Artist Discovery Works

When an artist's weight reaches `3.0` (the ceiling) in any mode, there is a 25% chance a discovery search is queued for that mode. This means only artists you genuinely love trigger discovery, and the pool grows gradually rather than flooding with new artists every session.

At the next track boundary the DJ queries Last.fm's `artist.getSimilar` endpoint, resolves each candidate's Spotify ID via search, filters candidates to those with at least 5 clean (non-alternate) tracks available in DE, and adds the first passing candidate to the pool as a trial artist at weight `1.0`.

Trial artists need 5 play-throughs of 80%+ to graduate. Graduated artists are permanently saved to `data/discovered_artists.json` and reloaded into pools on every restart.

## Data Files Reference

| File | Purpose | Reset by deleting? |
|------|---------|-------------------|
| `track_cache.json` | Cached artist track lists | Yes — refetched on next startup |
| `dj_memory.json` | Artist weights per mode | Yes — all weights reset to 1.0. Also delete if you rename modes, as old mode keys become orphaned |
| `recent_tracks.json` | Recently played titles | Yes — repeat protection clears |
| `discovered_artists.json` | Discovered/graduated artists | Yes — discovery history lost |
| `banned_tracks.json` | Permanently banned track IDs | Yes — all track bans cleared |
| `banned_artists.json` | Permanently banned discovered artist names | Yes — all artist bans cleared, banned artists may be re-discovered |
| `dj_crash.log` | Crash and exit log | Yes — safe to delete anytime |
| `.cache` | Spotify token for dj_server.py | Yes — re-authorized automatically on next start |
| `.cache-dj` | Spotify token for spotify_dj.py | Yes — requires one-time manual reauth (see Setup step 9) |
