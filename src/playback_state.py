"""Playback evidence, independent of Spotify I/O and persistent preferences."""


class PlaybackTracker:
    def __init__(self):
        self.track = {}
        self.control = False
        self.generation = 0

    def start(self, artist, mode, chosen, playback, now, ai=False, trial=False):
        self.generation += 1
        self.track.clear()
        self.track.update(
            artist=artist, mode=mode, uri=chosen['uri'],
            duration=chosen.get('duration_ms', 0),
            album_uri=chosen['album']['uri'],
            device_id=playback.get('device', {}).get('id'),
            progress_ms=playback.get('progress_ms') or 0,
            listened_ms=0, sampled_at=now, playing=True,
            confirmed=True, judged=False, reliable=True,
            is_ai_play=ai, is_trial=trial,
            generation=self.generation, stable_since=now, preparation_attempted=False,
        )
        self.control = True

    def gap(self):
        if self.track:
            self.track['sampled_at'] = None
            self.track['reliable'] = False
            self.track['stable_since'] = None

    def observe(self, pb, now):
        t = self.track
        if not pb or not pb.get('item'):
            self.gap()
            return 'unknown'
        if not t.get('confirmed'):
            return 'external'
        device = pb.get('device', {}).get('id')
        if not device or device != t['device_id']:
            self.control = False
            return 'manual'
        progress = pb.get('progress_ms')
        if not isinstance(progress, (int, float)) or progress < 0:
            self.gap()
            return 'unknown'
        elapsed = now - t['sampled_at'] if t.get('sampled_at') is not None else None
        continuous = elapsed is not None and 0 <= elapsed <= 12
        if pb['item'].get('uri') != t['uri']:
            # A URI change alone proves nothing. Only an expected album boundary
            # with actual listening evidence is eligible for automatic takeover.
            remaining = t['duration'] - t['progress_ms']
            natural = (
                self.control and continuous and t['playing'] and pb.get('is_playing')
                and 0 <= remaining <= 8000 and remaining <= elapsed * 1000 + 1500
                and progress <= elapsed * 1000 + 1500
                and t['duration'] > 0 and t['listened_ms'] >= t['duration'] * .80
                and (pb.get('context') or {}).get('uri') == t['album_uri']
            )
            if natural:
                return 'finished'
            self.control = False
            return 'manual'
        delta = progress - t['progress_ms']
        if continuous and t['playing'] and 0 <= delta <= elapsed * 1000 + 1500:
            t['listened_ms'] += min(delta, elapsed * 1000)
            if t.get('stable_since') is None:
                t['stable_since'] = now
        elif delta != 0 or not continuous:
            t['reliable'] = False
            t['stable_since'] = None
        t.update(progress_ms=progress, sampled_at=now, playing=bool(pb.get('is_playing')))
        if not t['playing']:
            t['stable_since'] = None
        return 'playing' if t['playing'] else 'paused'

    def can_finish_early(self, now, lead_ms=1500):
        t = self.track
        return bool(
            self.control and t.get('confirmed') and not t.get('judged') and t.get('playing')
            and t.get('sampled_at') is not None and 0 <= now - t['sampled_at'] <= 1
            and t.get('stable_since') is not None and now - t['stable_since'] >= 3
            and t['duration'] > 0 and 0 <= t['duration'] - t['progress_ms'] <= lead_ms
            and t['listened_ms'] >= t['duration'] * .80
        )

    def verdict(self, interrupted=False, neutral=False):
        t = self.track
        if not t.get('confirmed') or t.get('judged'):
            return None
        t['judged'] = True
        if neutral or t.get('is_ai_play'):
            return None
        if interrupted:
            # Explicit DJ skip retains the existing penalty, provided the last
            # sample still belongs to this playback and is reliable.
            return 'negative' if t.get('reliable') else None
        if t['duration'] > 0 and t['listened_ms'] >= t['duration'] * .80:
            return 'positive'
        return None
