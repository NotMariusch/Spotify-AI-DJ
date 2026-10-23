import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_request
import spotify_dj as dj
from music_request import (parse_music_request, RequestValidationError, constrain_tracks,
                           resolve_song_wishes, check_explicit_period)
from playback_state import PlaybackTracker


def request(**overrides):
    data = dict(kind='discovery', artists=['Artist'], songs=[], year_start=2000,
                year_end=2009, style='pop', mood='nostalgia', selection='any', continue_similar=False)
    data.update(overrides)
    return parse_music_request(data)


def song(title='Hit', year='2003', artist='artist', isrc=None, **overrides):
    t = dict(id=title + year, uri=f'spotify:track:{title}{year}', name=title, duration_ms=200000,
             album=dict(uri=f'spotify:album:Album{year}', name='Studio', album_type='album', release_date=year),
             artists=[dict(name='Artist', id=artist)], explicit=False)
    if isrc:
        t['external_ids'] = {'isrc': isrc}
    t.update(overrides)
    return t


class InterpretationTests(unittest.TestCase):
    def test_schema_retains_era_style_mood_and_hits(self):
        plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        self.assertEqual((plan.year_start, plan.year_end), (2000, 2009))
        self.assertEqual((plan.style, plan.mood, plan.selection), ('pop', 'nostalgia', 'hits'))
        self.assertIn('2000–2009', plan.summary())

    def test_invalid_periods_and_unchecked_ids_are_rejected(self):
        for values in (dict(year_start=2010, year_end=2000), dict(year_start=True), dict(year_end=None),
                       dict(spotify_id='invented'), dict(songs=[dict(artist='Artist', title='Hit', uri='fake')]),
                       dict(selection='hits'), dict(kind='songs')):
            with self.subTest(values=values), self.assertRaises(RequestValidationError):
                request(**values)

    def test_songs_outside_explicit_artist_scope_rejected(self):
        with self.assertRaises(RequestValidationError):
            request(kind='songs', songs=[dict(artist='Other', title='Hit')])

    def test_no_implicit_similarity_for_song_lists(self):
        plan = request(kind='songs', songs=[dict(artist='Artist', title='One'), dict(artist='Artist', title='Two')])
        self.assertFalse(plan.continue_similar)
        self.assertEqual([w.title for w in plan.songs], ['One', 'Two'])

    def test_explicit_decade_cannot_disappear_in_interpretation(self):
        for text in ('2000s pop', '2000er Pop', 'hits from 2000-2009'):
            self.assertEqual(check_explicit_period(request(), text).year_start, 2000)
            with self.assertRaises(RequestValidationError):
                check_explicit_period(request(year_start=None, year_end=None), text)

    def test_clarification_is_displayable_instead_of_guessing_artist(self):
        with self.assertRaisesRegex(RequestValidationError, 'Welchen'):
            request(artists=[], clarification='Welchen Kuenstler meinst du mit Hello?')

    def test_claude_contract_is_structured_and_does_not_send_catalog_data(self):
        payload = dict(kind='discovery', artists=['Artist'], songs=[dict(artist='Artist', title='Hit')],
                       year_start=2000, year_end=2009, style='pop', mood='nostalgia', selection='hits', continue_similar=False)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'content': [{'text': json.dumps(payload)}]}).encode()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'unit-test-only'}), \
                patch.object(ai_request.urllib.request, 'urlopen', return_value=response) as call:
            plan = ai_request.ask_claude('childhood banger pop songs from the 2000s')
        self.assertEqual(plan.selection, 'hits')
        sent = json.loads(call.call_args.args[0].data)
        self.assertIn('2000s', sent['messages'][0]['content'])
        self.assertNotIn('spotify:track:', json.dumps(sent))
        self.assertIn('Never output Spotify IDs', sent['system'])


