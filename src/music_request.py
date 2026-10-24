"""Validated temporary music wishes and Spotify-backed track resolution."""
from dataclasses import dataclass, replace
import datetime
import re
import unicodedata


class RequestValidationError(ValueError):
    pass


def text_key(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if c.isalnum())


def artist_key(name):
    key = text_key(name)
    return {'pink': 'pnk'}.get(key, key)


def song_key(title):
    # Only remove an explicit trailing feature credit, not version qualifiers.
    title = re.sub(r'\s*[\[(]\s*(?:feat\.?|ft\.?)\s+.*[\])]\s*$', '', title, flags=re.I)
    return text_key(title)


@dataclass(frozen=True)
class SongWish:
    artist: str
    title: str


@dataclass(frozen=True)
class MusicRequest:
    kind: str
    artists: tuple
    songs: tuple
    year_start: int | None
    year_end: int | None
    style: str
    mood: str
    selection: str
    continue_similar: bool = False
    context_action: str = 'new'
    artist_scope: str = 'fixed'
    excluded_artists: tuple = ()
    excluded_songs: tuple = ()
    avoid_session_repeats: bool = False
    artist_hints: tuple = ()  # Identity anchors only, never a playback queue.

    def summary(self):
        period = f'{self.year_start}–{self.year_end}' if self.year_start is not None else 'ohne Jahresgrenze'
        selection = 'bekannte Hits' if self.selection == 'hits' else 'freie Titelauswahl'
        if self.kind == 'songs':
            selection = f'{len(self.songs)} konkrete Songwünsche in Reihenfolge'
        return f'{period} · {self.style or "kein Genre vorgegeben"} · {self.mood or "keine Stimmung vorgegeben"} · {selection}'


