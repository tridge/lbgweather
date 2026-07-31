#!/usr/bin/env python3
"""End-to-end tests for cyc_ingest. Run: python3 test_cyc_ingest.py

Starts a real server on loopback and drives it over HTTP, so the auth, replay
and rate-limit paths are exercised as deployed rather than mocked.
"""

import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cyc_ingest  # noqa: E402
from cyc_ingest import Server, Stats, sign, load_keys  # noqa: E402
from cyc_store import Store, parse_obs_ts, utc_key  # noqa: E402

FAILURES = []
KEY = secrets.token_hex(32)
OTHER_KEY = secrets.token_hex(32)
STATION = 'cyc1'


def check(cond, what):
    if cond:
        print('  ok   %s' % what)
    else:
        print('  FAIL %s' % what)
        FAILURES.append(what)


class Harness:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='cycingest-test-')
        self.logs = os.path.join(self.d, 'wind_logs')
        self.state = os.path.join(self.d, 'state')
        self.status = os.path.join(self.d, 'status.json')
        self.keyfile = os.path.join(self.d, 'keys')
        os.makedirs(self.logs)
        with open(self.keyfile, 'w') as f:
            f.write('%s\t%s\n' % (STATION, KEY))
        cfg = {'keys': load_keys(self.keyfile), 'status': self.status,
               'enable_echo': True, 'max_conns': 16, 'primary': STATION}
        stores = {STATION: Store(self.logs, self.state, station=STATION)}
        self.srv = Server(('127.0.0.1', 0), cfg, stores)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.d, ignore_errors=True)
        return False

    def url(self, path):
        return 'http://127.0.0.1:%d%s' % (self.port, path)

    def post(self, path, doc, key=KEY, station=STATION, ts=None, nonce=None,
             corrupt_sig=False, raw=None):
        body = raw if raw is not None else json.dumps(doc).encode('utf-8')
        ts = int(time.time()) if ts is None else ts
        nonce = nonce or secrets.token_hex(16)
        sig = sign(bytes.fromhex(key), 'POST', path, station, ts, nonce, body)
        if corrupt_sig:
            sig = sig[:-1] + ('0' if sig[-1] != '0' else '1')
        req = urllib.request.Request(self.url(path), data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('X-CYC-Station', station)
        req.add_header('X-CYC-Timestamp', str(ts))
        req.add_header('X-CYC-Nonce', nonce)
        req.add_header('X-CYC-Signature', sig)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}

    def get(self, path):
        try:
            with urllib.request.urlopen(self.url(path), timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, {}


def obs(ts, seq, wind=1.0):
    return {'obs_ts': ts, 'seq': seq, 'src': 'loop', 'wind_kt': wind,
            'wind_avg_kt': 0.9, 'wind_dir': 270, 'wind_gust_kt': 2.0,
            'temp_c': 13.0, 'humidity': 50}


def doc(obslist):
    return {'schema': 1, 'station': STATION, 'obs': obslist,
            'collector': {'version': '0.1', 'spool_backlog': 0}}


def test_happy_path():
    print('\n== valid POST ==')
    with Harness() as h:
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:00:03+10:00', 1),
                                 obs('2026-07-31T10:01:03+10:00', 2)]))
        check(code == 200, 'HTTP 200')
        check(body.get('accepted') == 2, 'accepted 2')
        check(body.get('ack_seq') == 2, 'ack_seq is highest durably written')
        check(os.path.exists(os.path.join(h.logs, '2026', '07', '31.csv')),
              'CSV written')
        check(os.path.exists(h.status), 'status.json written')


def test_replay_and_nonce():
    print('\n== replay defence ==')
    with Harness() as h:
        nonce = secrets.token_hex(16)
        ts = int(time.time())
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        c1, b1 = h.post('/ingest/obs', d, ts=ts, nonce=nonce)
        c2, b2 = h.post('/ingest/obs', d, ts=ts, nonce=nonce)
        check(c1 == 200 and b1['accepted'] == 1, 'first POST accepted')
        check(c2 == 401, 'identical nonce replayed -> 401')

        # A fresh nonce with the same payload is legitimate (a retry after a
        # lost ACK) and must be a harmless no-op, not an error.
        c3, b3 = h.post('/ingest/obs', d)
        check(c3 == 200 and b3['accepted'] == 0 and b3['duplicates'] == 1,
              'retry with fresh nonce -> 0 accepted, 1 duplicate')


