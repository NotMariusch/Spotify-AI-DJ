"""Explicit interactive authorization; preserve existing token until success."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    import spotify_dj as dj
    from spotipy.cache_handler import MemoryCacheHandler
    from personal_mix import HISTORY_SCOPES
    from json_store import write_json_atomic
    scopes = set(dj.SCOPE.split()) | HISTORY_SCOPES
    try:
        auth = dj.SpotifyOAuth(client_id=dj.CLIENT_ID, client_secret=dj.CLIENT_SECRET,
            redirect_uri=dj.REDIRECT_URI, scope=' '.join(sorted(scopes)),
            cache_handler=MemoryCacheHandler(), open_browser=True, show_dialog=True)
        token = auth.get_access_token(as_dict=True)
        if not token or not scopes <= set((token.get('scope') or '').split()):
            print('Spotify-Freigabe unvollständig. Bestehende Anmeldung bleibt erhalten.')
            return 1
        write_json_atomic(ROOT / '.cache-dj', token)
        print('Spotify-Hörverlauf freigegeben. DJ neu starten und „Für mich“ wählen.')
        return 0
    except Exception as error:
        print(f'Freigabe fehlgeschlagen ({type(error).__name__}). Bestehende Anmeldung bleibt erhalten.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
