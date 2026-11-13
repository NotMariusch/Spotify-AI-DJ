import contextlib
import copy
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import spotify_dj as dj
from personal_mix import PersonalMix, HistoryPermissionRequired, require_history_permissions, identity
from playback_state import PlaybackTracker


def track(name='One', artist='Artist'):
    return dict(id=name, uri='spotify:track:' + name, name=name, duration_ms=200000,
                artists=[dict(name=artist, id=artist)],
                album=dict(uri='spotify:album:Album', name='Studio', album_type='album'))


class PersonalMixTests(unittest.TestCase):
    def setUp(self):
        self.sp = Mock()
        self.sp.current_user_recently_played.return_value = dict(items=[dict(track=track('Recent'))])
        self.sp.current_user_top_tracks.return_value = dict(items=[track()])
        self.pool = PersonalMix()

    def test_four_reads_cache_and_no_model_call(self):
        self.assertTrue(self.pool.load(self.sp, dj.select_best_tracks, now=0))
        self.assertEqual(self.sp.current_user_top_tracks.call_count, 3)
        self.sp.current_user_recently_played.assert_called_once_with(limit=50)
        self.assertFalse(self.pool.load(self.sp, dj.select_best_tracks, now=3599))
        self.assertEqual(len(self.pool.tracks), 2)
        self.assertEqual(self.pool.scores[identity(track())], 6)

    def test_ai_tracks_excluded_from_recent_but_top_data_remains_separate(self):
        self.pool.load(self.sp, dj.select_best_tracks, excluded_recent={identity(track('Recent'))})
        self.assertEqual([t['name'] for t in self.pool.tracks], ['One'])

    def test_invalid_unplayable_and_commentary_filtered(self):
        self.sp.current_user_top_tracks.return_value = dict(items=[track('Sad - Commentary'), {},
            {**track('Unavailable'), 'is_playable': False}])
        self.pool.load(self.sp, dj.select_best_tracks)
        self.assertEqual([t['name'] for t in self.pool.tracks], ['Recent'])

    def test_partial_refresh_failure_preserves_previous_pool(self):
        self.pool.load(self.sp, dj.select_best_tracks, now=0)
        old = copy.deepcopy(self.pool.tracks)
        self.sp.current_user_top_tracks.side_effect = dj.RateLimitError(60)
        with self.assertRaises(dj.RateLimitError):
            self.pool.load(self.sp, dj.select_best_tracks, now=3601)
        self.assertEqual(self.pool.tracks, old)

    def test_rotation_small_pool_and_bans(self):
        self.pool.tracks = [track('One', 'A'), track('Two', 'B')]
        first = self.pool.choose(lambda _: True)
        self.pool.record_start(first)
        self.assertNotEqual(identity(first), identity(self.pool.choose(lambda _: True)))
        self.assertIsNone(self.pool.choose(lambda _: False))
        self.pool.tracks = [first]
        self.assertEqual(self.pool.choose(lambda _: True), first)

    def test_missing_scopes_does_not_start_interactive_oauth(self):
        client = Mock()
        client.auth_manager.cache_handler.get_cached_token.return_value = {'scope': 'user-read-playback-state'}
        with self.assertRaises(HistoryPermissionRequired):
            require_history_permissions(client)
        client.auth_manager.get_access_token.assert_not_called()

    def test_connection_preserves_granted_history_scopes_on_refresh(self):
        with patch('spotipy.cache_handler.CacheFileHandler') as cache, patch.object(dj, 'SpotifyOAuth') as auth, \
                patch.object(dj.spotipy, 'Spotify'):
            cache.return_value.get_cached_token.return_value = dict(scope='user-top-read user-read-recently-played')
            dj.connect()
            self.assertIn('user-top-read', auth.call_args.kwargs['scope'])
            self.assertIn('user-modify-playback-state', auth.call_args.kwargs['scope'])