def test_bad_signature_and_rate_limit():
    print('\n== bad signature, rate limiting ==')
    with Harness() as h:
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        code, _ = h.post('/ingest/obs', d, corrupt_sig=True)
        check(code == 401, 'one-byte signature corruption -> 401')

        code, _ = h.post('/ingest/obs', d, key=secrets.token_hex(32))
        check(code == 401, 'wrong key -> 401')

        for _ in range(12):
            h.post('/ingest/obs', d, corrupt_sig=True)
        code, body = h.post('/ingest/obs', d, corrupt_sig=True)
        check(code == 429, 'repeated failures -> 429 rate limited')

        # A valid request from a rate-limited IP is also refused; that is the
        # intended behaviour, and it recovers once the window passes.
        code, _ = h.post('/ingest/obs', d)
        check(code == 429, 'rate limit applies to valid requests too')


def test_clock_skew():
    print('\n== clock skew is reported only to an authenticated caller ==')
    with Harness() as h:
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])

        # A bad signature with a skewed clock must look like plain auth failure,
        # not leak that the station exists via a distinguishable response.
        code, body = h.post('/ingest/obs', d, ts=int(time.time()) - 600,
                            corrupt_sig=True)
        check(code == 401 and body.get('error') == 'auth_failed',
              'unauthenticated skew is indistinguishable from auth failure')
        code, body = h.post('/ingest/obs', d, ts=int(time.time()) - 600,
                            station='no-such-station')
        check(code == 401, 'unknown station with skew -> 401, not 400')

        code, body = h.post('/ingest/obs', d, ts=int(time.time()) - 600)
        check(code == 400, 'ten minutes of skew, valid signature -> 400')
        check(body.get('error') == 'clock_skew', 'error is clock_skew, not auth_failed')
        check('server_time_epoch' in body, 'server time returned so collector can diagnose')

        # Crucially it must NOT count as an auth failure, or a drifting clock
        # would rate-limit the collector out entirely.
        code, _ = h.post('/ingest/obs', d)
        check(code == 200, 'skew rejection did not trip the rate limiter')


def test_schema_and_size():
    print('\n== malformed input ==')
    with Harness() as h:
        code, _ = h.post('/ingest/obs', None, raw=b'not json')
        check(code == 400, 'non-JSON body -> 400')
        code, body = h.post('/ingest/obs', {'schema': 1})
        check(code == 400 and body.get('error') == 'bad_schema', 'missing obs -> bad_schema')
        code, _ = h.post('/ingest/obs', {'schema': 1, 'obs': 'nope'})
        check(code == 400, 'obs not a list -> 400')

        big = doc([obs('2026-07-31T10:%02d:03+10:00' % (i % 60), i)
                   for i in range(600)])
        code, _ = h.post('/ingest/obs', big)
        check(code == 413, 'over 500 observations -> 413')

        # A bad timestamp inside an otherwise valid batch is rejected per-record
        # and quarantined, not allowed to block the whole spool.
        mixed = doc([obs('2026-07-31T10:00:03+10:00', 1),
                     {'obs_ts': '2026-07-31 10:01:03', 'seq': 2}])
        code, body = h.post('/ingest/obs', mixed)
        check(code == 200 and body['accepted'] == 1 and body['rejected'] == 1,
              'naive timestamp rejected per-record, good record still stored')


def test_health_and_echo():
    print('\n== health and echo ==')
    with Harness() as h:
        code, body = h.get('/ingest/health')
        check(code == 200 and body['ok'] is True, 'health responds unauthenticated')
        check(body['age_s'] is None, 'age is null before any observation')

        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        code, body = h.get('/ingest/health')
        check(body['age_s'] is not None, 'age computed live after ingest')

        code, body = h.post('/ingest/echo', doc([]))
        check(code == 200 and body['signing_string'].startswith('v1\nPOST\n/ingest/echo\n'),
              'echo returns the canonical signing string')

        code, _ = h.get('/ingest/nope')
        check(code == 404, 'unknown path -> 404')


