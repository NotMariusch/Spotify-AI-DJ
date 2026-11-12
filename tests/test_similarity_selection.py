import copy
import json
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_music_request import request
from music_request import RequestValidationError
from similarity_selection import validate_similarity_selection, select_similar_songs


def reference_plan():
    return request(artists=['Anchor Artist'], selection='hits', artist_scope='suggested',
                   songs=[dict(artist='Anchor Artist', title='Anchor')],
                   reference_songs=[dict(artist='Anchor Artist', title='Anchor')],
                   similarity_traits=['Mitsing-Refrain'])


def comparison():
    return dict(profiles=[dict(reference=0, known=True, chorus='skandierter Hook', rhythm='druckvolle Percussion',
                              energy='hoch', delivery='Rap und gerufener Refrain', production='harte Drums')],
                criteria=[dict(label='lauter Mitsing-Refrain', origin='user', dimension='chorus', quote='laut mitsingen'),
                          dict(label='skandierter Hook', origin='reference', dimension='chorus', quote=''),
                          dict(label='druckvolle Percussion', origin='reference', dimension='rhythm', quote='')],
                candidates=[dict(artist='Candidate', title='Close Song', reference=0, matches=[
                    dict(rating='strong', reason='Wiederholter Ruf-Refrain statt einer sanften Pop-Melodie'),
                    dict(rating='strong', reason='Kurze gerufene Hook mit Call-and-response wie die Referenz'),
                    dict(rating='strong', reason='Ähnlich harte synkopierte Drums und rhythmische Betonung')])])


class SimilaritySelectionTests(unittest.TestCase):
    def validate(self, data, plan=None, text='Songs zum laut mitsingen wie Anchor'):
        return validate_similarity_selection(data, plan or reference_plan(), text)

    def test_close_song_keeps_reference_repeats_and_temporary_preferences(self):
        plan = replace(reference_plan(), repeat_policy='session', avoid_session_repeats=True, less_artists=('Other',))
        result = self.validate(comparison(), plan)
        self.assertEqual([s.title for s in result.songs], ['Close Song'])
        self.assertEqual(result.reference_songs, plan.reference_songs)
        self.assertEqual(result.repeat_policy, 'session')
        self.assertEqual(result.less_artists, ('Other',))
        self.assertEqual(result.artists, ('Anchor Artist', 'Candidate'))

    def test_catchy_but_not_rhythmically_close_is_rejected(self):
        data = comparison()
        weak = copy.deepcopy(data['candidates'][0])
        weak['title'] = 'Generic Pop'
        weak['matches'][2]['rating'] = 'weak'
        data['candidates'].append(weak)
        self.assertEqual([s.title for s in self.validate(data).songs], ['Close Song'])

    def test_compact_ratings_apply_the_same_quality_gate(self):
        data = comparison()
        original = data['candidates'][0]
        compact = {k: original[k] for k in ('artist', 'title', 'reference')}
        compact.update(ratings=['strong'] * 3, reason='Gerufene Hook und harte synkopierte Drums wie die Referenz')
        data['candidates'] = [compact]
        self.assertEqual(len(self.validate(data).songs), 1)
        compact['ratings'][2] = 'weak'
        with self.assertRaises(RequestValidationError):
            self.validate(data)

    def test_compact_malformed_or_oversized_output_is_rejected(self):
        for ratings in ([], ['strong', 'strong', 'unknown']):
            data = comparison()
            data['candidates'] = [dict(artist='Candidate', title='Song', reference=0, ratings=ratings, reason='Comparison')]
            with self.assertRaises(RequestValidationError):
                self.validate(data)
        data = comparison()
        data['candidates'] *= 13
        with self.assertRaises(RequestValidationError):
            self.validate(data)

    def test_user_criterion_cannot_be_outvoted_by_reference_matches(self):
        data = comparison()
        data['candidates'][0]['matches'][0]['rating'] = 'partial'
        with self.assertRaisesRegex(RequestValidationError, 'Keine ausreichend nahen'):
            self.validate(data)

    def test_no_inferred_female_voice_restriction(self):
        data = comparison()
        data['criteria'][1]['label'] = 'weibliche Lead-Stimme'
        with self.assertRaises(RequestValidationError):
            self.validate(data)

    def test_fake_user_evidence_and_missing_singalong_priority_rejected(self):
        for quote in ('female vocals', 'Songs'):
            data = comparison()
            data['criteria'][0]['quote'] = quote
            with self.assertRaises(RequestValidationError):
                self.validate(data)

    def test_unknown_reference_sound_requires_clarification(self):
        data = comparison()
        data['profiles'][0]['known'] = False
        with self.assertRaisesRegex(RequestValidationError, 'kennt den Klang'):
            self.validate(data)

    def test_unknown_reference_index_and_missing_comparisons_rejected(self):
        for updates in (dict(reference=4), dict(matches=[]), dict(uri='spotify:track:invented')):
            data = comparison()
            data['candidates'][0].update(updates)
            with self.assertRaises(RequestValidationError):
                self.validate(data)

    def test_fixed_artists_and_exclusions_are_enforced_without_fallback(self):
        for plan in (replace(reference_plan(), artist_scope='fixed'),
                     replace(reference_plan(), artist_scope='suggested', excluded_artists=('Candidate',))):
            with self.assertRaisesRegex(RequestValidationError, 'Keine ausreichend nahen'):
                self.validate(comparison(), plan)

    def test_empty_pool_never_reuses_old_recommendations(self):
        data = comparison()
        data['candidates'] = []
        with self.assertRaises(RequestValidationError):
            self.validate(data)

    def test_one_bounded_model_call_contains_confirmed_titles_and_no_spotify_ids(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}), \
                patch('similarity_selection._request_model', return_value=comparison()) as model:
            result = select_similar_songs(replace(reference_plan(), artist_scope='suggested'), 'laut mitsingen')
            model.assert_called_once()
            payload = json.loads(model.call_args.args[0])
            self.assertIn('Anchor', payload['messages'][0]['content'])
            self.assertNotIn('spotify:', payload['messages'][0]['content'])
            self.assertEqual(payload['max_tokens'], 3000)
            self.assertEqual(model.call_args.kwargs, dict(timeout_seconds=90, stage='Musikalischer Vergleich'))
            self.assertEqual(len(result.songs), 1)

    def test_timeout_does_not_fallback_or_retry_comparison(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}), \
                patch('similarity_selection._request_model', side_effect=TimeoutError) as model:
            with self.assertRaises(TimeoutError):
                select_similar_songs(reference_plan(), 'laut mitsingen')
            model.assert_called_once()

    def test_continuation_excludes_existing_songs_and_keeps_criteria(self):
        from music_request import SongWish
        plan = replace(reference_plan(), artists=('Anchor Artist', 'Candidate'),
                       similarity_traits=tuple(c['label'] for c in comparison()['criteria']))
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}), \
                patch('similarity_selection._request_model', return_value=comparison()):
            result = select_similar_songs(plan, 'laut mitsingen', continuation=True,
                                         excluded=(SongWish('Candidate', 'Close Song'),))
            self.assertEqual(result.songs, ())
            with self.assertRaisesRegex(RequestValidationError, 'Auswahlkriterien'):
                select_similar_songs(replace(plan, similarity_traits=('changed',)),
                                     'laut mitsingen', continuation=True)
