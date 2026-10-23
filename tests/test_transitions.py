import contextlib
import copy
import io
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import spotify_dj as dj
from playback_state import PlaybackTracker


def track(name='Old'):
    return dict(id=name, uri=f'spotify:track:{name}', name=name, duration_ms=200000,
                album=dict(uri='spotify:album:Album', name='Studio', album_type='album'),
                artists=[dict(id='artist', name='Artist')])


def playback(position, name='Old', playing=True, device='pc', album='spotify:album:Album'):
    return dict(item=track(name), progress_ms=position, is_playing=playing,
                device=dict(id=device), context=dict(uri=album))


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        tracker = PlaybackTracker()
        self.stack.enter_context(patch.object(dj, 'tracker', tracker))
        self.stack.enter_context(patch.object(dj, 'now_playing', tracker.track))
        values = dict(_prepared_next=None, _pending_selection=None, _rate_limit_until=0,
                      auto_mode='jpop', current_pool=['Artist'], ai_pool=[], ai_artists={},
                      ai_session_catalogs={}, ai_artist_plays={}, ai_last_artist=None, ai_session=dj.AISession(),
                      ai_request_plan=None, ai_request_tracks_ready=False, ai_song_queue=[], ai_song_resolutions={},
                      artist_cache={'Artist': [track('Next')]}, track_disk_cache={},
                      memory=dj.default_memory(), banned_track_ids=set(), banned_artist_names=set())
        for name, value in values.items():
            self.stack.enter_context(patch.object(dj, name, value))
        for name in ('save_memory', 'save_recent', 'record_trial_play', 'run_pending_discoveries'):
            self.stack.enter_context(patch.object(dj, name))
        self.stack.enter_context(patch.object(dj.time, 'sleep'))
        self.spotify = self.stack.enter_context(patch.object(dj, 'sp', Mock()))
        # Do not modify the persistent or in-memory recent collections.
        self.stack.enter_context(patch.object(dj, 'recent_titles', []))
        self.stack.enter_context(patch.object(dj, 'recent_artists', []))
        self.stack.enter_context(patch.object(dj, 'ai_recent_titles', []))
        dj.tracker.start('Artist', 'jpop', track(), playback(0), 0)
        for second in range(5, 196, 5):
            dj.tracker.observe(playback(second * 1000), second)

    def poll(self, position=198500, now=198.5, **kwargs):
        self.spotify.current_playback.return_value = playback(position, **kwargs)
        with patch.object(dj.time, 'monotonic', return_value=now):
            return dj.track_finished(allow_early=True)

    def test_early_transition_uses_cache_and_starts_once_on_confirmed_device(self):
        with patch.object(dj, 'get_artist_tracks') as fetch:
            self.assertTrue(self.poll())
            self.assertFalse(dj.track_finished(allow_early=True))
            fetch.assert_not_called()
        self.assertEqual(dj.memory['modes']['jpop']['Artist'], 1.15)
        dj.save_memory.assert_called_once()
        self.spotify.current_playback.return_value = playback(0, name='Next')
        dj.advance_to_next_track()
        self.spotify.devices.assert_not_called()
        self.spotify.start_playback.assert_called_once_with(
            device_id='pc', context_uri='spotify:album:Album',
            offset={'uri': 'spotify:track:Next'}, position_ms=0)
        self.assertEqual(dj.now_playing['uri'], 'spotify:track:Next')
        self.assertEqual(dj.recent_titles, ['next'])

    def test_preparation_does_not_touch_recents_learning_or_playback(self):
        with patch.object(dj, 'get_artist_tracks') as fetch:
            dj.prepare_next_track()
            dj.prepare_next_track()
            fetch.assert_not_called()
        self.assertTrue(dj.prepared_next_is_current())
        self.assertEqual(dj.recent_titles, [])
        dj.save_recent.assert_not_called()
        dj.save_memory.assert_not_called()
        self.assertEqual(self.spotify.mock_calls, [])

    def test_does_not_switch_while_paused_near_end(self):
        self.assertFalse(self.poll(playing=False))
        dj.save_memory.assert_not_called()
        self.spotify.start_playback.assert_not_called()

    def test_does_not_cut_more_than_the_agreed_end_window(self):
        self.assertFalse(self.poll(position=198000, now=198))
        self.assertTrue(self.poll(position=198500, now=198.5))

    def test_preparation_cannot_leak_into_a_new_playback_of_same_uri(self):
        dj.prepare_next_track()
        dj.tracker.start('Artist', 'jpop', track(), playback(0), 201)
        self.assertFalse(dj.prepared_next_is_current())

    def test_resume_after_pause_restores_early_transition(self):
        dj.tracker.observe(playback(195000, playing=False), 196)
        dj.tracker.observe(playback(195000), 200)
        dj.tracker.observe(playback(196000), 201)
        self.assertTrue(self.poll(position=199000, now=204))

    def test_manual_track_or_device_change_discards_preparation(self):
        for kwargs in (dict(name='Manual', album='spotify:album:Other'), dict(device='phone')):
            with self.subTest(kwargs=kwargs):
                dj.tracker.control = True
                dj.now_playing.update(confirmed=True, judged=False, preparation_attempted=False)
                dj.prepare_next_track()
                self.assertFalse(self.poll(position=0, **kwargs))
                self.assertIsNone(dj._prepared_next)
                self.assertFalse(dj.tracker.control)
        self.spotify.start_playback.assert_not_called()
        dj.save_memory.assert_not_called()

    def test_seek_near_end_does_not_trigger_early_switch(self):
        dj.now_playing.update(progress_ms=10000, sampled_at=10, listened_ms=5000)
        self.assertFalse(self.poll(now=15))
        self.assertIsNone(dj.now_playing['stable_since'])
        dj.save_memory.assert_not_called()

    def test_seek_after_sufficient_listening_still_needs_stable_progress(self):
        dj.now_playing.update(progress_ms=170000, sampled_at=170)
        self.assertFalse(self.poll(now=175))
        dj.save_memory.assert_not_called()

    def test_unknown_and_rate_limited_data_never_trigger_transition(self):
        for response in (None, dict(item=None)):
            self.spotify.current_playback.return_value = response
            self.assertFalse(dj.track_finished(allow_early=True))
        self.spotify.current_playback.side_effect = dj.RateLimitError(30)
        self.assertFalse(dj.track_finished(allow_early=True))
        self.spotify.start_playback.assert_not_called()
        dj.save_memory.assert_not_called()

    def test_no_cached_candidate_does_not_cut_track_or_fetch_catalog(self):
        dj.artist_cache.clear()
        with patch.object(dj, 'get_artist_tracks') as fetch:
            self.assertFalse(self.poll())
            fetch.assert_not_called()
        dj.save_memory.assert_not_called()

    def test_ai_preparation_and_transition_stay_inside_request_without_learning(self):
        dj.auto_mode = 'ai'
        dj.ai_pool = [('Ado', 'ado'), ('Eve', 'eve')]
        dj.current_pool = None
        dj.artist_cache = {'Ado': [], 'Eve': [track('Next')], 'Other': [track('Other')]}
        dj.now_playing.update(mode='ai', is_ai_play=True)
        before = copy.deepcopy(dj.memory)
        self.assertTrue(self.poll())
        self.assertEqual(dj._prepared_next['selection'][0], 'Eve')
        self.spotify.current_playback.return_value = playback(0, name='Next')
        dj.advance_to_next_track()
        self.assertTrue(dj.now_playing['is_ai_play'])
        self.assertEqual(dj.memory, before)
        dj.save_memory.assert_not_called()
        dj.save_recent.assert_not_called()
        dj.run_pending_discoveries.assert_not_called()

    def test_mode_change_invalidates_old_preparation(self):
        dj.prepare_next_track()
        dj.auto_mode = 'german_trap'
        self.assertFalse(dj.prepared_next_is_current())

    def test_failed_transition_does_not_start_again_or_mark_recent(self):
        self.assertTrue(self.poll())
        self.spotify.current_playback.return_value = dict(item=None, is_playing=False)
        dj.advance_to_next_track()
        self.spotify.start_playback.assert_called_once()
        self.assertFalse(dj.tracker.control)
        self.assertIsNone(dj._prepared_next)
        self.assertEqual(dj.recent_titles, [])

    def test_sleep_targets_the_early_window_instead_of_overshooting_it(self):
        dj.prepare_next_track()
        with patch.object(dj.time, 'monotonic', return_value=195):
            self.assertEqual(dj.playback_poll_interval(), 3.5)
        dj.now_playing['playing'] = False
        self.assertEqual(dj.playback_poll_interval(), 5)

    def test_short_song_poll_targets_end_before_eighty_percent_sample(self):
        dj.prepare_next_track()
        dj.now_playing.update(duration=20000, progress_ms=15000, listened_ms=15000,
                              sampled_at=15, stable_since=0)
        with patch.object(dj.time, 'monotonic', return_value=15):
            self.assertEqual(dj.playback_poll_interval(), 3.5)
        self.assertFalse(dj.tracker.can_finish_early(15, dj.TRANSITION_LEAD_MS))


if __name__ == '__main__':
    unittest.main()