def parse_music_request(data, allow_empty_hit_refinement=False):
    fields = {'kind', 'artists', 'songs', 'year_start', 'year_end', 'style', 'mood', 'selection', 'continue_similar'}
    optional = {'clarification', 'context_action', 'artist_scope', 'excluded_artists', 'excluded_songs', 'avoid_session_repeats', 'artist_hints'}
    if not isinstance(data, dict) or set(data) - fields - optional or not fields <= set(data):
        raise RequestValidationError('Ungültige strukturierte AI-Antwort; bitte erneut versuchen.')
    clarification = data.get('clarification')
    if clarification is not None:
        if not isinstance(clarification, str) or not 1 <= len(clarification.strip()) <= 250:
            raise RequestValidationError('Ungültige Rückfrage der AI.')
        raise RequestValidationError('Rückfrage: ' + clarification.strip())
    if data['kind'] not in ('discovery', 'songs') or data['selection'] not in ('any', 'hits'):
        raise RequestValidationError('Unbekannte Art der Musikanfrage.')
    if type(data['continue_similar']) is not bool or (data['kind'] != 'songs' and data['continue_similar']):
        raise RequestValidationError('Ungültige Fortsetzung der Songwünsche.')
    for field in ('style', 'mood'):
        if not isinstance(data[field], str) or len(data[field]) > 160:
            raise RequestValidationError('Ungültige Stil-/Stimmungsangabe.')
    start, end = data['year_start'], data['year_end']
    if (start is None) != (end is None) or (start is not None and
            (type(start) is not int or type(end) is not int or not 1900 <= start <= end <= 2100)):
        raise RequestValidationError('Ungültiger Zeitraum in der Musikanfrage.')
    artists = data['artists']
    if not isinstance(artists, list) or not 1 <= len(artists) <= 12 or any(
            not isinstance(n, str) or not 1 <= len(n.strip()) <= 120 for n in artists):
        raise RequestValidationError('Die Anfrage braucht 1–12 konkrete Künstlernamen.')
    artists = tuple(dict.fromkeys(n.strip() for n in artists))
    allowed = {artist_key(n) for n in artists}
    if '' in allowed:
        raise RequestValidationError('Künstlernamen dürfen nicht nur aus Satzzeichen bestehen.')
    songs = data['songs']
    if not isinstance(songs, list) or len(songs) > 40:
        raise RequestValidationError('Maximal 40 Songwünsche/-vorschläge pro Anfrage.')
    parsed = []
    for song in songs:
        if not isinstance(song, dict) or set(song) != {'artist', 'title'}:
            raise RequestValidationError('Songvorschläge dürfen nur Künstler und Titel enthalten, keine Spotify-IDs.')
        if any(not isinstance(song[k], str) or not 1 <= len(song[k].strip()) <= 200 for k in song):
            raise RequestValidationError('Ungültiger Songtitel oder Künstler.')
        if artist_key(song['artist']) not in allowed:
            raise RequestValidationError('Songvorschlag liegt außerhalb der angefragten Künstler.')
        parsed.append(SongWish(song['artist'].strip(), song['title'].strip()))
    empty_refinement = (allow_empty_hit_refinement and data['kind'] == 'discovery'
                        and data.get('context_action') == 'refine' and data['selection'] == 'hits')
    if (data['kind'] == 'songs' or data['selection'] == 'hits') and not parsed and not empty_refinement:
        raise RequestValidationError('Für Songwünsche/bekannte Hits fehlen konkrete Songvorschläge.')
    hints = data.get('artist_hints', [])
    if not isinstance(hints, list) or len(hints) > 24:
        raise RequestValidationError('Maximal 24 Titelbelege zur Künstlerauflösung.')
    parsed_hints = []
    for hint in hints:
        if (not isinstance(hint, dict) or set(hint) != {'artist', 'title'}
                or any(not isinstance(hint[k], str) or not 1 <= len(hint[k].strip()) <= 200 for k in hint)
                or artist_key(hint['artist']) not in allowed or not song_key(hint['title'])):
            raise RequestValidationError('Ungültige Titelbelege: nur angefragte Künstler und Titel, keine Spotify-IDs.')
        parsed_hints.append(SongWish(hint['artist'].strip(), hint['title'].strip()))
    action, scope = data.get('context_action', 'new'), data.get('artist_scope', 'fixed')
    if action not in ('new', 'refine') or scope not in ('fixed', 'suggested'):
        raise RequestValidationError('Ungültiger Session-Bezug der Anfrage.')
    avoid = data.get('avoid_session_repeats', False)
    if type(avoid) is not bool:
        raise RequestValidationError('Ungültige Wiederholungsregel.')
    excluded = data.get('excluded_artists', [])
    if not isinstance(excluded, list) or len(excluded) > 24 or any(
            not isinstance(n, str) or not 1 <= len(n.strip()) <= 120 or not artist_key(n) for n in excluded):
        raise RequestValidationError('Ungültige ausgeschlossene Künstler.')
    excluded_songs = data.get('excluded_songs', [])
    if not isinstance(excluded_songs, list) or len(excluded_songs) > 40:
        raise RequestValidationError('Ungültige ausgeschlossene Songs.')
    excluded_wishes = []
    for song in excluded_songs:
        if (not isinstance(song, dict) or set(song) != {'artist', 'title'} or any(
                not isinstance(song[k], str) or not 1 <= len(song[k].strip()) <= 200 for k in song)
                or not artist_key(song['artist']) or not song_key(song['title'])):
            raise RequestValidationError('Ausgeschlossene Songs brauchen Künstler und Titel, keine Spotify-IDs.')
        excluded_wishes.append(SongWish(song['artist'].strip(), song['title'].strip()))
    return MusicRequest(data['kind'], artists, tuple(parsed), start, end,
                        data['style'].strip(), data['mood'].strip(), data['selection'], data['continue_similar'],
                        action, scope, tuple(dict.fromkeys(n.strip() for n in excluded)), tuple(excluded_wishes), avoid, tuple(parsed_hints))


