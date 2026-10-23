"""
ai_request.py — Claude-powered artist selection for SpotifyDJ.

Called by spotify_dj.py when it reads an "ai:..." command from dj_input.txt.
Returns a validated, temporary request; Spotify supplies all actual IDs.
"""

import json
import os
import urllib.request
import urllib.error
from music_request import MusicRequest, RequestValidationError, artist_key, parse_music_request, check_explicit_period


def ask_claude(user_request: str) -> MusicRequest:
    """Interpret a wish without sending Spotify catalog data to the model."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")

    system_prompt = (
        "Interpret a music request as ONE JSON object, no markdown or commentary. "
        "Never output Spotify IDs, URIs or URLs; the application resolves everything on Spotify. "
        "Required schema: {\"kind\":\"discovery\"|\"songs\", \"artists\":[strings], "
        "\"songs\":[{\"artist\":string,\"title\":string}], \"year_start\":integer|null, "
        "\"year_end\":integer|null, \"style\":string, \"mood\":string, "
        "\"selection\":\"any\"|\"hits\", \"continue_similar\":boolean}. "
        "Optional clarification: a short German question or null.\n"
        "Rules:\n"
        "- Preserve all explicit era/style/mood constraints. 2000s/2000er means 2000 through 2009; "
        "2010s means 2010 through 2019. No year constraint means BOTH year fields null. "
        "Do not guess a user's childhood decade if they did not give one.\n"
        "- kind=songs only for concrete song wishes. Preserve every requested song and its order. "
        "No additional songs or similar artists unless the user explicitly asks to continue with similar music; "
        "only then continue_similar=true. Song lists by themselves never imply similar music.\n"
        "- Artist-only wishes (e.g. Ado and Eve) use discovery, selection=any, songs=[] and ONLY the named artists. "
        "Do not turn a simple artist wish into a curated hits list.\n"
        "- For genres/moods/activities use discovery and 5-8 suitable artists, strictly matching the style. "
        "Never add artists outside an explicit artist restriction.\n"
        "- Hits, bangers, classics or nostalgia/childhood requests use selection=hits. For broad decade-pop "
        "requests favor recognizable hits. Provide 16-32 well-known concrete song suggestions across the "
        "selected artists (when possible at least two per artist), respecting the requested era and style. "
        "These are suggestions, not proof: the application validates them. Never invent song titles.\n"
        "- Every song artist must be in artists. Use accurate canonical names (e.g. P!nk). "
        "At most 12 artists and 40 songs. style and mood are concise descriptions, empty if absent.\n"
        "- If a concrete title has materially ambiguous artist attribution, ask via clarification instead of "
        "guessing. In that case include all schema fields but artists/songs may be empty. "
        "Do not request clarification for an unambiguous well-known full artist/title pair.\n"
        "- continue_similar is false for every discovery request."
    )
    user_prompt = f"Interpret this request: {user_request}"

    payload = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 4000,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_prompt}
        ]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST"
    )

    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 529 and attempt < 2:
                import time
                print(f"  Claude overloaded (529), retrying in 3s...")
                time.sleep(3)
                continue
            body = e.read().decode("utf-8")
            raise ValueError(f"Claude API {e.code}: {body}")
    if data is None:
        raise ValueError("Claude API failed after 3 attempts")

    raw = data["content"][0]["text"].strip()

    # Strip accidental code fences if Claude adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise RequestValidationError("Die AI-Antwort war kein gueltiges JSON; bitte erneut versuchen.") from None
    return check_explicit_period(parse_music_request(parsed), user_request)


def resolve_artists_to_ids(sp, artist_names: list[str], known_artists: dict) -> list[tuple[str, str]]:
    known = {artist_key(name): (name, artist_id) for name, artist_id in known_artists.items()}
    resolved, seen, resolved_keys = [], set(), set()
    for name in artist_names:
        key = artist_key(name)
        if not key or key in resolved_keys:
            continue
        if key in known:
            canonical, artist_id = known[key]
            if artist_id not in seen:
                print(f"  AI artist (known): {canonical}")
                resolved.append((canonical, artist_id))
                seen.add(artist_id)
                resolved_keys.add(key)
            continue
        query_name = "P!nk" if name.casefold().strip() == "pink" else name
        quoted_name = query_name.replace('"', ' ').replace('\\', ' ').strip()
        found = None
        try:
            # Two bounded searches, 10 candidates each. The top hit alone is
            # not evidence of identity (e.g. PinkPantheress for Pink).
            for query in (f'artist:"{quoted_name}"', query_name):
                results = sp.search(q=query, type="artist", limit=10, market="DE")
                items = (results or {}).get("artists", {}).get("items", [])
                exact = {a["id"]: a for a in items
                         if a.get("id") and isinstance(a.get("name"), str) and artist_key(a["name"]) == key}
                if len(exact) == 1:
                    found = next(iter(exact.values()))
                    break
                if len(exact) > 1:
                    print(f"  AI artist (ambiguous): multiple exact profiles for '{name}' — skipping")
                    break
            if found and found["id"] not in seen:
                print(f"  AI artist (found on Spotify): {found['name']} (searched: {name})")
                resolved.append((found["name"], found["id"]))
                seen.add(found["id"])
                resolved_keys.add(key)
            elif not found:
                print(f"  AI artist: no unambiguous matching profile for '{name}' — skipping")
        except Exception as error:
            if getattr(error, "http_status", None) == 429 or hasattr(error, "retry_after"):
                raise  # Respect the shared cooldown rather than trying every remaining artist.
            print(f"  AI artist search failed for '{name}': {type(error).__name__}")
    return resolved
