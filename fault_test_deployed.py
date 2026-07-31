"""Data-integrity fault injection against the DEPLOYED code.

Runs a second instance of ~/wind/cyc_ingest.py as a real subprocess on a spare
port with its own tree, so these tests exercise the deployed files over real
HTTP without polluting the live shadow data.

Covers the cases that would silently corrupt the historical record: crash
between the JSONL and CSV writes, out-of-order backfill, replay, the DST fold,
and the cutover instant.
"""
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

CODE = '/home/lbgweather/lib/cyc'    # service code lives outside DocumentRoot
sys.path.insert(0, CODE)
from cyc_ingest import sign            # noqa: E402
from cyc_store import parse_obs_ts, utc_key  # noqa: E402

PORT = 8791
FAILURES = []
KEY = secrets.token_hex(32)


def check(cond, what, detail=''):
    print('  %s %s%s' % ('ok  ' if cond else 'FAIL', what,
                         '' if not detail else '   [%s]' % detail))
    if not cond:
        FAILURES.append(what)


class Instance:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='cycfault-')
        self.logs = os.path.join(self.d, 'logs')
        self.state = os.path.join(self.d, 'state')
        self.status = os.path.join(self.d, 'status.json')
        self.keyfile = os.path.join(self.d, 'keys')
        os.makedirs(self.logs)
        with open(self.keyfile, 'w') as f:
            f.write('cyc1\t%s\n' % KEY)
        self.start()
        return self

    def start(self):
        self.p = subprocess.Popen(
            ['/usr/bin/python3', os.path.join(CODE, 'cyc_ingest.py'),
             '--host', '127.0.0.1', '--port', str(PORT),
             '--keyfile', self.keyfile,
             '--station', 'cyc1=' + self.logs,
             '--statedir', self.state, '--status', self.status],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    'http://127.0.0.1:%d/ingest/health' % PORT, timeout=2).read()
                return
            except Exception:
                time.sleep(0.25)
        raise SystemExit('instance did not come up')

    def kill(self, sig=signal.SIGKILL):
        self.p.send_signal(sig)
        self.p.wait(timeout=10)

    def __exit__(self, *exc):
        try:
            self.p.send_signal(signal.SIGTERM)
            self.p.wait(timeout=10)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass
        shutil.rmtree(self.d, ignore_errors=True)
        return False

    def post(self, obslist, timeout=30):
        path = '/ingest/obs'
        doc = {'schema': 1, 'station': 'cyc1',
               'collector': {'version': 'fault', 'spool_backlog': 0,
                             'source': 'fault-test'},
               'obs': obslist}
        body = json.dumps(doc).encode()
        ts = int(time.time())
        nonce = secrets.token_hex(16)
        req = urllib.request.Request(
            'http://127.0.0.1:%d%s' % (PORT, path), data=body, method='POST')
        for k, v in [('Content-Type', 'application/json'),
                     ('X-CYC-Station', 'cyc1'),
                     ('X-CYC-Timestamp', str(ts)),
                     ('X-CYC-Nonce', nonce),
                     ('X-CYC-Signature',
                      sign(bytes.fromhex(KEY), 'POST', path, 'cyc1', ts, nonce, body))]:
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def csv(self, y, m, d):
        p = os.path.join(self.logs, '%04d' % y, '%02d' % m, '%02d.csv' % d)
        return open(p, 'rb').read() if os.path.exists(p) else b''

    def jsonl_count(self, y, m, d):
        p = os.path.join(self.logs, '%04d' % y, '%02d' % m, '%02d.jsonl' % d)
        if not os.path.exists(p):
            return 0
        return sum(1 for line in open(p) if line.strip())


