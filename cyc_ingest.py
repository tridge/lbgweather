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
import queue
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cyc_store import (Store, TZ, parse_obs_ts, utc_key,  # noqa: E402
                       StoreError, CorruptionError, LockTimeout, DURABLE,
                       DUPLICATE, CONFLICT, _fsync_dir)

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
# Bounds on the in-memory guard tables.
HEARTBEAT_S = 60
# Refusing a connection: how long to spend letting the peer read the 503 and
# finish its body, how much of that body to discard, and how many refusals may
# queue before we simply drop the connection.
SHED_TIMEOUT_S = 5
SHED_DRAIN_CAP = 1 << 20
SHED_QUEUE = 256
# Degraded days rechecked per heartbeat. Bounded so a pile of bad days
# cannot hold the heartbeat past the shutdown join.
RETRY_PER_BEAT = 4
MAX_NONCES = 20000
MAX_FAIL_IPS = 4096


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
            if len(self.nonces) >= MAX_NONCES:
                # Bounded: refuse rather than grow without limit. Only
                # authenticated callers reach here, so this is a safety net,
                # not a normal path.
                return False
            self.nonces[nonce] = now + NONCE_TTL_S
            return True

    def rate_limited(self, ip, now):
        with self.lock:
            self._reap(now)
            return len(self.failures.get(ip, [])) >= AUTH_FAIL_LIMIT

    def record_failure(self, ip, now):
        with self.lock:
            if (ip not in self.failures) and len(self.failures) >= MAX_FAIL_IPS:
                # Under a distributed probe, stop tracking new sources rather
                # than let the table grow without bound.
                return
            hits = self.failures.setdefault(ip, [])
            hits.append(now)
            # Keep only what the limiter can act on; a single source cannot
            # grow its own list without bound either.
            if len(hits) > AUTH_FAIL_LIMIT * 2:
                del hits[:-AUTH_FAIL_LIMIT * 2]

    def recent_failures(self, now):
        """Auth failures still inside the limiter window (5 min, not an hour)."""
        with self.lock:
            return sum(len([t for t in v if t > now - AUTH_FAIL_WINDOW_S])
                       for v in self.failures.values())


