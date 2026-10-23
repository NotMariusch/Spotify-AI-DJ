import contextlib
import copy
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ai_request import resolve_artists_to_ids
from playback_state import PlaybackTracker
import spotify_dj as dj


def results(*artists):
    return {'artists': {'items': [dict(id=artist_id, name=name) for name, artist_id in artists]}}


class ArtistResolutionTests(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.sp = Mock()

    def test_correct_artist_can_appear_below_wrong_top_result(self):
        self.sp.search.return_value = results(('Avril Lavigne', 'avril'), ('Kelly Clarkson', 'kelly'))
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Kelly Clarkson'], {}), [('Kelly Clarkson', 'kelly')])
        self.sp.search.assert_called_once_with(q='artist:"Kelly Clarkson"', type='artist', limit=10, market='DE')

    def test_unfiltered_fallback_is_bounded_and_still_validates_name(self):
        self.sp.search.side_effect = [results(('Justin Timberlake', 'justin')), results(('Timbaland', 'tim'))]
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Timbaland'], {}), [('Timbaland', 'tim')])
        self.assertEqual(self.sp.search.call_count, 2)

    def test_pink_alias_never_resolves_to_pinkpantheress(self):
        self.sp.search.return_value = results(('PinkPantheress', 'wrong'), ('P!nk', 'pink'))
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Pink'], {}), [('P!nk', 'pink')])
        self.assertEqual(self.sp.search.call_args.kwargs['q'], 'artist:"P!nk"')

    def test_beyonce_accent_and_unicode_composition_are_supported(self):
        self.sp.search.return_value = results(('Rihanna', 'wrong'), ('Beyoncé', 'beyonce'))
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Beyonce'], {}), [('Beyoncé', 'beyonce')])
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Beyonce\u0301'], {'Beyoncé': 'known'}), [('Beyoncé', 'known')])

    def test_known_alias_does_not_need_spotify_search(self):
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Pink'], {'P!nk': 'pink'}), [('P!nk', 'pink')])
        self.sp.search.assert_not_called()

    def test_prefix_or_fuzzy_matches_are_rejected(self):
        self.sp.search.return_value = results(('PinkPantheress', 'wrong'), ('Pink Floyd', 'alsoWrong'))
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Pink'], {}), [])
        self.assertEqual(self.sp.search.call_count, 2)

    def test_multiple_exact_profiles_are_not_guessed(self):
        self.sp.search.return_value = results(('Artist', 'one'), ('Artist', 'two'))
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Artist'], {}), [])
        self.sp.search.assert_called_once()

    def test_aliases_do_not_duplicate_an_artist_in_pool(self):
        self.sp.search.return_value = results(('P!nk', 'pink'))
        self.assertEqual(resolve_artists_to_ids(self.sp, ['Pink', 'P!nk'], {}), [('P!nk', 'pink')])
        self.sp.search.assert_called_once()

    def test_rate_limit_aborts_remaining_searches(self):
        self.sp.search.side_effect = dj.RateLimitError(30)
        with self.assertRaises(dj.RateLimitError):
            resolve_artists_to_ids(self.sp, ['One', 'Two', 'Three'], {})
        self.sp.search.assert_called_once()


def track(artist, index):
    title = f'{artist}Song{index}'
    return dict(id=title, uri=f'spotify:track:{title}', name=title, duration_ms=200000,
                album=dict(uri='spotify:album:Album', name='Studio', album_type='album'),
                artists=[dict(id=artist, name=artist)])


class AIRotationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.tracker = PlaybackTracker()
        values = dict(tracker=self.tracker, now_playing=self.tracker.track,
                      ai_pool=[(n, n) for n in 'ABCD'], ai_artists={n: n for n in 'ABCD'},
                      ai_session_catalogs={}, ai_artist_plays={}, ai_last_artist=None,
                      ai_request_plan=None, ai_request_tracks_ready=False, ai_song_queue=[], ai_song_resolutions={},
                      ai_recent_titles=[], recent_titles=[], recent_artists=[],
                      current_pool=None, auto_mode='ai', _prepared_next=None,
                      _pending_selection=None, banned_track_ids=set(), banned_artist_names=set(),
                      memory=dj.default_memory(), artist_cache={}, track_disk_cache={})
        for name, value in values.items():
            self.stack.enter_context(patch.object(dj, name, value))
        for name in ('save_memory', 'save_recent', 'record_trial_play', 'run_pending_discoveries'):
            self.stack.enter_context(patch.object(dj, name))
        self.catalogs = {n: [track(n, i) for i in range(4)] for n in 'ABCD'}
        self.fetch = self.stack.enter_context(patch.object(dj, 'get_artist_tracks', side_effect=lambda n: self.catalogs[n]))
        self.start = self.stack.enter_context(patch.object(dj, 'safe_play', side_effect=self.confirm))

    def confirm(self, **kwargs):
        uri = kwargs['offset']['uri']
        return dict(item={'uri': uri}, device={'id': 'pc'}, progress_ms=0, is_playing=True)

    def prepare(self):
        dj.now_playing.update(progress_ms=185000, listened_ms=185000, preparation_attempted=False)
        dj.prepare_next_track()

    def test_all_requested_catalogs_are_ready_before_first_start(self):
        self.assertTrue(dj.play_artist('A', 'ai', no_judge=True))
        self.assertEqual(set(dj.ai_session_catalogs), set('ABCD'))
        self.assertEqual(self.fetch.call_count, 4)
        self.start.assert_called_once()

    def test_preparation_is_no_longer_biased_toward_only_disk_cached_artist(self):
        dj.artist_cache = {'A': self.catalogs['A']}
        dj.play_artist('A', 'ai', no_judge=True)
        self.prepare()
        self.assertIn(dj._prepared_next['selection'][0], 'BCD')
        self.assertEqual(self.fetch.call_count, 4)  # No additional end-window fetch.

    def test_two_random_rounds_cover_all_artists_without_adjacent_repeats(self):
        before = copy.deepcopy(dj.memory)
        dj.play_artist('A', 'ai', no_judge=True)
        played = [dj.now_playing['artist']]
        for _ in range(7):
            self.prepare()
            counts = dict(dj.ai_artist_plays)
            self.assertEqual(sum(counts.values()), len(played))
            dj.judge_last_track()
            dj.now_playing['confirmed'] = False
            dj.advance_to_next_track()
            played.append(dj.now_playing['artist'])
        self.assertEqual(set(played[:4]), set('ABCD'))
        self.assertEqual(set(played[4:]), set('ABCD'))
        self.assertTrue(all(a != b for a, b in zip(played, played[1:])))
        self.assertEqual(set(dj.ai_artist_plays.values()), {2})
        self.assertEqual(dj.memory, before)
        dj.save_memory.assert_not_called()
        dj.save_recent.assert_not_called()
        dj.record_trial_play.assert_not_called()
        dj.run_pending_discoveries.assert_not_called()

    def test_skip_cannot_force_repeat_when_other_requested_artists_are_playable(self):
        dj.play_artist('A', 'ai', no_judge=True)
        dj.play_artist('A', 'ai', no_judge=True, interrupted=False)
        self.assertNotEqual(dj.now_playing['artist'], 'A')
        dj.save_memory.assert_not_called()

    def test_explicit_single_artist_request_never_introduces_other_artists(self):
        dj.ai_pool = [('A', 'A')]
        dj.play_artist('A', 'ai', no_judge=True)
        self.prepare()
        self.assertEqual(dj._prepared_next['selection'][0], 'A')
        dj.now_playing['confirmed'] = False
        dj.advance_to_next_track()
        self.assertEqual(dj.ai_artist_plays, {'A': 2})
        self.assertEqual(self.fetch.call_count, 1)

    def test_empty_artist_does_not_prevent_other_requested_artists_playing(self):
        self.catalogs['A'] = []
        dj.play_artist('A', 'ai', no_judge=True)
        self.assertIn(dj.now_playing['artist'], 'BCD')
        self.assertNotIn('A', dj.ai_artist_plays)

    def test_failed_start_never_increments_rotation_counts(self):
        self.start.side_effect = None
        self.start.return_value = None
        self.assertFalse(dj.play_artist('A', 'ai', no_judge=True))
        self.assertEqual(dj.ai_artist_plays, {})
        self.assertIsNone(dj.ai_last_artist)
        self.assertEqual(dj.ai_recent_titles, [])

    def test_rate_limit_during_catalog_loading_does_not_fall_back_to_cached_artist(self):
        self.fetch.side_effect = [self.catalogs['A'], dj.RateLimitError(30)]
        self.assertFalse(dj.play_artist('A', 'ai', no_judge=True))
        self.start.assert_not_called()
        self.assertEqual(self.fetch.call_count, 2)
        self.assertEqual(dj.ai_artist_plays, {})

    def test_new_ai_request_clears_rotation_and_request_catalogs(self):
        dj.ai_artist_plays['A'] = 7
        dj.ai_session_catalogs['A'] = self.catalogs['A']
        dj.ai_last_artist = 'A'
        with patch.object(dj.os.path, 'exists', return_value=True), \
                patch('builtins.open', side_effect=[io.StringIO('ai:new'), io.StringIO('quit')]), \
                patch.object(dj.os, 'remove'), patch.object(dj, 'ask_claude', side_effect=ValueError('failed')), \
                patch.object(dj, 'observe_external_playback'), patch.object(dj.time, 'sleep'):
            with self.assertRaises(SystemExit):
                dj.run_dj()
        self.assertEqual(dj.ai_artist_plays, {})
        self.assertEqual(dj.ai_session_catalogs, {})
        self.assertIsNone(dj.ai_last_artist)


if __name__ == '__main__':
    unittest.main()
