#!/usr/bin/env python3
"""Collector and shipper, end to end against a real receiver.

Runs the whole chain -- simulated console, spool, HMAC, HTTP, store -- so the
pieces are proved against each other rather than against mocks of each other.
The receiver is a real cyc_ingest.Server on a loopback port.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cyc_ingest  # noqa: E402
from cyc_ingest import Server, load_keys  # noqa: E402
from cyc_store import Store  # noqa: E402
from spool import Spool  # noqa: E402
import ship  # noqa: E402

KEY = 'aa' * 32
STATION = 'cyc1'
FAILURES = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILURES.append(what)


class Rig:
    """A receiver plus a collector spool, wired together."""

    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='collect-test-')
        self.logs = os.path.join(self.d, 'wind_logs')
        self.state = os.path.join(self.d, 'state')
        self.spooldir = os.path.join(self.d, 'send')
        os.makedirs(self.logs)
        self.keyfile = os.path.join(self.d, 'keys')
        with open(self.keyfile, 'w') as f:
            f.write('%s\t%s\n' % (STATION, KEY))
        cfg = {'keys': load_keys(self.keyfile),
               'status': os.path.join(self.d, 'status.json'),
               'enable_echo': False, 'max_conns': 8, 'primary': STATION,
               'degraded': {}, 'statedir': self.state}
        self.srv = Server(('127.0.0.1', 0), cfg,
                          {STATION: Store(self.logs, self.state,
                                          station=STATION)})
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.d, ignore_errors=True)
        return False

    @property
    def url(self):
        return 'http://127.0.0.1:%d/ingest/obs' % self.port

    def collect(self, seconds=1.5, period=0.05, extra=None):
        cmd = [sys.executable, os.path.join(HERE, 'collect.py'), '--sim',
               '--spool', self.spooldir,
               '--heartbeat', os.path.join(self.d, 'hb.json'),
               '--period', str(period), '--seconds', str(seconds)]
        return subprocess.run(cmd + (extra or []), capture_output=True,
                              text=True, timeout=120)

    def ship(self, limit=50):
        return ship.main(['--url', self.url, '--station', STATION,
                          '--keyfile', self.keyfile, '--spool', self.spooldir,
                          '--limit', str(limit), '--quiet'])

    def stored(self):
        recs = []
        for root, _dirs, names in os.walk(self.logs):
            for n in names:
                if n.endswith('.jsonl'):
                    with open(os.path.join(root, n)) as f:
                        recs += [json.loads(line) for line in f if line.strip()]
        return recs


def test_end_to_end():
    print('\n== console -> spool -> receiver ==')
    with Rig() as r:
        run = r.collect(seconds=2.5)
        check(run.returncode == 0, 'the collector exited cleanly (%s)'
              % run.returncode)
        pend = Spool(r.spooldir).pending()
        check(len(pend) >= 2, 'observations were spooled (%d)' % len(pend))

        rc = r.ship()
        check(rc == 0, 'the shipper succeeded (rc=%s)' % rc)
        check(not Spool(r.spooldir).pending(),
              'and the spool is drained -- entries are deleted only on a real '
              'acknowledgement')

        stored = r.stored()
        check(len(stored) == len(pend),
              'every spooled observation reached storage (%d of %d)'
              % (len(stored), len(pend)))
        first = stored[0]
        check(first.get('src') == 'loop', 'stored as a LOOP observation')
        check(first.get('wind_avg_kt') == 11.3 and
              first.get('wind_gust_kt') == 16.5,
              'with the 10-minute average and gust from LOOP2 offsets 18 and '
              '22 (%s, %s)' % (first.get('wind_avg_kt'),
                               first.get('wind_gust_kt')))
        cur = json.load(open(os.path.join(r.state, STATION, 'cursor.json')))
        check(cur['last_ack'] == max(p[0] for p in pend),
              'and the receiver cursor matches the last spooled sequence (%s)'
              % cur['last_ack'])


def test_heartbeat_is_written():
    print('\n== the heartbeat the watchdog reads ==')
    with Rig() as r:
        r.collect(seconds=1.2)
        hb = json.load(open(os.path.join(r.d, 'hb.json')))
        check(hb.get('state') == 'stopped', 'final state recorded (%s)'
              % hb.get('state'))
        check(isinstance(hb.get('pid'), int) and hb.get('epoch'),
              'with a pid and a timestamp, which is what lets the watchdog '
              'tell a working collector from a wedged one')
        check(hb.get('observations', 0) > 0,
              'and a count of what it did (%s)' % hb.get('observations'))


def test_shipper_stops_at_the_first_failure():
    print('\n== shipping stops rather than skipping ahead ==')
    with Rig() as r:
        r.collect(seconds=2.5)
        pend = Spool(r.spooldir).pending()
        check(len(pend) >= 3, 'several entries queued (%d)' % len(pend))

        # Wrong key: every post is refused.
        bad = os.path.join(r.d, 'badkey')
        with open(bad, 'w') as f:
            f.write('%s\t%s\n' % (STATION, 'bb' * 32))
        rc = ship.main(['--url', r.url, '--station', STATION, '--keyfile', bad,
                        '--spool', r.spooldir, '--quiet'])
        check(rc == 1, 'the shipper reports failure (rc=%s)' % rc)
        check(len(Spool(r.spooldir).pending()) == len(pend),
              'and NOTHING was deleted -- a refused entry is not a shipped one')

        rc = r.ship()
        check(rc == 0, 'with the right key it drains (rc=%s)' % rc)
        check(not Spool(r.spooldir).pending(), 'spool empty')


def test_declared_loss_is_sent_and_cleared():
    print('\n== a pruned spool declares its loss to the receiver ==')
    with Rig() as r:
        r.collect(seconds=2.5)
        s = Spool(r.spooldir)
        pend = s.pending()
        check(len(pend) >= 3, 'entries queued (%d)' % len(pend))

        # Drop the oldest by hand, exactly as prune() would under disk
        # pressure, and record the loss.
        victim_seq, victim_path, _doc = pend[0]
        os.unlink(victim_path)
        s._record_loss(victim_seq, victim_seq)
        check(s.pending_loss() == {'from': victim_seq, 'to': victim_seq},
              'the loss is recorded')

        rc = r.ship()
        check(rc == 0, 'the remaining entries ship (rc=%s)' % rc)
        check(Spool(r.spooldir).pending_loss() is None,
              'and the declaration is cleared once accepted')
        st = json.load(open(os.path.join(r.d, 'status.json')))
        check(st['seq_gaps'] == 1,
              'the receiver counted the declared missing position (%s)'
              % st['seq_gaps'])


def test_resume_when_the_spool_is_behind_the_cursor():
    print('\n== an entry older than the receiver can verify ==')
    with Rig() as r:
        r.collect(seconds=2.0)
        r.ship()
        cursor = r.srv.last_ack(STATION)
        check(cursor > 0, 'the receiver has a cursor (%s)' % cursor)

        # Forget the ledger, as pruning would after a long backlog, then queue
        # an entry with a sequence the receiver can no longer verify.
        r.srv.ledgers[STATION].clear()
        s = Spool(r.spooldir)
        s._next = 1
        s.append([{'obs_ts': '2026-08-03T05:00:00+10:00', 'src': 'loop',
                   'wind_kt': 1.0}])
        rc = r.ship()
        check(rc == 0,
              'the shipper recovers rather than retrying for ever (rc=%s)' % rc)
        check(not Spool(r.spooldir).pending(),
              'the unverifiable entry is dropped so the queue behind it moves')


def main():
    for fn in [test_end_to_end, test_heartbeat_is_written,
               test_shipper_stops_at_the_first_failure,
               test_declared_loss_is_sent_and_cleared,
               test_resume_when_the_spool_is_behind_the_cursor]:
        fn()
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
