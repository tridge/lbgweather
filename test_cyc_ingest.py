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
from cyc_store import Store, parse_obs_ts, utc_key, TZ  # noqa: E402
from datetime import datetime  # noqa: E402

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


KEY2 = 'bb' * 32


class Harness:
    def __init__(self, second_station=None, shed_queue=None):
        self.shed_queue = shed_queue
        # A second, non-primary station: its data must not touch the primary's
        # storage, freshness or status document.
        self.second = second_station

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='cycingest-test-')
        self.logs = os.path.join(self.d, 'wind_logs')
        self.state = os.path.join(self.d, 'state')
        self.status = os.path.join(self.d, 'status.json')
        self.keyfile = os.path.join(self.d, 'keys')
        os.makedirs(self.logs)
        with open(self.keyfile, 'w') as f:
            f.write('%s\t%s\n' % (STATION, KEY))
            if self.second:
                f.write('%s\t%s\n' % (self.second, KEY2))
        cfg = {'keys': load_keys(self.keyfile), 'status': self.status,
               'enable_echo': True, 'max_conns': 16, 'primary': STATION,
               'degraded': {}, 'statedir': self.state}
        stores = {STATION: Store(self.logs, self.state, station=STATION)}
        if self.second:
            self.logs2 = os.path.join(self.d, 'wind_logs2')
            os.makedirs(self.logs2)
            stores[self.second] = Store(
                self.logs2, os.path.join(self.d, 'state2'),
                station=self.second)
        if self.shed_queue:
            cfg['shed_queue'] = self.shed_queue
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

    def signed_request(self, path, doc, key=KEY, station=STATION):
        body = json.dumps(doc).encode('utf-8')
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        req = urllib.request.Request(self.url(path), data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('X-CYC-Station', station)
        req.add_header('X-CYC-Timestamp', str(ts))
        req.add_header('X-CYC-Nonce', nonce)
        req.add_header('X-CYC-Signature',
                       sign(bytes.fromhex(key), 'POST', path, station, ts,
                            nonce, body))
        return req

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
            # Parse the error body too: discarding it made a correct 503 look
            # like an empty response and hid what health was actually saying.
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {}


def obs(ts, seq, wind=1.0):
    return {'obs_ts': ts, 'seq': seq, 'src': 'loop', 'wind_kt': wind,
            'wind_avg_kt': 0.9, 'wind_dir': 270, 'wind_gust_kt': 2.0,
            'temp_c': 13.0, 'humidity': 50}


def doc(obslist, station=STATION):
    return {'schema': 1, 'station': station, 'obs': obslist,
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
               'enable_echo': True, 'max_conns': 16, 'primary': STATION,
               'degraded': {}, 'statedir': h.state}
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
        # seq 5 was refused. Acknowledging 6 -- the highest DURABLE seq --
        # would make a cursor-based collector discard seq 5 for ever. The ACK
        # must stop at the first gap.
        check(body['ack_seq'] is None,
              'ack_seq stops at the first non-durable record (got %s)'
              % body['ack_seq'])

        # With the refused record last, the durable prefix is acknowledged.
        h.srv.stores[STATION] = Store(h.logs, h.state, station=STATION,
                                      min_obs_ts=cut)
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:02:00+10:00', 7),
                                 obs('2026-07-31T09:00:00+10:00', 8)]))
        check(body['ack_seq'] == 7,
              'contiguous durable prefix acknowledged (got %s)'
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



def test_station_required():
    print('\n== the body must declare its station ==')
    with Harness() as h:
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        del d['station']
        code, body = h.post('/ingest/obs', d)
        check(code == 400, 'a missing station is rejected, not accepted')


def test_collector_must_be_object():
    print('\n== malformed collector metadata is refused before storage ==')
    with Harness() as h:
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        d['collector'] = 'not an object'
        code, body = h.post('/ingest/obs', d)
        check(code == 400, 'truthy non-dict collector rejected')
        # And the record must NOT have been stored on the way to failing.
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        check(body['accepted'] == 1,
              'the observation was not committed by the rejected request')


def test_capacity_is_enforced_before_threads():
    print('\n== over-capacity connections are shed with a real 503 ==')
    import http.client
    with Harness() as h:
        h.srv.slots = threading.BoundedSemaphore(1)
        held = []
        # Occupy the single slot with a connection that has sent no request.
        c0 = http.client.HTTPConnection('127.0.0.1', h.port, timeout=10)
        c0.connect()
        held.append(c0)
        time.sleep(0.4)

        c = http.client.HTTPConnection('127.0.0.1', h.port, timeout=10)
        status = None
        try:
            c.request('GET', '/ingest/health')
            status = c.getresponse().status
        except Exception as e:
            status = 'error: %r' % e
        finally:
            c.close()
            for x in held:
                x.close()
        check(status == 503,
              'second connection gets a genuine 503, not a dropped socket '
              '(got %s)' % status)


def test_health_reports_degraded_storage():
    print('\n== unreadable storage is not reported as healthy ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        code, body = h.get('/ingest/health')
        check(code == 200 and body['ok'] is True, 'healthy while readable')

        jsonl = os.path.join(h.logs, '2026', '07', '31.jsonl')
        with open(jsonl, 'w') as f:
            f.write('{"garbage": true}\n')
        h.post('/ingest/obs', doc([obs('2026-07-31T10:05:00+10:00', 2)]))
        code, body = h.get('/ingest/health')
        check(code == 503 and body.get('ok') is False,
              'health turns unhealthy when a day cannot be read (got %s %s)'
              % (code, body))
        check(body.get('degraded'), 'the affected day is named')


def test_hmac_against_a_fixed_vector():
    print('\n== HMAC matches an independent fixed vector ==')
    import hashlib as _h, hmac as _m
    key = bytes.fromhex('00' * 32)
    body = b'{"x":1}'
    signing = '\n'.join(['v1', 'POST', '/ingest/obs', 'cyc1', '1700000000',
                          'abcdef', _h.sha256(body).hexdigest()])
    expect = 'v1=' + _m.new(key, signing.encode(), _h.sha256).hexdigest()
    got = sign(key, 'POST', '/ingest/obs', 'cyc1', 1700000000, 'abcdef', body)
    check(got == expect,
          'signature matches a vector computed without the production helper')
    check(expect.startswith('v1=') and len(expect) == 67, 'shape is v1=<64 hex>')



def test_shadow_only_advances_on_a_real_ack():
    print('\n== the shadow poster advances only on a real acknowledgement ==')
    from cyc_shadow_post import acknowledged
    check(acknowledged(200, {'ok': True, 'accepted': 1, 'rejected': 0,
                             'ack_seq': 7}, 7), 'accepted and acknowledged')
    check(acknowledged(200, {'ok': True, 'accepted': 0, 'duplicates': 1,
                             'rejected': 0, 'ack_seq': 7}, 7),
          'a duplicate is still durable')
    check(not acknowledged(200, {'ok': True, 'accepted': 0, 'rejected': 1,
                                 'ack_seq': None}, 7),
          'a refused record is NOT acknowledged despite the 200')
    check(not acknowledged(200, {'ok': True, 'rejected': 0, 'ack_seq': 6}, 7),
          'an ack for a different seq does not count')
    check(not acknowledged(200, {'ok': False, 'rejected': 0, 'ack_seq': 7}, 7),
          'ok:false is not an acknowledgement')
    check(not acknowledged(503, {}, 7), 'a 503 is not an acknowledgement')



def test_a_second_station_cannot_speak_for_the_primary():
    print('\n== one station must not affect another ==')
    with Harness(second_station='cyc2') as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        st1 = json.load(open(h.status))
        check(st1['station'] == STATION, 'status belongs to the primary')

        # cyc2 posts something much fresher, under its own valid key.
        fresh = datetime.now(TZ).replace(microsecond=0).isoformat(
            timespec='seconds')
        code, body = h.post('/ingest/obs',
                            doc([obs(fresh, 500)], station='cyc2'),
                            key=KEY2, station='cyc2')
        check(code == 200, 'cyc2 is accepted on its own key (got %s)' % code)

        st2 = json.load(open(h.status))
        check(st2['last_obs_epoch'] == st1['last_obs_epoch'],
              "cyc2's fresher observation did NOT advance the primary's "
              'freshness - a test station cannot make a dead primary look alive')
        check(st2['seq'] == st1['seq'], "and did not advance the primary's seq")
        check(st2['accepted'] == st1['accepted'],
              "and was not counted in the primary's totals")

        # Storage is separate too.
        p1 = os.path.join(h.logs, '2026', '07', '31.jsonl')
        check(len(open(p1).read().strip().splitlines()) == 1,
              "the primary's day file holds only its own record")
        check(os.path.exists(h.logs2), 'cyc2 wrote into its own tree')

        # And cyc2's key cannot be used to write as cyc1.
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T11:00:03+10:00', 9)]),
                            key=KEY2, station=STATION)
        check(code == 401,
              'cyc2 signing as cyc1 is rejected (got %s)' % code)



def test_concurrent_posts_do_not_deadlock_or_lose_records():
    print('\n== concurrent writers: no deadlock, no lost records ==')
    with Harness() as h:
        n = 24                    # deliberately above max_conns (16)
        results = [None] * n

        def stamp(i):
            return '2026-07-31T%02d:%02d:03+10:00' % (10 + i // 60, i % 60)

        def fire(i):
            try:
                results[i] = h.post('/ingest/obs',
                                    doc([obs(stamp(i), i + 1)]))
            except Exception as e:
                results[i] = ('exc', repr(e))
                print('       request %d raised: %r' % (i, e))

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(n)]
        t0 = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        elapsed = time.time() - t0

        check(not any(t.is_alive() for t in threads),
              'every request finished - the day lock plus the connection '
              'semaphore did not deadlock (%.1fs)' % elapsed)

        codes = [r[0] if r else None for r in results]
        shed = [i for i, c in enumerate(codes) if c == 503]
        # 503 is CORRECT here: 24 concurrent against a cap of 16. What matters
        # is that nothing else happened and that a shed request stored nothing
        # -- a partial write under load would be far worse than a refusal.
        check(all(c in (200, 503) for c in codes),
              'every response was either accepted or cleanly shed (codes %s)'
              % sorted(set(codes), key=repr))
        print('       (%d of %d shed by the connection cap)' % (len(shed), n))

        import cyc_store
        f = cyc_store.DayFiles(h.logs, 2026, 7, 31)
        stored = h.srv.stores[STATION]._read_jsonl(f)
        check(len(stored) == n - len(shed),
              'exactly the accepted requests are durable - a shed request '
              'wrote nothing (%d stored, %d accepted)'
              % (len(stored), n - len(shed)))
        check(len({r['utc'] for r in stored}) == len(stored),
              'and none duplicated')

        # Retrying the shed ones must succeed and complete the set.
        for i in shed:
            code, _ = h.post('/ingest/obs', doc([obs(stamp(i), i + 1)]))
            check(code == 200, 'shed request %d succeeds on retry' % i)
        stored = h.srv.stores[STATION]._read_jsonl(f)
        check(len(stored) == n,
              'all %d observations durable after retry (got %d)'
              % (n, len(stored)))


def test_shedding_stores_nothing_and_retries_cleanly():
    print('\n== a shed request must store nothing, provably ==')
    with Harness() as h:
        # Cap of 1, and one slot held open by a connection that never sends a
        # request. Every other request is now GUARANTEED to be shed, so the
        # assertions below cannot pass by luck the way the 24-way test can.
        import http.client
        h.srv.slots = threading.BoundedSemaphore(1)
        hog = http.client.HTTPConnection('127.0.0.1', h.port, timeout=10)
        hog.connect()
        time.sleep(0.4)
        try:
            code, body = h.post('/ingest/obs',
                                doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        finally:
            hog.close()
        check(code == 503, 'the post was shed (got %s)' % code)

        import cyc_store
        f = cyc_store.DayFiles(h.logs, 2026, 7, 31)
        check(not os.path.exists(f.jsonl),
              'nothing at all was written - the shed happened before any '
              'storage work')

        time.sleep(0.3)
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        check(code == 200 and body['accepted'] == 1,
              'and the slot was released, so the retry stores it (got %s)'
              % code)


def test_status_never_regresses_under_concurrent_writers():
    print('\n== the status document must not go backwards ==')
    with Harness() as h:
        seen = []
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                try:
                    with open(h.status) as f:
                        seen.append(json.load(f).get('last_obs_epoch'))
                except (OSError, ValueError):
                    pass
                time.sleep(0.002)
        w = threading.Thread(target=watch, daemon=True)
        w.start()

        # Many writers racing, plus the heartbeat, all calling write_status.
        results = [None] * 20

        def fire(i):
            results[i] = h.post(
                '/ingest/obs',
                doc([obs('2026-07-31T%02d:%02d:03+10:00'
                         % (10 + i // 60, i % 60), i + 1)]))
        ts = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        for _ in range(20):
            h.srv.write_status()
        stop.set()
        w.join(timeout=5)

        vals = [v for v in seen if v is not None]
        # Without this the test could pass having observed nothing at all.
        check(len(vals) > 20 and len(set(vals)) > 1,
              'the watcher actually saw the document change (%d reads, %d '
              'distinct values)' % (len(vals), len(set(vals))))
        codes = [r[0] if r else None for r in results]
        # 20 concurrent against a cap of 16 means some are legitimately shed.
        served = codes.count(200)
        check(served >= 10 and all(c in (200, 503) for c in codes),
              'enough posts got through to make the race real (%d served, '
              'codes %s)' % (served, sorted(set(codes), key=repr)))
        check(json.load(open(h.status))['accepted'] == served,
              'and every served post is in the published status (%s vs %s)'
              % (json.load(open(h.status))['accepted'], served))
        regressions = [(a, b) for a, b in zip(vals, vals[1:]) if b < a]
        check(not regressions,
              'last_obs_epoch never went backwards across %d reads (%s)'
              % (len(vals), regressions[:3]))


def test_ack_sequence_contract():
    print('\n== the ACK sequence contract, per input ==')
    from cyc_ingest import ack_from_dispositions as A
    from cyc_store import STORED, DUPLICATE, REJECTED

    def o(ts, seq=None):
        d = {'obs_ts': ts}
        if seq is not None:
            d['seq'] = seq
        return d

    t1, t2, t3 = ('2026-07-31T10:00:03+10:00', '2026-07-31T10:01:03+10:00',
                  '2026-07-31T10:02:03+10:00')

    ack, newest, gaps, _ = A([o(t1, 5), o(t2, 6)], [STORED, STORED])
    check(ack == 6 and gaps == 0, 'a clean +1 run is fully acknowledged')

    ack, _, _, _ = A([o(t1, 5), o(t2, 6)], [REJECTED, STORED])
    check(ack is None, 'a leading rejection stops the acknowledgement')

    # THE CASE THAT WAS WRONG. Acknowledging 7 when 6 was never seen tells a
    # cursor-based collector to discard 6 for ever.
    ack, _, gaps, why = A([o(t1, 5), o(t2, 7)], [STORED, STORED])
    check(ack == 5 and why,
          'an UNDECLARED seq gap stops the acknowledgement (got %s, %r)'
          % (ack, why))
    check(gaps == 0, 'and nothing is counted as lost - it may still arrive')

    # ...unless the collector declares exactly what it dropped.
    ack, _, gaps, why = A([o(t1, 5), o(t2, 7)], [STORED, STORED],
                          loss_from=6, loss_to=6)
    check(ack == 7 and gaps == 1 and not why,
          'a DECLARED loss range authorises the jump and is counted '
          '(ack=%s gaps=%s)' % (ack, gaps))

    ack, _, _, _ = A([o(t1, 5), o(t2, 7)], [STORED, STORED],
                     loss_from=6, loss_to=9)
    check(ack == 5, 'a loss range that does not match the gap is refused')

    # Continuity holds ACROSS requests, not only within one.
    ack, _, _, why = A([o(t2, 7)], [STORED], last_ack=5)
    check(ack is None and why,
          'a gap against the PREVIOUS request is caught too (got %s)' % ack)
    ack, _, _, _ = A([o(t2, 6)], [STORED], last_ack=5)
    check(ack == 6, 'and the contiguous continuation is accepted')
    ack, _, gaps, _ = A([o(t2, 7)], [STORED], last_ack=5,
                        loss_from=6, loss_to=6)
    check(ack == 7 and gaps == 1, 'a declared cross-request loss is allowed')

    ack, _, _, why = A([o(t1, 10), o(t2, 9)], [STORED, STORED])
    check(ack == 10 and why, 'a DECREASING seq stops at the violation')

    ack, _, _, why = A([o(t1, 5), o(t2), o(t3, 7)], [STORED, STORED, STORED])
    check(ack == 5 and why,
          'a durable record with no seq stops the run rather than being '
          'stepped over (got %s)' % ack)

    ack, _, _, _ = A([o(t1, 5), o(t2, True)], [STORED, STORED])
    check(ack == 5, 'a bool is not an acceptable seq')

    # The UTC-laundering case: two inputs on ONE instant, the first stored and
    # the second rejected. A UTC key set cannot tell them apart.
    ack, _, _, _ = A([o(t1, 5), o(t1, 6)], [STORED, REJECTED])
    check(ack == 5,
          'a record rejected on an instant another record occupies is NOT '
          'acknowledged (got %s)' % ack)

    ack, _, _, _ = A([o(t1, 5), o(t1, 6)], [STORED, DUPLICATE])
    check(ack == 6, 'but a genuine duplicate IS durable and acknowledged')

    # Freshness must cover everything durable, not just the acked prefix.
    _, newest, _, _ = A([o(t1), o(t3, 9)], [STORED, STORED])
    check(newest == utc_key(parse_obs_ts(t3)),
          'newest instant reflects the later durable record even though the '
          'ACK stopped early')


def test_seq_gaps_are_counted_from_the_batch():
    print('\n== a clean batch must not report phantom losses ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 4)]))
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:01:03+10:00', 5),
                                 obs('2026-07-31T10:02:03+10:00', 6)]))
        check(body['ack_seq'] == 6, 'the batch is acknowledged to 6')
        st = json.load(open(h.status))
        # Deriving gaps from last_seq and the final ack counted 6 - 4 - 1 = 1
        # lost observation even though the batch delivered 5 AND 6.
        check(st['seq_gaps'] == 0,
              'a contiguous batch reports no lost observations (got %s)'
              % st['seq_gaps'])

        # A real, declared loss IS counted.
        d = doc([obs('2026-07-31T10:05:03+10:00', 10)])
        d['data_loss'] = {'from': 7, 'to': 9}
        code, body = h.post('/ingest/obs', d)
        check(body['ack_seq'] == 10, 'the declared jump is acknowledged')
        st = json.load(open(h.status))
        check(st['seq_gaps'] == 3,
              'and the three dropped observations are recorded (got %s)'
              % st['seq_gaps'])


def test_duplicate_instant_in_one_batch():
    print('\n== two observations sharing an instant in one request ==')
    with Harness() as h:
        ts = '2026-07-31T10:00:03+10:00'
        code, body = h.post('/ingest/obs', doc([obs(ts, 1, wind=1.0),
                                                obs(ts, 2, wind=9.9)]))
        check(body['accepted'] == 1 and body['duplicates'] == 1,
              'one stored, one recognised as the same instant')
        check(body['ack_seq'] == 2,
              'both acknowledged - the second is durable in the sense that '
              'the instant is on disk (got %s)' % body['ack_seq'])
        import cyc_store
        recs = h.srv.stores[STATION]._read_jsonl(
            cyc_store.DayFiles(h.logs, 2026, 7, 31))
        check(len(recs) == 1, 'only one record on disk')
        check(recs[0]['wind_kt'] == 1.0, 'first write wins')


def test_busy_carries_retry_after():
    print('\n== a transient refusal says when to come back ==')
    with Harness() as h:
        import cyc_store
        orig = cyc_store.Store.put

        def busy(self, observations):
            raise cyc_store.LockTimeout('busy for the test')
        cyc_store.Store.put = busy
        try:
            req = h.signed_request('/ingest/obs',
                                   doc([obs('2026-07-31T10:00:03+10:00', 1)]))
            try:
                urllib.request.urlopen(req, timeout=10)
                code, hdrs = 200, {}
            except urllib.error.HTTPError as e:
                code, hdrs = e.code, dict(e.headers)
        finally:
            cyc_store.Store.put = orig
        check(code == 503, 'a busy day lock sheds with 503 (got %s)' % code)
        check(hdrs.get('Retry-After') == '5',
              'and carries Retry-After (got %r)' % hdrs.get('Retry-After'))


def test_heartbeat_stops_with_the_server():
    print('\n== the heartbeat does not outlive the server ==')
    with Harness() as h:
        beat = h.srv._beat
        check(beat.is_alive(), 'heartbeat running while serving')
    check(not beat.is_alive(),
          'heartbeat stopped by server_close - it used to keep writing into '
          'a deleted directory for ever')



def test_lost_ack_retry_is_acknowledged_again():
    print('\n== a retry after a lost response must be acknowledged ==')
    with Harness() as h:
        d = doc([obs('2026-07-31T10:00:03+10:00', 5)])
        code, body = h.post('/ingest/obs', d)
        check(body['ack_seq'] == 5, 'first delivery acknowledged')

        # The collector never saw that response. It resends the same record,
        # which comes back as a durable duplicate. Demanding +1 here refused
        # it for ever and the collector could never advance -- and the old
        # replay test only checked duplicate counts, so it passed anyway.
        code, body = h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00',
                                                    5)]))
        check(body['duplicates'] == 1, 'the retry is a duplicate')
        check(body['ack_seq'] == 5,
              'and is acknowledged AGAIN rather than stalling the collector '
              '(got %s)' % body['ack_seq'])

        # A replayed prefix followed by genuinely new data still advances.
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:00:03+10:00', 5),
                                 obs('2026-07-31T10:01:03+10:00', 6)]))
        check(body['ack_seq'] == 6,
              'a replayed prefix plus new data advances (got %s)'
              % body['ack_seq'])

        # An old, long-acknowledged record is still confirmed durable.
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:00:03+10:00', 5)]))
        check(body['ack_seq'] == 5,
              'an old record is confirmed at its own seq (got %s)'
              % body['ack_seq'])


