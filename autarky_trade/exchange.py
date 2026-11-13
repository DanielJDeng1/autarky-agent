"""Kraken spot REST transport. No order placement, withdrawal, or cancellation API."""
from __future__ import annotations
from contextlib import closing
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


class ExchangeError(RuntimeError):
    """An explicit Kraken rejection, not a transport ambiguity."""


class ExchangeUnavailable(RuntimeError):
    """No trustworthy response was received; do not blindly retry a mutation."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def signature(path, body, nonce, secret):
    digest = hashlib.sha256(str(nonce).encode() + body).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret, validate=True),
                                    path.encode() + digest, hashlib.sha512).digest()).decode()


def decode_response(raw):
    try:
        data = json.loads(raw, parse_float=str)
        if not isinstance(data, dict) or not isinstance(data.get('error'), list):
            raise ValueError('invalid envelope')
        if data['error']:
            # Do not echo untrusted remote text, request headers, or credentials.
            raise ExchangeError('Kraken explicitly rejected the request')
        if not isinstance(data.get('result'), dict):
            raise ValueError('invalid result')
        return data['result']
    except (ValueError, TypeError, UnicodeError) as e:
        raise ExchangeUnavailable('Invalid exchange response') from e


class Kraken:
    HOST = 'https://api.kraken.com'
    READ = frozenset({'BalanceEx', 'OpenOrders', 'QueryOrders', 'QueryTrades'})
    PUBLIC = frozenset({'AssetPairs', 'Ticker', 'SystemStatus'})

    def __init__(self, key, secret, nonce_db, transport=None):
        if not key or not secret or not base64.b64decode(secret, validate=True):
            raise ValueError('Kraken API key and base64 secret required')
        self._key, self._secret = key, secret
        self._nonce_db = str(nonce_db)
        self._fingerprint = hashlib.sha256(key.encode()).hexdigest()
        self._transport = transport or self._http
        Path(self._nonce_db).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._nonce_db)) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS nonces(key TEXT PRIMARY KEY, value INTEGER NOT NULL)')
        # A separate SQLite lock serializes network calls from processes sharing this nonce store.
        with closing(sqlite3.connect(self._nonce_db + '.lock')) as c, c:
            c.execute('CREATE TABLE IF NOT EXISTS mutex(id INTEGER PRIMARY KEY)')

    @classmethod
    def from_env(cls, nonce_db):
        return cls(os.environ.get('AUTARKY_KRAKEN_KEY', ''),
                   os.environ.get('AUTARKY_KRAKEN_SECRET', ''), nonce_db)

    @staticmethod
    def _http(path, body, headers):
        try:
            # Do not follow redirects or inherit arbitrary proxy configuration.
            opener = build_opener(NoRedirect(), ProxyHandler({}))
            with opener.open(Request(Kraken.HOST + path, data=body, headers=headers), timeout=15) as r:
                data = r.read(2_000_001)
                if len(data) > 2_000_000:
                    raise ExchangeUnavailable('Exchange response exceeded limit')
                return data
        except (OSError, TimeoutError, ValueError) as e:
            raise ExchangeUnavailable('Exchange request failed') from e

    def _private(self, method, fields):
        if method not in self.READ and method != 'AddOrder':
            raise ValueError('Private endpoint forbidden')
        if method == 'AddOrder':
            allowed = {'pair', 'type', 'ordertype', 'volume', 'price', 'cl_ord_id', 'validate'}
            if set(fields) != allowed or fields['validate'] != 'true' or fields['ordertype'] != 'limit':
                raise ValueError('Only validation-only spot limit orders are permitted')
        if 'nonce' in fields:
            raise ValueError('Nonce is generated internally')
        path = '/0/private/' + method
        with closing(sqlite3.connect(self._nonce_db + '.lock', timeout=30)) as lock, lock:
            lock.execute('BEGIN IMMEDIATE')
            with closing(sqlite3.connect(self._nonce_db, timeout=30)) as c, c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT value FROM nonces WHERE key=?', (self._fingerprint,)).fetchone()
                nonce = max(time.time_ns() // 1_000_000, (row[0] + 1) if row else 1)
                c.execute('INSERT INTO nonces VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (self._fingerprint, nonce))
            # The nonce is committed before network I/O, including crashes/timeouts.
            body = urlencode({'nonce': nonce, **fields}).encode()
            headers = {'API-Key': self._key, 'API-Sign': signature(path, body, nonce, self._secret),
                       'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'Autarky/0.9'}
            return decode_response(self._transport(path, body, headers))

    def public(self, method, fields=None):
        if method not in self.PUBLIC:
            raise ValueError('Public endpoint forbidden')
        path = '/0/public/' + method
        if fields:
            path += '?' + urlencode(fields)
        return decode_response(self._transport(path, None, {'User-Agent': 'Autarky/0.9'}))

    def read(self, method, **fields):
        if method not in self.READ:
            raise ValueError('Only read endpoints are permitted')
        return self._private(method, fields)

    def validate_order(self, *, pair, side, quantity, price, client_id):
        if pair not in ('XBTUSD', 'ETHUSD') or side not in ('buy', 'sell'):
            raise ValueError('Unsupported spot order')
        return self._private('AddOrder', {'pair': pair, 'type': side, 'ordertype': 'limit',
            'volume': quantity, 'price': price, 'cl_ord_id': client_id, 'validate': 'true'})
