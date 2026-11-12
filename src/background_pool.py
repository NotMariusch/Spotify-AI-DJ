"""One daemon job, bounded attempts, generation-safe results; no playback state."""
import queue
import threading
import time


class Cancelled(Exception):
    pass


class BackgroundPool:
    TARGET = 36
    MAX_ATTEMPTS = 6

    def __init__(self):
        self.results = queue.Queue()
        self.thread = None
        self.generation = 0
        self.cancelled = threading.Event()
        self.attempts = 0
        self.due = 0

    def reset(self):
        self.cancelled.set()
        self.cancelled = threading.Event()
        self.generation += 1
        self.attempts = 0
        self.due = 0

    def can_start(self, count, now=None):
        now = time.monotonic() if now is None else now
        return (count < self.TARGET and self.attempts < self.MAX_ATTEMPTS and now >= self.due
                and (self.thread is None or not self.thread.is_alive()))

    def start(self, job, count, now=None):
        if not self.can_start(count, now):
            return False
        generation, cancelled = self.generation, self.cancelled
        self.attempts += 1
        def run():
            try:
                value = job(cancelled)
                error = None
            except Exception as failure:
                value, error = None, failure
            self.results.put((generation, value, error))
        self.thread = threading.Thread(target=run, name='ai-pool-refill', daemon=True)
        self.thread.start()
        return True

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        try:
            generation, value, error = self.results.get_nowait()
        except queue.Empty:
            return None
        if generation != self.generation:
            return None
        delay = 30 if error is None else max(60 * min(self.attempts, 4), getattr(error, 'retry_after', 0) or 0)
        self.due = now + delay
        return value, error


class CancellableSpotify:
    """Check invalidation between bounded Spotify calls; never start playback."""
    def __init__(self, client, cancelled):
        self.client, self.cancelled = client, cancelled

    def search(self, **kwargs):
        if self.cancelled.is_set():
            raise Cancelled()
        result = self.client.search(**kwargs)
        if self.cancelled.is_set():
            raise Cancelled()
        return result
