"""Conservative confirmation of Spotify catalog substitutions, not fuzzy search."""
import math
import re


def isrc(track):
    value = (track.get('external_ids') or {}).get('isrc', '')
    value = value.upper() if isinstance(value, str) else ''
    return value if re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}', value) else None


def compatible(expected, actual):
    """Screen metadata before identity proof; translated titles may differ."""
    names = [t.get('name') for t in (expected, actual)]
    if not all(isinstance(name, str) and name.strip() for name in names):
        return False
    # A matching ISRC must not hide an explicit version conflict in the metadata.
    # Do not require title equality: localized names can denote the same recording.
    versions = [set(re.findall(r'\b(?:remix|live|acoustic|instrumental|karaoke|slowed|sped[ -]?up|remaster(?:ed)?|radio edit)\b',
                               name.casefold())) for name in names]
    if versions[0] != versions[1]:
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