def preserve_refinement_selection(plan, previous, user_text):
    """Mood/filter refinements inherit the selection rule in both directions."""
    if not previous or plan.context_action != 'refine' or plan.kind != 'discovery' or previous.kind != 'discovery':
        return plan
    text = user_text.casefold()
    if re.search(r'\b(no|not|keine?|ohne)\s+(?:bekannte\s+|popular\s+)?hits?\b'
                 r'|\b(deep[ -]?cuts?|album[ -]?tracks?|b[ -]?seiten)\b'
                 r'|\b(full|whole|entire)\s+catalog(?:ue)?\b'
                 r'|\b(gesamte[rn]?|ganze[rn]?)\s+katalog\b|\bfreie\s+titelauswahl\b', text):
        selection = 'any'
    elif re.search(r'\b(hits?|bangers?|classics?|klassiker)\b', text):
        selection = 'hits'
    else:
        selection = previous.selection
    if selection == 'any':
        # Generated hit hints must not become a restricted pool or trigger track
        # lookups. Retain at most two anchors/artist for identity checks instead.
        hints, counts, seen = [], {}, set()
        for hint in plan.artist_hints + plan.songs:
            name, title = artist_key(hint.artist), song_key(hint.title)
            if counts.get(name, 0) < 2 and (name, title) not in seen:
                hints.append(hint)
                counts[name] = counts.get(name, 0) + 1
                seen.add((name, title))
        return replace(plan, selection='any', songs=(), artist_hints=tuple(hints))
    return replace(plan, selection=selection)


def check_explicit_period(request, text):
    """A clearly stated decade/range must not disappear during interpretation."""
    decades = [int(y) for y in re.findall(r'\b((?:19|20)\d0)(?:s|er(?:n)?)\b', text, flags=re.I)]
    ranges = re.findall(r'\b((?:19|20)\d{2})\s*(?:[-–]|bis|to)\s*((?:19|20)\d{2})\b', text, flags=re.I)
    if ranges:
        expected = (min(int(a) for a, _ in ranges), max(int(b) for _, b in ranges))
    elif decades:
        expected = (min(decades), max(decades) + 9)
    else:
        return request
    if (request.year_start, request.year_end) != expected:
        raise RequestValidationError(f'Der genannte Zeitraum {expected[0]}–{expected[1]} wurde nicht korrekt interpretiert; bitte erneut versuchen.')
    return request


def check_request_kind(request, text, session_context=None):
    """Do not promote model-invented hit suggestions to explicit song wishes.

    This deliberately narrow guard handles broad genre/nostalgia wishes with no
    named proposed title. Other uncertain interpretations stay with validation.
    """
    if request.kind != 'songs':
        return request
    pending = (session_context or {}).get('pending') or {}
    source = text + ' ' + pending.get('request', '')
    def words(value):
        return re.sub(r'[\W_]+', ' ', unicodedata.normalize('NFKC', value).casefold()).strip()
    if any(' ' + words(s.title) + ' ' in ' ' + words(source) + ' ' for s in request.songs):
        return request
    broad = re.search(r'\b(pop|rap|trap|rock|jazz|metal|music|musik|hits?|bangers?|childhood|kindheit|nostalgi\w*)\b', source, re.I)
    if broad:
        named_artist = any(' ' + words(n) + ' ' in ' ' + words(source) + ' ' for n in request.artists)
        print('  AI: Allgemeiner Musikwunsch erkannt; Songvorschläge sind Hits, keine feste Wunschliste.')
        return replace(request, kind='discovery', selection='hits', continue_similar=False,
                       artist_scope=request.artist_scope if named_artist else 'suggested')
    return request


def release_year(track):
    raw = (track.get('album') or {}).get('release_date', '')
    if not isinstance(raw, str) or not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', raw):
        return None
    try:
        datetime.date.fromisoformat(raw if len(raw) == 10 else raw + ('-01-01' if len(raw) == 4 else '-01'))
    except ValueError:
        return None
    return int(raw[:4])


def recording_isrc(track):
    value = (track.get('external_ids') or {}).get('isrc', '')
    return value.upper() if isinstance(value, str) and re.fullmatch(r'[A-Za-z]{2}[A-Za-z0-9]{3}\d{7}', value) else None


def same_recording(a, b):
    left, right = recording_isrc(a), recording_isrc(b)
    if left and right:
        return left == right
    # Conservative evidence of an older release. This heuristic never licenses
    # a later reissue; accepting one requires a shared ISRC below.
    return (song_key(a.get('name', '')) == song_key(b.get('name', ''))
            and {x.get('id') for x in a.get('artists', [])} == {x.get('id') for x in b.get('artists', [])}
            and isinstance(a.get('duration_ms'), (int, float))
            and isinstance(b.get('duration_ms'), (int, float))
            and abs(a['duration_ms'] - b['duration_ms']) <= 2000
            and a.get('explicit', False) == b.get('explicit', False))


