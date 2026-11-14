import contextlib
import copy
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import spotify_dj as dj
from recording_match import match_reason
from playback_state import PlaybackTracker
from test_reliability import track, playback


class RecordingMatchTests(unittest.TestCase):
    def setUp(self):
        self.expected = track()
        self.expected.update(name='What Lovers Do', external_ids={'isrc': 'USABC1700001'})
        self.actual = copy.deepcopy(self.expected)
        self.actual.update(uri='spotify:track:replacement', id='replacement', name='What Lovers Do (feat. SZA)')
        self.pb = playback()
        self.pb['item'] = self.actual
        self.sp = Mock()
        self.sp.current_playback.return_value = self.pb
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in dict(sp=self.sp, DEBUG_MODE=False, banned_track_ids=set()).items():
            self.stack.enter_context(patch.object(dj, key, value))
        self.stack.enter_context(patch.object(dj.time, 'sleep'))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def start(self):
        return dj.safe_play(_device_id='pc', _expected_track=self.expected,
                           context_uri='spotify:album:album', offset={'uri': self.expected['uri']}, position_ms=0)

    def test_equal_isrc_confirms_without_extra_metadata_reads(self):
        self.assertIs(self.start(), self.pb)
        self.sp.track.assert_not_called()
        self.sp.start_playback.assert_called_once_with(device_id='pc', context_uri='spotify:album:album',
                                                     offset={'uri': self.expected['uri']}, position_ms=0)

    def test_direct_link_without_isrc_is_accepted(self):
        self.actual.pop('external_ids')
        self.actual['linked_from'] = {'uri': self.expected['uri']}
        self.assertIs(self.start(), self.pb)
        self.sp.track.assert_not_called()

    def test_similar_title_different_recording_or_version_is_rejected(self):
        for change in ({'external_ids': {'isrc': 'USABC1700002'}}, {'name': 'What Lovers Do - Remix'},
                       {'explicit': True}, {'duration_ms': 220000}, {'artists': [{'id': 'other'}]}):
            actual = dict(self.actual, **change)
            self.assertIsNone(match_reason(self.expected, actual))

    def test_missing_metadata_is_fetched_once_and_failure_does_not_retry(self):
        detail = copy.deepcopy(self.expected)
        self.expected.pop('external_ids')
        self.sp.track.return_value = detail
        self.assertIs(self.start(), self.pb)
        self.sp.track.assert_called_once_with(self.expected['uri'])
        self.sp.track.reset_mock()
        self.sp.track.side_effect = RuntimeError('unavailable')
        self.assertIsNone(self.start())
        self.sp.track.assert_called_once()

    def test_missing_both_ids_has_two_read_limit_and_no_name_only_acceptance(self):
        self.expected.pop('external_ids')
        self.actual.pop('external_ids')
        self.sp.track.return_value = {}
        self.assertIsNone(self.start())
        self.assertEqual(self.sp.track.call_count, 2)
        self.assertEqual(self.sp.current_playback.call_count, 4)

    def test_matching_recording_cannot_override_pause_device_album_or_ban(self):
        for field, value in [('is_playing', False), ('device', {'id': 'phone'}),
                             ('context', {'uri': 'spotify:album:other'}), ('progress_ms', 50000)]:
            old = self.pb[field]
            self.pb[field] = value
            self.assertIsNone(self.start())
            self.pb[field] = old
        dj.banned_track_ids.add('replacement')
        self.assertIsNone(self.start())
        self.sp.track.assert_not_called()

    def test_tracker_uses_actual_uri_while_ordered_wish_is_consumed_once(self):
        tracker = PlaybackTracker()
        with patch.object(dj, 'tracker', tracker), patch.object(dj, 'now_playing', tracker.track), \
                patch.object(dj, 'ai_session', dj.AISession()), patch.object(dj, 'ai_song_queue', [('Artist', self.expected)]), \
                patch.object(dj, 'ai_song_cycle', ()), patch.object(dj, 'ai_artist_plays', {}), \
                patch.object(dj, 'ai_history_exclusions', set()), patch.object(dj, 'ai_recent_titles', []), \
                patch.object(dj, 'ai_request_plan', None), patch.object(dj, 'get_active_device_id', return_value='pc'):
            self.assertTrue(dj.start_selected('Artist', 'ai', self.expected, True))
            self.assertEqual(tracker.track['uri'], self.actual['uri'])
            self.assertEqual(dj.ai_song_queue, [])
            self.assertEqual(dj.ai_artist_plays['Artist'], 1)
            now = tracker.track['sampled_at']
            self.pb['progress_ms'] = 5000
            self.assertEqual(tracker.observe(self.pb, now + 5), 'playing')
            self.pb['is_playing'] = False
            self.assertEqual(tracker.observe(self.pb, now + 6), 'paused')
            self.pb['item'] = track('spotify:track:manual')
            self.assertEqual(tracker.observe(self.pb, now + 7), 'manual')
