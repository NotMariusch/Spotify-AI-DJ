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
                           resolve_song_wishes, check_explicit_period, check_request_kind)
from playback_state import PlaybackTracker
from music_request import resolve_reference_contexts


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
    def test_reference_schema_and_summary(self):
        plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')],
                       reference_songs=[dict(artist='Artist', title='Anchor')],
                       similarity_traits=['starker Mitsing-Refrain', 'hohe Energie'])
        self.assertIn('starker Mitsing-Refrain', plan.summary())
        self.assertEqual(plan.reference_songs[0].title, 'Anchor')
        for override in (dict(selection='any'), dict(similarity_traits=[]),
                         dict(reference_songs=[dict(artist='Artist', title='Anchor', uri='fake')])):
            from dataclasses import asdict
            data = json.loads(json.dumps(asdict(plan)))
            data.update(override)
            with self.assertRaises(RequestValidationError):
                parse_music_request(data)

    def test_reference_interpretation_defers_candidates_until_spotify_confirmation(self):
        data = dict(kind='discovery', artists=['Artist'], songs=[], year_start=None, year_end=None,
                    style='', mood='', selection='hits', continue_similar=False,
                    reference_songs=[dict(artist='Artist', title='Anchor')], similarity_traits=['Mitsingen'])
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}), \
                patch.object(ai_request, '_request_model', return_value=data) as model:
            plan = ai_request.ask_claude('Songs zum Mitsingen wie Anchor')
            self.assertEqual(plan.songs, ())
            model.assert_called_once()

    def test_german_and_english_reference_requests_use_same_pipeline(self):
        from dataclasses import asdict
        plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')],
                       reference_songs=[dict(artist='Artist', title='Anchor')],
                       similarity_traits=['starker Mitsing-Refrain'])
        for text in ('Songs zum Mitsingen wie Anchor', 'Songs to sing along to like Anchor'):
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}), \
                    patch.object(ai_request, '_request_model', return_value=json.loads(json.dumps(asdict(plan)))) as model:
                self.assertEqual(ai_request.ask_claude(text), plan)
                payload = json.loads(model.call_args.args[0])
                self.assertIn('NOT verified audio analysis', payload['system'])

    def test_childhood_pop_is_discovery_even_if_model_returns_fixed_song_list(self):
        plan = request(kind='songs', songs=[dict(artist='Artist', title='SexyBack'), dict(artist='Artist', title='Toxic')])
        fixed = check_request_kind(plan, '2000s pop from my childhood please')
        self.assertEqual((fixed.kind, fixed.selection, fixed.artist_scope), ('discovery', 'hits', 'suggested'))
        self.assertFalse(fixed.continue_similar)
        self.assertEqual(fixed.songs, plan.songs)

    def test_actual_named_songs_and_clarification_answers_remain_explicit(self):
        plan = request(kind='songs', songs=[dict(artist='Artist', title='SexyBack')])
        self.assertEqual(check_request_kind(plan, 'Play SexyBack, a pop song').kind, 'songs')
        context = dict(pending=dict(request='Play SexyBack, a pop song', question='Which artist?'))
        self.assertEqual(check_request_kind(plan, 'Justin Timberlake', context).kind, 'songs')

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
            request(artists=[], clarification='Welchen Künstler meinst du mit Hello?')

    def test_claude_contract_is_structured_and_does_not_send_catalog_data(self):
        payload = dict(kind='songs', artists=['Artist'], songs=[dict(artist='Artist', title='Hit')],
                       year_start=2000, year_end=2009, style='pop', mood='nostalgia', selection='hits', continue_similar=False)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'content': [{'text': json.dumps(payload)}]}).encode()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'unit-test-only'}), \
                patch.object(ai_request.urllib.request, 'urlopen', return_value=response) as call:
            plan = ai_request.ask_claude('childhood banger pop songs from the 2000s')
        self.assertEqual(plan.selection, 'hits')
        self.assertEqual(plan.kind, 'discovery')
        sent = json.loads(call.call_args.args[0].data)
        self.assertIn('2000s', sent['messages'][0]['content'])
        self.assertNotIn('spotify:track:', json.dumps(sent))
        self.assertIn('Never output Spotify IDs', sent['system'])

    def test_model_rate_limit_is_typed_and_does_not_retry_or_expose_error_body(self):
        error = ai_request.urllib.error.HTTPError('https://example.invalid', 429, 'limited', {'Retry-After': '12'}, io.BytesIO(b'private error body'))
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'unit-test-only'}), \
                patch.object(ai_request.urllib.request, 'urlopen', side_effect=error) as call:
            with self.assertRaises(ai_request.ModelRateLimitError) as raised:
                ai_request.ask_claude('2000s pop')
        self.assertEqual(raised.exception.retry_after, 12)
        call.assert_called_once()
        self.assertNotIn('private error body', str(raised.exception))

    def test_model_timeout_has_clear_message_and_no_automatic_duplicate(self):
        for error in (TimeoutError(), ai_request.urllib.error.URLError(TimeoutError())):
            with patch.object(ai_request.urllib.request, 'urlopen', side_effect=error) as call:
                with self.assertRaises(ai_request.ModelTimeoutError):
                    ai_request._request_model(b'{}', 'test')
                call.assert_called_once()
                self.assertEqual(call.call_args.kwargs['timeout'], 60)

    def test_timeout_during_response_read_is_also_handled(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = TimeoutError()
        with patch.object(ai_request.urllib.request, 'urlopen', return_value=response) as call:
            with self.assertRaises(ai_request.ModelTimeoutError):
                ai_request._request_model(b'{}', 'test')
            call.assert_called_once()

    def test_comparison_timeout_names_stage_without_retry(self):
        with patch.object(ai_request.urllib.request, 'urlopen', side_effect=TimeoutError()) as call:
            with self.assertRaisesRegex(ai_request.ModelTimeoutError, 'Musikalischer Vergleich.*90s'):
                ai_request._request_model(b'{}', 'test', timeout_seconds=90, stage='Musikalischer Vergleich')
            call.assert_called_once()
            self.assertEqual(call.call_args.kwargs['timeout'], 90)

    def test_truncated_response_is_not_parsed_as_a_music_pool(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(dict(stop_reason='max_tokens', content=[dict(text='{}')])).encode()
        with patch.object(ai_request.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(RequestValidationError, 'abgeschnitten'):
                ai_request._request_model(b'{}', 'test')


class SoundtrackReferenceTests(unittest.TestCase):
    def setUp(self):
        self.sp = Mock()
        self.plan = request(artists=['Arcane (TV Series)', 'Artist'], selection='hits',
                            songs=[dict(artist='Artist', title='Hit')],
                            reference_songs=[dict(artist='Arcane (TV Series)', title='Renegade')],
                            similarity_traits=['starker Mitsing-Refrain'], year_start=None, year_end=None)
        self.track = song('Renegade (We Never Run) - from the series Arcane League of Legends', '2024',
                          id='Recording', uri='spotify:track:Recording', isrc='USABC2400001')
        self.track['artists'] = [dict(id='Raja', name='Raja Kumari'), dict(id='Stefflon', name='Stefflon Don')]
        self.track['album']['name'] = 'Arcane League of Legends: Season 2'
        self.sp.search.return_value = dict(tracks=dict(items=[self.track]))

    def resolve(self, plan=None):
        return resolve_reference_contexts(self.sp, plan or self.plan,
            lambda ts: dj.select_best_tracks(ts, min_duration_ms=1000, allow_compilations=True))

    def test_series_is_resolved_from_actual_track_credits_and_full_title(self):
        fixed, evidence = self.resolve()
        self.assertEqual(fixed.reference_songs[0].artist, 'Raja Kumari')
        self.assertEqual(fixed.reference_songs[0].title, self.track['name'])
        self.assertEqual(fixed.artists, ('Raja Kumari', 'Artist'))
        self.assertEqual(evidence['Raja Kumari'], [self.track])
        self.sp.search.assert_called_once()
        self.assertNotIn('artist:', self.sp.search.call_args.kwargs['q'])
        from dataclasses import asdict
        self.assertEqual(parse_music_request(json.loads(json.dumps(asdict(fixed)))), fixed)

    def test_explicit_context_with_complete_title_and_real_performer(self):
        from dataclasses import replace
        from music_request import ReferenceSong
        plan = replace(self.plan, artists=('Stefflon Don', 'Artist'),
                       reference_songs=(ReferenceSong('Stefflon Don', 'Renegade (We Never Run)', 'Arcane'),))
        fixed, _ = self.resolve(plan)
        self.assertEqual(fixed.reference_songs[0].artist, 'Stefflon Don')

    def test_explicit_context_type_label_is_not_required_in_catalog(self):
        from dataclasses import replace
        from music_request import ReferenceSong
        for performer in ('Arcane (TV Series)', 'Arcane', 'Stefflon Don'):
            plan = replace(self.plan, artists=(performer, 'Artist'),
                           reference_songs=(ReferenceSong(performer, 'Renegade', 'Arcane (TV Series)'),))
            fixed, _ = self.resolve(plan)
            self.assertEqual(fixed.reference_songs[0].context, 'Arcane')
            self.assertNotIn('TV Series', self.sp.search.call_args.kwargs['q'])

    def test_wrong_context_unrelated_title_and_live_version_are_rejected(self):
        for changes in (dict(name='Renegade Love'), dict(name='Renegade (Live)'),
                        dict(name='Renegade', album={**self.track['album'], 'name': 'Unrelated'})):
            self.sp.reset_mock()
            self.sp.search.return_value = dict(tracks=dict(items=[{**self.track, **changes}]))
            with self.assertRaises(RequestValidationError):
                self.resolve()
            self.assertEqual(self.sp.search.call_count, 2)

    def test_multiple_recordings_do_not_choose_first_result(self):
        other = copy.deepcopy(self.track)
        other['external_ids']['isrc'] = 'USABC2400002'
        self.sp.search.return_value = dict(tracks=dict(items=[self.track, other]))
        with self.assertRaises(RequestValidationError):
            self.resolve()

    def test_context_does_not_override_explicit_different_artist(self):
        from dataclasses import replace
        from music_request import ReferenceSong
        plan = replace(self.plan, reference_songs=(ReferenceSong('Artist', 'Renegade', 'Arcane'),))
        with self.assertRaises(RequestValidationError):
            self.resolve(plan)

    def test_rate_limit_propagates_without_more_searches(self):
        self.sp.search.side_effect = dj.RateLimitError(20)
        with self.assertRaises(dj.RateLimitError):
            self.resolve()
        self.sp.search.assert_called_once()

    def test_ordinary_reference_needs_no_context_search(self):
        from dataclasses import replace
        from music_request import SongWish
        plan = replace(self.plan, reference_songs=(SongWish('Artist', 'Hit'),))
        self.assertEqual(self.resolve(plan), (plan, {}))
        self.sp.search.assert_not_called()


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

    def test_broad_hit_suggestions_do_not_require_user_to_choose_a_version(self):
        self.plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        found, missing = self.resolve([song(isrc='USABC0300001'), song(isrc='USABC0300002', duration_ms=240000)])
        self.assertEqual(len(found), 1)
        self.assertEqual(missing, [])

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
                      ai_request_plan=None, ai_request_tracks_ready=False, ai_song_queue=[], ai_song_cycle=(), ai_song_resolutions={},
                      ai_pool=[('Artist', 'artist')], ai_artists={'Artist': 'artist'},
                      ai_session_catalogs={}, ai_artist_plays={}, ai_last_artist=None, ai_session=dj.AISession(),
                      ai_recent_titles=[], recent_titles=[], recent_artists=[],
                      auto_mode='ai', current_pool=None, _prepared_next=None, _pending_selection=None,
                      artist_cache={}, track_disk_cache={},
                      banned_track_ids=set(), banned_artist_names=set())
        for key, value in values.items():
            self.stack.enter_context(patch.object(dj, key, value))
        self.sp = self.stack.enter_context(patch.object(dj, 'sp', Mock()))
        self.sp.search.return_value = {'tracks': {'items': []}}
        self.stack.enter_context(patch.object(dj.time, 'sleep'))
        for name in ('write_json_atomic', 'save_track_cache'):
            self.stack.enter_context(patch.object(dj, name))
        self.fetch = self.stack.enter_context(patch.object(dj, 'get_artist_tracks'))
        self.start = self.stack.enter_context(patch.object(dj, 'safe_play', side_effect=self.confirm))

    def confirm(self, **kwargs):
        return dict(item={'uri': kwargs['offset']['uri']}, is_playing=True, progress_ms=0, device={'id': 'pc'})

    def reference_plan(self, **overrides):
        return request(selection='hits', songs=[dict(artist='Artist', title='Hit')],
                       reference_songs=[dict(artist='Artist', title='Anchor')],
                       similarity_traits=['starker Mitsing-Refrain'], **overrides)

    def test_reference_first_then_only_matching_pool_without_learning(self):
        dj.ai_request_plan = self.reference_plan()
        dj.artist_cache['Artist'] = [song('Anchor'), song('Hit'), song('Unrelated')]
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertEqual(dj.now_playing['uri'], song('Anchor')['uri'])
        self.assertFalse(dj.explicit_songs_pending())
        self.assertEqual({t['name'] for t in dj.ai_session_catalogs['Artist']}, {'Anchor', 'Hit'})
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True, interrupted=False))
        self.assertEqual(dj.now_playing['uri'], song('Hit')['uri'])
        dj.write_json_atomic.assert_not_called()
        self.fetch.assert_not_called()

    def test_verified_soundtrack_evidence_is_used_for_first_playback(self):
        fixture = SoundtrackReferenceTests()
        fixture.setUp()
        plan, evidence = fixture.resolve()
        dj.ai_request_plan = plan
        dj.ai_pool = [('Raja Kumari', 'Raja'), ('Artist', 'artist')]
        dj.ai_artists = dict(dj.ai_pool)
        dj.ai_session_catalogs.update(evidence)
        dj.artist_cache['Artist'] = [song('Hit')]
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertEqual(dj.now_playing['uri'], fixture.track['uri'])
        self.sp.search.assert_not_called()
        self.fetch.assert_not_called()
        dj.write_json_atomic.assert_not_called()

    def test_all_references_confirmed_before_musical_selection(self):
        from dataclasses import replace
        plan = self.reference_plan()
        dj.artist_cache['Artist'] = [song('Anchor')]
        with patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]), \
                patch.object(dj, 'select_similar_songs', return_value=plan) as select:
            result, evidence = dj.prepare_reference_selection(replace(plan, songs=()), 'Songs wie Anchor')
            self.assertEqual(evidence['Artist'][0]['uri'], song('Anchor')['uri'])
            self.assertEqual(select.call_args.args[0].reference_songs[0].title, 'Anchor')
            self.assertEqual(select.call_args.args[0].songs, ())
            self.start.assert_not_called()
            dj.write_json_atomic.assert_not_called()

    def test_missing_reference_never_triggers_recommendation_call(self):
        with patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]), \
                patch.object(dj, 'select_similar_songs') as select:
            with self.assertRaises(RequestValidationError):
                dj.prepare_reference_selection(self.reference_plan(), 'Songs wie Anchor')
            select.assert_not_called()
            self.start.assert_not_called()

    def test_repeat_only_change_reuses_reference_pool_without_model_or_search(self):
        dj.ai_session.plan = self.reference_plan()
        with patch.object(dj, 'select_similar_songs') as select:
            plan, evidence = dj.prepare_reference_selection(dj.ai_session.plan, 'Wiederholungen erlauben')
            self.assertEqual(plan, dj.ai_session.plan)
            self.sp.search.assert_not_called()
            select.assert_not_called()

    def test_background_merge_deduplicates_and_never_changes_permanent_learning(self):
        from music_request import SongWish
        dj.ai_request_plan = self.reference_plan()
        dj.ai_session_catalogs['Artist'] = [song('Hit')]
        controller = Mock()
        controller.poll.return_value = (([('Artist', song('Hit')), ('Artist', song('New'))],
                                         (SongWish('Artist', 'New'),)), None)
        controller.TARGET = 36
        with patch.object(dj, 'ai_refill', controller), patch.object(dj, 'ai_refill_seen', set()):
            dj.service_ai_refill()
            self.assertEqual([t['name'] for t in dj.ai_session_catalogs['Artist']], ['Hit', 'New'])
            dj.write_json_atomic.assert_not_called()
            dj.save_track_cache.assert_not_called()
            self.start.assert_not_called()

    def test_background_error_keeps_pool_and_does_not_restart_playback(self):
        dj.ai_request_plan = self.reference_plan()
        dj.ai_session_catalogs['Artist'] = [song('Hit')]
        controller = Mock()
        controller.poll.return_value = (None, TimeoutError())
        controller.due = 0
        controller.attempts, controller.MAX_ATTEMPTS = 1, 6
        with patch.object(dj, 'ai_refill', controller):
            dj.service_ai_refill()
            self.assertEqual(dj.ai_session_catalogs['Artist'], [song('Hit')])
            self.start.assert_not_called()

    def test_background_result_rechecks_current_bans(self):
        dj.ai_request_plan = self.reference_plan()
        dj.ai_session_catalogs['Artist'] = [song('Hit')]
        dj.banned_track_ids.add(song('New')['id'])
        controller = Mock()
        controller.poll.return_value = (([('Artist', song('New'))], ()), None)
        controller.TARGET = 36
        with patch.object(dj, 'ai_refill', controller), patch.object(dj, 'ai_refill_seen', set()):
            dj.service_ai_refill()
            self.assertEqual(dj.ai_session_catalogs['Artist'], [song('Hit')])

    def test_missing_reference_stops_without_substitute(self):
        dj.ai_request_plan = self.reference_plan()
        dj.artist_cache['Artist'] = [song('Hit')]
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.start.assert_not_called()
        self.assertFalse(dj.ai_request_tracks_ready)

    def test_refinement_keeps_anchor_but_does_not_queue_it_again(self):
        dj.ai_request_plan = self.reference_plan(context_action='refine')
        dj.artist_cache['Artist'] = [song('Anchor'), song('Hit')]
        self.assertTrue(dj.ensure_structured_request())
        self.assertEqual(dj.ai_song_queue, [])

    def test_reference_conflict_with_session_ban_requires_clarification(self):
        from music_request import SongWish
        dj.ai_session.excluded_songs = (SongWish('Artist', 'Anchor'),)
        with self.assertRaises(RequestValidationError):
            dj.ai_session.reconcile(self.reference_plan())

    def test_childhood_request_can_play_despite_ambiguous_copies_and_unresolved_artist(self):
        plan = request(kind='songs', artists=['Artist', 'Unresolved'], songs=[
            dict(artist='Artist', title='SexyBack'), dict(artist='Unresolved', title='OtherHit')])
        dj.ai_request_plan = check_request_kind(plan, '2000s pop from my childhood please')
        dj.artist_cache['Artist'] = [song('SexyBack', isrc='USABC0300001'),
                                    song('SexyBack', isrc='USABC0300002', duration_ms=240000)]
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertEqual(dj.now_playing['uri'], song('SexyBack')['uri'])
        self.assertEqual(dj.ai_song_queue, [])
        self.fetch.assert_not_called()
        dj.write_json_atomic.assert_not_called()

    def prepare(self):
        dj.now_playing.update(progress_ms=185000, listened_ms=185000, preparation_attempted=False)
        dj.prepare_next_track()

    def test_hits_pool_contains_only_resolved_hints_in_era_and_keeps_constraints_on_transition(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit'), dict(artist='Artist', title='HitTwo')])
        dj.artist_cache['Artist'] = [song(), song('HitTwo'), song('DeepCut'), song('Future', '2013')]
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertEqual({t['name'] for t in dj.ai_session_catalogs['Artist']}, {'Hit', 'HitTwo'})
        first = dj.now_playing['uri']
        self.prepare()
        self.assertNotEqual(dj._prepared_next['selection'][2]['uri'], first)
        dj.judge_last_track()
        dj.now_playing['confirmed'] = False
        dj.advance_to_next_track()
        self.assertTrue(dj.now_playing['is_ai_play'])
        dj.write_json_atomic.assert_not_called()
        dj.write_json_atomic.assert_not_called()
        self.fetch.assert_not_called()

    def test_explicit_list_order_overrides_recent_history_and_stops_after_last(self):
        dj.ai_request_plan = request(kind='songs', repeat_policy='session', avoid_session_repeats=True,
                                    songs=[dict(artist='Artist', title='Two'), dict(artist='Artist', title='One')])
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
        dj.ai_request_plan = request(selection='hits', repeat_policy='session', avoid_session_repeats=True,
                                    songs=[dict(artist='Artist', title='Hit')])
        dj.artist_cache['Artist'] = [song()]
        dj.play_artist('Artist', 'ai', no_judge=True)
        self.prepare()
        self.assertTrue(dj._prepared_next['stop'])

    def test_rate_limit_during_resolution_stays_controlled_and_does_not_learn(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        self.sp.search.side_effect = dj.RateLimitError(30)
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.start.assert_not_called()
        dj.write_json_atomic.assert_not_called()
        self.sp.search.assert_called_once()
        self.fetch.assert_not_called()

    def test_request_to_playback_keeps_the_structured_constraints(self):
        plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        dj.artist_cache['Artist'] = [song(), song('Future', '2013')]
        with patch.object(dj.os.path, 'exists', return_value=True), \
                patch('builtins.open', side_effect=[io.StringIO('ai:2000s pop'), io.StringIO('quit')]), \
                patch.object(dj.os, 'remove'), patch.object(dj, 'ask_claude', return_value=plan), \
                patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]), \
                patch.object(dj, 'observe_external_playback'), patch.object(dj, 'track_finished', return_value=False):
            with self.assertRaises(SystemExit):
                dj.run_dj()
        self.assertEqual(dj.ai_request_plan.year_end, 2009)
        self.assertEqual(dj.now_playing['uri'], song()['uri'])
        dj.write_json_atomic.assert_not_called()

    def test_normal_mode_does_not_inherit_ai_year_constraint(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        dj.artist_cache['Artist'] = [song()]
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.fetch.return_value = [song('Future', '2013')]
        dj.ai_pool.clear()
        dj.play_artist('Artist', 'jpop', ['Artist'], mode_switch=True)
        self.assertEqual(dj.now_playing['uri'], song('Future', '2013')['uri'])
        self.assertFalse(dj.now_playing['is_ai_play'])
        dj.write_json_atomic.assert_not_called()

    def test_broad_hits_search_directly_without_downloading_full_catalog(self):
        dj.ai_request_plan = request(selection='hits', songs=[dict(artist='Artist', title='Hit')])
        self.sp.search.return_value = dict(tracks=dict(items=[song()]))
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.fetch.assert_not_called()
        self.sp.search.assert_called_once()

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
