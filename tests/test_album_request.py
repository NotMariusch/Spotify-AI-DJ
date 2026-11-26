import contextlib
import datetime
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from album_request import resolve_album
from music_request import parse_music_request, RequestValidationError
from ai_session import AISession
import ai_request
import spotify_dj as dj

NAME = 'John Michael Howell'


def data(latest=False):
    return dict(kind='album', artists=[NAME], songs=[], year_start=None, year_end=None,
                mood='', style='', selection='any', continue_similar=False,
                album=dict(title='' if latest else 'Poet', latest=latest))


def album(name='The Poet', aid='artist', release='2026-11-06', uri='spotify:album:Poet', count=2):
    return dict(name=name, artists=[dict(name=NAME, id=aid)], release_date=release,
                uri=uri, id=uri.split(':')[-1], album_type='album', total_tracks=count)


def track(name, num):
    return dict(name=name, id=str(num), uri=f'spotify:track:T{num}', duration_ms=120000,
                artists=[dict(name=NAME, id='artist')], disc_number=1, track_number=num,
                explicit=False, is_playable=True)


class AlbumTests(unittest.TestCase):
    def setUp(self):
        self.sp = Mock()
        self.sp.search.return_value = dict(albums=dict(items=[album()]))
        self.sp.album_tracks.return_value = dict(items=[track('Intro', 1), track('Fairy', 2)], next=None)
        self.today = datetime.date(2026, 11, 25)

    def resolve(self, plan=None, known=None, qualify=lambda ts: ts):
        with contextlib.redirect_stdout(io.StringIO()):
            return resolve_album(self.sp, plan or parse_music_request(data()), known or {}, qualify, self.today)

    def test_named_album_matches_omitted_the_and_preserves_order_and_context(self):
        name, aid, tracks = self.resolve()
        self.assertEqual((name, aid), (NAME, 'artist'))
        self.assertEqual([t['name'] for t in tracks], ['Intro', 'Fairy'])
        self.assertTrue(all(t['album']['uri'] == 'spotify:album:Poet' for t in tracks))
        self.assertEqual(self.sp.search.call_count, 2)
        self.sp.start_playback.assert_not_called()

    def test_latest_uses_verified_artist_dates_not_api_order_or_singles(self):
        single = dict(album('New single', release='2026-11-20'), album_type='single')
        future = album('Future', release='2027-01-01', uri='spotify:album:Future')
        self.sp.artist_albums.return_value = dict(items=[single, album(), future,
            album('Older', release='2025-01-01', uri='spotify:album:Older')], next=None)
        _, _, tracks = self.resolve(parse_music_request(data(True)), {NAME: 'artist'})
        self.assertEqual(tracks[0]['album']['name'], 'The Poet')
        self.sp.search.assert_not_called()

    def test_ambiguous_named_artist_profiles_are_not_guessed(self):
        self.sp.search.return_value = dict(albums=dict(items=[album(), album(aid='other', uri='spotify:album:Other')]))
        with self.assertRaisesRegex(RequestValidationError, 'Mehrere'):
            self.resolve()
        self.sp.album_tracks.assert_not_called()

    def test_verified_artist_excludes_namesakes(self):
        self.sp.search.return_value = dict(albums=dict(items=[album(aid='other', uri='spotify:album:Other'), album()]))
        self.assertEqual(self.resolve(known={NAME: 'artist'})[1], 'artist')

    def test_no_album_never_falls_back_to_artist_catalog(self):
        self.sp.search.return_value = dict(albums=dict(items=[album('Other')]))
        with self.assertRaisesRegex(RequestValidationError, 'Kein passendes Album'):
            self.resolve()
        self.sp.artist_albums.assert_not_called()
        self.sp.album_tracks.assert_not_called()

    def test_pagination_is_bounded_and_does_not_start_partial_album(self):
        self.sp.album_tracks.return_value = dict(items=[track('Fairy', 1)], next='more')
        with self.assertRaisesRegex(RequestValidationError, 'zu viele Titel'):
            self.resolve()
        self.assertEqual(self.sp.album_tracks.call_count, 4)

    def test_album_filter_keeps_order_and_reports_exclusions(self):
        _, _, tracks = self.resolve(qualify=lambda ts: [t for t in ts if t['name'] == 'Fairy'])
        self.assertEqual([t['name'] for t in tracks], ['Fairy'])

    def test_rate_limit_propagates_without_fallback(self):
        self.sp.search.side_effect = dj.RateLimitError(30)
        with self.assertRaises(dj.RateLimitError):
            self.resolve()
        self.sp.search.assert_called_once()

    def test_schema_rejects_model_ids_missing_album_and_mixed_song_requests(self):
        for extra in (dict(album=None), dict(album=dict(title='Poet', latest=False, id='fake')),
                      dict(album=dict(title='Poet', latest=True)), dict(artists=[NAME, 'Other']),
                      dict(songs=[dict(artist=NAME, title='Fairy')])):
            with self.assertRaises(RequestValidationError):
                parse_music_request(dict(data(), **extra))

    def test_verified_profile_is_only_from_confirmed_playback_and_not_model_context(self):
        session = AISession()
        session.record_play(NAME, track('Fairy', 2))
        self.assertEqual(session.verified_artists, {})
        session.record_play(NAME, track('Fairy', 2), count_start=True)
        self.assertEqual(session.verified_artists, {NAME: 'artist'})
        self.assertNotIn('verified_artists', session.context())
        session.clear()
        self.assertEqual(session.verified_artists, {})

    def test_album_clarification_preserves_pending_context(self):
        session = AISession()
        session.note_problem('play the newest album by John Michael Howell', 'Albumtitel?')
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}), patch.object(ai_request, '_request_model', return_value=data()) as model:
            plan = ai_request.ask_claude('Poet heißt das Album', session.context())
        self.assertEqual(plan.kind, 'album')
        payload = json.loads(model.call_args.args[0])
        self.assertIn('newest album', payload['messages'][0]['content'])
        self.assertIn('Spotify determines the release', payload['system'])
        self.assertEqual(session.reconcile(plan).album['title'], 'Poet')

    def test_album_uses_ordered_queue_and_stays_inside_album_on_repeat(self):
        plan = parse_music_request(data())
        session = AISession()
        plan = session.reconcile(plan)
        tracks = [dict(track('Intro', 1), album=album()), dict(track('Fairy', 2), album=album())]
        with patch.object(dj, 'ai_request_plan', plan), patch.object(dj, 'ai_request_tracks_ready', False), \
             patch.object(dj, 'ai_album_tracks', tracks), patch.object(dj, 'ai_pool', [(NAME, 'artist')]), \
             patch.object(dj, 'ai_song_queue', []), patch.object(dj, 'ai_song_cycle', ()), \
             patch.object(dj, 'ai_session_catalogs', {}), patch.object(dj, 'ai_session', session), \
             patch.object(dj, 'banned_track_ids', set()), patch.object(dj, 'banned_artist_names', set()), \
             patch.object(dj, 'auto_mode', 'ai'), patch.object(dj, 'sp') as sp:
            self.assertTrue(dj.ensure_structured_request())
            self.assertTrue(dj.explicit_songs_pending())
            self.assertEqual(dj.next_song_wish()[1]['name'], 'Intro')
            dj.ai_song_queue.pop(0)
            self.assertEqual(dj.next_song_wish()[1]['name'], 'Fairy')
            dj.ai_song_queue.clear()
            self.assertEqual(dj.next_song_wish()[1]['name'], 'Intro')
            sp.search.assert_not_called()