class CursorError(Exception):
    """The persisted sequence cursor could not be read."""


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

    def record(self, a, d, r, obs_utc, seq, collector, gaps=0):
        """Fold one request's outcome in.

        `gaps` is counted by the caller from the batch itself. Deriving it here
        from the previous ack and the new one counted every intervening
        sequence as lost even when the batch delivered them: last_seq 4 plus a
        clean batch [5, 6] reported one lost observation, because 6 - 4 - 1 = 1.
        """
        with self.lock:
            self.accepted += a
            self.duplicates += d
            self.rejected += r
            self.last_post = time.time()
            self.seq_gaps += gaps
            if obs_utc is not None:
                if self.last_obs_utc is None or obs_utc > self.last_obs_utc:
                    self.last_obs_utc = obs_utc
            if seq is not None:
                self.last_seq = seq if self.last_seq is None else max(
                    seq, self.last_seq)
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

    def respond(self, code, payload, retry_after=None):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if retry_after is not None:
            # Tell a transiently-refused caller when to come back rather than
            # leaving it to guess or to give up.
            self.send_header('Retry-After', str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, error, retry_after=None, **extra):
        payload = {'ok': False, 'error': error, 'server_time': now_iso()}
        payload.update(extra)
        self.respond(code, payload, retry_after=retry_after)

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
        st = self.server.snapshot()
        now = time.time()
        last = st['last_obs_utc']
        degraded = self.server.current_degraded()
        self.respond(200 if not degraded else 503, {
            # Storage we cannot read is not "ok" -- returning 200 while a day
            # is unreadable is exactly the silent failure this project exists
            # to remove.
            'ok': not degraded,
            'age_s': None if last is None else int(now - last),
            'last_obs_ts': (None if last is None else
                            datetime.fromtimestamp(last, TZ)
                            .isoformat(timespec='seconds')),
            'degraded': degraded,
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
        # Required, not optional: an absent field would otherwise be accepted
        # and the check would only bite callers honest enough to fill it in.
        declared = doc.get('station')
        if declared != station:
            return self.fail(400, 'bad_schema',
                             detail='station must be present and match the '
                                    'signing key')

        # Validated BEFORE anything is stored: rejecting after the write meant
        # a malformed request stored its observations and then returned 400.
        loss = doc.get('data_loss')
        if loss is not None and not isinstance(loss, dict):
            return self.fail(400, 'bad_schema',
                             detail='data_loss must be an object')
        loss = loss or {}
        loss_from, loss_to = loss.get('from'), loss.get('to')
        if (loss_from is not None or loss_to is not None) and not (
                is_int(loss_from) and is_int(loss_to)
                and loss_to >= loss_from):
            return self.fail(400, 'bad_schema',
                             detail='data_loss from/to must be integers with '
                                    'to >= from')

        collector = doc.get('collector')
        if collector is not None and not isinstance(collector, dict):
            # A truthy non-dict would be committed and then crash the status
            # writer after the record was already stored.
            return self.fail(400, 'bad_schema', detail='collector must be an object')

        obs = doc.get('obs')
        if not isinstance(obs, list):
            return self.fail(400, 'bad_schema')

        # Every observation must carry a sequence. Without one it is stored
        # but unacknowledgeable, and a later request naming the same instant
        # WITH a sequence then classifies as a duplicate and advances the
        # cursor -- a record admitted with no sequence at all being used to
        # move the cursor forward.
        for o in obs:
            if isinstance(o, dict) and not is_int(o.get('seq')):
                return self.fail(400, 'bad_schema',
                                 detail='every observation needs an integer '
                                        'seq; %r has %r'
                                        % (o.get('obs_ts'), o.get('seq')))
        if len(obs) > MAX_OBS:
            return self.fail(413, 'too_large')

        store = self.server.stores.get(station)
        if store is None:
            # Authenticated but no storage configured for this station: refuse
            # rather than fall back to somebody else's tree.
            return self.fail(403, 'station_not_configured')

        # ONE transaction per station: read the cursor, store, decide, commit
        # the cursor. Splitting these let two concurrent requests both measure
        # against the same cursor, both store, and the loser's conflict end up
        # on disk -- after which its retry was an ordinary duplicate and was
        # acknowledged. There is one collector per station, so serialising a
        # station's writes costs nothing real.
        with self.server.cursor_lock(station):
            cursor_now = self.server.last_ack(station)
            try:
                accepted, dup, rejected, details, disp = store.put(
                    obs, cursor=cursor_now)
            except LockTimeout as e:
                # Busy is NOT a conflict. Conflating them turned transient
                # contention into a 409 that reads as a permanent protocol
                # violation and stalled a healthy collector.
                self.log_message('day lock busy: %s', e)
                return self.fail(503, 'busy', retry_after=5)
            except CorruptionError as e:
                self.log_message('CORRUPTION: %s', e)
                return self.fail(500, 'storage_corrupt')
            except StoreError as e:
                return self.fail(400, 'bad_schema', detail=str(e))
            except OSError as e:
                self.log_message('storage error: %s', e)
                return self.fail(500, 'storage_error')

            if CONFLICT in disp:
                # Nothing was stored, so a retry cannot become a duplicate.
                return self.fail(
                    409, 'seq_conflict',
                    detail=(details[0] if details else
                            'a sequence at or below the acknowledged cursor '
                            'is not a record we hold'))

            ack_seq, newest_utc, gaps, seq_problem = ack_from_dispositions(
                obs, disp, last_ack=cursor_now,
                loss_from=loss_from, loss_to=loss_to)
            if seq_problem:
                details.append(seq_problem)
            committed = self.server.record(
                station, accepted, dup, rejected, newest_utc, ack_seq,
                collector or {}, gaps=gaps)

        if not committed:
            # The observations ARE durable; only the cursor is not. Refusing
            # to acknowledge is the safe half: the collector retries, the
            # store dedups, and the ACK arrives once the cursor commits.
            self.log_message('cursor not persisted for %s; withholding ack',
                             station)
            return self.fail(500, 'cursor_write_failed', retry_after=30)

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
    # socketserver defaults this to 5. A burst of connections past that is
    # refused by the kernel before we ever see it, so the caller gets a
    # connection reset rather than our 503 -- an outcome the connection cap
    # exists to avoid. Queue them and let the cap do the shedding.
    request_queue_size = 128

    def __init__(self, addr, cfg, stores):
        super().__init__(addr, Handler)
        self.cfg = cfg
        self.stores = stores
        self.guard = Guard()
        # Per-station stats. Sharing one set let a secondary station advance the
        # primary's apparent freshness and replace its collector metadata, i.e.
        # a test station could make a dead primary look healthy.
        self.stats_by_station = {name: Stats() for name in stores}
        self.primary = cfg.get('primary')
        self.degraded = dict(cfg.get('degraded') or {})
        # Bound concurrent handlers. Acquired in process_request, BEFORE the
        # thread is spawned: acquiring inside the handler would cap nothing,
        # since ThreadingHTTPServer has already created the thread by then.
        self.slots = threading.BoundedSemaphore(cfg.get('max_conns', 16))
        self._status_lock = threading.Lock()
        # Reading the cursor and recording the new one must be one transaction,
        # or two concurrent requests evaluate contiguity against the same value
        # and both advance it.
        self._cursor_locks = {name: threading.Lock() for name in stores}
        # Seed here, not in main(): a Server constructed anywhere -- including
        # in a test standing in for a restart -- must come up with the same
        # cursor the previous process left behind.
        for name in stores:
            last_ack, gaps = self.load_cursor(name)
            self.stats_by_station[name].last_seq = last_ack
            self.stats_by_station[name].seq_gaps = gaps
        # obs_age_s must keep growing when the producer dies, or a dead feed
        # looks frozen-but-fresh -- the exact failure this project exists to
        # remove. The producer refreshes its own document; nothing else may
        # write it, since a read-modify-write from outside races this.
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)
        self._beat_stop = threading.Event()
        self._beat.start()
        self.shed_q = queue.Queue(maxsize=cfg.get('shed_queue',
                                                   SHED_QUEUE))
        self._shed_closed = False
        self._shed_gate = threading.Lock()
        self._retry_cursor = 0
        self._shedder = threading.Thread(target=self._shed_worker, daemon=True)
        self._shedder.start()

    def _heartbeat(self):
        while not self._beat_stop.wait(HEARTBEAT_S):
            try:
                self.retry_degraded()
                self.write_status()
            except Exception as e:
                sys.stderr.write('heartbeat: %s\n' % e)

    def server_close(self):
        # Stop the heartbeat before the socket goes. Left running, it outlives
        # the server and keeps writing into a directory that may already be
        # gone -- harmless in production, but every test harness leaked one.
        self._beat_stop.set()
        try:
            self._beat.join(timeout=HEARTBEAT_S + 5)
        except RuntimeError:
            pass
        # Close anything still queued rather than waiting behind it: the
        # sentinel goes to the back of the queue, so a full queue or a slow
        # peer would otherwise leave the worker and its sockets alive after
        # server_close returned.
        with self._shed_gate:
            self._shed_closed = True
        while True:
            try:
                pending = self.shed_q.get_nowait()
            except queue.Empty:
                break
            if pending is not None:
                try:
                    self.shutdown_request(pending)
                except Exception:
                    pass
        try:
            self.shed_q.put_nowait(None)
            self._shedder.join(timeout=SHED_TIMEOUT_S + 5)
        except (queue.Full, RuntimeError):
            pass
        super().server_close()

    def retry_degraded(self):
        """Re-test degraded days, a bounded number per heartbeat.

        Each entry names its own day, and that day is what gets rechecked --
        not whatever reconcile_all happens to walk. A day that stays broken
        long enough to fall outside the recent-files window would otherwise
        stop being examined and then look recovered because nothing looked.

        Bounded per heartbeat, with a short lock timeout, because fourteen bad
        days at ten seconds each would block the heartbeat well past the
        shutdown join.
        """
        # Corruption found while serving lives in the store, not here. It was
        # displayed by current_degraded() but never retried, so it could only
        # ever clear by chance.
        for name, store in self.stores.items():
            for day, msg in store.degraded().items():
                self.degraded.setdefault('%s/%s' % (name, day), msg)

        if not self.degraded:
            return
        # Round-robin. Taking the first four every beat retried the same four
        # permanent failures for ever and starved everything after them.
        keys = sorted(self.degraded)
        start = self._retry_cursor % len(keys)
        # Wrap, but never check the same day twice in one beat: with fewer
        # entries than the window, (keys + keys) repeated them.
        n = min(RETRY_PER_BEAT, len(keys))
        window = [keys[(start + i) % len(keys)] for i in range(n)]
        self._retry_cursor = (start + n) % len(keys)
        for key in window:
            name, _, day = key.partition('/')
            store = self.stores.get(name)
            if store is None:
                continue
            if not day:
                # A whole-station failure (reconcile_all itself blew up).
                try:
                    _repaired, errors = store.reconcile_all()
                except Exception as e:
                    self.degraded[key] = str(e)
                    continue
                if errors:
                    self.degraded[key] = errors[0][1]
                else:
                    self.degraded.pop(key, None)
                    for d, msg in store.degraded().items():
                        self.degraded['%s/%s' % (name, d)] = msg
                continue
            try:
                store.recheck_day(day)
            except Exception as e:
                self.degraded[key] = str(e)      # still broken; keep the entry
                continue
            if day in store.degraded():
                self.degraded[key] = store.degraded()[day]
            else:
                self.degraded.pop(key, None)

    # ------------------------------------------------------------ capacity

    def process_request(self, request, client_address):
        if self._shed_closed:
            # Shutting down: nothing may join the queue behind the sentinel.
            self.shutdown_request(request)
            return
        if not self.slots.acquire(blocking=False):
            # Hand the refusal to the shed worker. Writing the 503 and then
            # closing here would block the accept loop -- exactly when we are
            # already over capacity -- and closing on a half-sent request body
            # sends RST, which discards the 503 the client never got to read.
            # The check and the insertion must be one step, or server_close
            # can drain the queue and add its sentinel in between and this
            # request is stranded behind it.
            with self._shed_gate:
                try:
                    if self._shed_closed:
                        raise queue.Full
                    self.shed_q.put_nowait(request)
                except queue.Full:
                    self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # Thread creation can fail (RuntimeError: can't start new thread).
            # Without this the slot is held for ever and the accepted socket
            # is never closed, so the cap ratchets down to zero.
            self.slots.release()
            self.shutdown_request(request)
            raise

    def _shed_worker(self):
        """Refuse over-capacity connections without stalling the accept loop.

        One thread, not one per refusal: the whole point of the cap is not to
        create a thread per connection. The sequence matters -- write the
        response, half-close so the peer sees our bytes followed by EOF, then
        drain what it was still sending. Closing on an unread body makes the
        kernel send RST and the client reports a broken pipe rather than the
        503 we wrote.

        The budget is an ABSOLUTE deadline, not a per-recv timeout. A socket
        timeout restarts on every successful read, so a peer trickling a byte
        every few seconds could hold the only shed worker until it reached the
        drain cap -- and once the queue behind it filled, every later refusal
        degraded to the bare close this worker exists to avoid.
        """
        while True:
            request = self.shed_q.get()
            if request is None:
                return
            deadline = time.monotonic() + SHED_TIMEOUT_S
            try:
                request.settimeout(SHED_TIMEOUT_S)
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                                b'Retry-After: 5\r\n'
                                b'Content-Length: 0\r\n'
                                b'Connection: close\r\n\r\n')
                try:
                    request.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                drained = 0
                while drained < SHED_DRAIN_CAP:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    request.settimeout(left)
                    chunk = request.recv(65536)
                    if not chunk:
                        break
                    drained += len(chunk)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    # --------------------------------------------------------------- stats

    @property
    def stats(self):
        return self.stats_by_station[self.primary]

    def cursor_path(self, station):
        return os.path.join(self.cfg['statedir'], station, 'cursor.json')

    def load_cursor(self, station):
        """The acknowledged sequence, from disk.

        This is NOT derivable from storage. Durable and acknowledged are
        different: a batch with durable seq 5 and 7 and an undeclared gap
        acknowledges only 5, yet both are in the JSONL. Seeding the cursor from
        the newest stored observation would launder 7 -- the record we
        deliberately refused -- straight into the cursor on the next restart.
        """
        try:
            with open(self.cursor_path(station)) as f:
                doc = json.load(f)
        except FileNotFoundError:
            return None, 0                      # genuinely fresh
        except (OSError, ValueError) as e:
            # Unreadable is NOT the same as absent. Starting from a cold
            # cursor would accept any sequence the collector offered next.
            raise CursorError('%s: %s' % (self.cursor_path(station), e))
        if not isinstance(doc, dict):
            raise CursorError('%s: not an object' % self.cursor_path(station))
        last = doc.get('last_ack')
        gaps = doc.get('seq_gaps')
        if last is not None and (not is_int(last) or last < 0):
            raise CursorError('%s: last_ack is %r'
                              % (self.cursor_path(station), last))
        if gaps is not None and (not is_int(gaps) or gaps < 0):
            # Silently zeroing this lost the record of how much data we know
            # went missing.
            raise CursorError('%s: seq_gaps is %r'
                              % (self.cursor_path(station), gaps))
        return (last if is_int(last) else None, gaps if is_int(gaps) else 0)

    def save_cursor(self, station, last_ack, gaps):
        """Commit the cursor. Returns True only if it is genuinely on disk.

        The caller must not acknowledge anything this fails to persist. A
        swallowed error here means: we tell the collector seq 6 is safe, it
        drops its copy, we restart, reload the older cursor, and then refuse
        seq 7 for ever because nobody has 6 any more.
        """
        path = self.cursor_path(station)
        d = os.path.dirname(path)
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix='.cursor-', suffix='.tmp')
        except OSError as e:
            sys.stderr.write('cursor write failed for %s: %s\n' % (station, e))
            return False
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump({'station': station, 'last_ack': last_ack,
                           'seq_gaps': gaps, 'written_at': now_iso()}, f)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            # The rename itself has to survive a crash. If THIS fails the new
            # cursor may be on disk while memory still holds the old one, so
            # report failure and let the caller withhold the acknowledgement:
            # a cursor ahead of memory only ever costs a duplicate retry.
            _fsync_dir(d)
            return True
        except OSError as e:
            sys.stderr.write('cursor write failed for %s: %s\n' % (station, e))
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def cursor_lock(self, station):
        return self._cursor_locks.setdefault(station, threading.Lock())

    def last_ack(self, station):
        st = self.stats_by_station.get(station)
        if st is None:
            return None
        with st.lock:
            return st.last_seq

    def record(self, station, accepted, dup, rejected, newest_utc, seq,
               collector, gaps=0):
        st = self.stats_by_station.get(station)
        if st is None:
            return False
        if seq is not None or gaps:
            # Persist BEFORE advancing in memory, so a failed write leaves the
            # in-memory cursor and the on-disk cursor agreeing. Advancing
            # first would leave them divergent until the next restart silently
            # rewound us.
            with st.lock:
                new_last = seq if st.last_seq is None else max(seq,
                                                               st.last_seq)
                new_gaps = st.seq_gaps + gaps
                changed = (new_last != st.last_seq or new_gaps != st.seq_gaps)
            # A replay at or below the cursor changes nothing. Rewriting the
            # file anyway meant a read-only state directory turned an
            # acknowledgement that needed no write into a permanent 500.
            if changed and not self.save_cursor(station, new_last, new_gaps):
                return False
        st.record(accepted, dup, rejected, newest_utc, seq, collector,
                  gaps=gaps)
        # Only the primary station owns the public status document.
        if station == self.primary:
            self.write_status()
        return True

    def snapshot(self, station=None):
        st = self.stats_by_station[station or self.primary]
        with st.lock:
            return dict(accepted=st.accepted, duplicates=st.duplicates,
                        rejected=st.rejected, last_post=st.last_post,
                        last_obs_utc=st.last_obs_utc, last_seq=st.last_seq,
                        seq_gaps=st.seq_gaps, collector=dict(st.collector))

    def write_status(self):
        """Publish the primary station's status.

        Absolute timestamps are authoritative; obs_age_s is a convenience that
        goes stale if nothing rewrites it, so the heartbeat thread rewrites
        this document every HEARTBEAT_S and a dead producer shows a growing age
        rather than a frozen one. Nothing outside this process may write it.

        The snapshot is taken INSIDE the lock and held through the rename.
        Taking it outside let a slow writer read an old snapshot, get
        overtaken by a newer one, and then replace the file with its stale
        copy -- publishing a regression in the very field that proves
        liveness.
        """
        with self._status_lock:
            self._write_status_locked()

    def _write_status_locked(self):
        st = self.snapshot()
        now = time.time()
        last_obs = st['last_obs_utc']
        last_post = st['last_post']
        doc = {
            'station': self.primary,
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
            # Name matches what feed_watch reads. A rename on one side only
            # made authentication failures invisible to the watchdog.
            'auth_failures_recent': self.guard.recent_failures(now),
            'source': (st['collector'] or {}).get('source', 'unknown'),
            'collector': st['collector'],
            'degraded': self.current_degraded(),
        }
        path = self.cfg['status']
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

    def current_degraded(self):
        """Days any store currently cannot read, plus startup failures."""
        out = dict(self.degraded)
        for name, store in self.stores.items():
            for day, msg in store.degraded().items():
                out['%s/%s' % (name, day)] = msg
        return out


