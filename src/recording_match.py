"""Conservative confirmation of Spotify catalog substitutions, not fuzzy search."""
import math
import re
from music_request import song_key


def isrc(track):
    value = (track.get('external_ids') or {}).get('isrc', '')
    value = value.upper() if isinstance(value, str) else ''
    return value if re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}', value) else None


def compatible(expected, actual):
    """Reject other artists, versions, durations and explicit/clean conflicts."""
    if not expected.get('name') or song_key(expected['name']) != song_key(actual.get('name', '')):
        return False
    primary = (expected.get('artists') or [{}])[0].get('id')
    if not primary or primary not in {a.get('id') for a in actual.get('artists', [])}:
        return False
    durations = [t.get('duration_ms') for t in (expected, actual)]
    if not all(type(d) in (int, float) and math.isfinite(d) and d > 0 for d in durations):
        return False
    if abs(durations[0] - durations[1]) > 2000:
        return False
    if (type(expected.get('explicit')) is bool and type(actual.get('explicit')) is bool
            and expected['explicit'] != actual['explicit']):
        return False
    return True


def match_reason(expected, actual):
    if not actual.get('uri') or not compatible(expected, actual):
        return None
    left, right = isrc(expected), isrc(actual)
    if left and right and left != right:
        return None
    if (actual.get('linked_from') or {}).get('uri') == expected.get('uri'):
        return 'Spotify-Verknüpfung'
    if left and left == right:
        return 'gleiche ISRC'
    return None
