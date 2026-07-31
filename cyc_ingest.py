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
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cyc_store import Store, TZ, parse_obs_ts, utc_key, StoreError  # noqa: E402

SCHEME = 'v1'
MAX_BODY = 2 * 1024 * 1024
MAX_OBS = 500
SKEW_S = 300
NONCE_TTL_S = 600
AUTH_FAIL_WINDOW_S = 300
AUTH_FAIL_LIMIT = 10


def now_iso():
    return datetime.now(TZ).isoformat(timespec='seconds')


def load_keys(path):
    """station<TAB>hexkey per line. Kept outside DocumentRoot."""
    keys = {}
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            keys[parts[0]] = parts[1].encode('ascii')
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

    def failures_1h(self, now):
        with self.lock:
            return sum(len([t for t in v if t > now - 3600])
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

    def read_body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return None, 'bad_request'
        if length > MAX_BODY:
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

        # Clock skew is a distinct, non-punitive failure: the collector must be
        # told so it can alarm, and it must NOT advance its cursor.
        if abs(now - ts) > SKEW_S:
            return None, (400, 'clock_skew')

        expect = sign(key, self.command, self.path, station, ts, nonce, body)
        if not hmac.compare_digest(expect, sig):
            return deny('auth_failed')

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

        obs = doc.get('obs')
        if not isinstance(obs, list):
            return self.fail(400, 'bad_schema')
        if len(obs) > MAX_OBS:
            return self.fail(413, 'too_large')

        try:
            accepted, dup, rejected, details = self.server.store.put(obs)
        except StoreError as e:
            return self.fail(400, 'bad_schema', detail=str(e))
        except OSError as e:
            self.log_message('storage error: %s', e)
            return self.fail(500, 'storage_error')

        # ack_seq is the highest seq DURABLY written; the collector advances its
        # cursor only this far. Duplicates count as durable -- they are already
        # stored -- otherwise a retry after a lost ACK would never drain.
        ack_seq = None
        newest_utc = None
        for o in obs:
            try:
                u = utc_key(parse_obs_ts(o.get('obs_ts')))
            except StoreError:
                continue
            if newest_utc is None or u > newest_utc:
                newest_utc = u
            s = o.get('seq')
            if isinstance(s, int) and (ack_seq is None or s > ack_seq):
                ack_seq = s

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

    def __init__(self, addr, cfg, store):
        super().__init__(addr, Handler)
        self.cfg = cfg
        self.store = store
        self.guard = Guard()
        self.stats = Stats()

    def write_status(self):
        """Absolute timestamps are authoritative; age_s is a convenience that
        goes stale if nothing rewrites it. feed_watch.py refreshes it every
        minute so a dead producer shows a growing age rather than a frozen one.
        """
        st = self.stats
        now = time.time()
        doc = {
            'station': 'cyc1',
            'written_at': now_iso(),
            'written_at_epoch': int(now),
            'last_obs_ts': (None if st.last_obs_utc is None else
                            datetime.fromtimestamp(st.last_obs_utc, TZ)
                            .isoformat(timespec='seconds')),
            'last_obs_epoch': st.last_obs_utc,
            'obs_age_s': (None if st.last_obs_utc is None
                          else int(now - st.last_obs_utc)),
            'last_post_epoch': int(st.last_post) if st.last_post else None,
            'post_age_s': (None if not st.last_post
                           else int(now - st.last_post)),
            # Time between the observation being made and us receiving it.
            # For the direct collector this should be a second or two, so growth
            # means either a wrong collector clock or a spool backlog. For the
            # cloud shadow path it is dominated by the ~15 min feed latency, so
            # thresholds must be set per source.
            'recv_lag_s': (None if st.last_obs_utc is None or not st.last_post
                           else int(st.last_post - st.last_obs_utc)),
            'seq': st.last_seq,
            'seq_gaps': st.seq_gaps,
            'accepted': st.accepted,
            'duplicates': st.duplicates,
            'rejected': st.rejected,
            'auth_failures_1h': self.guard.failures_1h(now),
            'source': (st.collector or {}).get('source', 'unknown'),
            'collector': st.collector,
        }
        path = self.cfg['status']
        tmp = path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=1, sort_keys=True)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as e:
            sys.stderr.write('status write failed: %s\n' % e)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8790)
    p.add_argument('--keyfile', required=True)
    p.add_argument('--logdir', required=True)
    p.add_argument('--statedir', required=True)
    p.add_argument('--status', required=True)
    p.add_argument('--min-obs-ts', type=int, default=None,
                   help='exact UTC epoch of cutover; obs at or before are refused')
    p.add_argument('--enable-echo', action='store_true')
    args = p.parse_args(argv)

    cfg = {
        'keys': load_keys(args.keyfile),
        'status': args.status,
        'enable_echo': args.enable_echo,
    }
    store = Store(args.logdir, args.statedir, min_obs_ts=args.min_obs_ts)

    srv = Server((args.host, args.port), cfg, store)

    # Seed from storage before serving. Stats are in-memory, so without this a
    # restart makes status.json report "no observation has ever been recorded"
    # until the next one arrives -- which feed_watch correctly treats as an
    # alarm. Every deploy would then fire a false alert.
    try:
        last = store.last_observation()
    except OSError:
        last = None
    if last:
        srv.stats.last_obs_utc = int(last['utc'])
        if isinstance(last.get('seq'), int):
            srv.stats.last_seq = last['seq']
        sys.stderr.write('seeded from storage: last observation %s\n'
                         % datetime.fromtimestamp(srv.stats.last_obs_utc, TZ)
                         .isoformat(timespec='seconds'))

    srv.write_status()
    sys.stderr.write('cyc-ingest listening on %s:%d, %d key(s)\n'
                     % (args.host, args.port, len(cfg['keys'])))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
