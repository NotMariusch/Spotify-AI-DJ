import threading
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import io
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from background_pool import BackgroundPool, CancellableSpotify, Cancelled


class BackgroundPoolTests(unittest.TestCase):
    def test_cancelled_model_job_does_not_send_or_retry_requests(self):
        import ai_request
        stop = threading.Event()
        stop.set()
        with patch.object(ai_request.urllib.request, 'urlopen') as call:
            with self.assertRaises(Cancelled):
                ai_request._request_model(b'{}', 'test', cancel_event=stop)
            call.assert_not_called()
        stop.clear()
        def overloaded(*args, **kwargs):
            stop.set()
            raise urllib.error.HTTPError('https://example.invalid', 529, 'overloaded', {}, io.BytesIO())
        with patch.object(ai_request.urllib.request, 'urlopen', side_effect=overloaded) as call:
            with self.assertRaises(Cancelled):
                ai_request._request_model(b'{}', 'test', cancel_event=stop)
            call.assert_called_once()

    def test_nonblocking_single_job_and_stale_results_after_new_request(self):
        pool = BackgroundPool()
        entered, release = threading.Event(), threading.Event()
        def job(cancelled):
            entered.set()
            release.wait(2)
            return ['old music']
        self.assertTrue(pool.start(job, 10, now=0))
        self.assertTrue(entered.wait(1))
        self.assertFalse(pool.start(job, 10, now=0))
        old_cancel = pool.cancelled
        pool.reset()  # Same operation used for new AI request, mode change and quit.
        self.assertTrue(old_cancel.is_set())
        self.assertFalse(pool.start(job, 10, now=0))
        release.set()
        pool.thread.join(2)
        self.assertIsNone(pool.poll(now=1))
        self.assertEqual(pool.attempts, 0)

    def test_success_delay_target_and_attempt_cap(self):
        pool = BackgroundPool()
        self.assertFalse(pool.start(lambda _: [], 36, now=0))
        for attempt in range(pool.MAX_ATTEMPTS):
            now = attempt * 30
            self.assertTrue(pool.start(lambda _: ['music'], 10, now=now))
            pool.thread.join(1)
            self.assertEqual(pool.poll(now=now), (['music'], None))
            self.assertFalse(pool.can_start(10, now=now + 1))
        self.assertFalse(pool.can_start(10, now=10000))

    def test_rate_limit_retry_after_and_timeout_backoff(self):
        pool = BackgroundPool()
        error = RuntimeError('limited')
        error.retry_after = 180
        def fail(_):
            raise error
        pool.start(fail, 8, now=0)
        pool.thread.join(1)
        self.assertIs(pool.poll(now=0)[1], error)
        self.assertFalse(pool.can_start(8, now=179))
        self.assertTrue(pool.can_start(8, now=180))
        error = TimeoutError()
        pool.start(fail, 8, now=180)
        pool.thread.join(1)
        pool.poll(now=180)
        self.assertEqual(pool.due, 300)

    def test_cancelled_job_stops_between_spotify_searches(self):
        stop = threading.Event()
        client = Mock()
        guarded = CancellableSpotify(client, stop)
        client.search.side_effect = lambda **_: stop.set()
        with self.assertRaises(Cancelled):
            guarded.search(q='first')
        with self.assertRaises(Cancelled):
            guarded.search(q='second')
        client.search.assert_called_once()