def is_int(v):
    """A real integer. bool is a subclass of int and must not qualify."""
    return isinstance(v, int) and not isinstance(v, bool)


def ack_from_dispositions(obs, disp, last_ack=None, loss_from=None,
                          loss_to=None):
    """Highest safely-acknowledgeable seq, the newest durable instant, gaps.

    Returns (ack, newest_utc, gaps, problem).

    Three rules, and they are not the same rule:

      * ADVANCING past the cursor requires exactly +1. Acknowledging seq 7 when
        6 was never seen tells a cursor-based collector to discard 6, and it
        makes no difference whether the hole was inside one batch or between
        two requests.
      * RE-acknowledging at or below the cursor is always allowed for a durable
        record. This is the ordinary lost-response retry: the collector never
        saw our 200, resends, and the record comes back as a duplicate. Demanding
        +1 there refuses the retry for ever and wedges the collector -- which is
        exactly the stall this rule used to cause.
      * Within one request the sequence must strictly increase, so a
        mis-ordered batch is refused rather than partly acknowledged.

    A collector that drops spool files must SAY SO, by declaring
    `data_loss: {from, to}`. That authorises exactly one jump over exactly that
    range, and the skipped count is recorded as seq_gaps so the loss is in the
    record rather than inferred from silence.

    Durability is taken per input, never from a set of UTC instants: two inputs
    can share an instant, and a set cannot say which of them is on disk.
    """
    newest = None
    for o, d in zip(obs, disp):
        # newest_utc covers every durable record, not merely the acknowledged
        # prefix -- a good observation after a malformed one is still on disk,
        # and reporting stale freshness because of the malformed one hides it.
        if d not in DURABLE or not isinstance(o, dict):
            continue
        try:
            u = utc_key(parse_obs_ts(o.get('obs_ts')))
        except StoreError:
            continue
        if newest is None or u > newest:
            newest = u

    ack = None
    gaps = 0
    problem = None
    cursor = last_ack
    in_batch = None
    loss_used = False

    def take_loss(upto, after):
        """Consume a declared loss range covering EXACTLY (after, upto).

        Both ends matter. Checking only the end let a declaration of 7..9
        against cursor 5 authorise a jump to 10 while seq 6 was never
        mentioned, and 1..9 overlap history that was already acknowledged.
        `after` is None only from a cold cursor, where there is no history to
        overlap and only the end can be checked.
        """
        nonlocal gaps, loss_used
        if loss_used or not (is_int(loss_from) and is_int(loss_to)):
            return False
        if loss_to != upto - 1 or loss_to < loss_from:
            return False
        if after is not None and loss_from != after + 1:
            return False
        gaps += loss_to - loss_from + 1
        loss_used = True
        return True

    for o, d in zip(obs, disp):
        if d not in DURABLE:
            problem = problem or ('acknowledgement stops at a %s record' % d)
            break
        sq = o.get('seq') if isinstance(o, dict) else None
        if not is_int(sq):
            problem = problem or 'a durable record carries no integer seq'
            break
        if sq < 0:
            problem = problem or 'seq %s is negative' % sq
            break
        if in_batch is not None and sq <= in_batch:
            problem = problem or ('seq %s does not increase after %s within '
                                  'the request' % (sq, in_batch))
            break

        if cursor is None:
            # First sequence this station has ever had acknowledged. A loss
            # declared against that first record still has to be counted;
            # ignoring it recorded the gap as zero and the loss vanished.
            take_loss(sq, None)
            cursor = sq
        elif sq == cursor + 1:
            cursor = sq                                # the ordinary case
        elif sq <= cursor:
            # Re-acknowledgement is ONLY for a record already on disk. A
            # newly STORED record below the cursor is not a replay, it is a
            # sequence conflict -- a collector reinstalled with a reset
            # counter, or a second collector on the same station. Ack it and
            # the reset is laundered and the numbering silently collides.
            if d != DUPLICATE:
                problem = problem or (
                    'seq %s is at or below the cursor %s but is newly stored: '
                    'a reset or a second collector' % (sq, cursor))
                break
        elif take_loss(sq, cursor):
            cursor = sq
        else:
            problem = problem or (
                'seq %s does not follow %s and no data_loss covers the gap'
                % (sq, cursor))
            break

        in_batch = sq
        ack = sq

    return ack, newest, gaps, problem


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
    p.add_argument('--allow-crash-injection', action='store_true',
                   help=argparse.SUPPRESS)
    p.add_argument('--max-conns', type=int, default=16)
    p.add_argument('--enable-echo', action='store_true')
    args = p.parse_args(argv)

    # CYC_STORE_CRASH_AT makes the store os._exit mid-write. It exists for the
    # crash-interleaving tests. A stray Environment= line in a unit file would
    # otherwise give a permanent exit/restart loop that looks like a crash bug,
    # so refuse to start rather than obey it by accident.
    if os.environ.get('CYC_STORE_CRASH_AT') and not args.allow_crash_injection:
        raise SystemExit('CYC_STORE_CRASH_AT is set (%r). That makes the store '
                         'exit mid-write. Unset it, or pass '
                         '--allow-crash-injection if this really is a test.'
                         % os.environ['CYC_STORE_CRASH_AT'])


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
           'primary': primary, 'statedir': args.statedir}

    # Two stations must never name the same tree: their locks are
    # station-specific, so they would mutate the same JSONL and CSV under
    # different locks and corrupt each other.
    seen_paths = {}
    for name, logdir in stations.items():
        real = os.path.realpath(logdir)
        if real in seen_paths:
            raise SystemExit('stations %r and %r both map to %s'
                             % (seen_paths[real], name, real))
        seen_paths[real] = name

    stores = {}
    for name, logdir in stations.items():
        stores[name] = Store(logdir, os.path.join(args.statedir, name),
                             station=name, min_obs_ts=args.min_obs_ts)

    # Repair anything a previous crash left inconsistent, including days no
    # later request would touch. Failures are carried into health rather than
    # logged once and forgotten: serving 200 over unreadable storage is the
    # silent failure this project exists to remove.
    degraded = {}
    for name, store in stores.items():
        try:
            repaired, errors = store.reconcile_all()
            if repaired:
                sys.stderr.write('reconciled %s: %s\n'
                                 % (name, ', '.join(repaired)))
            for day, msg in errors:
                degraded['%s/%s' % (name, day)] = msg
                sys.stderr.write('DEGRADED %s/%s: %s\n' % (name, day, msg))
        except Exception as e:
            degraded[name] = str(e)
            sys.stderr.write('DEGRADED reconcile_all(%s): %s\n' % (name, e))

    cfg['degraded'] = degraded
    try:
        srv = Server((args.host, args.port), cfg, stores)
    except CursorError as e:
        raise SystemExit(
            'cursor unreadable: %s\n'
            'Refusing to start: a cold cursor would accept whatever sequence '
            'the collector offered next. Fix or delete the file deliberately.'
            % e)

    # Seed from storage before serving. Stats are in-memory, so without this a
    # restart makes status.json report "no observation has ever been recorded"
    # until the next one arrives -- which feed_watch correctly treats as an
    # alarm. Every deploy would otherwise fire a false alert.
    for name, store in stores.items():
        st = srv.stats_by_station[name]
        try:
            last = store.last_observation()
        except Exception:
            last = None
        if last:
            st.last_obs_utc = int(last['utc'])
            sys.stderr.write('seeded %s from storage: last observation %s\n'
                             % (name,
                                datetime.fromtimestamp(st.last_obs_utc, TZ)
                                .isoformat(timespec='seconds')))
        # The cursor was seeded by Server.__init__ from its own file, never
        # from storage: a durable record is not necessarily an acknowledged
        # one, and inferring it from the newest observation would launder a
        # record we explicitly refused to acknowledge.
        if st.last_seq is not None:
            sys.stderr.write('seeded %s cursor: last_ack=%s seq_gaps=%s\n'
                             % (name, st.last_seq, st.seq_gaps))

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
