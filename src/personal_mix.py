"""Spotify-based personal selection, process-local and independent of learning."""
import random
import time
from collections import Counter

from music_request import artist_key, song_key, recording_isrc


HISTORY_SCOPES = {'user-read-recently-played', 'user-top-read'}


class HistoryPermissionRequired(Exception):
    pass


def identity(track):
    return (artist_key((track.get('artists') or [{}])[0].get('name', '')), song_key(track.get('name', '')))


def require_history_permissions(client):
    token = client.auth_manager.cache_handler.get_cached_token() or {}
    if not HISTORY_SCOPES <= set((token.get('scope') or '').split()):
        raise HistoryPermissionRequired()


class PersonalMix:
    TTL = 3600

    def __init__(self):
        self.tracks = []
        self.scores = {}
        self.sources = {}
        self.loaded_at = None
        self.refresh_after = 0
        self.plays = Counter()
        self.last_artist = None
        self.last_track = None

    def load(self, spotify, qualify, excluded_recent=(), now=None):
        now = time.monotonic() if now is None else now
        if self.tracks and now < self.refresh_after:
            return False
        if self.tracks and self.loaded_at is not None and now - self.loaded_at < self.TTL:
            return False
        # Four bounded reads, no pagination or artist-catalog downloads. Commit
        # only a complete snapshot so a failure cannot overwrite the old pool.
        recent = spotify.current_user_recently_played(limit=50)
        sources = [([item.get('track', {}) for item in recent.get('items', [])
                     if identity(item.get('track') or {}) not in excluded_recent], 1)]
        for period, weight in (('short_term', 3), ('medium_term', 2), ('long_term', 1)):
            sources.append((spotify.current_user_top_tracks(limit=50, time_range=period).get('items', []), weight))
        tracks, scores, memberships = {}, Counter(), {}
        for label, (items, weight) in zip(('recent', 'short_term', 'medium_term', 'long_term'), sources):
            memberships[label] = set()
            seen = set()
            for track in [t for item in items if isinstance(item, dict) for t in qualify([item])]:
                key = identity(track)
                if not all(key) or not track.get('artists', [{}])[0].get('name'):
                    continue
                recording = recording_isrc(track) or key
                if recording in seen:
                    continue
                seen.add(recording)
                tracks.setdefault(key, track)
                scores[key] += weight
                memberships[label].add(key)
        self.tracks, self.scores, self.loaded_at = list(tracks.values()), dict(scores), now
        self.sources = memberships
        return True

    def choose(self, allowed, tracks=None):
        candidates = [t for t in (self.tracks if tracks is None else tracks) if allowed(t)]
        if not candidates:
            return None
        alternatives = [t for t in candidates if identity(t) != self.last_track]
        candidates = alternatives or candidates
        diverse = [t for t in candidates if identity(t)[0] != self.last_artist]
        candidates = diverse or candidates
        minimum = min(self.plays[identity(t)] for t in candidates)
        candidates = [t for t in candidates if self.plays[identity(t)] == minimum]
        return random.choices(candidates, weights=[self.scores.get(identity(t), 1) for t in candidates], k=1)[0]

    def record_start(self, track):
        key = identity(track)
        self.plays[key] += 1
        self.last_track, self.last_artist = key, key[0]
