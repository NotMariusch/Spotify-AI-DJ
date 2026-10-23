import contextlib
import io
import json
from pathlib import Path
import queue
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import dj_server as server
import spotify_dj as dj


class ProgressTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(server, 'state', dict(server.state, playback_progress=None)))
        self.stack.enter_context(patch.object(server, 'dj_log_queue', queue.Queue()))
        self.spotify = self.stack.enter_context(patch.object(dj, 'sp', Mock()))
        self.stack.enter_context(patch.object(dj, 'ai_session', dj.AISession()))

    def send(self, position=42000, uri='spotify:track:one', playing=True, duration=200000):
        pb = dict(item=dict(uri=uri, name='Song', duration_ms=duration, artists=[]),
                  progress_ms=position, is_playing=playing)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            dj.publish_playback(pb)
        for line in output.getvalue().splitlines():
            server.handle_dj_line(line)

    def test_existing_response_reaches_dashboard_without_api_calls_or_log_spam(self):
        self.send()
        sample = server.dashboard_state()['playback_progress']
        self.assertEqual(sample['progress_ms'], 42000)
        self.assertEqual(sample['duration_ms'], 200000)
        self.assertTrue(sample['is_playing'])
        self.assertNotIn('received_at', sample)
        self.assertTrue(server.dj_log_queue.empty())
        self.assertEqual(self.spotify.mock_calls, [])

    def test_pause_seek_and_track_change_replace_display_sample(self):
        self.send()
        first = server.state['playback_progress']['sample_id']
        self.send(45000, playing=False)
        self.assertFalse(server.state['playback_progress']['is_playing'])
        self.assertNotEqual(first, server.state['playback_progress']['sample_id'])
        self.send(10000)
        self.assertEqual(server.state['playback_progress']['progress_ms'], 10000)
        self.send(0, uri='spotify:track:two')
        self.assertEqual(server.state['playback_progress']['uri'], 'spotify:track:two')
        self.assertEqual(server.state['playback_progress']['progress_ms'], 0)

    def test_missing_playback_clears_previous_progress(self):
        self.send()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            dj.publish_playback(None)
        for line in output.getvalue().splitlines():
            server.handle_dj_line(line)
        self.assertIsNone(server.state['playback_progress'])

    def test_invalid_timing_is_unknown(self):
        for position, duration in ((None, 200000), (-1, 200000), (1, 0), (1, None), (float('nan'), 200000)):
            self.send(position, duration=duration)
            self.assertIsNone(server.state['playback_progress'])

    def test_malformed_protocol_cannot_keep_old_progress_or_spam_log(self):
        for payload in ('broken', '[]', '{"uri": "track"}', 'null'):
            self.send()
            server.handle_dj_line('PLAYBACK_PROGRESS:' + payload)
            self.assertIsNone(server.state['playback_progress'])
            self.assertTrue(server.dj_log_queue.empty())

    def test_broadcasts_do_not_refresh_sample_age(self):
        with patch.object(server.time, 'monotonic', return_value=100):
            self.send()
        sample_id = server.state['playback_progress']['sample_id']
        with patch.object(server.time, 'monotonic', return_value=106):
            snapshot = server.dashboard_state()['playback_progress']
        self.assertEqual(snapshot['age_ms'], 6000)
        self.assertEqual(snapshot['sample_id'], sample_id)
        self.assertEqual(server.state['playback_progress']['received_at'], 100)

    def test_duration_boundary_clamped(self):
        self.send(250000)
        self.assertEqual(server.state['playback_progress']['progress_ms'], 200000)


if __name__ == '__main__':
    unittest.main()
