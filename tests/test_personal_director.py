import contextlib
import io
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import personal_director as director
import spotify_dj as dj
from personal_mix import PersonalMix, identity
from test_personal_mix import track


def pool():
    value = PersonalMix()
    value.tracks = [track('Top', 'A'), track('Recent', 'B')]
    value.sources = {'short_term': {identity(value.tracks[0])}, 'recent': {identity(value.tracks[1])}}
    return value


def response(kind='recent', artist='B', title='Recent'):
    return dict(kind=kind, title='Ein neuer Abschnitt', intro='Weiter mit deiner Musik.',
                songs=[dict(artist=artist, title=title, based_on=0, reason='Ähnliche Energie.')])


class PersonalDirectorTests(unittest.TestCase):
    def test_feedback_is_temporary_invalidates_queue_and_is_idempotent(self):
        p, d = pool(), director.PersonalDirector()
        chosen = d.choose(p, lambda _: True)
        d.confirm(chosen)
        d.queued = director.MusicBlock('discovery', 'Old', 'Old', [track('Old')])
        generation = d.jobs.generation
        self.assertTrue(d.feedback(False, chosen))
        self.assertIsNone(d.queued)
        self.assertIsNone(d.proposal)
        self.assertEqual(d.active.played, d.BLOCK_SIZE)
        self.assertGreater(d.jobs.generation, generation)
        generation = d.jobs.generation
        self.assertFalse(d.feedback(False, chosen))
        self.assertEqual(d.jobs.generation, generation)
        self.assertTrue(d.feedback(True, chosen))
        self.assertEqual(d.preferences[identity(chosen)[0]]['preference'], 'more')
        d.reset()
        self.assertEqual(d.preferences, {})

    def test_less_prefers_other_artist_but_small_pool_does_not_stall(self):
        p, d = pool(), director.PersonalDirector()
        d.preferences = {'a': dict(preference='less')}
        self.assertEqual(d.select(p, lambda _: True, p.tracks)['artists'][0]['name'], 'B')
        self.assertEqual(d.select(p, lambda _: True, p.tracks[:1])['artists'][0]['name'], 'A')

    def test_standard_feedback_does_not_start_music_or_touch_ai(self):
        d = director.PersonalDirector()
        p = pool()
        chosen = d.choose(p, lambda _: True)
        d.confirm(chosen)
        with patch.object(dj, 'personal_director', d), patch.object(dj, 'auto_mode', 'ai'), \
             patch.object(dj, 'status'), patch.object(dj, 'now_playing', {'confirmed': True}), \
             patch.object(dj, '_prepared_next', {'old': True}), patch.object(dj, 'sp') as sp, \
             patch.object(dj.tracker, 'control', True):
            self.assertFalse(dj.standard_feedback(False))
            self.assertEqual(d.preferences, {})
            with patch.object(dj, 'auto_mode', 'global'):
                self.assertTrue(dj.standard_feedback(False))
            self.assertIsNone(dj._prepared_next)
            self.assertTrue(dj.now_playing['confirmed'])
            sp.start_playback.assert_not_called()

    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'})
    def test_planning_cadence_and_session_feedback_without_extra_calls(self):
        for history, kind in [([{'kind': 'favorites'}, {'kind': 'recent'}], 'discovery'),
                              ([{'kind': 'discovery'}, {'kind': 'favorites'}], 'recent')]:
            data = response(kind, 'C', 'New') if kind == 'discovery' else response()
            data['direction'] = 'Treibende Gitarren mit großen Refrains'
            preferences = [dict(artist='A', title='Top', preference='less', direction='Ruhiger Pop')]
            with patch.object(director, '_request_model', return_value=data) as model, \
                 patch.object(director, 'resolve_artists_to_ids', return_value=[('C', 'id')]), \
                 patch.object(director, 'resolve_song_wishes', return_value=([('C', track('New', 'C'))], [])):
                block = director.plan_personal_block(pool(), history, [], Mock(), lambda ts: ts,
                    threading.Event(), preferences=preferences)
            model.assert_called_once()
            context = json.loads(json.loads(model.call_args.args[0])['messages'][0]['content'])
            self.assertEqual(context['allowed_kinds'], [kind])
            self.assertEqual(context['session_feedback'], preferences)
            self.assertIn('KI-Richtung:', block.intro)
            self.assertEqual(block.direction, data['direction'])

    def test_discovery_uses_no_more_than_two_anchors(self):
        data = response('discovery')
        data['songs'] = [dict(artist=f'New{i}', title=f'New{i}', based_on=i, reason='Treibt mit verzerrten Gitarren') for i in range(3)]
        evidence = [dict(artist=f'A{i}', title=f'T{i}', sources=['recent']) for i in range(3)]
        result = director.validate_block(data, evidence, ['discovery'], [])
        self.assertEqual(len(result[3]), 2)

    def test_artist_rotation_downweights_recent_artists_without_changing_scores(self):
        p = pool()
        p.recent_artists.extend(['a', 'a'])
        with patch('personal_mix.random.choices', return_value=[p.tracks[1]]) as choose:
            p.choose(lambda _: True)
        self.assertEqual(choose.call_args.kwargs['weights'], [.2, 1])
        self.assertEqual(p.scores, {})

    def test_feedback_chat_aliases_are_standard_only(self):
        import dj_server
        with patch.dict(dj_server.state, mode='Standard'), patch.object(dj_server, 'send_command') as send:
            dj_server.handle_command({'cmd': 'Weniger davon!'})
            send.assert_called_once_with('feedback-less')
        with patch.dict(dj_server.state, mode='AI'), patch.object(dj_server, 'send_command') as send:
            dj_server.handle_command({'cmd': 'mehr davon'})
            send.assert_called_once_with('ai:mehr davon')

    def test_two_song_block_moves_on_without_filling_four_starts(self):
        p, d = pool(), director.PersonalDirector()
        d.queued = director.MusicBlock('discovery', 'Short', 'Short', [track('Bones', 'A'), track('Patrick', 'B')])
        first = d.choose(p, lambda _: True)
        d.confirm(first)
        d.queued = director.MusicBlock('recent', 'Next', 'Next', [track('Next', 'C')])
        second = d.choose(p, lambda _: True)
        self.assertNotEqual(identity(first), identity(second))
        d.confirm(second)
        self.assertEqual(d.choose(p, lambda _: True)['name'], 'Next')

    def test_artist_rotation_cannot_repeat_song_before_unplayed_alternatives(self):
        p, d = pool(), director.PersonalDirector()
        songs = [track('Phoenix', 'Fall Out Boy')] + [track(str(i), 'Ado') for i in range(4)]
        d.queued = director.MusicBlock('recent', 'Mix', 'Mix', songs)
        played = []
        for _ in range(4):
            chosen = d.choose(p, lambda _: True)
            played.append(identity(chosen))
            d.confirm(chosen)
            p.record_start(chosen)
        self.assertEqual(len(set(played)), 4)

    def test_same_recording_on_other_album_is_not_repeated(self):
        p, d = pool(), director.PersonalDirector()
        songs = [dict(track('One'), external_ids={'isrc': 'USABC0000001'}),
                 dict(track('Another title'), external_ids={'isrc': 'USABC0000001'})]
        d.queued = director.MusicBlock('discovery', 'Copies', 'Copies', songs)
        d.confirm(d.choose(p, lambda _: True))
        d.queued = director.MusicBlock('recent', 'Next', 'Next', [track('Next')])
        self.assertEqual(d.choose(p, lambda _: True)['name'], 'Next')

    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'})
    def test_wrong_source_song_is_removed_without_new_model_call_or_false_intro(self):
        data = response()
        data['title'], data['intro'] = 'Nur A', 'Jetzt ausschließlich A.'
        data['songs'].append(dict(artist='A', title='Top', based_on=0, reason='Fits'))
        with patch.object(director, '_request_model', return_value=data) as model:
            block = director.plan_personal_block(pool(), [], [], Mock(), lambda ts: ts, threading.Event())
        model.assert_called_once()
        self.assertEqual([t['name'] for t in block.tracks], ['Recent'])
        self.assertEqual(block.title, 'Zuletzt bei dir: B')
        self.assertNotIn('ausschließlich', block.intro)
        payload = json.loads(model.call_args.args[0])
        self.assertEqual(json.loads(payload['messages'][0]['content'])['eligible_source_indices']['recent'], [1])

    def test_grounded_announcement_refreshes_after_ban_and_waits_for_confirmation(self):
        p, d = pool(), director.PersonalDirector()
        d.queued = director.MusicBlock('discovery', 'Stale', 'Stale',
                                      [track('One', 'A'), track('Two', 'B')], grounded=True)
        chosen = d.choose(p, lambda t: t['artists'][0]['name'] == 'B')
        self.assertIsNone(d.active)
        self.assertEqual(d.queued.heard, set())
        title, intro = d.confirm(chosen)
        self.assertEqual(title, 'Entdeckungen: B')
        self.assertIn('mit B.', intro)

    def test_long_presentation_text_is_shortened_without_losing_music(self):
        data = response()
        data['title'], data['intro'] = 'T' * 90, 'I' * 300
        data['songs'][0]['reason'] = 'R' * 200
        _, title, intro, songs = director.validate_block(data, director.history_snapshot(pool()), ['recent'], [])
        self.assertEqual((len(title), len(intro), len(songs)), (70, 220, 1))

    def test_validation_explains_source_and_index_errors(self):
        evidence = director.history_snapshot(pool())
        with self.assertRaisesRegex(director.RequestValidationError, 'gewählten Quelle'):
            director.validate_block(response('favorites'), evidence, ['favorites'], [])
        data = response()
        data['songs'][0]['based_on'] = True
        with self.assertRaisesRegex(director.RequestValidationError, 'Index von 0 bis 1'):
            director.validate_block(data, evidence, ['recent'], [])

    def test_excess_artists_are_limited_without_discarding_valid_songs(self):
        data = response('discovery')
        data['songs'] = [dict(artist=f'New{i}', title=f'Song{i}', based_on=0, reason='Ähnliche Energie') for i in range(6)]
        result = director.validate_block(data, director.history_snapshot(pool()), ['discovery'], [])
        self.assertEqual(len(result[3]), 4)

    def test_snapshot_retains_sources_beyond_source_sampling_cutoff(self):
        p = pool()
        extra = [track(str(i), 'C') for i in range(15)]
        p.tracks = extra + p.tracks
        p.sources['recent'] = {identity(t) for t in p.tracks}
        snapshot = director.history_snapshot(p)
        top = next(t for t in snapshot if t['title'] == 'Top')
        self.assertIn('recent', top['sources'])

    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'})
    def test_next_attempt_receives_feedback_without_an_extra_model_call(self):
        with patch.object(director, '_request_model', return_value=response()) as model:
            director.plan_personal_block(pool(), [], [], Mock(), lambda ts: ts,
                                        threading.Event(), 'Song 1: Quelle falsch.')
        model.assert_called_once()
        payload = json.loads(model.call_args.args[0])
        context = json.loads(payload['messages'][0]['content'])
        self.assertEqual(context['previous_validation_error'], 'Song 1: Quelle falsch.')

    def test_exhaustion_reports_no_further_automatic_attempt(self):
        d = director.PersonalDirector()
        d.jobs.attempts = d.jobs.MAX_ATTEMPTS
        d.jobs.results.put((d.jobs.generation, None, director.RequestValidationError('Song 1: Quelle falsch.')))
        with patch.object(dj, 'personal_director', d), patch.object(dj, 'auto_mode', 'global'), \
                patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}), contextlib.redirect_stdout(io.StringIO()) as output:
            dj.service_personal_director()
        self.assertIn('Song 1: Quelle falsch.', output.getvalue())
        self.assertIn('Automatische Planung beendet', output.getvalue())
        self.assertNotIn('Frühestens', output.getvalue())
        self.assertFalse(d.jobs.can_start(0, now=float('inf')))

    def test_start_failure_reports_expected_and_actual_without_extra_requests(self):
        sp = Mock()
        sp.current_playback.return_value = dict(item=track('Wrong', 'Other'), is_playing=True,
                                               device=dict(id='pc'), context=dict(uri='spotify:album:Album'), progress_ms=0)
        with patch.object(dj, 'sp', sp), patch.object(dj, 'DEBUG_MODE', False), \
                patch.object(dj.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
            result = dj.safe_play(_device_id='pc', _expected_title='Artist — Wanted',
                                 context_uri='spotify:album:Album', offset={'uri': 'spotify:track:Wanted'}, position_ms=0)
        self.assertIsNone(result)
        self.assertIn('Artist — Wanted', output.getvalue())
        self.assertIn('Other — Wrong', output.getvalue())
        self.assertEqual(sp.current_playback.call_count, 4)
        sp.start_playback.assert_called_once_with(device_id='pc', context_uri='spotify:album:Album',
                                                offset={'uri': 'spotify:track:Wanted'}, position_ms=0)

    def test_blocks_change_only_after_confirmed_starts(self):
        p, d = pool(), director.PersonalDirector()
        p.tracks = [track(str(i), 'A') for i in range(4)]
        p.sources = {'short_term': {identity(t) for t in p.tracks}}
        first = d.choose(p, lambda _: True)
        self.assertIsNone(d.active)  # Request/failure is not playback.
        self.assertIsNone(d.confirm(track('Wrong')))
        self.assertEqual(d.confirm(first)[0], 'Deine Favoriten')
        self.assertIsNone(d.confirm(first))  # No duplicate counting/announcement.
        self.assertEqual(d.active.played, 1)
        d.queued = director.MusicBlock('discovery', 'Neues', 'Entdecken wir Musik.', [track('New', 'C')])
        for _ in range(3):
            chosen = d.choose(p, lambda _: True)
            self.assertFalse(director.recording_keys(chosen) & d.active.heard)
            self.assertIsNone(d.confirm(chosen))
        chosen = d.choose(p, lambda _: True)
        self.assertEqual(chosen['name'], 'New')
        self.assertEqual(d.confirm(chosen)[0], 'Neues')
        self.assertIsNone(d.queued)

    def test_banned_queued_block_falls_back_and_reset_discards_proposal(self):
        p, d = pool(), director.PersonalDirector()
        d.queued = director.MusicBlock('discovery', 'X', 'X', [track('Blocked')])
        chosen = d.choose(p, lambda t: t['name'] != 'Blocked')
        self.assertNotEqual(chosen['name'], 'Blocked')
        self.assertIsNone(d.queued)
        d.reset()
        self.assertIsNone(d.confirm(chosen))

    def test_snapshot_is_bounded_text_with_source_membership(self):
        p = pool()
        p.tracks += [track(str(i), str(i)) for i in range(100)]
        p.sources['long_term'] = {identity(t) for t in p.tracks}
        evidence = director.history_snapshot(p)
        self.assertLessEqual(len(evidence), 48)
        self.assertEqual(set(evidence[0]), {'artist', 'title', 'sources'})
        self.assertIn('short_term', evidence[0]['sources'])
        self.assertNotIn('spotify:', json.dumps(evidence))

    def test_validation_rejects_wrong_source_invalid_anchor_and_known_discovery(self):
        evidence = director.history_snapshot(pool())
        cases = [response('favorites'), response('discovery'), response()]
        cases[2]['songs'][0]['based_on'] = 99
        for data in cases:
            with self.subTest(data=data), self.assertRaises(director.RequestValidationError):
                director.validate_block(data, evidence, ['favorites', 'recent', 'discovery'], [])
        with self.assertRaises(director.RequestValidationError):
            director.validate_block(response(), evidence, ['recent'], [identity(track('Recent', 'B'))])

    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'})
    def test_recent_block_uses_actual_history_without_spotify_search(self):
        connect = Mock()
        with patch.object(director, '_request_model', return_value=response()) as model:
            block = director.plan_personal_block(pool(), [], [], connect, lambda ts: ts, threading.Event())
        self.assertEqual([t['name'] for t in block.tracks], ['Recent'])
        connect.assert_not_called()
        payload = json.loads(model.call_args.args[0])
        self.assertIn('based_on', payload['system'])

    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'})
    def test_discoveries_require_spotify_resolution_and_exclude_history(self):
        with patch.object(director, '_request_model', return_value=response('discovery', 'C', 'New')), \
                patch.object(director, 'resolve_artists_to_ids', return_value=[('C', 'id')]), \
                patch.object(director, 'resolve_song_wishes', return_value=(
                    [('C', track('New', 'C')), ('A', track('Top', 'A'))], [])) as resolve:
            block = director.plan_personal_block(pool(), [], [], Mock(), lambda ts: ts, threading.Event())
        self.assertEqual([t['name'] for t in block.tracks], ['New'])
        resolve.assert_called_once()

    def test_late_worker_result_is_discarded_after_mode_reset(self):
        d = director.PersonalDirector()
        old = d.jobs.generation
        d.reset()
        d.jobs.results.put((old, director.MusicBlock('recent', 'Old', 'Old', []), None))
        self.assertIsNone(d.jobs.poll())

    def test_background_failure_keeps_active_pool_and_applies_backoff(self):
        d = director.PersonalDirector()
        d.active = director.MusicBlock('favorites', 'Keep', 'Keep', pool().tracks)
        d.jobs.attempts = 1
        d.jobs.results.put((d.jobs.generation, None, dj.RateLimitError(120)))
        with patch.object(dj, 'personal_director', d), patch.object(dj, 'auto_mode', 'global'), \
                patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}), contextlib.redirect_stdout(io.StringIO()) as output:
            dj.service_personal_director()
        self.assertIn('Rate-Limit', output.getvalue())
        self.assertEqual(d.active.title, 'Keep')
        self.assertFalse(d.jobs.can_start(0))

    def test_worker_is_nonblocking_and_bans_checked_when_result_arrives(self):
        d = director.PersonalDirector()
        entered, release = threading.Event(), threading.Event()
        def plan(*args):
            entered.set()
            release.wait(2)
            return director.MusicBlock('discovery', 'Ready', 'Ready', [track('New', 'C')])
        with patch.object(dj, 'personal_director', d), patch.object(dj, 'personal_mix', pool()), \
                patch.object(dj, 'auto_mode', 'global'), patch.object(dj, 'tracker', Mock(control=True)), \
                patch.object(dj, 'now_playing', dict(confirmed=True, playing=True)), \
                patch.object(dj, '_rate_limit_until', 0), patch.object(dj, 'plan_personal_block', side_effect=plan), \
                patch.object(dj, 'personal_track_allowed', return_value=True) as allowed, \
                patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-only'}), contextlib.redirect_stdout(io.StringIO()):
            try:
                dj.service_personal_director()
                self.assertTrue(entered.wait(1))
                self.assertIsNone(d.queued)
                release.set()
                d.jobs.thread.join(2)
                allowed.return_value = False
                dj.service_personal_director()
                self.assertIsNone(d.queued)
            finally:
                release.set()
                d.jobs.thread.join(2)
