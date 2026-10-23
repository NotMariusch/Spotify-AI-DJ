"""Validated temporary music wishes and Spotify-backed track resolution."""
from dataclasses import dataclass
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

    def summary(self):
        period = f'{self.year_start}–{self.year_end}' if self.year_start is not None else 'ohne Jahresgrenze'
        selection = 'bekannte Hits' if self.selection == 'hits' else 'freie Titelauswahl'
        if self.kind == 'songs':
            selection = f'{len(self.songs)} konkrete Songwuensche in Reihenfolge'
        return f'{period} · {self.style or "kein Genre vorgegeben"} · {self.mood or "keine Stimmung vorgegeben"} · {selection}'


def parse_music_request(data):
    fields = {'kind', 'artists', 'songs', 'year_start', 'year_end', 'style', 'mood', 'selection', 'continue_similar'}
    if not isinstance(data, dict) or set(data) - fields - {'clarification'} or not fields <= set(data):
        raise RequestValidationError('Ungueltige strukturierte AI-Antwort; bitte erneut versuchen.')
    clarification = data.get('clarification')
    if clarification is not None:
        if not isinstance(clarification, str) or not 1 <= len(clarification.strip()) <= 250:
            raise RequestValidationError('Ungueltige Rueckfrage der AI.')
        raise RequestValidationError('Rueckfrage: ' + clarification.strip())
    if data['kind'] not in ('discovery', 'songs') or data['selection'] not in ('any', 'hits'):
        raise RequestValidationError('Unbekannte Art der Musikanfrage.')
    if type(data['continue_similar']) is not bool or (data['kind'] != 'songs' and data['continue_similar']):
        raise RequestValidationError('Ungueltige Fortsetzung der Songwuensche.')
    for field in ('style', 'mood'):
        if not isinstance(data[field], str) or len(data[field]) > 160:
            raise RequestValidationError('Ungueltige Stil-/Stimmungsangabe.')
    start, end = data['year_start'], data['year_end']
    if (start is None) != (end is None) or (start is not None and
            (type(start) is not int or type(end) is not int or not 1900 <= start <= end <= 2100)):
        raise RequestValidationError('Ungueltiger Zeitraum in der Musikanfrage.')
    artists = data['artists']
    if not isinstance(artists, list) or not 1 <= len(artists) <= 12 or any(
            not isinstance(n, str) or not 1 <= len(n.strip()) <= 120 for n in artists):
        raise RequestValidationError('Die Anfrage braucht 1–12 konkrete Kuenstlernamen.')
    artists = tuple(dict.fromkeys(n.strip() for n in artists))
    allowed = {artist_key(n) for n in artists}
    if '' in allowed:
        raise RequestValidationError('Kuenstlernamen duerfen nicht nur aus Satzzeichen bestehen.')
    songs = data['songs']
    if not isinstance(songs, list) or len(songs) > 40:
        raise RequestValidationError('Maximal 40 Songwuensche/-vorschlaege pro Anfrage.')
    parsed = []
    for song in songs:
        if not isinstance(song, dict) or set(song) != {'artist', 'title'}:
            raise RequestValidationError('Songvorschlaege duerfen nur Kuenstler und Titel enthalten, keine Spotify-IDs.')
        if any(not isinstance(song[k], str) or not 1 <= len(song[k].strip()) <= 200 for k in song):
            raise RequestValidationError('Ungueltiger Songtitel oder Kuenstler.')
        if artist_key(song['artist']) not in allowed:
            raise RequestValidationError('Songvorschlag liegt ausserhalb der angefragten Kuenstler.')
        parsed.append(SongWish(song['artist'].strip(), song['title'].strip()))
    if (data['kind'] == 'songs' or data['selection'] == 'hits') and not parsed:
        raise RequestValidationError('Fuer Songwuensche/bekannte Hits fehlen konkrete Songvorschlaege.')
    return MusicRequest(data['kind'], artists, tuple(parsed), start, end,
                        data['style'].strip(), data['mood'].strip(), data['selection'], data['continue_similar'])


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
