import concurrent.futures
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import ai_costs
import ai_request


def response(**usage):
    return dict(model='claude-sonnet-4-6', usage=dict(input_tokens=1000, output_tokens=500, **usage),
                content=[dict(text='{}')])


class CostTests(unittest.TestCase):
    def test_standard_and_cache_costs_are_not_double_counted(self):
        self.assertEqual(ai_costs.estimate(response()), 10500000)
        self.assertEqual(ai_costs.estimate(response(cache_read_input_tokens=1000,
            cache_creation_input_tokens=2000, cache_creation=dict(ephemeral_5m_input_tokens=1000,
                                                                 ephemeral_1h_input_tokens=1000))), 20550000)

    def test_unknown_model_or_incomplete_usage_is_unknown_not_free(self):
        for data in ({}, dict(model='future', usage={}), dict(model='claude-sonnet-4-6', usage={}),
                     response(cache_creation_input_tokens=-1)):
            self.assertIsNone(ai_costs.estimate(data))

    def test_totals_survive_restart_session_resets_and_concurrent_writes_are_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'usage.json'
            ledger = ai_costs.CostLedger(path)
            with concurrent.futures.ThreadPoolExecutor(4) as executor:
                list(executor.map(lambda _: ledger.record(response()), range(20)))
            ledger.record()
            saved = ai_costs.read_state(path)
            self.assertEqual(saved['total'], 20 * 10500000)
            self.assertEqual(saved['session'], saved['total'])
            self.assertEqual(saved['unknown'], 1)
            restarted = ai_costs.CostLedger(path)
            self.assertEqual(restarted.data['session'], 0)
            self.assertEqual(restarted.data['total'], saved['total'])
            self.assertNotIn('content', path.read_text())

    def test_corrupt_existing_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'usage.json'
            path.write_text('broken')
            ledger = ai_costs.CostLedger(path)
            ledger.record(response())
            self.assertEqual(path.read_text(), 'broken')

    def test_usage_recorded_before_validation_failure_or_cancellation(self):
        for mode in ('invalid', 'truncated', 'cancelled'):
            data = response()
            event = threading.Event()
            if mode == 'invalid':
                data['content'][0]['text'] = 'not json'
            if mode == 'truncated':
                data['stop_reason'] = 'max_tokens'
            http = Mock()
            def read():
                if mode == 'cancelled':
                    event.set()
                return json.dumps(data).encode()
            http.__enter__ = Mock(return_value=http)
            http.__exit__ = Mock(return_value=False)
            http.read.side_effect = read
            with patch.object(ai_request.urllib.request, 'urlopen', return_value=http), \
                    patch.object(ai_costs, 'record') as record, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(Exception):
                    ai_request._request_model(b'{}', 'test', cancel_event=event)
                record.assert_called_once_with(data)

    def test_timeout_is_recorded_as_unknown(self):
        with patch.object(ai_request.urllib.request, 'urlopen', side_effect=TimeoutError), \
                patch.object(ai_costs, 'record') as record, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ai_request.ModelTimeoutError):
                ai_request._request_model(b'{}', 'test')
            record.assert_called_once_with()
