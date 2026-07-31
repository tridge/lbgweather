#!/usr/bin/env python3
"""HMAC-authenticated receiver for observations pushed by the CYC collector.

Runs as a systemd service bound to loopback; Apache reverse-proxies /ingest/ to
it. That shape is already used by three other services on this box, needs no new
Apache modules (proxy and proxy_http are enabled; wsgi and cgid are not), and
keeps the process isolated from the web server.

Storage is delegated to cyc_store; this module is only transport and auth.

Endpoints
  POST /ingest/obs     authenticated ingest
  GET  /ingest/health  unauthenticated liveness, computes age live
  POST /ingest/echo    authenticated no-op returning the canonical signing
                       string, for debugging signature mismatches without
                       touching real data (--enable-echo, off by default)
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cyc_store import (Store, TZ, parse_obs_ts, utc_key,  # noqa: E402
                       StoreError, CorruptionError)

SCHEME = 'v1'
MAX_BODY = 2 * 1024 * 1024
MAX_OBS = 500
SKEW_S = 300
NONCE_TTL_S = 600
AUTH_FAIL_WINDOW_S = 300
AUTH_FAIL_LIMIT = 10
# Read and discard at most this much of an over-large body before rejecting it.
DRAIN_CAP = 8 * 1024 * 1024
# Wall-clock cap on a single client connection.
SOCKET_TIMEOUT_S = 20


def now_iso():
    return datetime.now(TZ).isoformat(timespec='seconds')


def load_keys(path):
    """station<TAB>64-hex-char key per line. Kept outside DocumentRoot.

    The format is enforced rather than assumed: without a length check a
    one-character typo silently becomes a working key, and a short key weakens
    the HMAC without anything reporting it.
    """
    keys = {}
    with open(path, 'r', encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) != 2:
                raise SystemExit('%s line %d: expected "station<TAB>hexkey"'
                                 % (path, n))
            station, hexkey = parts
            try:
                raw = bytes.fromhex(hexkey)
            except ValueError:
                raise SystemExit('%s line %d: key for %r is not hex'
                                 % (path, n, station))
            if len(raw) != 32:
                raise SystemExit('%s line %d: key for %r is %d bytes, want 32'
                                 % (path, n, station, len(raw)))
            keys[station] = raw
    if not keys:
        raise SystemExit('no keys loaded from %s' % path)
    return keys


def signing_string(method, path, station, ts, nonce, body):
    """Newline-joined, exactly seven lines, no trailing newline.

    The body is hashed rather than signed directly so the receiver can reject on
    Content-Length before reading it, and so there is no charset ambiguity.
    """
    return '\n'.join([
        SCHEME,
        method,
        path,
        station,
        str(ts),
        nonce,
        hashlib.sha256(body).hexdigest(),
    ])


def sign(key, method, path, station, ts, nonce, body):
    s = signing_string(method, path, station, ts, nonce, body)
    return SCHEME + '=' + hmac.new(key, s.encode('utf-8'),
                                   hashlib.sha256).hexdigest()


class Guard:
    """Nonce replay cache and per-IP auth-failure limiter."""

    def __init__(self):
        self.lock = threading.Lock()
        self.nonces = {}          # nonce -> expiry
        self.failures = {}        # ip -> [timestamps]

    def _reap(self, now):
        for n, exp in list(self.nonces.items()):
            if exp < now:
                del self.nonces[n]
        for ip, times in list(self.failures.items()):
            keep = [t for t in times if t > now - AUTH_FAIL_WINDOW_S]
            if keep:
                self.failures[ip] = keep
            else:
                del self.failures[ip]

    def check_nonce(self, nonce, now):
        with self.lock:
            self._reap(now)
            if nonce in self.nonces:
                return False
            self.nonces[nonce] = now + NONCE_TTL_S
            return True

    def rate_limited(self, ip, now):
        with self.lock:
            self._reap(now)
            return len(self.failures.get(ip, [])) >= AUTH_FAIL_LIMIT

    def record_failure(self, ip, now):
        with self.lock:
            self.failures.setdefault(ip, []).append(now)

    def recent_failures(self, now):
        """Auth failures still inside the limiter window (5 min, not an hour)."""
        with self.lock:
            return sum(len([t for t in v if t > now - AUTH_FAIL_WINDOW_S])
                       for v in self.failures.values())


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.accepted = 0
        self.duplicates = 0
        self.rejected = 0
        self.last_post = None
        self.last_obs_utc = None
        self.last_seq = None
        self.seq_gaps = 0
        self.collector = {}

    def record(self, a, d, r, obs_utc, seq, collector):
        with self.lock:
            self.accepted += a
            self.duplicates += d
            self.rejected += r
            self.last_post = time.time()
            if obs_utc is not None:
                if self.last_obs_utc is None or obs_utc > self.last_obs_utc:
                    self.last_obs_utc = obs_utc
            if seq is not None:
                if self.last_seq is not None and seq > self.last_seq + 1:
                    self.seq_gaps += seq - self.last_seq - 1
                self.last_seq = max(seq, self.last_seq or seq)
            if collector:
                self.collector = collector


class Handler(BaseHTTPRequestHandler):
    server_version = 'cyc-ingest/1.0'
    protocol_version = 'HTTP/1.1'
    # A client that opens a connection and dribbles bytes must not hold a
    # thread indefinitely.
    timeout = SOCKET_TIMEOUT_S

    def setup(self):
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT_S)
        self._slot = self.server.slots.acquire(blocking=False)

    def finish(self):
        try:
            super().finish()
        finally:
            if getattr(self, '_slot', False):
                self.server.slots.release()

    def handle_one_request(self):
        if not getattr(self, '_slot', False):
            # Over capacity: shed the connection rather than queue threads.
            try:
                self.send_response(503)
                self.send_header('Content-Length', '0')
                self.send_header('Retry-After', '5')
                self.end_headers()
            except Exception:
                pass
            self.close_connection = True
            return
        super().handle_one_request()

    # ------------------------------------------------------------- utilities

    def log_message(self, fmt, *args):
        # journald already timestamps; keep it terse and never log headers.
        sys.stderr.write('%s %s\n' % (self.client_ip(), fmt % args))

    def client_ip(self):
        """mod_proxy appends the real peer to any client-supplied header, so the
        last element is the only trustworthy one."""
        xff = self.headers.get('X-Forwarded-For', '')
        if xff:
            return xff.split(',')[-1].strip()
        return self.client_address[0]

    def respond(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, error, **extra):
        payload = {'ok': False, 'error': error, 'server_time': now_iso()}
        payload.update(extra)
        self.respond(code, payload)

    # ----------------------------------------------------------------- auth

    def drain(self, length):
        """Discard an over-large request body before rejecting it.

        Responding without reading the body leaves the peer still sending. Apache
        then reports 'AH01084 pass request body failed - Broken pipe' and hands
        the CLIENT a 502 instead of our 413, which is both misleading and looks
        like the backend fell over. LimitRequestBody does not gate
        reverse-proxied bodies, so this has to be handled here.

        Bounded: beyond DRAIN_CAP we stop reading and let the connection close,
        because draining is a courtesy and must not become a way to occupy the
        server indefinitely.
        """
        remaining = min(length, DRAIN_CAP)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def read_body(self):
        # Exactly one non-negative Content-Length, and no transfer encodings we
        # do not implement. Without this, a negative length reaches read(-1)
        # and a chunked body is silently treated as empty.
        if self.headers.get('Transfer-Encoding'):
            return None, 'bad_request'
        lengths = self.headers.get_all('Content-Length') or []
        if len(lengths) > 1:
            return None, 'bad_request'
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            return None, 'bad_request'
        if length < 0:
            return None, 'bad_request'
        if length > MAX_BODY:
            self.drain(length)
            self.close_connection = True
            return None, 'too_large'
        return self.rfile.read(length) if length else b'', None

    def authenticate(self, body):
        """Returns (station, None) or (None, (code, error))."""
        cfg = self.server.cfg
        now = time.time()
        ip = self.client_ip()

        if self.server.guard.rate_limited(ip, now):
            return None, (429, 'rate_limited')

        station = self.headers.get('X-CYC-Station', '')
        sig = self.headers.get('X-CYC-Signature', '')
        nonce = self.headers.get('X-CYC-Nonce', '')
        ts_raw = self.headers.get('X-CYC-Timestamp', '')

        def deny(err, code=401):
            self.server.guard.record_failure(ip, now)
            return None, (code, err)

        if not (station and sig and nonce and ts_raw):
            return deny('auth_failed')
        key = cfg['keys'].get(station)
        if key is None:
            return deny('auth_failed')
        try:
            ts = int(ts_raw)
        except ValueError:
            return deny('auth_failed')

        # Verify the signature FIRST. Reporting clock skew to an unauthenticated
        # caller would let anyone enumerate configured station names by the
        # distinguishable response, and would do so without tripping the
        # auth-failure limiter.
        expect = sign(key, self.command, self.path, station, ts, nonce, body)
        if not hmac.compare_digest(expect, sig):
            return deny('auth_failed')

        # Only now, to a proven-authentic caller: skew is a distinct,
        # non-punitive failure. The collector must be told so it can alarm, and
        # it must NOT advance its cursor. It is deliberately not counted as an
        # auth failure, or a drifting clock would rate-limit the collector out
        # entirely.
        if abs(now - ts) > SKEW_S:
            return None, (400, 'clock_skew')

        if not self.server.guard.check_nonce(nonce, now):
            return deny('auth_failed')

        return station, None

    # -------------------------------------------------------------- routing

    def do_GET(self):
        if self.path.rstrip('/') == '/ingest/health':
            return self.health()
        self.fail(404, 'not_found')

    def do_POST(self):
        body, err = self.read_body()
        if err:
            return self.fail(413 if err == 'too_large' else 400, err)

        station, aerr = self.authenticate(body)
        if aerr:
            code, error = aerr
            extra = {'server_time_epoch': int(time.time())} if error == 'clock_skew' else {}
            return self.fail(code, error, **extra)

        path = self.path.rstrip('/')
        if path == '/ingest/obs':
            return self.ingest(station, body)
        if path == '/ingest/echo' and self.server.cfg['enable_echo']:
            ts = self.headers.get('X-CYC-Timestamp', '')
            nonce = self.headers.get('X-CYC-Nonce', '')
            return self.respond(200, {
                'ok': True,
                'signing_string': signing_string(
                    self.command, self.path, station, ts, nonce, body),
                'body_sha256': hashlib.sha256(body).hexdigest(),
                'server_time': now_iso(),
            })
        self.fail(404, 'not_found')

    # -------------------------------------------------------------- handlers

    def health(self):
        st = self.server.stats
        now = time.time()
        age = None if st.last_obs_utc is None else int(now - st.last_obs_utc)
        self.respond(200, {
            'ok': True,
            'age_s': age,
            'last_obs_ts': (None if st.last_obs_utc is None else
                            datetime.fromtimestamp(st.last_obs_utc, TZ)
                            .isoformat(timespec='seconds')),
            'server_time': now_iso(),
        })

    def ingest(self, station, body):
        try:
            doc = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return self.fail(400, 'bad_request')
        if not isinstance(doc, dict):
            return self.fail(400, 'bad_schema')

        if doc.get('schema') != 1:
            return self.fail(400, 'bad_schema', detail='unsupported schema')
        # The station in the body must match the key that signed the request.
        # Otherwise the field is decorative and a test key could write into the
        # real station's tree.
        declared = doc.get('station')
        if declared is not None and declared != station:
            return self.fail(400, 'bad_schema',
                             detail='station does not match the signing key')

        obs = doc.get('obs')
        if not isinstance(obs, list):
            return self.fail(400, 'bad_schema')
        if len(obs) > MAX_OBS:
            return self.fail(413, 'too_large')

        store = self.server.stores.get(station)
        if store is None:
            # Authenticated but no storage configured for this station: refuse
            # rather than fall back to somebody else's tree.
            return self.fail(403, 'station_not_configured')

        try:
            accepted, dup, rejected, details, durable = store.put(obs)
        except CorruptionError as e:
            self.log_message('CORRUPTION: %s', e)
            return self.fail(500, 'storage_corrupt')
        except StoreError as e:
            return self.fail(400, 'bad_schema', detail=str(e))
        except OSError as e:
            self.log_message('storage error: %s', e)
            return self.fail(500, 'storage_error')

        # ack_seq is the highest seq the store reported as DURABLE -- newly
        # written or already present. Records the store refused (before the
        # cutover instant, malformed, future-dated) must NOT be acknowledged, or
        # the collector advances its cursor past data that was never stored.
        ack_seq = None
        newest_utc = None
        for o in obs:
            if not isinstance(o, dict):
                continue
            try:
                u = utc_key(parse_obs_ts(o.get('obs_ts')))
            except StoreError:
                continue
            if u not in durable:
                continue
            if newest_utc is None or u > newest_utc:
                newest_utc = u
            sq = o.get('seq')
            if isinstance(sq, int) and (ack_seq is None or sq > ack_seq):
                ack_seq = sq

        self.server.stats.record(accepted, dup, rejected, newest_utc, ack_seq,
                                 doc.get('collector') or {})
        self.server.write_status()

        self.respond(200, {
            'ok': True,
            'accepted': accepted,
            'duplicates': dup,
            'rejected': rejected,
            'ack_seq': ack_seq,
            'server_time': now_iso(),
            'warnings': details[:5],
        })


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, cfg, stores):
        super().__init__(addr, Handler)
        self.cfg = cfg
        self.stores = stores
        self.guard = Guard()
        self.stats = Stats()
        # Bound concurrent handlers. Without a cap, slow clients each hold a
        # thread (and an Apache worker) for the full socket timeout; on a shared
        # VPS also running postfix and five other sites that is a cheap way to
        # exhaust the box.
        self.slots = threading.BoundedSemaphore(cfg.get('max_conns', 16))

    def snapshot(self):
        st = self.stats
        with st.lock:
            return dict(accepted=st.accepted, duplicates=st.duplicates,
                        rejected=st.rejected, last_post=st.last_post,
                        last_obs_utc=st.last_obs_utc, last_seq=st.last_seq,
                        seq_gaps=st.seq_gaps, collector=dict(st.collector))

    def write_status(self):
        """Absolute timestamps are authoritative; obs_age_s is a convenience
        that goes stale if nothing rewrites it. feed_watch.py refreshes it every
        minute so a dead producer shows a growing age rather than a frozen one.
        """
        st = self.snapshot()
        now = time.time()
        last_obs = st['last_obs_utc']
        last_post = st['last_post']
        doc = {
            'station': self.cfg.get('primary', 'cyc1'),
            'written_at': now_iso(),
            'written_at_epoch': int(now),
            'last_obs_ts': (None if last_obs is None else
                            datetime.fromtimestamp(last_obs, TZ)
                            .isoformat(timespec='seconds')),
            'last_obs_epoch': last_obs,
            'obs_age_s': None if last_obs is None else int(now - last_obs),
            'last_post_epoch': int(last_post) if last_post else None,
            'post_age_s': None if not last_post else int(now - last_post),
            # Time between an observation being made and us receiving it. For
            # the direct collector this should be a second or two, so growth
            # means a wrong collector clock or a spool backlog. On the cloud
            # shadow path it is dominated by the ~15 min feed latency, so the
            # alarm threshold must be set per source.
            'recv_lag_s': (None if last_obs is None or not last_post
                           else int(last_post - last_obs)),
            'seq': st['last_seq'],
            'seq_gaps': st['seq_gaps'],
            'accepted': st['accepted'],
            'duplicates': st['duplicates'],
            'rejected': st['rejected'],
            'auth_failures_recent': self.guard.recent_failures(now),
            'source': (st['collector'] or {}).get('source', 'unknown'),
            'collector': st['collector'],
        }
        path = self.cfg['status']
        # Unique temp name: two concurrent handlers sharing a fixed
        # "status.json.tmp" can truncate each other's inode, and one may still
        # be writing after the other has renamed it into place.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or '.',
                                   prefix='.status-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=1, sort_keys=True)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as e:
            sys.stderr.write('status write failed: %s\n' % e)
            try:
                os.unlink(tmp)
            except OSError:
                pass


def parse_station_arg(value):
    """NAME=LOGDIR"""
    if '=' not in value:
        raise argparse.ArgumentTypeError('expected NAME=LOGDIR, got %r' % value)
    name, logdir = value.split('=', 1)
    if not name or not logdir:
        raise argparse.ArgumentTypeError('expected NAME=LOGDIR, got %r' % value)
    return name, logdir


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8790)
    p.add_argument('--keyfile', required=True)
    p.add_argument('--station', action='append', required=True,
                   type=parse_station_arg, metavar='NAME=LOGDIR',
                   help='repeatable; each station writes its own tree, so a '
                        'test key cannot reach the real one')
    p.add_argument('--statedir', required=True)
    p.add_argument('--status', required=True)
    p.add_argument('--min-obs-ts', type=int, default=None,
                   help='exact UTC epoch of cutover; obs at or before are refused')
    p.add_argument('--max-conns', type=int, default=16)
    p.add_argument('--enable-echo', action='store_true')
    args = p.parse_args(argv)

    keys = load_keys(args.keyfile)
    stations = dict(args.station)

    unknown = set(stations) - set(keys)
    if unknown:
        raise SystemExit('no key for configured station(s): %s'
                         % ', '.join(sorted(unknown)))
    keyless = set(keys) - set(stations)
    if keyless:
        # A key with no storage is authenticated but cannot write; say so
        # loudly rather than letting it silently 403 later.
        sys.stderr.write('warning: key(s) with no --station mapping, they '
                         'cannot write: %s\n' % ', '.join(sorted(keyless)))

    primary = args.station[0][0]
    cfg = {'keys': keys, 'status': args.status,
           'enable_echo': args.enable_echo, 'max_conns': args.max_conns,
           'primary': primary}

    stores = {}
    for name, logdir in stations.items():
        stores[name] = Store(logdir, os.path.join(args.statedir, name),
                             station=name, min_obs_ts=args.min_obs_ts)

    # Repair anything a previous crash left inconsistent, including days no
    # later request would touch.
    for name, store in stores.items():
        try:
            repaired = store.reconcile_all()
            if repaired:
                sys.stderr.write('reconciled %s: %s\n'
                                 % (name, ', '.join(repaired)))
        except Exception as e:
            sys.stderr.write('reconcile_all(%s) failed: %s\n' % (name, e))

    srv = Server((args.host, args.port), cfg, stores)

    # Seed from storage before serving. Stats are in-memory, so without this a
    # restart makes status.json report "no observation has ever been recorded"
    # until the next one arrives -- which feed_watch correctly treats as an
    # alarm. Every deploy would otherwise fire a false alert.
    try:
        last = stores[primary].last_observation()
    except Exception:
        last = None
    if last:
        srv.stats.last_obs_utc = int(last['utc'])
        if isinstance(last.get('seq'), int):
            srv.stats.last_seq = last['seq']
        sys.stderr.write('seeded from storage: last observation %s\n'
                         % datetime.fromtimestamp(srv.stats.last_obs_utc, TZ)
                         .isoformat(timespec='seconds'))

    srv.write_status()
    sys.stderr.write('cyc-ingest listening on %s:%d, stations: %s\n'
                     % (args.host, args.port, ', '.join(sorted(stations))))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