def test_status_document():
    print('\n== status.json content ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 5)]))
        with open(h.status) as f:
            st = json.load(f)
        for k in ('last_obs_epoch', 'written_at_epoch', 'obs_age_s',
                  'recv_lag_s', 'seq', 'auth_failures_recent'):
            check(k in st, 'status has %s' % k)
        check(st['seq'] == 5, 'seq recorded')
        check(isinstance(st['last_obs_epoch'], int),
              'absolute epoch stored, not just a relative age')


def test_restart_seeding():
    print('\n== stats survive a restart ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 7)]))
        # Restart the service against the same storage, as a deploy does.
        h.srv.shutdown(); h.srv.server_close()
        cfg = {'keys': load_keys(h.keyfile), 'status': h.status,
               'enable_echo': True, 'max_conns': 16, 'primary': STATION}
        store = Store(h.logs, h.state, station=STATION)
        h.srv = Server(('127.0.0.1', 0), cfg, {STATION: store})
        h.port = h.srv.server_address[1]
        last = store.last_observation()
        if last:
            h.srv.stats.last_obs_utc = int(last['utc'])
            if isinstance(last.get('seq'), int):
                h.srv.stats.last_seq = last['seq']
        h.srv.write_status()
        h.t = threading.Thread(target=h.srv.serve_forever, daemon=True)
        h.t.start()

        code, body = h.get('/ingest/health')
        check(body.get('age_s') is not None,
              'health reports an age after restart, not null')
        with open(h.status) as f:
            st = json.load(f)
        check(st.get('last_obs_epoch') is not None,
              'status.json still knows the last observation after restart')
        check(st.get('seq') == 7, 'sequence recovered from storage')



def test_station_isolation():
    print('\n== a station cannot write as another ==')
    with Harness() as h:
        # Body declares a different station than the signing key.
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        d['station'] = 'someone-else'
        code, body = h.post('/ingest/obs', d)
        check(code == 400 and 'station' in (body.get('detail') or ''),
              'declared station must match the signing key')

        # A key with no configured store is authenticated but cannot write.
        with open(h.keyfile, 'a') as f:
            f.write('%s\t%s\n' % ('cyc1-test', OTHER_KEY))
        h.srv.cfg['keys'] = load_keys(h.keyfile)
        d2 = doc([obs('2026-07-31T10:05:00+10:00', 2)])
        d2['station'] = 'cyc1-test'
        code, body = h.post('/ingest/obs', d2, key=OTHER_KEY,
                            station='cyc1-test')
        check(code == 403 and body.get('error') == 'station_not_configured',
              'authenticated but unconfigured station refused, not fallen back')


def test_schema_version():
    print('\n== schema version is enforced ==')
    with Harness() as h:
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        d['schema'] = 99
        code, body = h.post('/ingest/obs', d)
        check(code == 400 and body.get('error') == 'bad_schema',
              'unknown schema version rejected')


def test_request_framing():
    print('\n== request framing ==')
    import http.client
    with Harness() as h:
        def raw(headers, body=b'{}'):
            c = http.client.HTTPConnection('127.0.0.1', h.port, timeout=10)
            c.putrequest('POST', '/ingest/obs', skip_host=True,
                         skip_accept_encoding=True)
            for k, v in headers:
                c.putheader(k, v)
            c.endheaders()
            try:
                c.send(body)
            except Exception:
                pass
            try:
                r = c.getresponse()
                return r.status
            except Exception:
                return None
            finally:
                c.close()

        code = raw([('Host', 'x'), ('Content-Length', '-1')])
        check(code == 400, 'negative Content-Length rejected (got %s)' % code)

        code = raw([('Host', 'x'), ('Transfer-Encoding', 'chunked')])
        check(code == 400, 'chunked transfer rejected (got %s)' % code)

        code = raw([('Host', 'x'), ('Content-Length', '2'),
                    ('Content-Length', '3')])
        check(code == 400, 'duplicate Content-Length rejected (got %s)' % code)


def test_ack_seq_only_durable():
    print('\n== ack_seq never covers records that were not stored ==')
    with Harness() as h:
        # Rebuild the store with a cutover instant partway through the batch.
        cut = utc_key(parse_obs_ts('2026-07-31T10:00:30+10:00'))
        h.srv.stores[STATION] = Store(h.logs, h.state, station=STATION,
                                      min_obs_ts=cut)
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:00:00+10:00', 5),
                                 obs('2026-07-31T10:01:00+10:00', 6)]))
        check(body['accepted'] == 1 and body['rejected'] == 1,
              'one accepted, one refused as pre-cutover')
        check(body['ack_seq'] == 6,
              'ack_seq is the stored record (6), not the refused one (got %s)'
              % body['ack_seq'])


def test_key_file_validation():
    print('\n== key file format is enforced ==')
    import tempfile as _tf
    for content, label in [('cyc1\tnothex!!\n', 'non-hex key'),
                           ('cyc1\tdeadbeef\n', 'short key'),
                           ('cyc1\n', 'missing key')]:
        d = _tf.mkdtemp()
        path = os.path.join(d, 'k')
        with open(path, 'w') as f:
            f.write(content)
        raised = False
        try:
            load_keys(path)
        except SystemExit:
            raised = True
        check(raised, '%s rejected at load' % label)
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_happy_path()
    test_replay_and_nonce()
    test_bad_signature_and_rate_limit()
    test_clock_skew()
    test_schema_and_size()
    test_health_and_echo()
    test_status_document()
    test_restart_seeding()
    test_station_isolation()
    test_schema_version()
    test_request_framing()
    test_ack_seq_only_durable()
    test_key_file_validation()

    print()
    if FAILURES:
        print('%d FAILURE(S):' % len(FAILURES))
        for f in FAILURES:
            print('  - %s' % f)
        return 1
    print('all tests passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
