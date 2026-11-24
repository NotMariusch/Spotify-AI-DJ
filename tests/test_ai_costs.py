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
    def test_balance_resets_display_preserves_history_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            usage, balance = Path(folder) / 'usage.json', Path(folder) / 'balance.json'
            ledger = ai_costs.CostLedger(usage)
            ledger.record(response())
            ledger.record()
            ai_costs.set_balance('23,00', usage, balance)
            state = ai_costs.dashboard_costs(usage, balance)
            self.assertEqual((state['session_usd'], state['total_usd'], state['remaining_usd'], state['unknown']), (0, 0, 23, 0))
            self.assertEqual(ai_costs.read_state(usage)['total'], 10500000)
            ledger.record(response())
            state = ai_costs.dashboard_costs(usage, balance)
            self.assertEqual(state['remaining_usd'], 22.9895)
            self.assertEqual(state['total_usd'], .0105)
            restarted = ai_costs.CostLedger(usage)
            state = ai_costs.dashboard_costs(usage, balance)
            self.assertEqual(state['session_usd'], 0)
            self.assertEqual(state['remaining_usd'], 22.9895)
            restarted.record(response())
            self.assertEqual(ai_costs.dashboard_costs(usage, balance)['session_usd'], .0105)
            ai_costs.set_balance('0.00', usage, balance)
            restarted.record(response())
            self.assertEqual(ai_costs.dashboard_costs(usage, balance)['remaining_usd'], -.0105)

    def test_invalid_balance_and_corruption_do_not_erase_data(self):
        with tempfile.TemporaryDirectory() as folder:
            usage, balance = Path(folder) / 'usage.json', Path(folder) / 'balance.json'
            ledger = ai_costs.CostLedger(usage)
            ai_costs.set_balance('10.00', usage, balance)
            original = balance.read_bytes()
            for value in ('-1', 'NaN', '1.234', '1e3', '1000000', '', None, 23, True):
                with self.assertRaises(ValueError):
                    ai_costs.set_balance(value, usage, balance)
                self.assertEqual(balance.read_bytes(), original)
            balance.write_text('broken')
            state = ai_costs.dashboard_costs(usage, balance)
            self.assertTrue(state['balance_error'])
            self.assertIsNone(state['remaining_usd'])
            self.assertEqual(balance.read_text(), 'broken')

    def test_balance_endpoint_validation_and_disk_failure(self):
        import dj_server
        client = dj_server.app.test_client()
        with patch.object(ai_costs, 'set_balance') as save, patch.object(ai_costs, 'dashboard_costs', return_value={'available': True}):
            self.assertEqual(client.post('/ai_balance', json={'amount': '23,00'}, headers={'Origin': 'https://other.example'}).status_code, 403)
            self.assertEqual(client.post('/ai_balance', json=[]).status_code, 400)
            save.assert_not_called()
            self.assertEqual(client.post('/ai_balance', json={'amount': '23,00'}).status_code, 200)
            save.assert_called_once_with('23,00')
            save.side_effect = OSError()
            self.assertEqual(client.post('/ai_balance', json={'amount': '23'}).status_code, 503)

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
