#!/usr/bin/env python3
"""Tests for cyc_store. Run: python3 test_cyc_store.py

Covers the cases that would silently corrupt the historical record: the DST
fall-back fold, out-of-order backfill, replay, and the crash window between the
JSONL and CSV writes.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cyc_store import Store, DayFiles, StoreError, parse_obs_ts, utc_key  # noqa: E402

FAILURES = []


def check(cond, what):
    if cond:
        print('  ok   %s' % what)
    else:
        print('  FAIL %s' % what)
        FAILURES.append(what)


def obs(ts, wind=1.0, avg=0.9, direction=270, gust=2.0, temp=13.0, hum=50):
    return {'obs_ts': ts, 'src': 'loop', 'seq': int(utc_key(parse_obs_ts(ts))),
            'wind_kt': wind, 'wind_avg_kt': avg, 'wind_dir': direction,
            'wind_gust_kt': gust, 'temp_c': temp, 'humidity': hum}


def read_csv_bytes(path):
    with open(path, 'rb') as f:
        return f.read()


def csv_lines(path):
    with open(path, 'rb') as f:
        raw = f.read().decode('utf-8')
    return raw.split('\r\n')[:-1] if raw.endswith('\r\n') else raw.split('\r\n')


class Tmp:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='cycstore-test-')
        self.logs = os.path.join(self.d, 'wind_logs')
        self.state = os.path.join(self.d, 'state')
        os.makedirs(self.logs)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.d, ignore_errors=True)
        return False


def test_basic_and_format():
    print('\n== basic write, CSV format contract ==')
    with Tmp() as t:
        s = Store(t.logs, t.state)
        a, d, r, _ = s.put([obs('2026-07-31T10:00:03+10:00'),
                            obs('2026-07-31T10:01:03+10:00')])
        check((a, d, r) == (2, 0, 0), 'accepted 2, 0 dup, 0 rejected')
        f = DayFiles(t.logs, 2026, 7, 31)
        raw = read_csv_bytes(f.csv)
        check(raw.startswith(b'timestamp,wind_kt,wind_avg_kt,wind_dir,'
                             b'wind_gust_kt,temp_c,humidity\r\n'),
              'header matches wind_logger.py contract')
        check(b'\r\n' in raw and b'\n\n' not in raw, 'CRLF line endings')
        lines = csv_lines(f.csv)
        check(lines[1].startswith('2026-07-31 10:00:03,'),
              'timestamp rendered as naive local string')
        check(len(lines) == 3, 'header + 2 rows')
        check(os.path.exists(f.jsonl), 'JSONL truth file written')


def test_dedup_replay_idempotent():
    print('\n== replay is a byte-identical no-op ==')
    with Tmp() as t:
        s = Store(t.logs, t.state)
        batch = [obs('2026-07-31T10:00:03+10:00'), obs('2026-07-31T10:01:03+10:00')]
        s.put(batch)
        f = DayFiles(t.logs, 2026, 7, 31)
        before = read_csv_bytes(f.csv)
        a, d, r, _ = s.put(batch)
        check((a, d) == (0, 2), 'replay -> accepted 0, duplicates 2')
        check(read_csv_bytes(f.csv) == before, 'CSV byte-identical after replay')

        # first-write-wins: an archive record must not overwrite a loop record
        arch = obs('2026-07-31T10:00:03+10:00', wind=99.0)
        arch['src'] = 'archive'
        s.put([arch])
        check(b'99.0' not in read_csv_bytes(f.csv),
              'first write wins - archive did not overwrite loop')


def test_out_of_order_backfill():
    print('\n== out-of-order backfill lands in sorted position ==')
    with Tmp() as t:
        s = Store(t.logs, t.state)
        s.put([obs('2026-07-31T10:00:03+10:00', wind=1.0),
               obs('2026-07-31T10:05:03+10:00', wind=5.0)])
        s.put([obs('2026-07-31T10:02:03+10:00', wind=2.0)])   # arrives late
        f = DayFiles(t.logs, 2026, 7, 31)
        lines = csv_lines(f.csv)
        stamps = [l.split(',')[0] for l in lines[1:]]
        check(stamps == sorted(stamps), 'rows sorted by time, not append order')
        check(stamps[1] == '2026-07-31 10:02:03',
              'backfilled row inserted in position, not appended')
        check(len(lines) == 4, 'header + 3 rows')


def test_dst_fold():
    print('\n== DST fall-back: two 02:00:03 rows, both preserved ==')
    # Australia/Sydney 2026-04-05: 03:00 AEDT -> 02:00 AEST, so 02:00:03 happens
    # twice. wind_logs/2026/04/05.csv already contains exactly this pattern.
    early = '2026-04-05T02:00:03+11:00'      # 2026-04-04T15:00:03Z
    late = '2026-04-05T02:00:03+10:00'       # 2026-04-04T16:00:03Z
    check(utc_key(parse_obs_ts(early)) != utc_key(parse_obs_ts(late)),
          'the two folds have different UTC instants')
    check(utc_key(parse_obs_ts(late)) - utc_key(parse_obs_ts(early)) == 3600,
          'they are exactly one hour apart')
    with Tmp() as t:
        s = Store(t.logs, t.state)
        a, d, _, _ = s.put([obs(early, wind=0.9), obs(late, wind=0.0)])
        check((a, d) == (2, 0), 'both stored, neither treated as duplicate')
        f = DayFiles(t.logs, 2026, 4, 5)
        lines = csv_lines(f.csv)
        folds = [l for l in lines if l.startswith('2026-04-05 02:00:03,')]
        check(len(folds) == 2, 'both 02:00:03 rows present in CSV')
        check(folds[0].split(',')[1] == '0.9' and folds[1].split(',')[1] == '0.0',
              'earlier (AEDT) fold sorts first, values distinct')


def test_crash_between_jsonl_and_csv():
    print('\n== crash between JSONL and CSV write is repaired ==')
    with Tmp() as t:
        s = Store(t.logs, t.state)
        s.put([obs('2026-07-31T10:00:03+10:00')])
        f = DayFiles(t.logs, 2026, 7, 31)

        # Simulate the crash window: JSONL has the record, CSV never got it.
        with open(f.jsonl, 'a', encoding='utf-8') as fh:
            rec = obs('2026-07-31T10:01:03+10:00', wind=7.7)
            dt = parse_obs_ts(rec['obs_ts'])
            rec['utc'] = utc_key(dt)
            rec['local'] = '2026-07-31 10:01:03'
            fh.write(json.dumps(rec, sort_keys=True) + '\n')
        check(b'7.7' not in read_csv_bytes(f.csv), 'CSV is short, as after a crash')

        # The retry is all-duplicates -- which is exactly when we must repair.
        a, d, _, _ = s2_put(t, [obs('2026-07-31T10:01:03+10:00', wind=7.7)])
        check(d == 1, 'retry classified as duplicate')
        check(b'7.7' in read_csv_bytes(f.csv), 'reconcile repaired the CSV')
        check(len(csv_lines(f.csv)) == 3, 'CSV now has header + 2 rows')


def s2_put(t, batch):
    """A fresh Store, as a restarted process would be (no cached index)."""
    s = Store(t.logs, t.state)
    return s.put(batch)


def test_min_obs_ts_and_validation():
    print('\n== cutover instant and timestamp validation ==')
    with Tmp() as t:
        cutover = utc_key(parse_obs_ts('2026-07-31T12:00:00+10:00'))
        s = Store(t.logs, t.state, min_obs_ts=cutover)
        a, d, r, _ = s.put([obs('2026-07-31T11:59:00+10:00'),   # before
                            obs('2026-07-31T12:00:00+10:00'),   # exactly at
                            obs('2026-07-31T12:01:00+10:00')])  # after
        check((a, r) == (1, 2), 'only the post-cutover observation accepted')

        a, d, r, det = s.put([{'obs_ts': '2026-07-31 12:05:00'}])   # naive
        check(r == 1 and 'offset' in det[0], 'naive timestamp rejected')
        a, d, r, det = s.put([{'obs_ts': 'not a date'}])
        check(r == 1, 'garbage timestamp rejected')


def test_day_boundary_by_local_date():
    print('\n== day file chosen by Canberra local date ==')
    with Tmp() as t:
        s = Store(t.logs, t.state)
        # 2026-07-31T23:59:00+10:00 is 13:59Z, still local 31 July
        s.put([obs('2026-07-31T23:59:00+10:00')])
        # 2026-08-01T00:01:00+10:00 is 14:01Z on 31 July UTC, but local 1 Aug
        s.put([obs('2026-08-01T00:01:00+10:00')])
        check(os.path.exists(DayFiles(t.logs, 2026, 7, 31).csv), 'July file')
        check(os.path.exists(DayFiles(t.logs, 2026, 8, 1).csv), 'August file')
        check(len(csv_lines(DayFiles(t.logs, 2026, 8, 1).csv)) == 2,
              'August file has exactly one row')


def main():
    test_basic_and_format()
    test_dedup_replay_idempotent()
    test_out_of_order_backfill()
    test_dst_fold()
    test_crash_between_jsonl_and_csv()
    test_min_obs_ts_and_validation()
    test_day_boundary_by_local_date()

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
