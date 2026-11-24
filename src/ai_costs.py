"""Local USD estimate from returned usage; never an account balance."""
import json
import threading
from pathlib import Path
from json_store import write_json_atomic

PATH = Path(__file__).resolve().parents[1] / 'data' / 'ai_costs.json'
PRICING_DATE = '2026-11-24'
# USD nanodollars per token, standard global Claude API pricing.
# https://platform.claude.com/docs/en/about-claude/pricing
RATES = {'claude-sonnet-4-6': (3000, 15000, 3750, 6000, 300)}


def estimate(response):
    rates = RATES.get(response.get('model'))
    usage = response.get('usage')
    if not rates or not isinstance(usage, dict):
        return None
    def count(key, default=None, source=usage):
        value = source.get(key, default)
        if type(value) is not int or value < 0:
            raise ValueError()
        return value
    try:
        base, output = count('input_tokens'), count('output_tokens')
        read, created = count('cache_read_input_tokens', 0), count('cache_creation_input_tokens', 0)
        creation = usage.get('cache_creation')
        short, long = created, 0
        if creation is not None:
            short = count('ephemeral_5m_input_tokens', 0, creation)
            long = count('ephemeral_1h_input_tokens', 0, creation)
            if short + long != created:
                return None
        # Unexpected paid tools or service tiers need explicit pricing support.
        if usage.get('server_tool_use') or usage.get('service_tier', 'standard') not in ('standard', None):
            return None
        return sum(n * r for n, r in zip((base, output, short, long, read), rates))
    except (ValueError, TypeError, AttributeError):
        return None


def read_state(path=PATH):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('version') != 1 or any(type(data.get(k)) is not int or data[k] < 0
                for k in ('total', 'session', 'requests', 'unknown')):
            raise ValueError()
        return data
    except FileNotFoundError:
        return dict(version=1, total=0, session=0, requests=0, unknown=0, active=False)
    except (ValueError, TypeError, AttributeError, OSError):
        return None


class CostLedger:
    def __init__(self, path=PATH):
        self.path = path
        self.lock = threading.Lock()
        self.data = read_state(path)
        if self.data is not None:
            self.data.update(session=0, active=True, pricing_date=PRICING_DATE)
            self._save()

    def _save(self):
        try:
            write_json_atomic(self.path, self.data)
        except OSError:
            self.data = None
            print('KI-Kosten: Speichern fehlgeschlagen; Kostenanzeige unvollständig.')

    def record(self, response=None):
        with self.lock:
            if self.data is None:
                return
            amount = estimate(response) if isinstance(response, dict) else None
            self.data['requests'] += 1
            if amount is None:
                self.data['unknown'] += 1
            else:
                self.data['total'] += amount
                self.data['session'] += amount
            self._save()


ledger = None


def configure():
    global ledger
    ledger = CostLedger()


def record(response=None):
    # Enabled explicitly by the DJ entry point, never by imports/test fixtures.
    if ledger is not None:
        ledger.record(response)


def dashboard_costs():
    data = read_state()
    if data is None:
        return dict(available=False)
    return dict(available=True, active=data.get('active', False),
                session_usd=data['session'] / 1_000_000_000,
                total_usd=data['total'] / 1_000_000_000,
                unknown=data['unknown'], requests=data['requests'], pricing_date=PRICING_DATE)