def test_cursor_survives_restart_without_laundering():
    print('\n== the acknowledged cursor is persisted, not inferred ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 5)]))
        # A batch whose LATER record is durable but NOT acknowledged: seq 7
        # after 5 with no data_loss. It is stored, but the ack stops at 5.
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T11:00:03+10:00', 7)]))
        check(body['ack_seq'] is None, 'the undeclared gap is not acknowledged')
        import cyc_store
        recs = h.srv.stores[STATION]._read_jsonl(
            cyc_store.DayFiles(h.logs, 2026, 7, 31))
        check(len(recs) == 2, 'but seq 7 IS durable on disk')

        cur = os.path.join(h.state, STATION, 'cursor.json')
        check(os.path.exists(cur), 'the cursor is written to its own file')
        check(json.load(open(cur))['last_ack'] == 5,
              'and holds 5, not the newer durable 7 (got %s)'
              % json.load(open(cur))['last_ack'])

        # An ACTUAL restart, not just a call to load_cursor on the running
        # server: build a second Server over the same state and check what it
        # seeds itself with. The previous version of this test never
        # constructed one, so it could not have caught a bad seeding path.
        cfg2 = dict(h.srv.cfg)
        stores2 = {STATION: Store(h.logs, h.state, station=STATION)}
        srv2 = Server(('127.0.0.1', 0), cfg2, stores2)
        try:
            last_ack, gaps = srv2.load_cursor(STATION)
            srv2.stats_by_station[STATION].last_seq = last_ack
            check(last_ack == 5,
                  'the restarted server seeds 5, not the durable 7 (got %s)'
                  % last_ack)
            check(srv2.last_ack(STATION) == 5,
                  'and its live cursor agrees')
            # Prove the consequence: seq 7 is STILL refused after the restart.
            ack, _, _, why = cyc_ingest.ack_from_dispositions(
                [{'obs_ts': '2026-07-31T11:00:03+10:00', 'seq': 7}],
                ['duplicate'], last_ack=srv2.last_ack(STATION))
            check(ack is None and why,
                  'seq 7 is still not acknowledged after the restart (%s)'
                  % ack)
        finally:
            srv2.server_close()


def test_malformed_data_loss_stores_nothing():
    print('\n== a malformed data_loss must not store first ==')
    with Harness() as h:
        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        d['data_loss'] = 'not an object'
        code, _ = h.post('/ingest/obs', d)
        check(code == 400, 'rejected (got %s)' % code)

        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        d['data_loss'] = {'from': True, 'to': True}
        code, _ = h.post('/ingest/obs', d)
        check(code == 400, 'boolean bounds rejected (got %s)' % code)

        d = doc([obs('2026-07-31T10:00:03+10:00', 1)])
        d['data_loss'] = {'from': 9, 'to': 2}
        code, _ = h.post('/ingest/obs', d)
        check(code == 400, 'to < from rejected (got %s)' % code)

        # Nothing may have been written on the way to any of those 400s.
        import cyc_store
        check(not os.path.exists(
            os.path.join(h.logs, '2026', '07', '31.jsonl')),
            'and no observation was stored before the rejection')


def test_shed_queue_saturation_still_closes_cleanly():
    print('\n== a full shed queue degrades to a close, not a hang ==')
    import http.client
    with Harness(shed_queue=1) as h:
        # The queue size is set BEFORE the worker starts. Swapping srv.shed_q
        # afterwards left the worker blocked on the original queue, so the
        # test exercised the bare-close path and never the worker at all.
        h.srv.slots = threading.BoundedSemaphore(1)
        hog = http.client.HTTPConnection('127.0.0.1', h.port, timeout=10)
        hog.connect()
        time.sleep(0.3)

        outcomes = []
        conns = []
        t0 = time.time()
        try:
            for _ in range(6):
                c = http.client.HTTPConnection('127.0.0.1', h.port, timeout=10)
                conns.append(c)
                try:
                    c.request('GET', '/ingest/health')
                    outcomes.append(c.getresponse().status)
                except Exception as e:
                    outcomes.append(type(e).__name__)
        finally:
            elapsed = time.time() - t0
            for c in conns:
                c.close()
            hog.close()
        # With a deliberately tiny queue most refusals DO degrade to a bare
        # close -- that is the documented behaviour. What must never happen is
        # a hang, or an over-capacity request being served.
        check(200 not in outcomes,
              'no over-capacity request was served (%s)' % outcomes)
        check(len(outcomes) == 6 and elapsed < 40,
              'all six completed promptly rather than hanging (%.1fs)'
              % elapsed)



def test_retry_degraded_distinguishes_transient_from_permanent():
    print('\n== degraded days are rechecked, not aged out ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        jsonl = os.path.join(h.logs, '2026', '07', '31.jsonl')
        good = open(jsonl).read()

        # A genuinely broken day, recorded as degraded.
        with open(jsonl, 'w') as f:
            f.write('{"not": "a record"}\n')
        h.srv.degraded['%s/2026-07-31' % STATION] = 'seeded by the test'

        h.srv.retry_degraded()
        check('%s/2026-07-31' % STATION in h.srv.degraded,
              'a still-broken day is KEPT, not cleared (%s)' % h.srv.degraded)

        # The recheck names the specific day, so it works even for a day that
        # no recent-files scan would reach.
        h.srv.degraded['%s/2019-01-01' % STATION] = 'ancient and broken'
        h.srv.retry_degraded()
        check('%s/2019-01-01' % STATION not in h.srv.degraded,
              'a day with nothing wrong is cleared even though it is far '
              'outside any recent window (%s)' % h.srv.degraded)

        # Repair the real one; the next heartbeat must clear it.
        with open(jsonl, 'w') as f:
            f.write(good)
        h.srv.retry_degraded()
        check(not h.srv.degraded,
              'a repaired day is cleared (%s)' % h.srv.degraded)
        code, body = h.get('/ingest/health')
        check(code == 200 and body['ok'] is True, 'and health recovers')



def test_sequence_conflicts_are_not_laundered():
    print('\n== a reset or a second collector must not be waved through ==')
    from cyc_ingest import ack_from_dispositions as A
    from cyc_store import STORED, DUPLICATE
    o = lambda sq: {'obs_ts': '2026-07-31T10:00:03+10:00', 'seq': sq}

    # A collector reinstalled with a fresh counter, or a second collector on
    # the same station. The record is NEW, not a replay -- acknowledging it
    # would hide the collision behind a cursor that stays at 34.
    ack, _, _, why = A([o(1)], [STORED], last_ack=34)
    check(ack is None and 'newly stored' in (why or ''),
          'a STORED record below the cursor is a conflict, not a replay '
          '(ack=%s, %r)' % (ack, why))

    # The genuine replay is still fine.
    check(A([o(5)], [DUPLICATE], last_ack=34)[0] == 5,
          'a DUPLICATE below the cursor is re-acknowledged')
    check(A([o(34)], [DUPLICATE], last_ack=34)[0] == 34,
          'and so is one exactly at the cursor')

    ack, _, _, why = A([o(-3)], [STORED], last_ack=None)
    check(ack is None and 'negative' in (why or ''),
          'a negative seq is refused even from a cold cursor')

    # A loss declared against the very first record must still be counted.
    ack, _, gaps, _ = A([o(7)], [STORED], last_ack=None,
                        loss_from=1, loss_to=6)
    check(ack == 7 and gaps == 6,
          'a loss declared from a cold cursor is counted, not silently '
          'dropped (ack=%s gaps=%s)' % (ack, gaps))


def test_ack_withheld_when_the_cursor_cannot_be_persisted():
    print('\n== never acknowledge what we could not write down ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        before = json.load(open(os.path.join(h.state, STATION, 'cursor.json')))

        real = h.srv.save_cursor
        h.srv.save_cursor = lambda *a, **k: False
        try:
            code, body = h.post('/ingest/obs',
                                doc([obs('2026-07-31T10:01:03+10:00', 2)]))
        finally:
            h.srv.save_cursor = real
        check(code == 500 and body.get('error') == 'cursor_write_failed',
              'the request fails loudly rather than acknowledging (got %s %s)'
              % (code, body.get('error')))
        check('ack_seq' not in body, 'and no ack_seq is returned')

        after = json.load(open(os.path.join(h.state, STATION, 'cursor.json')))
        check(after['last_ack'] == before['last_ack'],
              'the on-disk cursor did not move (%s -> %s)'
              % (before['last_ack'], after['last_ack']))
        check(h.srv.last_ack(STATION) == before['last_ack'],
              'and neither did the in-memory one, so they still agree')

        # The observation IS durable, so the retry is a duplicate -- and now
        # that the cursor can be written, it is acknowledged.
        code, body = h.post('/ingest/obs',
                            doc([obs('2026-07-31T10:01:03+10:00', 2)]))
        check(code == 200 and body['ack_seq'] == 2,
              'the retry succeeds once the cursor can commit (got %s %s)'
              % (code, body.get('ack_seq')))


def test_every_degraded_day_gets_a_turn():
    print('\n== degraded retries rotate rather than starving the tail ==')
    with Harness() as h:
        for i in range(1, 11):
            h.srv.degraded['%s/2019-01-%02d' % (STATION, i)] = 'seeded'
        seen = set()
        for _ in range(4):                      # 4 beats x 4 per beat
            before = set(h.srv.degraded)
            h.srv.retry_degraded()
            seen |= before - set(h.srv.degraded)
        check(len(seen) == 10,
              'all ten entries were rechecked within a few beats, not the '
              'same four for ever (%d cleared)' % len(seen))


def test_runtime_corruption_is_retried_not_just_displayed():
    print('\n== corruption found while serving is rechecked ==')
    with Harness() as h:
        h.post('/ingest/obs', doc([obs('2026-07-31T10:00:03+10:00', 1)]))
        jsonl = os.path.join(h.logs, '2026', '07', '31.jsonl')
        good = open(jsonl).read()
        with open(jsonl, 'w') as f:
            f.write('{"broken": true}\n')
        h.post('/ingest/obs', doc([obs('2026-07-31T10:05:00+10:00', 2)]))

        check(not h.srv.degraded,
              'the failure is in the store, not yet in the retry set')
        h.srv.retry_degraded()
        check(any('2026-07-31' in k for k in h.srv.degraded),
              'the heartbeat adopts it so it will be retried (%s)'
              % h.srv.degraded)

        with open(jsonl, 'w') as f:
            f.write(good)
        h.srv.retry_degraded()
        check(not h.srv.degraded, 'and clears it once repaired (%s)'
              % h.srv.degraded)


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
    test_station_required()
    test_collector_must_be_object()
    test_capacity_is_enforced_before_threads()
    test_health_reports_degraded_storage()
    test_hmac_against_a_fixed_vector()
    test_shadow_only_advances_on_a_real_ack()
    test_a_second_station_cannot_speak_for_the_primary()
    test_concurrent_posts_do_not_deadlock_or_lose_records()
    test_shedding_stores_nothing_and_retries_cleanly()
    test_status_never_regresses_under_concurrent_writers()
    test_ack_sequence_contract()
    test_lost_ack_retry_is_acknowledged_again()
    test_cursor_survives_restart_without_laundering()
    test_malformed_data_loss_stores_nothing()
    test_shed_queue_saturation_still_closes_cleanly()
    test_retry_degraded_distinguishes_transient_from_permanent()
    test_sequence_conflicts_are_not_laundered()
    test_ack_withheld_when_the_cursor_cannot_be_persisted()
    test_every_degraded_day_gets_a_turn()
    test_runtime_corruption_is_retried_not_just_displayed()
    test_seq_gaps_are_counted_from_the_batch()
    test_duplicate_instant_in_one_batch()
    test_busy_carries_retry_after()
    test_heartbeat_stops_with_the_server()

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
