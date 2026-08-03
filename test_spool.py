#!/usr/bin/env python3
"""Spool tests, centred on the one property the receiver depends on.

The receiver trusts the collector's `seq -> instant` assignment absolutely. If
an entry and its sequence could be written separately, a crash between them
would lose an observation with nothing on the server able to notice. These
tests are mostly about proving that window does not exist.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from spool import Spool, ENTRY_RE, LOSS_MARKER  # noqa: E402

FAILURES = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILURES.append(what)


class Tmp:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='spool-test-')
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.d, ignore_errors=True)
        return False

    def spool(self, **kw):
        return Spool(os.path.join(self.d, 'send'), **kw)


def obs(minute, seq_placeholder=None):
    return {'obs_ts': '2026-08-03T10:%02d:00+10:00' % minute, 'src': 'loop',
            'wind_kt': 5.0, 'wind_avg_kt': 4.0, 'wind_dir': 245,
            'wind_gust_kt': 8.0, 'temp_c': 14.3, 'humidity': 49}


def test_sequence_is_the_filename():
    print('\n== the sequence and the record are one write ==')
    with Tmp() as t:
        s = t.spool()
        a = s.append([obs(0)])
        b = s.append([obs(1)])
        check((a, b) == (1, 2), 'sequences start at 1 and increment (%s, %s)'
              % (a, b))

        names = sorted(n for n in os.listdir(s.dir) if ENTRY_RE.match(n))
        check(names == ['000000000001.json', '000000000002.json'],
              'the sequence IS the filename (%s)' % names)

        doc = json.load(open(os.path.join(s.dir, names[0])))
        check(doc['seq'] == 1, 'and is inside the payload too')
        check(all(o['seq'] == 1 for o in doc['obs']),
              'every observation carries it, which is what the receiver ACKs')

        # There is no moment where a record exists without its sequence, so
        # there is nothing to test for "half written" -- the temp file is not
        # an entry until os.replace makes it one.
        check(not [n for n in os.listdir(s.dir) if n.startswith('.wr-')],
              'no temp files are left behind')


def test_sequence_never_reused_across_restart():
    print('\n== a restart never reuses a sequence ==')
    with Tmp() as t:
        s = t.spool()
        for i in range(3):
            s.append([obs(i)])
        check(t.spool().append([obs(9)]) == 4,
              'a fresh Spool continues from the spool contents')

        # The counter file alone must also carry it, for a spool that has been
        # fully shipped and deleted.
        s2 = t.spool()
        for seq, path, _doc in s2.pending():
            s2.ack(seq, path)
        check(not [n for n in os.listdir(s2.dir) if ENTRY_RE.match(n)],
              'spool drained')
        check(t.spool().append([obs(10)]) == 5,
              'and an empty spool still does not go back to 1 -- reusing a '
              'sequence reads to the receiver as a collector reset')


def test_crash_between_entry_and_counter():
    print('\n== crash after the entry, before the counter ==')
    with Tmp() as t:
        s = t.spool()
        s.append([obs(0)])
        s.append([obs(1)])

        # Simulate exactly that crash: the entry is on disk, the counter file
        # never got updated. The scan has to recover the sequence.
        state_path = os.path.join(s.dir, 'spool_state.json')
        state = json.load(open(state_path))
        state['next_seq'] = 2                     # stale: entry 2 already exists
        with open(state_path, 'w') as f:
            json.dump(state, f)

        nxt = t.spool().append([obs(2)])
        check(nxt == 3,
              'the entry on disk wins over the stale counter (%d)' % nxt)


def test_torn_entry_is_quarantined_not_shipped():
    print('\n== an unreadable entry ==')
    with Tmp() as t:
        s = t.spool()
        s.append([obs(0)])
        s.append([obs(1)])
        victim = os.path.join(s.dir, '000000000001.json')
        with open(victim, 'w') as f:
            f.write('{"truncated": ')

        pend = s.pending()
        check(len(pend) == 1 and pend[0][0] == 2,
              'the readable entry is still shippable (%s)'
              % [p[0] for p in pend])
        check(os.path.exists(victim + '.bad'),
              'and the torn one is quarantined, not silently deleted')
        check(not os.path.exists(victim), 'it is out of the shipping path')


def test_ack_deletes_and_records_high_water():
    print('\n== acknowledgement ==')
    with Tmp() as t:
        s = t.spool()
        s.append([obs(0)])
        s.append([obs(1)])
        pend = s.pending()
        s.ack(pend[0][0], pend[0][1])
        check(len(s.pending()) == 1, 'the acknowledged entry is gone')
        state = json.load(open(os.path.join(s.dir, 'spool_state.json')))
        check(state.get('highest_acked') == 1,
              'and the high-water mark is recorded (%s)'
              % state.get('highest_acked'))


def test_pruning_records_the_loss():
    print('\n== dropping data under disk pressure is DECLARED ==')
    with Tmp() as t:
        s = t.spool(max_bytes=2000)
        for i in range(20):
            s.append([obs(i)])
        before = len(s.pending())
        lo, hi = s.prune()
        after = len(s.pending())
        check(after < before, 'oldest entries were dropped (%d -> %d)'
              % (before, after))
        check(lo == 1 and hi is not None and hi >= lo,
              'the dropped range starts at the oldest sequence (%s..%s)'
              % (lo, hi))

        loss = s.pending_loss()
        check(loss == {'from': lo, 'to': hi},
              'and is recorded durably for the next post (%s)' % loss)
        check(os.path.exists(os.path.join(s.dir, LOSS_MARKER)),
              'the marker survives a restart')
        check(t.spool(max_bytes=2000).pending_loss() == loss,
              'and reloads')

        # A second prune extends the range rather than replacing it: losing
        # the earlier declaration would leave a hole nothing accounts for.
        for i in range(20):
            s.append([obs(i)])
        lo2, hi2 = s.prune()
        merged = s.pending_loss()
        check(merged['from'] == lo and merged['to'] >= hi2,
              'a second prune widens the declared range (%s)' % merged)

        s.clear_loss()
        check(s.pending_loss() is None,
              'and it is cleared only once the receiver has taken it')


def test_age_pruning():
    print('\n== entries older than the retention window ==')
    with Tmp() as t:
        s = t.spool(max_age_s=60)
        s.append([obs(0)])
        s.append([obs(1)])
        old = os.path.join(s.dir, '000000000001.json')
        stale = time.time() - 3600
        os.utime(old, (stale, stale))
        lo, hi = s.prune()
        check(lo == 1 and hi == 1, 'the stale entry is dropped (%s..%s)'
              % (lo, hi))
        check(len(s.pending()) == 1, 'the fresh one stays')
        check(s.pending_loss() == {'from': 1, 'to': 1},
              'and the loss is declared, not silent')


def test_kill_during_append_leaves_no_partial_entry():
    print('\n== SIGKILL during a write ==')
    with Tmp() as t:
        d = os.path.join(t.d, 'send')
        src = (
            'import os, sys, time, signal; sys.path.insert(0, %r)\n'
            'from spool import Spool\n'
            's = Spool(%r)\n'
            'for i in range(400):\n'
            '    s.append([{"obs_ts": "2026-08-03T10:00:00+10:00",\n'
            '               "src": "loop", "wind_kt": 1.0}])\n'
            % (HERE, d))
        p = subprocess.Popen([sys.executable, '-c', src])
        time.sleep(0.35)
        p.kill()
        p.wait(timeout=10)

        s = Spool(d)
        entries = [n for n in os.listdir(d) if ENTRY_RE.match(n)]
        check(len(entries) > 0, 'some entries were written (%d)' % len(entries))
        # THE POINT: every file that exists is a complete, parseable entry
        # whose sequence matches its name. A half-written record is impossible
        # because the temp file is not an entry until os.replace.
        bad = []
        for n in sorted(entries):
            try:
                doc = json.load(open(os.path.join(d, n)))
                if doc['seq'] != int(n[:12]):
                    bad.append(n)
            except Exception as e:
                bad.append('%s (%s)' % (n, e))
        check(not bad, 'every surviving entry is complete and self-consistent '
                       '(%s)' % bad[:3])
        leftovers = [n for n in os.listdir(d) if n.startswith('.wr-')]
        check(len(leftovers) <= 1,
              'at most the one temp file the kill interrupted (%s)' % leftovers)
        check(s.append([obs(0)]) > max(int(n[:12]) for n in entries),
              'and the next sequence is past everything on disk')


def main():
    for fn in [test_sequence_is_the_filename,
               test_sequence_never_reused_across_restart,
               test_crash_between_entry_and_counter,
               test_torn_entry_is_quarantined_not_shipped,
               test_ack_deletes_and_records_high_water,
               test_pruning_records_the_loss, test_age_pruning,
               test_kill_during_append_leaves_no_partial_entry]:
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
