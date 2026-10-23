import contextlib
import copy
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import spotify_dj as dj
from playback_state import PlaybackTracker
from instance_lock import InstanceLock


def track(uri='spotify:track:old', album='spotify:album:album', explicit=False):
    return dict(uri=uri, id=uri.split(':')[-1], name=uri.split(':')[-1],
                duration_ms=200000, explicit=explicit,
                album=dict(uri=album, id='album', name='Studio', album_type='album'),
                artists=[dict(id='artist', name='Artist')])


def playback(progress=0, uri='spotify:track:old', playing=True, device='pc', album='spotify:album:album'):
    return dict(item=track(uri), progress_ms=progress, is_playing=playing,
                device=dict(id=device), context=dict(uri=album))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.t = PlaybackTracker()
        self.t.start('Artist', 'jpop', track(), playback(), 0)

    def listen(self, end=195):
        for second in range(5, end + 1, 5):
            self.assertEqual(self.t.observe(playback(second * 1000), second), 'playing')

    def test_full_track_then_album_track_does_not_overwrite_old_progress(self):
        self.listen()
        self.assertEqual(self.t.observe(playback(100, 'spotify:track:new'), 200), 'finished')
        self.assertEqual(self.t.track['progress_ms'], 195000)
        self.assertEqual(self.t.verdict(), 'positive')

    def test_judge_once(self):
        self.listen()
        self.assertEqual(self.t.verdict(), 'positive')
        self.assertIsNone(self.t.verdict(interrupted=True))

    def test_explicit_skip(self):
        self.t.observe(playback(5000), 5)
        self.assertEqual(self.t.verdict(interrupted=True), 'negative')

    def test_manual_switch_mid_track_releases_control(self):
        self.t.observe(playback(5000), 5)
        self.assertEqual(self.t.observe(playback(0, 'spotify:track:new'), 10), 'manual')
        self.assertFalse(self.t.control)
        self.assertIsNone(self.t.verdict(neutral=True))

    def test_different_context_near_end_is_manual(self):
        self.listen()
        self.assertEqual(self.t.observe(playback(0, 'spotify:track:new', album='spotify:album:other'), 200), 'manual')

    def test_pause_resume_counts_only_listened_time(self):
        self.t.observe(playback(5000), 5)
        self.assertEqual(self.t.observe(playback(6000, playing=False), 10), 'paused')
        for second in (15, 20, 25):
            self.t.observe(playback(6000, playing=False), second)
        self.t.observe(playback(6000), 30)
        self.t.observe(playback(11000), 35)
        self.assertEqual(self.t.track['listened_ms'], 11000)
        self.assertTrue(self.t.control)

    def test_pause_near_end_does_not_restart(self):
        self.listen()
        self.assertEqual(self.t.observe(playback(199000, playing=False), 200), 'paused')

    def test_seek_to_end_is_not_listening(self):
        self.t.observe(playback(5000), 5)
        self.t.observe(playback(195000), 10)
        self.assertEqual(self.t.track['listened_ms'], 5000)
        self.assertEqual(self.t.observe(playback(0, 'spotify:track:new'), 15), 'manual')
        self.assertIsNone(self.t.verdict())

    def test_missing_playback_and_recovery(self):
        self.t.observe(playback(5000), 5)
        self.assertEqual(self.t.observe(None, 10), 'unknown')
        self.assertEqual(self.t.observe(playback(15000), 15), 'playing')
        self.t.observe(playback(20000), 20)
        self.assertEqual(self.t.track['listened_ms'], 10000)
        self.assertIsNone(self.t.verdict(interrupted=True))

    def test_missing_item(self):
        self.assertEqual(self.t.observe(dict(item=None, is_playing=False), 5), 'unknown')
        self.assertTrue(self.t.control)

    def test_device_change(self):
        self.assertEqual(self.t.observe(playback(5000, device='phone'), 5), 'manual')

    def test_ai_never_judged(self):
        self.t.track['is_ai_play'] = True
        self.listen()
        self.assertIsNone(self.t.verdict(interrupted=True))

    def test_long_gap_not_credited(self):
        self.t.observe(playback(190000), 190)
        self.assertEqual(self.t.track['listened_ms'], 0)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        for attr in ('save_memory', 'save_recent', 'save_track_cache', 'record_trial_play', 'run_pending_discoveries'):
            self.stack.enter_context(patch.object(dj, attr))
        self.stack.enter_context(patch.object(dj.time, 'sleep'))
        self.stack.enter_context(patch.dict(os.environ, {'SPOTIFY_DEVICE_NAME': ''}))
        dj.tracker = PlaybackTracker()
        dj.now_playing = dj.tracker.track
        dj.memory = dj.default_memory()
        dj.ai_pool = []
        dj.ai_artists = {}
        dj.ai_session_catalogs = {}
        dj.ai_artist_plays = {}
        dj.ai_last_artist = None
        dj.ai_request_plan = None
        dj.ai_request_tracks_ready = False
        dj.ai_song_queue = []
        dj.ai_song_resolutions = {}
        dj.artist_cache = {}
        dj.track_disk_cache = {}
        dj.recent_titles.clear()
        dj.recent_artists.clear()
        dj.ai_recent_titles.clear()
        dj.banned_track_ids = set()
        dj.discovered_artists = {}
        dj._rate_limit_until = 0
        dj._pending_selection = None
        dj._prepared_next = None
        dj._next_external_poll = 0
        dj.auto_mode = None
        dj.current_pool = None
        dj.banned_artist_names = set()
        dj.DEBUG_MODE = False
        dj.sp = Mock()
        dj.sp.devices.return_value = {'devices': [dict(id='pc', name='PC', is_active=True)]}

    def test_successful_request_without_track_is_not_success(self):
        dj.sp.current_playback.return_value = dict(item=None, is_playing=False)
        self.assertFalse(dj.start_selected('Artist', 'jpop', track(), False))
        self.assertEqual(dj.sp.current_playback.call_count, 4)
        self.assertEqual(dj.sp.start_playback.call_count, 1)
        self.assertFalse(dj.now_playing.get('confirmed'))
        self.assertEqual(list(dj.recent_titles), [])
        dj.save_recent.assert_not_called()
        dj.judge_last_track(interrupted=True)
        dj.save_memory.assert_not_called()

    def test_delayed_confirmation_then_recent_history(self):
        dj.sp.current_playback.side_effect = [None, playback(uri='spotify:track:other'), playback()]
        self.assertTrue(dj.start_selected('Artist', 'jpop', track(), False))
        self.assertTrue(dj.now_playing['confirmed'])
        self.assertEqual(list(dj.recent_titles), ['old'])
        self.assertEqual(dj.sp.start_playback.call_args.kwargs,
                         dict(device_id='pc', context_uri='spotify:album:album',
                              offset={'uri': 'spotify:track:old'}, position_ms=0))

    def test_wrong_device_is_not_confirmation(self):
        dj.sp.current_playback.return_value = playback(device='phone')
        self.assertFalse(dj.start_selected('Artist', 'jpop', track(), False))

    def test_no_arbitrary_device_fallback(self):
        dj.sp.devices.return_value = {'devices': [dict(id='phone', name='iPhone', is_active=False)]}
        with self.assertRaises(dj.DeviceUnavailable):
            dj.safe_play(context_uri='spotify:album:album', offset={'uri': 'spotify:track:old'})
        dj.sp.start_playback.assert_not_called()

    def test_preferred_device_missing_does_not_use_active_phone(self):
        with patch.dict(os.environ, {'SPOTIFY_DEVICE_NAME': 'MOROR'}):
            with self.assertRaises(dj.DeviceUnavailable):
                dj.get_active_device_id()

    def test_preferred_device_name_case_insensitive(self):
        with patch.dict(os.environ, {'SPOTIFY_DEVICE_NAME': 'pc'}):
            self.assertEqual(dj.get_active_device_id(), 'pc')

    def test_waiting_for_device_then_single_confirmed_start(self):
        dj.sp.devices.return_value = {'devices': []}
        with patch.object(dj, 'get_artist_tracks', return_value=[track()]):
            self.assertFalse(dj.play_artist('Artist', 'jpop', interrupted=False))
        self.assertIsNotNone(dj._pending_selection)
        dj.sp.start_playback.assert_not_called()
        dj.sp.devices.return_value = {'devices': [dict(id='pc', name='PC', is_active=True)]}
        dj.sp.current_playback.return_value = playback()
        dj._next_device_check = 0
        dj.service_pending_start()
        self.assertIsNone(dj._pending_selection)
        self.assertEqual(dj.sp.start_playback.call_count, 1)

    def test_ai_fallback_only_within_request_and_no_learning(self):
        dj.ai_pool = [('Ado', 'ado'), ('Eve', 'eve')]
        dj.tracker.start('Previous', 'global', track(), playback(), 0)
        before = copy.deepcopy(dj.memory)
        with patch.object(dj, 'get_artist_tracks', side_effect=[[], [track()]]) as fetch:
            dj.sp.current_playback.return_value = playback()
            self.assertTrue(dj.play_artist('Ado', 'ai', dj.GLOBAL_POOL, no_judge=True, mode_switch=True))
        self.assertEqual([c.args[0] for c in fetch.call_args_list], ['Ado', 'Eve'])
        self.assertTrue(dj.now_playing['is_ai_play'])
        dj.judge_last_track(interrupted=True)
        self.assertEqual(before, dj.memory)
        dj.save_memory.assert_not_called()
        dj.run_pending_discoveries.assert_not_called()
        dj.save_recent.assert_not_called()
        self.assertEqual(list(dj.recent_titles), [])
        self.assertEqual(list(dj.ai_recent_titles), ['old'])

    def test_ai_no_tracks_is_bounded(self):
        dj.ai_pool = [('Ado', 'ado'), ('Eve', 'eve')]
        with patch.object(dj, 'get_artist_tracks', return_value=[]) as fetch:
            self.assertFalse(dj.play_artist('Ado', 'ai', dj.GLOBAL_POOL, no_judge=True))
        self.assertEqual(fetch.call_count, 2)
        dj.sp.start_playback.assert_not_called()
        self.assertFalse(dj.tracker.control)

    def test_nonexplicit_original_allowed_live_and_clean_rejected(self):
        studio = track()
        live, clean = track('spotify:track:live'), track('spotify:track:clean')
        live['album']['name'] = 'Live at Arena'
        clean['name'] = 'Song (Clean)'
        self.assertEqual(dj.select_best_tracks([studio, live, clean]), [studio])

    def test_live_word_does_not_exclude_believe_studio_album(self):
        studio = track()
        studio['album']['name'] = 'Believe'
        self.assertEqual(dj.select_best_tracks([studio]), [studio])

    def test_invalid_album_metadata_not_played(self):
        for album in (None, {}, {'uri': 'broken'}, {'uri': None}, {'uri': 'spotify:album:valid'}, 'broken'):
            t = track()
            t['album'] = album
            self.assertEqual(dj.select_best_tracks([t]), [])

    def test_catalog_pagination_and_legacy_cache_repair(self):
        live = track()
        live['album']['name'] = 'WORLD TOUR Live'
        dj.track_disk_cache['Ado'] = dict(fetched_at=dj.time.time(), tracks=[live])
        album = track()['album']
        dj.sp._get.return_value = dict(items=[], next='page2')
        dj.sp.next.side_effect = [dict(items=[album], next=None), dict(items=[track('spotify:track:second')], next=None)]
        dj.sp.album_tracks.return_value = dict(items=[track()], next='tracks2')
        result = dj.fetch_artist_tracks_by_id('Ado', 'artist')
        self.assertEqual(len(result), 2)
        dj.sp._get.assert_called_once_with('artists/artist/albums', include_groups='album,single', market='DE', limit=10)
        self.assertEqual(dj.track_disk_cache['Ado']['catalog_version'], 2)

    def test_partial_catalog_fetch_does_not_replace_cache(self):
        entry = dict(fetched_at=0, tracks=[track()])
        dj.track_disk_cache['Artist'] = copy.deepcopy(entry)
        dj.sp._get.return_value = dict(items=[track()['album']], next=None)
        dj.sp.album_tracks.side_effect = dj.RateLimitError(30)
        with self.assertRaises(dj.RateLimitError):
            dj.fetch_artist_tracks_by_id('Artist', 'artist')
        self.assertEqual(dj.track_disk_cache['Artist'], entry)
        dj.save_track_cache.assert_not_called()

    def test_gateway_rate_limit_and_recovery(self):
        client = Mock()
        client.current_playback.side_effect = [dj.spotipy.SpotifyException(429, -1, 'limited', headers={'Retry-After': '5'}), playback()]
        gateway = dj.SpotifyGateway(client)
        with patch.object(dj.time, 'time', return_value=100):
            with self.assertRaises(dj.RateLimitError):
                gateway.current_playback()
            with self.assertRaises(dj.RateLimitError):
                gateway.devices()
        client.devices.assert_not_called()
        with patch.object(dj.time, 'time', return_value=106):
            self.assertTrue(gateway.current_playback()['is_playing'])
        self.assertEqual(client.current_playback.call_count, 2)

    def test_retry_after_invalid_values(self):
        for value in ('0', '-1', '', None, 'nan', 'inf', 'invalid'):
            self.assertEqual(dj.retry_seconds(Mock(headers={'Retry-After': value})), 30)
        self.assertEqual(dj.retry_seconds(Mock(headers={})), 30)

    def test_poll_rate_limit_never_judges_or_starts(self):
        dj.tracker.start('Artist', 'jpop', track(), playback(), 0)
        dj.sp.current_playback.side_effect = dj.RateLimitError(30)
        with patch.object(dj, 'publish_progress') as telemetry:
            self.assertFalse(dj.track_finished())
        telemetry.assert_called_once_with(None)
        self.assertFalse(dj.now_playing['reliable'])
        dj.save_memory.assert_not_called()
        dj.sp.start_playback.assert_not_called()

    def test_natural_boundary_updates_weight_once(self):
        dj.tracker.start('Artist', 'jpop', track(), playback(), 0)
        for second in range(5, 200, 5):
            dj.tracker.observe(playback(second * 1000), second)
        dj.sp.current_playback.return_value = playback(100, 'spotify:track:new')
        with patch.object(dj.time, 'monotonic', return_value=200):
            self.assertTrue(dj.track_finished())
            self.assertFalse(dj.track_finished())
        dj.judge_last_track()
        self.assertEqual(dj.memory['modes']['jpop']['Artist'], 1.15)
        dj.save_memory.assert_called_once()

    def test_manual_change_before_skip_does_not_penalize_old_artist(self):
        dj.tracker.start('Artist', 'jpop', track(), playback(), 0)
        dj.sp.current_playback.return_value = playback(0, 'spotify:track:manual')
        with patch.object(dj, 'get_artist_tracks', return_value=[]):
            dj.play_artist('Next', 'jpop', interrupted=True)
        dj.save_memory.assert_not_called()

    def test_playback_error_does_not_reauthenticate_or_fallback(self):
        dj.sp.start_playback.side_effect = dj.spotipy.SpotifyException(404, -1, 'not ready')
        with patch.object(dj, 'connect') as reconnect, patch.object(dj, 'get_artist_tracks', return_value=[track()]) as fetch:
            self.assertFalse(dj.play_artist('Artist', 'jpop', ['Artist', 'Other']))
        reconnect.assert_not_called()
        self.assertEqual(fetch.call_count, 1)

    def test_external_monitor_never_reclaims_control_or_judges(self):
        dj.auto_mode = 'jpop'
        dj.sp.current_playback.return_value = playback(10000, 'spotify:track:manual')
        with patch.object(dj.time, 'monotonic', return_value=100):
            dj.observe_external_playback()
            dj.observe_external_playback()
        dj.sp.current_playback.assert_called_once()
        dj.sp.start_playback.assert_not_called()
        dj.save_memory.assert_not_called()
        self.assertFalse(dj.tracker.control)

    def test_normal_mode_after_ai_does_not_judge_ai_track(self):
        dj.tracker.start('Ado', 'ai', track(), playback(), 0, ai=True)
        dj.sp.current_playback.return_value = playback()
        with patch.object(dj, 'get_artist_tracks', return_value=[track()]):
            self.assertTrue(dj.play_artist('Artist', 'jpop', mode_switch=True))
        self.assertFalse(dj.now_playing['is_ai_play'])
        dj.save_memory.assert_not_called()

    def test_ai_natural_completion_does_not_update_preferences(self):
        dj.tracker.start('Ado', 'ai', track(), playback(), 0, ai=True, trial=True)
        for second in range(5, 200, 5):
            dj.tracker.observe(playback(second * 1000), second)
        dj.sp.current_playback.return_value = playback(0, 'spotify:track:new')
        with patch.object(dj.time, 'monotonic', return_value=200):
            self.assertTrue(dj.track_finished())
        dj.save_memory.assert_not_called()
        dj.record_trial_play.assert_not_called()

    def test_failed_ai_request_then_skip_cannot_escape_to_old_global_pool(self):
        dj.auto_mode = 'global'
        dj.current_pool = dj.GLOBAL_POOL
        inputs = [io.StringIO('ai:Play Ado and Eve'), io.StringIO('skip'), io.StringIO('quit')]
        with patch.object(dj.os.path, 'exists', return_value=True), \
                patch('builtins.open', side_effect=inputs), \
                patch.object(dj.os, 'remove'), \
                patch.object(dj, 'ask_claude', side_effect=ValueError('failed')), \
                patch.object(dj, 'observe_external_playback'), \
                patch.object(dj, 'play_global_mix') as global_play:
            with self.assertRaises(SystemExit):
                dj.run_dj()
        self.assertEqual(dj.auto_mode, 'ai')
        self.assertIsNone(dj.current_pool)
        global_play.assert_not_called()
        dj.save_memory.assert_not_called()


