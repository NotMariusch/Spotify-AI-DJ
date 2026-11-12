"""Process-local conversation and listening context. No persistence or learning."""
from collections import OrderedDict, deque
from dataclasses import asdict, replace
import re

from music_request import RequestValidationError, artist_key, song_key, preserve_refinement_selection, repeat_policy_from_text


def is_session_reset(text):
    # Accept the old ASCII spelling too; visible messages use real umlauts.
    return text.casefold().strip().rstrip('.!') in {
        'new ai session', 'reset ai session', 'ai reset', 'session reset',
        'neue ai-session', 'neue ai session', 'ai-session zuruecksetzen',
        'ai-session zurücksetzen', 'session zurücksetzen',
    }


class AISession:
    HISTORY_LIMIT = 2000

    def __init__(self):
        self.clear()

    def clear(self):
        self.plan = None
        self.turns = deque(maxlen=4)
        self.pending = None
        self.last_song = None
        self.heard = OrderedDict()
        self.excluded_artists = ()
        self.excluded_songs = ()
        self.avoid_repeats = False
        self.repeat_policy = 'allow'
        self.less_artists = ()
        self.less_songs = ()
        self.recent_music = deque(maxlen=12)
        self.track_plays = OrderedDict()

    def leave_ai(self):
        # A normal mode ends the old conversation topic, not the listening session.
        self.plan = None
        self.turns.clear()
        self.pending = None

    def context(self):
        return dict(active_request=asdict(self.plan) if self.plan else None,
                    recent_user_requests=list(self.turns), pending=self.pending,
                    last_confirmed_song=self.last_song,
                    excluded_artists=list(self.excluded_artists),
                    excluded_songs=[asdict(s) for s in self.excluded_songs],
                    avoid_session_repeats=self.avoid_repeats,
                    repeat_policy=self.repeat_policy,
                    less_artists=list(self.less_artists),
                    less_songs=[asdict(s) for s in self.less_songs],
                    recent_music=list(self.recent_music),
                    heard_recordings=len(self.heard))

    def note_problem(self, user_text, message):
        self.pending = dict(request=user_text[:1000], question=message[:500])

    def reconcile(self, plan, user_text=None):
        previous = self.plan
        if user_text is not None:
            plan = preserve_refinement_selection(plan, previous, user_text)
        if plan.context_action == 'refine':
            if previous is None and self.pending is None and self.last_song is None:
                raise RequestValidationError('Rückfrage: Worauf bezieht sich dein Wunsch? Bitte zuerst Musik nennen.')
            if previous:
                if previous.reference_songs and not plan.reference_songs:
                    raise RequestValidationError('Die Folgeanfrage hat die Referenzsongs verloren; bitte erneut versuchen.')
                values = dict(style=plan.style or previous.style, mood=plan.mood or previous.mood,
                              artist_scope=previous.artist_scope)
                if plan.year_start is None:
                    values.update(year_start=previous.year_start, year_end=previous.year_end)
                elif previous.year_start is not None and (plan.year_start, plan.year_end) != (previous.year_start, previous.year_end):
                    raise RequestValidationError('Die Folgeanfrage hat den Zeitraum verändert. Bitte den neuen Zeitraum ausdrücklich anfragen.')
                if previous.artist_scope == 'fixed':
                    allowed = {artist_key(n) for n in previous.artists}
                    if any(artist_key(n) not in allowed for n in plan.artists):
                        raise RequestValidationError('Die Folgeanfrage würde deine feste Künstlerauswahl erweitern. Bitte die neue Auswahl ausdrücklich nennen.')
                    values['artist_scope'] = 'fixed'
                if user_text is None and previous.selection == 'hits' and plan.kind == 'discovery' and plan.selection != 'hits':
                    raise RequestValidationError('Die Folgeanfrage hat die Hit-Auswahl verloren; bitte erneut konkretisieren.')
                plan = replace(plan, **values)
        excluded = {artist_key(n): n for n in self.excluded_artists + plan.excluded_artists}
        songs = {(artist_key(s.artist), song_key(s.title)): s for s in self.excluded_songs + plan.excluded_songs}
        if len(excluded) > 24 or len(songs) > 40:
            raise RequestValidationError('Zu viele Session-Ausschlüsse; mit "Neue AI-Session" zurücksetzen.')
        explicit_policy = repeat_policy_from_text(user_text or '')
        policy = explicit_policy or plan.repeat_policy
        if user_text is not None and explicit_policy is None:
            # A mood/artist preference alone cannot silently enable a repeat
            # block. Explicit semantic repeat requests may still use the model.
            if not re.search(r'\b(repeat\w*|wiederhol\w*|again|nochmal|twice|doppelt|zweimal|einmal|once|playlist|loop\w*)\b', user_text, re.I):
                policy = 'inherit'
                plan = replace(plan, avoid_session_repeats=False)
        if policy == 'inherit':
            policy = 'session' if plan.avoid_session_repeats else self.repeat_policy
        plan = replace(plan, excluded_artists=tuple(excluded.values()), excluded_songs=tuple(songs.values()),
                       repeat_policy=policy, avoid_session_repeats=policy == 'session',
                       less_artists=self.less_artists if plan.less_artists is None else plan.less_artists,
                       less_songs=self.less_songs if plan.less_songs is None else plan.less_songs)
        artists = tuple(n for n in plan.artists if artist_key(n) not in excluded)
        if any(artist_key(s.artist) in excluded or (artist_key(s.artist), song_key(s.title)) in songs
               for s in plan.reference_songs):
            raise RequestValidationError('Ein Referenzsong widerspricht deinen Session-Ausschlüssen. Bitte die Anfrage anpassen.')
        if plan.kind == 'songs' and (len(artists) != len(plan.artists) or any(
                (artist_key(s.artist), song_key(s.title)) in songs for s in plan.songs)):
            raise RequestValidationError('Ein Songwunsch widerspricht deinen Session-Ausschlüssen. Mit "Neue AI-Session" zurücksetzen.')
        wishes = tuple(s for s in plan.songs if artist_key(s.artist) not in excluded
                       and (artist_key(s.artist), song_key(s.title)) not in songs)
        if not artists or ((plan.kind == 'songs' or plan.selection == 'hits') and not wishes):
            raise RequestValidationError('Nach den Session-Ausschlüssen bleibt keine passende Auswahl. Bitte die Anfrage anpassen.')
        hints = tuple(h for h in plan.artist_hints if artist_key(h.artist) not in excluded)
        return replace(plan, artists=artists, songs=wishes, artist_hints=hints)

    def accept(self, plan, user_text):
        self.plan = plan
        self.excluded_artists = plan.excluded_artists
        self.excluded_songs = plan.excluded_songs
        self.repeat_policy = (plan.repeat_policy if plan.repeat_policy != 'inherit'
                              else 'session' if plan.avoid_session_repeats else self.repeat_policy)
        self.avoid_repeats = self.repeat_policy == 'session'
        if plan.less_artists is not None:
            self.less_artists = plan.less_artists
        if plan.less_songs is not None:
            self.less_songs = plan.less_songs
        self.turns.append(user_text[:1000])
        self.pending = None

    def record_play(self, name, track, count_start=False):
        """Call only for confirmed DJ starts or an observed playing external track."""
        title = track.get('name')
        if not isinstance(title, str) or not song_key(title):
            return
        self.last_song = dict(artist=name, title=title)
        if not self.recent_music or self.recent_music[-1] != self.last_song:
            self.recent_music.append(dict(self.last_song))
        for key in self.track_keys(name, track):
            self.heard[key] = None
            self.heard.move_to_end(key)
            if count_start:
                self.track_plays[key] = self.track_plays.get(key, 0) + 1
                self.track_plays.move_to_end(key)
        while len(self.heard) > self.HISTORY_LIMIT:
            self.heard.popitem(last=False)
        while len(self.track_plays) > self.HISTORY_LIMIT:
            self.track_plays.popitem(last=False)

    def artist_reduced(self, plan, name):
        return bool(plan and artist_key(name) in {artist_key(n) for n in (plan.less_artists or ())})

    def song_preference_rank(self, plan, name, track):
        keys = self.track_keys(name, track)
        count = max((self.track_plays.get(key, 0) for key in keys), default=0)
        reduced = bool(plan and (keys & {(artist_key(s.artist), song_key(s.title)) for s in (plan.less_songs or ())}))
        return count * 4 + 3 if reduced else count

    @staticmethod
    def track_keys(name, track):
        names = {artist_key(name)} | {artist_key(a.get('name', '')) for a in track.get('artists', [])}
        return {(n, song_key(track.get('name', ''))) for n in names if n}

    def allows(self, plan, name, track, explicit=False):
        if plan is None:
            return True
        keys = self.track_keys(name, track)
        excluded = {artist_key(n) for n in plan.excluded_artists}
        excluded_songs = {(artist_key(s.artist), song_key(s.title)) for s in plan.excluded_songs}
        if any(n in excluded for n, _ in keys) or keys & excluded_songs:
            return False
        # A concrete repeat request takes priority over general rotation history.
        return explicit or not plan.avoid_session_repeats or not keys.intersection(self.heard)
