import contextlib
import copy
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ai_session import AISession, is_session_reset
from music_request import RequestValidationError, parse_music_request, preserve_refinement_selection
import ai_request
import spotify_dj as dj
from playback_state import PlaybackTracker
import test_music_request as fixtures


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = AISession()

    def accept(self, plan=None, text='2000s pop'):
        plan = self.session.reconcile(plan or fixtures.request(artist_scope='suggested'))
        self.session.accept(plan, text)
        return plan

    def test_followup_retains_period_and_style_but_updates_mood(self):
        self.accept()
        plan = self.session.reconcile(fixtures.request(context_action='refine', year_start=None,
                                                       year_end=None, style='', mood='less aggressive'))
        self.assertEqual((plan.year_start, plan.year_end, plan.style), (2000, 2009, 'pop'))
        self.assertEqual(plan.mood, 'less aggressive')

    def test_fixed_artist_pool_cannot_silently_expand(self):
        self.accept(fixtures.request(artists=['Ado', 'Eve']))
        with self.assertRaises(RequestValidationError):
            self.session.reconcile(fixtures.request(context_action='refine', artists=['YOASOBI']))

    def test_suggested_pool_can_adapt_to_mood(self):
        self.accept()
        plan = self.session.reconcile(fixtures.request(context_action='refine', artists=['Other']))
        self.assertEqual(plan.artists, ('Other',))
        self.assertEqual(plan.artist_scope, 'suggested')

    def test_mood_change_preserves_free_selection_and_keeps_hints_out_of_queue(self):
        self.accept(fixtures.request(artist_scope='suggested'))
        incoming = fixtures.request(context_action='refine', selection='hits', mood='aggressive, hype',
                                    songs=[dict(artist='Artist', title='HypeOne'), dict(artist='Artist', title='HypeTwo')])
        plan = self.session.reconcile(incoming, 'more hype/aggressive')
        self.assertEqual((plan.selection, plan.mood), ('any', 'aggressive, hype'))
        self.assertEqual(plan.songs, ())
        self.assertEqual([h.title for h in plan.artist_hints], ['HypeOne', 'HypeTwo'])

    def test_mood_change_preserves_hits_when_model_suggests_free_selection(self):
        previous = fixtures.request(selection='hits', songs=[dict(artist='Artist', title='Old')])
        incoming = fixtures.request(context_action='refine', mood='aggressive', songs=[dict(artist='Artist', title='New')])
        plan = preserve_refinement_selection(incoming, previous, 'more hype/aggressive')
        self.assertEqual(plan.selection, 'hits')
        self.assertEqual(plan.songs[0].title, 'New')

    def test_explicit_selection_changes_still_work_in_both_directions(self):
        self.accept(fixtures.request(selection='hits', songs=[dict(artist='Artist', title='Old')]))
        free = self.session.reconcile(fixtures.request(context_action='refine'), 'use the full catalog')
        self.assertEqual(free.selection, 'any')
        hits = preserve_refinement_selection(fixtures.request(context_action='refine',
            songs=[dict(artist='Artist', title='Hit')]), fixtures.request(), 'more hits please')
        self.assertEqual(hits.selection, 'hits')
        self.assertEqual(preserve_refinement_selection(hits, self.session.plan, 'keine Hits').selection, 'any')

    def test_identity_hints_are_validated_and_do_not_create_song_wishes(self):
        plan = fixtures.request(artist_hints=[dict(artist='Artist', title='First'), dict(artist='Artist', title='Second')])
        self.assertEqual(plan.songs, ())
        self.assertEqual(len(plan.artist_hints), 2)
        for hints in ([dict(artist='Other', title='First')], [dict(artist='Artist', title='First', id='invented')]):
            with self.assertRaises(RequestValidationError):
                fixtures.request(artist_hints=hints)

    def test_followup_cannot_silently_change_existing_period(self):
        self.accept()
        with self.assertRaises(RequestValidationError):
            self.session.reconcile(fixtures.request(context_action='refine', year_start=2010, year_end=2019))

    def test_new_request_can_change_period_and_artist_scope(self):
        self.accept(fixtures.request())
        plan = self.session.reconcile(fixtures.request(artists=['Ado'], year_start=None, year_end=None, style='jpop'))
        self.assertEqual(plan.artists, ('Ado',))
        self.assertIsNone(plan.year_start)
        self.assertEqual(plan.style, 'jpop')

    def test_lost_hits_constraint_requires_correction(self):
        self.accept(fixtures.request(selection='hits', songs=[dict(artist='Artist', title='Hit')]))
        with self.assertRaises(RequestValidationError):
            self.session.reconcile(fixtures.request(context_action='refine'))

    def test_no_referent_asks_for_clarification(self):
        with self.assertRaisesRegex(RequestValidationError, 'Worauf'):
            self.session.reconcile(fixtures.request(context_action='refine'))

    def test_pending_clarification_and_last_song_are_available_without_ids(self):
        self.session.note_problem('Play Hello', 'Welchen Künstler meinst du?')
        self.session.record_play('Artist', fixtures.song())
        context = self.session.context()
        self.assertEqual(context['pending']['request'], 'Play Hello')
        self.assertEqual(context['last_confirmed_song'], dict(artist='Artist', title='Hit'))
        self.assertNotIn('spotify:', json.dumps(context))
        self.accept(fixtures.request(kind='songs', songs=[dict(artist='Artist', title='Hello')]))
        self.assertIsNone(self.session.pending)

    def test_exclusions_accumulate_and_apply_to_new_requests_and_collaborations(self):
        plan = self.accept(fixtures.request(excluded_artists=['Other'],
                                           excluded_songs=[dict(artist='Artist', title='Hit')]))
        self.assertFalse(self.session.allows(plan, 'Artist', fixtures.song()))
        collaboration = fixtures.song('Different')
        collaboration['artists'].append(dict(name='Other', id='other'))
        self.assertFalse(self.session.allows(plan, 'Artist', collaboration))
        next_plan = self.session.reconcile(fixtures.request())
        self.assertEqual(next_plan.excluded_artists, ('Other',))
        self.assertEqual(next_plan.excluded_songs[0].title, 'Hit')

    def test_excluding_artist_narrows_pool_and_hit_hints(self):
        plan = self.session.reconcile(fixtures.request(artists=['Artist', 'Other'], excluded_artists=['Other'],
            selection='hits', songs=[dict(artist='Artist', title='Hit'), dict(artist='Other', title='OtherHit')]))
        self.assertEqual(plan.artists, ('Artist',))
        self.assertEqual(len(plan.songs), 1)

    def test_explicit_song_conflicting_with_exclusion_is_not_silently_removed(self):
        with self.assertRaises(RequestValidationError):
            self.session.reconcile(fixtures.request(kind='songs', songs=[dict(artist='Artist', title='Hit')],
                excluded_songs=[dict(artist='Artist', title='Hit')]))

    def test_no_repeats_includes_earlier_session_not_only_recent_thirty(self):
        self.session.record_play('Artist', fixtures.song())
        for i in range(40):
            self.session.record_play('Artist', fixtures.song(f'Other{i}'))
        plan = self.accept(fixtures.request(avoid_session_repeats=True))
        other_album = fixtures.song(year='2020')
        self.assertFalse(self.session.allows(plan, 'Artist', other_album))
        self.assertTrue(self.session.allows(plan, 'Artist', fixtures.song('Fresh')))
        self.assertTrue(self.session.allows(plan, 'Artist', other_album, explicit=True))
        self.assertTrue(self.session.reconcile(fixtures.request()).avoid_session_repeats)

    def test_same_title_by_different_artist_is_not_a_repeat(self):
        self.session.record_play('Artist', fixtures.song())
        other = fixtures.song(artist='other')
        other['artists'][0]['name'] = 'Other'
        self.assertTrue(self.session.allows(fixtures.request(avoid_session_repeats=True), 'Other', other))

    def test_normal_mode_leaves_topic_but_retains_session_history_and_exclusions(self):
        self.accept(fixtures.request(excluded_artists=['Other']))
        self.session.record_play('Artist', fixtures.song())
        self.session.leave_ai()
        self.assertIsNone(self.session.plan)
        self.assertEqual(list(self.session.turns), [])
        self.assertTrue(self.session.heard)
        self.assertEqual(self.session.excluded_artists, ('Other',))

    def test_reset_and_new_process_have_no_old_context_or_filters(self):
        self.accept(fixtures.request(avoid_session_repeats=True, excluded_artists=['Other']))
        self.session.record_play('Artist', fixtures.song())
        self.session.clear()
        self.assertEqual(self.session.context(), AISession().context())
        self.assertFalse(self.session.heard)
        for text in ('Neue AI-Session', 'AI reset', 'new AI session'):
            self.assertTrue(is_session_reset(text))
        self.assertFalse(is_session_reset('Play AI Reset by Artist'))

    def test_context_and_history_stay_bounded(self):
        for i in range(8):
            self.accept(text=str(i) * 2000)
        self.assertEqual(len(self.session.turns), 4)
        self.assertTrue(all(len(t) <= 1000 for t in self.session.turns))
        self.session.HISTORY_LIMIT = 3
        for i in range(5):
            self.session.record_play('Artist', fixtures.song(str(i)))
        self.assertEqual(len(self.session.heard), 3)

    def test_invalid_session_fields_and_ids_are_rejected(self):
        for extra in (dict(context_action='guess'), dict(artist_scope=[]), dict(avoid_session_repeats='yes'),
                      dict(excluded_artists='Artist'), dict(excluded_songs=[dict(uri='spotify:track:fake')])):
            with self.subTest(extra=extra), self.assertRaises(RequestValidationError):
                fixtures.request(**extra)

    def test_model_receives_bounded_context_and_clarification_answer(self):
        self.accept()
        self.session.note_problem('Play Hello', 'Welcher Künstler?')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(dict(content=[dict(text=json.dumps(asdict(fixtures.request())))] )).encode()
        # JSON uses lists, whereas asdict retains tuples; json.dumps normalizes them.
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}), \
                patch.object(ai_request.urllib.request, 'urlopen', return_value=response) as call:
            ai_request.ask_claude('Adele', self.session.context())
        sent = json.loads(call.call_args.args[0].data)
        content = sent['messages'][0]['content']
        self.assertIn('Play Hello', content)
        self.assertIn('Adele', content)
        self.assertIn('2000', content)
        self.assertNotIn('spotify:', content)