class EraEvidenceTests(unittest.TestCase):
    def test_only_requested_decade_inclusive_is_kept(self):
        tracks = [song(year=y) for y in ('1999', '2000', '2009', '2010', '2013')]
        # Distinct recordings; avoid treating intentionally identical fixture names as reissues.
        for i, track in enumerate(tracks):
            track['name'] = f'Song{i}'
        self.assertEqual([t['album']['release_date'] for t in constrain_tracks(tracks, request())], ['2000', '2009'])

    def test_missing_or_invalid_date_is_not_invented(self):
        for year in ('', 'unknown', '2003-99', '2003-02-31'):
            self.assertEqual(constrain_tracks([song(year=year)], request()), [])

    def test_later_reissue_needs_isrc_evidence_not_model_era_claim(self):
        reissue = song(year='2020', isrc='USABC0300001')
        proof = song(year='2003', isrc='USABC0300001')
        self.assertEqual(constrain_tracks([reissue], request()), [])
        matched = constrain_tracks([reissue], request(), [proof])
        self.assertEqual(matched[0]['_dj_source_year'], 2003)
        self.assertNotIn('_dj_source_year', reissue)

    def test_later_copy_with_same_title_but_no_isrc_is_not_licensed(self):
        self.assertEqual(constrain_tracks([song(year='2020')], request(), [song(year='2003')]), [])

    def test_old_song_reissued_in_requested_decade_stays_out(self):
        original = song(year='1997', isrc='USABC9700001')
        reissue = song(year='2003', isrc='USABC9700001')
        self.assertEqual(constrain_tracks([reissue], request(), [original]), [])

    def test_year_constraints_apply_before_normal_version_deduplication(self):
        original, later = song(), song(year='2020', explicit=True)
        result = dj.select_best_tracks(constrain_tracks([original, later], request()))
        self.assertEqual([t['uri'] for t in result], [original['uri']])


class SongResolutionTests(unittest.TestCase):
    def setUp(self):
        self.sp = Mock()
        self.sp.search.return_value = {'tracks': {'items': []}}
        self.qualify = lambda ts: dj.select_best_tracks(ts, min_duration_ms=1000, allow_compilations=True)
        self.plan = request(kind='songs', songs=[dict(artist='Artist', title='Hit')])

    def resolve(self, tracks=()):
        return resolve_song_wishes(self.sp, self.plan, [('Artist', 'artist')], {'Artist': list(tracks)}, self.qualify)

    def test_catalog_match_requires_no_search(self):
        found, missing = self.resolve([song()])
        self.assertEqual(found[0][1]['name'], 'Hit')
        self.assertEqual(missing, [])
        self.sp.search.assert_not_called()

    def test_rate_limited_lookup_retries_only_unfinished_wish(self):
        plan = request(kind='songs', songs=[dict(artist='Artist', title='Hit'),
                                          dict(artist='Artist', title='Second')])
        memo = {}
        self.sp.search.side_effect = [dict(tracks=dict(items=[song()])), dj.RateLimitError(30)]
        args = (self.sp, plan, [('Artist', 'artist')], {}, self.qualify, memo)
        with self.assertRaises(dj.RateLimitError):
            resolve_song_wishes(*args)
        self.assertEqual(len(memo), 1)
        self.sp.search.reset_mock()
        self.sp.search.side_effect = None
        self.sp.search.return_value = dict(tracks=dict(items=[song('Second')]))
        found, missing = resolve_song_wishes(*args)
        self.assertEqual([t['name'] for _, t in found], ['Hit', 'Second'])
        self.assertEqual(missing, [])
        self.sp.search.assert_called_once()

    def test_wrong_artist_cover_remix_and_wrong_year_cannot_substitute(self):
        self.sp.search.return_value = {'tracks': {'items': [song(artist='cover'), song(title='Hit (Remix)'), song(year='2013')]}}
        found, missing = self.resolve()
        self.assertEqual(found, [])
        self.assertEqual(len(missing), 1)
        self.assertEqual(self.sp.search.call_count, 2)
        self.assertIn('year:2000-2009', self.sp.search.call_args_list[0].kwargs['q'])

    def test_search_result_after_wrong_top_hit_is_resolved(self):
        self.sp.search.return_value = {'tracks': {'items': [song(artist='wrong'), song()]}}
        found, missing = self.resolve()
        self.assertEqual(found[0][1]['artists'][0]['id'], 'artist')
        self.assertEqual(missing, [])

    def test_two_distinct_studio_recordings_need_clarification(self):
        tracks = [song(isrc='USABC0300001'), song(isrc='USABC0300002', duration_ms=240000)]
        with self.assertRaisesRegex(RequestValidationError, 'Mehrdeutiger Song'):
            self.resolve(tracks)

    def test_unavailable_original_can_prove_isrc_for_compilation_copy(self):
        original = song(isrc='USABC0300001', is_playable=False)
        copy = song(year='2020', isrc='USABC0300001')
        copy['album']['album_type'] = 'compilation'
        found, missing = self.resolve([original, copy])
        self.assertEqual(missing, [])
        self.assertEqual(found[0][1]['uri'], copy['uri'])
        self.assertEqual(found[0][1]['_dj_source_year'], 2003)


class StructuredPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.tracker = PlaybackTracker()
        values = dict(tracker=self.tracker, now_playing=self.tracker.track,
                      ai_request_plan=None, ai_request_tracks_ready=False, ai_song_queue=[], ai_song_resolutions={},
                      ai_pool=[('Artist', 'artist')], ai_artists={'Artist': 'artist'},
                      ai_session_catalogs={}, ai_artist_plays={}, ai_last_artist=None, ai_session=dj.AISession(),
                      ai_recent_titles=[], recent_titles=[], recent_artists=[],
                      auto_mode='ai', current_pool=None, _prepared_next=None, _pending_selection=None,
                      memory=dj.default_memory(), artist_cache={}, track_disk_cache={},
                      banned_track_ids=set(), banned_artist_names=set())
        for key, value in values.items():
            self.stack.enter_context(patch.object(dj, key, value))
        self.sp = self.stack.enter_context(patch.object(dj, 'sp', Mock()))
        self.sp.search.return_value = {'tracks': {'items': []}}
        self.stack.enter_context(patch.object(dj.time, 'sleep'))
        for name in ('save_memory', 'save_recent', 'save_track_cache', 'run_pending_discoveries', 'record_trial_play'):
            self.stack.enter_context(patch.object(dj, name))
        self.fetch = self.stack.enter_context(patch.object(dj, 'get_artist_tracks'))
        self.start = self.stack.enter_context(patch.object(dj, 'safe_play', side_effect=self.confirm))

    def confirm(self, **kwargs):
        return dict(item={'uri': kwargs['offset']['uri']}, is_playing=True, progress_ms=0, device={'id': 'pc'})

    def prepare(self):
        dj.now_playing.update(progress_ms=185000, listened_ms=185000, preparation_attempted=False)
        dj.prepare_next_track()

    def test_hits_pool_contains_only_resolved_hints_in_era_and_keeps_constraints_on_transition(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit'), dict(artist='Artist', title='HitTwo')])
        self.fetch.return_value = [song(), song('HitTwo'), song('DeepCut'), song('Future', '2013')]
        before = copy.deepcopy(dj.memory)
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertEqual({t['name'] for t in dj.ai_session_catalogs['Artist']}, {'Hit', 'HitTwo'})
        first = dj.now_playing['uri']
        self.prepare()
        self.assertNotEqual(dj._prepared_next['selection'][2]['uri'], first)
        dj.judge_last_track()
        dj.now_playing['confirmed'] = False
        dj.advance_to_next_track()
        self.assertTrue(dj.now_playing['is_ai_play'])
        self.assertEqual(dj.memory, before)
        dj.save_memory.assert_not_called()
        dj.save_recent.assert_not_called()
        dj.run_pending_discoveries.assert_not_called()

    def test_explicit_list_order_overrides_recent_history_and_stops_after_last(self):
        dj.ai_request_plan = request(kind='songs', songs=[dict(artist='Artist', title='Two'), dict(artist='Artist', title='One')])
        dj.artist_cache['Artist'] = [song('One'), song('Two')]
        dj.recent_titles.append('two')
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertEqual(dj.now_playing['uri'], song('Two')['uri'])
        self.fetch.assert_not_called()
        self.prepare()
        dj.now_playing['confirmed'] = False
        dj.advance_to_next_track()
        self.assertEqual(dj.now_playing['uri'], song('One')['uri'])
        self.assertEqual(dj.ai_song_queue, [])
        self.prepare()
        self.assertTrue(dj._prepared_next['stop'])
        self.sp.current_playback.return_value = dict(device={'id': 'pc'}, is_playing=False, item=song('One'))
        dj.advance_to_next_track()
        self.sp.pause_playback.assert_called_once_with(device_id='pc')
        self.assertFalse(dj.tracker.control)
        self.assertEqual(self.start.call_count, 2)

    def test_failed_start_does_not_consume_song_wish(self):
        dj.ai_request_plan = request(kind='songs', songs=[dict(artist='Artist', title='Hit')])
        dj.artist_cache['Artist'] = [song()]
        self.start.side_effect = None
        self.start.return_value = None
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertEqual(len(dj.ai_song_queue), 1)
        self.assertEqual(dj.ai_artist_plays, {})

    def test_missing_explicit_song_does_not_start_partial_list_or_other_music(self):
        dj.ai_request_plan = request(kind='songs', songs=[dict(artist='Artist', title='Hit'), dict(artist='Artist', title='Missing')])
        dj.artist_cache['Artist'] = [song()]
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.start.assert_not_called()
        self.assertEqual(dj.ai_song_queue, [])

    def test_similarity_requires_explicit_flag_and_starts_only_after_wish(self):
        dj.ai_request_plan = request(kind='songs', songs=[dict(artist='Artist', title='Hit')], continue_similar=True)
        self.fetch.return_value = [song(), song('Other')]
        dj.play_artist('Artist', 'ai', no_judge=True)
        self.assertEqual(dj.now_playing['uri'], song()['uri'])
        self.prepare()
        self.assertEqual(dj._prepared_next['selection'][2]['name'], 'Other')

    def test_no_valid_hits_never_falls_back_to_arbitrary_catalog(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Missing')])
        self.fetch.return_value = [song('DeepCut')]
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.start.assert_not_called()

    def test_exhausted_hits_pool_prepares_pause_not_album_fallback(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        self.fetch.return_value = [song()]
        dj.play_artist('Artist', 'ai', no_judge=True)
        self.prepare()
        self.assertTrue(dj._prepared_next['stop'])

    def test_rate_limit_during_resolution_stays_controlled_and_does_not_learn(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        self.fetch.side_effect = dj.RateLimitError(30)
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.start.assert_not_called()
        dj.save_memory.assert_not_called()

    def test_request_to_playback_keeps_the_structured_constraints(self):
        plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        self.fetch.return_value = [song(), song('Future', '2013')]
        with patch.object(dj.os.path, 'exists', return_value=True), \
                patch('builtins.open', side_effect=[io.StringIO('ai:2000s pop'), io.StringIO('quit')]), \
                patch.object(dj.os, 'remove'), patch.object(dj, 'ask_claude', return_value=plan), \
                patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]), \
                patch.object(dj, 'observe_external_playback'), patch.object(dj, 'track_finished', return_value=False):
            with self.assertRaises(SystemExit):
                dj.run_dj()
        self.assertEqual(dj.ai_request_plan.year_end, 2009)
        self.assertEqual(dj.now_playing['uri'], song()['uri'])
        dj.save_memory.assert_not_called()

    def test_normal_mode_does_not_inherit_ai_year_constraint(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        self.fetch.return_value = [song()]
        dj.play_artist('Artist', 'ai', no_judge=True)
        self.fetch.return_value = [song('Future', '2013')]
        dj.ai_pool.clear()
        dj.play_artist('Artist', 'jpop', ['Artist'], mode_switch=True)
        self.assertEqual(dj.now_playing['uri'], song('Future', '2013')['uri'])
        self.assertFalse(dj.now_playing['is_ai_play'])
        dj.save_memory.assert_not_called()

    def test_completed_song_request_does_not_pause_a_subsequent_normal_mode(self):
        dj.ai_request_plan = request(kind='songs', songs=[dict(artist='Artist', title='Hit')])
        dj.ai_request_tracks_ready = True
        dj.ai_song_queue.clear()
        dj.ai_pool.clear()
        dj.auto_mode = 'jpop'
        dj.current_pool = ['Artist']
        self.assertFalse(dj.explicit_songs_pending())
        with patch.object(dj, 'play_from_pool') as play:
            dj.advance_to_next_track()
            play.assert_called_once_with(['Artist'], 'jpop', interrupted=False)
        self.sp.pause_playback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