def constrain_tracks(tracks, request, evidence=()):
    if request.year_start is None:
        return list(tracks)
    sources = list(evidence) + list(tracks)
    accepted = []
    for track in tracks:
        year = release_year(track)
        isrc = recording_isrc(track)
        related = [(release_year(source), source) for source in sources if same_recording(track, source)]
        observed = [y for y, _ in related if y is not None]
        earliest = min(observed) if observed else None
        if earliest is not None and earliest < request.year_start:
            continue  # A reissue in the requested decade does not make an older song new.
        if year is not None and request.year_start <= year <= request.year_end:
            accepted.append(track)
            continue
        verified = [y for y, source in related if y is not None and isrc and recording_isrc(source) == isrc]
        if verified and request.year_start <= min(verified) <= request.year_end:
            accepted.append(dict(track, _dj_source_year=min(verified), _dj_era_basis='Spotify ISRC'))
    return accepted


def resolve_song_wishes(sp, request, resolved_artists, catalogs, qualify, memo=None):
    """Resolve text only against Spotify IDs and metadata; <=2 searches/wish."""
    lookup = {artist_key(name): (name, artist_id) for name, artist_id in resolved_artists}
    memo = {} if memo is None else memo
    resolved, missing = [], []
    for wish in request.songs:
        artist = lookup.get(artist_key(wish.artist))
        if not artist:
            missing.append(f'{wish.artist} — {wish.title}')
            continue
        name, artist_id = artist
        cache_key = (artist_id, song_key(wish.title))
        if cache_key in memo:
            if memo[cache_key] is None:
                missing.append(f'{wish.artist} — {wish.title}')
            else:
                resolved.append((name, memo[cache_key]))
            continue
        def matches(t):
            return (isinstance(t, dict) and isinstance(t.get('name'), str)
                    and song_key(t['name']) == song_key(wish.title)
                    and artist_id in {a.get('id') for a in t.get('artists', [])})
        def eligible(evidence):
            constrained = constrain_tracks(evidence, request, evidence)
            if request.kind == 'songs':
                originals = [t for t in constrained if qualify([t])]
                for i, left in enumerate(originals):
                    for right in originals[i + 1:]:
                        if (recording_isrc(left) and recording_isrc(right)
                                and recording_isrc(left) != recording_isrc(right)
                                and abs(left.get('duration_ms', 0) - right.get('duration_ms', 0)) > 5000):
                            raise RequestValidationError(f'Mehrdeutiger Song: {name} — {wish.title}. Bitte die genaue Version nennen.')
            return qualify(constrained)
        evidence = [t for t in catalogs.get(name, []) if matches(t)]
        candidates = eligible(evidence)
        if not candidates:
            safe_title = wish.title.replace('"', ' ').replace('\\', ' ')
            safe_artist = name.replace('"', ' ').replace('\\', ' ')
            query = f'track:"{safe_title}" artist:"{safe_artist}"'
            first = query + (f' year:{request.year_start}-{request.year_end}' if request.year_start is not None else '')
            fallback = query if first != query else f'{name} {wish.title}'
            for search in (first, fallback):
                result = sp.search(q=search, type='track', limit=10, market='DE')
                evidence.extend(t for t in (result or {}).get('tracks', {}).get('items', []) if matches(t))
                candidates = eligible(evidence)
                if candidates:
                    break
        if not candidates:
            memo[cache_key] = None
            missing.append(f'{wish.artist} — {wish.title}')
            continue
        # Original-era album copies before verified later ISRC reissues.
        chosen = min(candidates, key=lambda t: (bool(t.get('_dj_era_basis')),
                                                (t.get('album') or {}).get('album_type') == 'compilation',
                                                release_year(t) or 9999))
        resolved.append((name, chosen))
        memo[cache_key] = chosen
    return resolved, missing