class DashboardTests(unittest.TestCase):
    def test_poll_updates_stay_out_of_log_but_events_and_errors_remain(self):
        import dj_server as server
        import queue
        old = dict(server.state)
        self.addCleanup(lambda: server.state.update(old))
        with patch.object(server, 'dj_log_queue', queue.Queue()):
            lines = ['MODE:german_trap', 'DJ_STATUS:Start angefordert; warte auf Spotify-Bestaetigung',
                     'DJ -> Sierra Kidd', '  Sierra Kidd — Song',
                     'NOW_PLAYING:Song', 'IS_PLAYING:true', 'DJ_STATUS:DJ aktiv']
            lines += ['CURRENT_ARTIST:Sierra Kidd'] * 20
            lines += ['DJ_STATUS:Pausiert; auf Fortsetzen in Spotify warten', 'IS_PLAYING:false',
                      'weight [german_trap] Sierra Kidd: 0.500 -> 0.650',
                      'DJ_STATUS:Start nicht bestaetigt; neuer DJ-Befehl erforderlich']
            for line in lines:
                server.handle_dj_line(line)
            messages = []
            while not server.dj_log_queue.empty():
                messages.append(server.dj_log_queue.get_nowait())
            self.assertEqual(messages, ['DJ -> Sierra Kidd', 'Sierra Kidd — Song',
                                       'Pausiert; auf Fortsetzen in Spotify warten',
                                       'weight [german_trap] Sierra Kidd: 0.500 -> 0.650',
                                       'Start nicht bestaetigt; neuer DJ-Befehl erforderlich'])
            self.assertEqual(server.state['current_artist'], 'Sierra Kidd')
            self.assertEqual(server.state['current_track'], 'Song')
            self.assertEqual(server.state['mode'], 'German Trap')
            self.assertFalse(server.state['is_playing'])

    def test_dashboard_and_status_protocol_without_child_or_network(self):
        import dj_server as server
        old = dict(server.state)
        self.addCleanup(lambda: server.state.update(old))
        with patch.object(server, 'subprocess') as process:
            server.parse_dj_line('MODE:german_trap')
            server.parse_dj_line('DJ_STATUS:Warte auf Spotify-Geraet')
            server.parse_dj_line('CURRENT_ARTIST:Artist')
            server.parse_dj_line('NOW_PLAYING:Song')
            server.parse_dj_line('IS_PLAYING:false')
            response = server.app.test_client().get('/')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="dj-status"', response.data)
            self.assertEqual(server.state['status'], 'Warte auf Spotify-Geraet')
            self.assertEqual(server.state['mode'], 'German Trap')
            self.assertEqual(server.state['current_track'], 'Song')
            self.assertFalse(server.state['is_playing'])
            process.Popen.assert_not_called()


class LockTests(unittest.TestCase):
    def test_duplicate_process_and_stale_contents(self):
        with tempfile.TemporaryDirectory(prefix='.test-lock-', dir=Path(__file__).resolve().parents[1]) as folder:
            path = str(Path(folder) / 'dj.lock')
            Path(path).write_text('99999999')
            first = InstanceLock(path)
            self.assertTrue(first.acquire())
            try:
                script = 'import sys; from instance_lock import InstanceLock; sys.exit(1 if InstanceLock(sys.argv[1]).acquire() else 0)'
                result = subprocess.run([sys.executable, '-c', script, path],
                                        env=dict(os.environ, PYTHONPATH=str(Path(dj.__file__).parent)),
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
            finally:
                first.release()
            second = InstanceLock(path)
            self.assertTrue(second.acquire())
            second.release()


if __name__ == '__main__':
    unittest.main()