def model_response(data):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(dict(content=[dict(text=json.dumps(data))])).encode()
    return response


class IncompleteRefinementTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'unit-test-only'}))
        self.http = self.stack.enter_context(patch.object(ai_request.urllib.request, 'urlopen'))
        self.session = AISession()
        self.previous = fixtures.request(selection='hits', artist_scope='suggested',
                                        songs=[dict(artist='Artist', title='Hit')])
        self.session.accept(self.previous, '2000s pop from my childhood please')

    def test_free_gaming_selection_ignores_model_hit_switch_without_repair_call(self):
        self.session.accept(fixtures.request(artist_scope='suggested', year_start=None, year_end=None,
                                            style='gaming music', mood='focused'), 'Music to game to')
        data = asdict(fixtures.request(context_action='refine', year_start=None,
                                      year_end=None, style='gaming music', mood='aggressive, hype'))
        data.update(selection='hits', songs=[])
        self.http.return_value = model_response(data)
        plan = ai_request.ask_claude('more hype/aggressive', self.session.context())
        self.assertEqual(plan.selection, 'any')
        self.assertEqual(plan.mood, 'aggressive, hype')
        self.assertEqual(plan.songs, ())
        self.http.assert_called_once()

    def test_filter_only_update_reuses_existing_hints_without_second_model_call(self):
        data = asdict(self.previous)
        data.update(context_action='refine', songs=[], avoid_session_repeats=True)
        self.http.return_value = model_response(data)
        plan = self.session.reconcile(ai_request.ask_claude('Heute keine Wiederholungen von vorhin', self.session.context()))
        self.assertEqual(plan.songs, self.previous.songs)
        self.assertEqual(plan.mood, self.previous.mood)
        self.assertEqual((plan.year_start, plan.year_end), (2000, 2009))
        self.assertTrue(plan.avoid_session_repeats)
        self.http.assert_called_once()

    def test_hype_update_repairs_missing_hints_once_and_keeps_years_and_artists(self):
        data = asdict(self.previous)
        data.update(context_action='refine', songs=[], mood='aggressive, hype', year_start=None, year_end=None)
        self.http.side_effect = [model_response(data), model_response(dict(
            songs=[dict(artist='Artist', title='HypeHit')], mood='aggressive, hype'))]
        plan = self.session.reconcile(ai_request.ask_claude('Mehr davon, aber mehr aggressiv/hype', self.session.context()))
        self.assertEqual(plan.songs[0].title, 'HypeHit')
        self.assertEqual(plan.mood, 'aggressive, hype')
        self.assertEqual((plan.year_start, plan.year_end), (2000, 2009))
        self.assertEqual(plan.artists, self.previous.artists)
        self.assertEqual(self.http.call_count, 2)
        repair = json.loads(self.http.call_args.args[0].data)
        self.assertIn('Mehr davon, aber mehr aggressiv/hype', repair['messages'][0]['content'])

    def test_unchanged_model_mood_does_not_make_hype_request_filter_only(self):
        data = asdict(self.previous)
        data.update(context_action='refine', songs=[])
        self.http.side_effect = [model_response(data), model_response(dict(
            songs=[dict(artist='Artist', title='NewHypeHit')], mood='more aggressive and energetic'))]
        plan = ai_request.ask_claude('Mehr davon, aber mehr aggressiv/hype', self.session.context())
        self.assertEqual(plan.mood, 'more aggressive and energetic')
        self.assertEqual(plan.songs[0].title, 'NewHypeHit')
        self.assertEqual(self.http.call_count, 2)

    def test_invalid_repair_stops_after_one_attempt_without_changing_session(self):
        data = asdict(self.previous)
        data.update(context_action='refine', songs=[], mood='hype')
        self.http.side_effect = [model_response(data), model_response(dict(songs=[], mood='hype'))]
        with self.assertRaises(RequestValidationError):
            ai_request.ask_claude('more of that but hype', self.session.context())
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.session.plan, self.previous)

    def test_repair_cannot_inject_artist_or_spotify_ids(self):
        data = asdict(self.previous)
        data.update(context_action='refine', songs=[], mood='hype')
        self.http.side_effect = [model_response(data), model_response(dict(
            songs=[dict(artist='Other', title='Hit', uri='invented')], mood='hype'))]
        with self.assertRaises(RequestValidationError):
            ai_request.ask_claude('more of that but hype', self.session.context())
        self.assertEqual(self.http.call_count, 2)

    def test_no_empty_hits_relaxation_for_explicit_song_wishes_or_new_requests(self):
        for kind, action in [('songs', 'refine'), ('discovery', 'new')]:
            data = asdict(self.previous)
            data.update(kind=kind, context_action=action, songs=[])
            self.http.return_value = model_response(data)
            self.http.reset_mock()
            with self.subTest(kind=kind), self.assertRaises(RequestValidationError):
                ai_request.ask_claude('a concrete song request', self.session.context())
            self.http.assert_called_once()

    def test_empty_refinement_without_previous_pool_does_not_trigger_repair(self):
        data = asdict(self.previous)
        data.update(context_action='refine', songs=[])
        self.http.return_value = model_response(data)
        with self.assertRaises(RequestValidationError):
            ai_request.ask_claude('more of that', AISession().context())
        self.http.assert_called_once()


class PlaybackSessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        tracker = PlaybackTracker()
        values = dict(ai_session=AISession(), tracker=tracker, now_playing=tracker.track,
                      ai_request_plan=None, ai_request_tracks_ready=False, ai_song_queue=[], ai_song_resolutions={},
                      ai_pool=[('Artist', 'artist')], ai_artists={'Artist': 'artist'},
                      ai_session_catalogs={}, ai_artist_plays={}, ai_last_artist=None,
                      ai_recent_titles=[], recent_titles=[], recent_artists=[],
                      auto_mode='ai', current_pool=None, _prepared_next=None, _pending_selection=None,
                      memory=dj.default_memory(), artist_cache={}, track_disk_cache={},
                      banned_track_ids=set(), banned_artist_names=set())
        for key, value in values.items():
            self.stack.enter_context(patch.object(dj, key, value))
        self.sp = self.stack.enter_context(patch.object(dj, 'sp', Mock()))
        self.sp.search.return_value = dict(tracks=dict(items=[]))
        self.stack.enter_context(patch.object(dj.time, 'sleep'))
        for name in ('save_memory', 'save_recent', 'save_track_cache', 'save_banned', 'save_banned_artists',
                     'run_pending_discoveries', 'record_trial_play'):
            self.stack.enter_context(patch.object(dj, name))
        self.fetch = self.stack.enter_context(patch.object(dj, 'get_artist_tracks', return_value=[fixtures.song()]))
        self.start = self.stack.enter_context(patch.object(dj, 'safe_play', side_effect=self.confirm))

    def confirm(self, **kwargs):
        return dict(item=dict(uri=kwargs['offset']['uri']), is_playing=True, progress_ms=0, device=dict(id='pc'))

    def commands(self, commands):
        with patch.object(dj.os.path, 'exists', return_value=True), \
                patch('builtins.open', side_effect=[io.StringIO(c) for c in commands + ['quit']]), \
                patch.object(dj.os, 'remove'), patch.object(dj, 'observe_external_playback'), \
                patch.object(dj, 'track_finished', return_value=False):
            with self.assertRaises(SystemExit):
                dj.run_dj()

    def test_followup_receives_previous_intent_and_last_confirmed_song(self):
        first = fixtures.request(artist_scope='suggested')
        followup = fixtures.request(context_action='refine', artist_scope='suggested',
                                   mood='less aggressive', style='', year_start=None, year_end=None)
        before = copy.deepcopy(dj.memory)
        with patch.object(dj, 'ask_claude', side_effect=[first, followup]) as ask, \
                patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]):
            self.commands(['ai:2000s pop', 'ai:mehr davon, aber weniger aggressiv'])
        context = ask.call_args_list[1].args[1]
        self.assertEqual(context['active_request']['year_end'], 2009)
        self.assertEqual(context['last_confirmed_song']['title'], 'Hit')
        self.assertEqual(dj.ai_request_plan.year_start, 2000)
        self.assertEqual(dj.ai_request_plan.mood, 'less aggressive')
        self.assertEqual(dj.memory, before)
        for name in ('save_memory', 'save_recent', 'save_banned', 'save_banned_artists'):
            getattr(dj, name).assert_not_called()

    def test_hype_then_no_repeats_keeps_new_mood_and_never_learns(self):
        first = fixtures.request(selection='hits', artist_scope='suggested', songs=[dict(artist='Artist', title='Hit')])
        hype = asdict(first)
        hype.update(context_action='refine', mood='aggressive, hype', songs=[])
        filters = dict(hype, avoid_session_repeats=True)
        repair = dict(songs=[dict(artist='Artist', title='HypeOne'), dict(artist='Artist', title='HypeTwo')], mood='aggressive, hype')
        dj.artist_cache['Artist'] = [fixtures.song(), fixtures.song('HypeOne'), fixtures.song('HypeTwo')]
        before = copy.deepcopy(dj.memory)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'unit-test-only'}), \
                patch.object(ai_request.urllib.request, 'urlopen', side_effect=[
                    model_response(asdict(first)), model_response(hype), model_response(repair), model_response(filters)]) as http, \
                patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]):
            self.commands(['ai:2000s pop from my childhood please', 'ai:Mehr davon, aber mehr aggressiv/hype',
                           'ai:Heute keine Wiederholungen von vorhin'])
        self.assertEqual(http.call_count, 4)
        self.assertEqual(self.start.call_count, 3)
        self.assertEqual(dj.ai_request_plan.mood, 'aggressive, hype')
        self.assertTrue(dj.ai_request_plan.avoid_session_repeats)
        self.assertEqual(dj.memory, before)
        self.assertNotEqual(self.start.call_args_list[1].kwargs['offset']['uri'], self.start.call_args_list[2].kwargs['offset']['uri'])
        dj.save_memory.assert_not_called()
        dj.save_recent.assert_not_called()

    def test_gaming_hype_refinement_keeps_free_catalog_beyond_suggested_hits(self):
        first = fixtures.request(artist_scope='suggested', style='gaming music', year_start=None, year_end=None)
        hype = asdict(fixtures.request(context_action='refine', artist_scope='suggested', selection='hits',
            style='gaming music', mood='aggressive, hype', year_start=None, year_end=None,
            songs=[dict(artist='Artist', title='HypeOne'), dict(artist='Artist', title='HypeTwo')]))
        self.fetch.return_value = [fixtures.song('HypeOne'), fixtures.song('HypeTwo'), fixtures.song('DeepCut')]
        before = copy.deepcopy(dj.memory)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'unit-test-only'}), \
                patch.object(ai_request.urllib.request, 'urlopen', side_effect=[model_response(asdict(first)), model_response(hype)]) as http, \
                patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]):
            self.commands(['ai:Music to game to', 'ai:more hype/aggressive'])
        self.assertEqual(http.call_count, 2)
        self.assertEqual(dj.ai_request_plan.selection, 'any')
        self.assertEqual(dj.ai_request_plan.mood, 'aggressive, hype')
        self.assertEqual(dj.ai_song_queue, [])
        self.assertEqual({t['name'] for t in dj.ai_session_catalogs['Artist']}, {'HypeOne', 'HypeTwo', 'DeepCut'})
        self.assertEqual(dj.memory, before)
        dj.save_memory.assert_not_called()
        dj.save_recent.assert_not_called()

    def test_real_resolver_receives_identity_anchors_and_cached_catalog(self):
        plan = fixtures.request(artist_hints=[dict(artist='Artist', title='First'), dict(artist='Artist', title='Second')])
        dj.artist_cache['Artist'] = [fixtures.song('First'), fixtures.song('Second')]
        self.sp.search.return_value = dict(artists=dict(items=[dict(name='Artist', id='wrong'), dict(name='Artist', id='artist')]))
        with patch.object(dj, 'ask_claude', return_value=plan):
            self.commands(['ai:Music to game to'])
        self.assertEqual(dj.ai_pool, [('Artist', 'artist')])
        self.sp.search.assert_called_once()
        self.assertEqual(self.start.call_count, 1)
        dj.save_memory.assert_not_called()

    def test_failed_start_does_not_enter_session_history(self):
        dj.ai_request_plan = fixtures.request()
        self.start.side_effect = None
        self.start.return_value = None
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertFalse(dj.ai_session.heard)
        self.assertIsNone(dj.ai_session.last_song)

    def test_no_repeat_filter_applies_to_start_and_prepared_transition(self):
        dj.ai_request_plan = fixtures.request(avoid_session_repeats=True)
        dj.ai_session.record_play('Artist', fixtures.song())
        self.fetch.return_value = [fixtures.song(), fixtures.song('Fresh'), fixtures.song('Third')]
        self.assertTrue(dj.play_artist('Artist', 'ai', no_judge=True))
        self.assertNotEqual(dj.now_playing['uri'], fixtures.song()['uri'])
        dj.now_playing.update(progress_ms=185000, listened_ms=185000, preparation_attempted=False)
        dj.prepare_next_track()
        chosen = dj._prepared_next['selection'][2]
        self.assertNotEqual(chosen['uri'], fixtures.song()['uri'])
        self.assertNotEqual(chosen['uri'], dj.now_playing['uri'])
        dj.now_playing['confirmed'] = False
        dj.advance_to_next_track()
        dj.save_memory.assert_not_called()

    def test_exclusions_remain_effective_when_pool_has_no_alternative(self):
        dj.ai_request_plan = fixtures.request(excluded_artists=['Artist'])
        self.assertFalse(dj.play_artist('Artist', 'ai', no_judge=True))
        self.start.assert_not_called()
        dj.save_banned.assert_not_called()
        dj.save_banned_artists.assert_not_called()

    def test_normal_mode_ignores_ai_exclusions_and_records_confirmed_history(self):
        dj.ai_request_plan = fixtures.request(excluded_artists=['Artist'], avoid_session_repeats=True)
        dj.ai_session.record_play('Artist', fixtures.song())
        dj.ai_pool.clear()
        dj.auto_mode = 'jpop'
        self.assertTrue(dj.play_artist('Artist', 'jpop', ['Artist'], mode_switch=True))
        self.assertFalse(dj.now_playing['is_ai_play'])

    def test_reset_uses_no_model_api_or_new_playback_and_preserves_data(self):
        dj.ai_session.accept(fixtures.request(excluded_artists=['Other']), 'exclude Other')
        dj.ai_session.record_play('Artist', fixtures.song())
        dj.ai_recent_titles.append('hit')
        with patch.object(dj, 'ask_claude') as ask:
            self.commands(['ai:Neue AI-Session'])
        ask.assert_not_called()
        self.start.assert_not_called()
        self.assertFalse(dj.ai_session.heard)
        self.assertEqual(dj.ai_recent_titles, [])
        self.assertFalse(dj.tracker.control)
        dj.save_memory.assert_not_called()
        dj.save_recent.assert_not_called()

    def test_clarification_keeps_original_request_for_next_answer(self):
        dj.artist_cache['Artist'] = [fixtures.song()]
        plan = fixtures.request(kind='songs', songs=[dict(artist='Artist', title='Hit')])
        with patch.object(dj, 'ask_claude', side_effect=[RequestValidationError('Rückfrage: Welcher Künstler?'), plan]) as ask, \
                patch.object(dj, 'resolve_artists_to_ids', return_value=[('Artist', 'artist')]):
            self.commands(['ai:Play Hit', 'ai:Artist'])
        context = ask.call_args_list[1].args[1]
        self.assertEqual(context['pending']['request'], 'Play Hit')
        self.assertIsNone(dj.ai_session.pending)
        self.assertEqual(dj.now_playing['uri'], fixtures.song()['uri'])


if __name__ == '__main__':
    unittest.main()
