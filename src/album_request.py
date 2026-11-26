"""Bounded Spotify album lookup; no model-supplied IDs or catalog fallback."""
import datetime
import re
from music_request import RequestValidationError, artist_key, song_key, constrain_tracks
from ai_request import resolve_artists_to_ids


def album_key(title):
    return song_key(re.sub(r'^the\s+', '', title.strip(), flags=re.I))


def resolve_album(sp, plan, known_artists, qualify, today=None):
    name = plan.artists[0]
    known = {artist_key(n): (n, aid) for n, aid in known_artists.items()}
    profile = known.get(artist_key(name))
    albums = []
    latest = plan.album['latest']
    if latest:
        if not profile:
            resolved = resolve_artists_to_ids(sp, [name], {}, song_hints=plan.artist_hints)
            if not resolved:
                raise RequestValidationError('Künstler für das neueste Album nicht eindeutig. Bitte einen Albumtitel oder Song nennen.')
            profile = resolved[0]
        for page in range(4):
            result = sp.artist_albums(profile[1], album_type='album', limit=50, offset=page * 50, country='DE')
            albums.extend(result.get('items', []))
            if not result.get('next'):
                break
        else:
            raise RequestValidationError('Zu viele Veröffentlichungen für eine sichere Suche nach dem neuesten Album. Bitte einen Titel nennen.')
    else:
        title = plan.album['title'].replace('"', ' ').replace('\\', ' ')
        artist = name.replace('"', ' ').replace('\\', ' ')
        for query in (f'album:"{title}" artist:"{artist}"', f'{title} {artist}'):
            result = sp.search(q=query, type='album', limit=10, market='DE')
            albums.extend((result or {}).get('albums', {}).get('items', []))
    candidates = {}
    now = today or datetime.date.today()
    for album in albums:
        if album.get('album_type') != 'album' or not re.fullmatch(r'spotify:album:[A-Za-z0-9]+', album.get('uri', '')):
            continue
        matches = [a for a in album.get('artists', []) if artist_key(a.get('name', '')) == artist_key(name)
                   and a.get('id') and (not profile or a['id'] == profile[1])]
        if len(matches) != 1:
            continue
        if not latest and album_key(album.get('name', '')) != album_key(plan.album['title']):
            continue
        date = album.get('release_date', '')
        try:
            released = datetime.date.fromisoformat(date + ('-01-01' if len(date) == 4 else '-01' if len(date) == 7 else ''))
        except (ValueError, TypeError):
            continue
        if released > now or (plan.year_start is not None and not plan.year_start <= released.year <= plan.year_end):
            continue
        candidates[album['uri']] = (album, matches[0], released)
    values = list(candidates.values())
    if latest and values:
        newest = max(v[2] for v in values)
        values = [v for v in values if v[2] == newest]
    if not values:
        raise RequestValidationError('Kein passendes Album auf Spotify gefunden. Bitte Albumtitel und Künstler prüfen.')
    # Identical release metadata may represent market duplicates; different
    # names, profiles, release dates or track counts require user clarification.
    identities = {(v[1]['id'], song_key(v[0]['name']), v[2], v[0].get('total_tracks')) for v in values}
    if len(identities) != 1:
        titles = ', '.join(dict.fromkeys(v[0]['name'] for v in values))
        raise RequestValidationError('Mehrere passende Alben/Versionen: ' + titles + '. Bitte genauer benennen.')
    album, artist, _ = values[0]
    raw = []
    for page in range(4):
        result = sp.album_tracks(album['uri'], limit=50, offset=page * 50, market='DE')
        raw.extend(result.get('items', []))
        if not result.get('next'):
            break
    else:
        raise RequestValidationError('Album hat zu viele Titel; keine unvollständige Wiedergabeliste gestartet.')
    if not raw or len(raw) != album.get('total_tracks'):
        raise RequestValidationError('Spotify hat keine vollständige Albumtitelliste geliefert; bitte erneut versuchen.')
    tracks = []
    for track in raw:
        full = dict(track, album=album)
        if qualify(constrain_tracks([full], plan, [full])):
            tracks.append(full)
    if not tracks:
        raise RequestValidationError('Keine passenden, freigegebenen Titel in diesem Album.')
    print(f"  AI: Album auf Spotify bestätigt: {artist['name']} — {album['name']} ({len(tracks)}/{len(raw)} Titel, Albumreihenfolge).")
    if len(tracks) != len(raw):
        print(f'  AI: {len(raw) - len(tracks)} Albumtitel durch Filter oder Sperren ausgelassen.')
    return artist['name'], artist['id'], tracks