class HistoryAuthorizationTests(unittest.TestCase):
    def test_failed_authorization_preserves_existing_token(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import authorize_history
        output = io.StringIO()
        with patch.object(dj, 'SpotifyOAuth') as auth, patch('json_store.write_json_atomic') as save, \
                contextlib.redirect_stdout(output):
            auth.return_value.get_access_token.side_effect = RuntimeError('sensitive failure details')
            self.assertEqual(authorize_history.main(), 1)
            save.assert_not_called()
        self.assertNotIn('sensitive failure details', output.getvalue())

    def test_successful_authorization_saves_atomically_without_printing_token(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import authorize_history
        output = io.StringIO()
        token = dict(scope=dj.SCOPE + ' user-top-read user-read-recently-played', access_token='secret-fixture')
        with patch.object(dj, 'SpotifyOAuth') as auth, patch('json_store.write_json_atomic') as save, \
                contextlib.redirect_stdout(output):
            auth.return_value.get_access_token.return_value = token
            self.assertEqual(authorize_history.main(), 0)
            save.assert_called_once()
        self.assertNotIn('secret-fixture', output.getvalue())


class PersonalPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        tracker = PlaybackTracker()
        values = dict(tracker=tracker, now_playing=tracker.track, personal_mix=PersonalMix(), personal_director=dj.PersonalDirector(),
                      _prepared_next=None, _pending_selection=None, auto_mode='global', ai_pool=[],
                      banned_track_ids=set(), banned_artist_names=set(), ai_session=dj.AISession(),
                      discovered_artists={})
        for key, value in values.items():
            self.stack.enter_context(patch.object(dj, key, value))
        self.sp = self.stack.enter_context(patch.object(dj, 'sp', Mock()))
        self.permissions = self.stack.enter_context(patch.object(dj, 'require_history_permissions'))
        self.sp.current_user_recently_played.return_value = dict(items=[dict(track=track())])
        self.sp.current_user_top_tracks.return_value = dict(items=[track('Two', 'Other')])
        self.start = self.stack.enter_context(patch.object(dj, 'safe_play', side_effect=self.confirm))
        for method in ('write_json_atomic',):
            self.stack.enter_context(patch.object(dj, method))

    def confirm(self, **kwargs):
        return dict(item=dict(uri=kwargs['offset']['uri']), is_playing=True, device=dict(id='phone'), progress_ms=0)

    def test_global_uses_history_without_persistent_learning_and_prepares_from_memory(self):
        self.assertTrue(dj.play_global_mix(mode_switch=True))
        self.assertEqual(dj.now_playing['mode'], 'global')
        self.assertEqual(sum(dj.personal_mix.plays.values()), 1)
        dj.judge_last_track(interrupted=True)
        dj.write_json_atomic.assert_not_called()
        self.sp.reset_mock()
        dj.now_playing.update(progress_ms=185000, preparation_attempted=False)
        dj.prepare_next_track()
        self.assertTrue(dj._prepared_next['personal'])
        self.sp.current_user_top_tracks.assert_not_called()
        self.sp.current_user_recently_played.assert_not_called()

    def test_missing_permission_leaves_other_modes_and_data_untouched(self):
        self.permissions.side_effect = HistoryPermissionRequired()
        self.assertFalse(dj.play_global_mix(mode_switch=True))
        self.start.assert_not_called()
        self.sp.current_user_recently_played.assert_not_called()
        dj.write_json_atomic.assert_not_called()

    def test_failed_start_does_not_count_as_listened(self):
        self.start.side_effect = None
        self.start.return_value = None
        self.assertFalse(dj.play_global_mix())
        self.assertEqual(sum(dj.personal_mix.plays.values()), 0)

    def test_rate_limit_without_old_pool_does_not_fallback_to_global_artists(self):
        self.sp.current_user_recently_played.side_effect = dj.RateLimitError(30)
        self.assertFalse(dj.play_global_mix())
        self.start.assert_not_called()