def ago(seconds):
    """An ISO timestamp `seconds` in the past, with an explicit offset.

    Fixtures must be in the past: future-dated observations are refused, which
    is itself deliberate -- a collector with a bad clock would otherwise write
    records that look perpetually fresh.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    t = datetime.now(ZoneInfo('Australia/Sydney')) - timedelta(seconds=seconds)
    return t.replace(microsecond=0).isoformat(timespec='seconds')


def obs(ts, seq, wind=1.0):
    return {'obs_ts': ts, 'seq': seq, 'src': 'loop', 'wind_kt': wind,
            'wind_avg_kt': 0.9, 'wind_dir': 270, 'wind_gust_kt': 2.0,
            'temp_c': 13.0, 'humidity': 50}


print('== out-of-order backfill (6 hours late) ==')
with Instance() as inst:
    recent, later, old = ago(120), ago(60), ago(3600)
    inst.post([obs(recent, 1, 1.0), obs(later, 2, 2.0)])
    code, body = inst.post([obs(old, 3, 6.0)])
    check(code == 200 and body['accepted'] == 1, 'backfill accepted')
    y, m, d = [int(x) for x in old[:10].split('-')]
    lines = inst.csv(y, m, d).decode().rstrip('\r\n').split('\r\n')
    stamps = [l.split(',')[0] for l in lines[1:]]
    check(stamps == sorted(stamps), 'CSV re-sorted, not appended', str(stamps))
    check(stamps[0] == old[11:19].join(['%s ' % old[:10], '']).strip()
          or stamps[0].endswith(old[11:19]),
          'hour-old record placed first (%s)' % stamps[0])
    check(len(lines) == 4, 'header + 3 rows')

print()
print('== replay is byte-identical ==')
with Instance() as inst:
    a, b = ago(120), ago(60)
    batch = [obs(a, 1), obs(b, 2)]
    inst.post(batch)
    y, m, d = [int(x) for x in a[:10].split('-')]
    before = inst.csv(y, m, d)
    code, body = inst.post(batch)
    check(body['accepted'] == 0 and body['duplicates'] == 2,
          'replay -> 0 accepted, 2 duplicates')
    check(inst.csv(y, m, d) == before, 'CSV unchanged byte-for-byte')

print()
print('== DST fall-back fold, real Canberra dates ==')
with Instance() as inst:
    early = '2026-04-05T02:00:03+11:00'
    late = '2026-04-05T02:00:03+10:00'
    code, body = inst.post([obs(early, 1, 0.9), obs(late, 2, 0.0)])
    check(body['accepted'] == 2, 'both folds stored, neither a duplicate')
    lines = inst.csv(2026, 4, 5).decode().rstrip('\r\n').split('\r\n')
    folds = [l for l in lines if l.startswith('2026-04-05 02:00:03,')]
    check(len(folds) == 2, 'both 02:00:03 rows present')
    check(folds[0].split(',')[1] == '0.9' and folds[1].split(',')[1] == '0.0',
          'AEDT fold sorts before AEST fold, values distinct')

print()
print('== SIGKILL mid-write, then retry ==')
with Instance() as inst:
    base = ago(7200)
    y, m, d = [int(x) for x in base[:10].split('-')]
    inst.post([obs(base, 1)])
    before_rows = inst.jsonl_count(y, m, d)

    # Kill hard while a large batch is in flight.
    big = [obs(ago(7200 - 60 * i), 100 + i) for i in range(1, 61)]
    import threading
    res = {}

    def fire():
        try:
            res['r'] = inst.post(big, timeout=10)
        except Exception as e:
            res['e'] = repr(e)
    t = threading.Thread(target=fire)
    t.start()
    time.sleep(0.05)
    inst.kill()
    t.join(timeout=15)
    # NOT check(True): that could never fail. Assert what actually happened.
    killed = inst.p.poll() is not None
    check(killed, 'service is dead after SIGKILL (client saw %s)'
          % ('error' if 'e' in res else 'status %s' % res.get('r', ('?',))[0]))

    inst.start()          # restart, as systemd Restart=always would
    code, body = inst.post(big)
    check(code == 200, 'retry after restart accepted')

    rows = inst.jsonl_count(y, m, d)
    csv_rows = len(inst.csv(y, m, d).decode().rstrip('\r\n').split('\r\n')) - 1
    check(rows == csv_rows,
          'JSONL and CSV agree after crash + retry (%d vs %d)' % (rows, csv_rows))
    check(rows == before_rows + len(big),
          'no duplication and nothing lost (%d rows)' % rows)

print()
print('== cutover instant refuses pre-cutover data ==')
with Instance() as inst:
    inst.post([obs(ago(600), 1)])
    # restart with a cutover instant set
    inst.kill(signal.SIGTERM)
    cut = utc_key(parse_obs_ts(ago(300)))
    inst.p = subprocess.Popen(
        ['/usr/bin/python3', os.path.join(CODE, 'cyc_ingest.py'),
         '--host', '127.0.0.1', '--port', str(PORT),
         '--keyfile', inst.keyfile,
         '--station', 'cyc1=' + inst.logs,
         '--statedir', inst.state, '--status', inst.status,
         '--min-obs-ts', str(cut)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen('http://127.0.0.1:%d/ingest/health' % PORT,
                                   timeout=2).read()
            break
        except Exception:
            time.sleep(0.25)
    code, body = inst.post([obs(ago(400), 9), obs(ago(120), 10)])
    check(body['accepted'] == 1 and body['rejected'] == 1,
          'pre-cutover refused, post-cutover accepted')

print()
if FAILURES:
    print('%d FAILURE(S): %s' % (len(FAILURES), '; '.join(FAILURES)))
    sys.exit(1)
print('all data-integrity fault cases passed')
