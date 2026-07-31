#!/usr/bin/env python3
"""Tests for cyc_store. Run: python3 test_cyc_store.py

Covers the cases that would silently corrupt the historical record: the DST
fall-back fold, out-of-order backfill, replay, the crash window between the
JSONL and CSV writes, a torn JSONL tail, a failed durable append, and adoption
of a pre-existing legacy CSV at cutover.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cyc_store import (Store, DayFiles, StoreError, CorruptionError,  # noqa: E402
                       parse_obs_ts, utc_key, CSV_FIELDS)

FAILURES = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILURES.append(what)


def obs(ts, wind=1.0, avg=0.9, direction=270, gust=2.0, temp=13.0, hum=50):
    return {'obs_ts': ts, 'src': 'loop',
            'wind_kt': wind, 'wind_avg_kt': avg, 'wind_dir': direction,
            'wind_gust_kt': gust, 'temp_c': temp, 'humidity': hum}


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def lines(path):
    raw = read(path).decode()
    return [l for l in raw.split('\r\n') if l]


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

    def store(self, **kw):
        return Store(self.logs, self.state, **kw)


def test_format_contract():
    print('\n== CSV format contract ==')
    with Tmp() as t:
        s = t.store()
        a, d, r, _, _ = s.put([obs('2026-07-31T10:00:03+10:00'),
                               obs('2026-07-31T10:01:03+10:00')])
        check((a, d, r) == (2, 0, 0), 'accepted 2')
        f = DayFiles(t.logs, 2026, 7, 31)
        raw = read(f.csv)
        check(raw.startswith(b'timestamp,wind_kt,wind_avg_kt,wind_dir,'
                             b'wind_gust_kt,temp_c,humidity\r\n'), 'header')
        check(b'\r\n' in raw and b'\n\n' not in raw, 'CRLF')
        check(lines(f.csv)[1].startswith('2026-07-31 10:00:03,'),
              'naive local timestamp')


def test_replay_and_first_write_wins():
    print('\n== replay is a byte-identical no-op ==')
    with Tmp() as t:
        s = t.store()
        batch = [obs('2026-07-31T10:00:03+10:00'),
                 obs('2026-07-31T10:01:03+10:00')]
        s.put(batch)
        f = DayFiles(t.logs, 2026, 7, 31)
        before = read(f.csv)
        a, d, _, _, durable = s.put(batch)
        check((a, d) == (0, 2), 'replay -> 0 accepted, 2 duplicates')
        check(read(f.csv) == before, 'CSV byte-identical')
        check(len(durable) == 2, 'duplicates reported as durable')

        arch = obs('2026-07-31T10:00:03+10:00', wind=99.0)
        arch['src'] = 'archive'
        s.put([arch])
        check(b'99.0' not in read(f.csv), 'archive did not overwrite loop')


def test_out_of_order():
    print('\n== out-of-order backfill ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T10:00:03+10:00', 1.0),
               obs('2026-07-31T10:05:03+10:00', 5.0)])
        s.put([obs('2026-07-31T10:02:03+10:00', 2.0)])
        f = DayFiles(t.logs, 2026, 7, 31)
        stamps = [l.split(',')[0] for l in lines(f.csv)[1:]]
        check(stamps == sorted(stamps), 'rows sorted by instant')
        check(stamps[1] == '2026-07-31 10:02:03', 'inserted, not appended')


def test_dst_fold():
    print('\n== DST fall-back fold ==')
    early = '2026-04-05T02:00:03+11:00'
    late = '2026-04-05T02:00:03+10:00'
    check(utc_key(parse_obs_ts(late)) - utc_key(parse_obs_ts(early)) == 3600,
          'folds are one hour apart')
    with Tmp() as t:
        s = t.store()
        a, d, _, _, _ = s.put([obs(early, 0.9), obs(late, 0.0)])
        check((a, d) == (2, 0), 'both stored')
        f = DayFiles(t.logs, 2026, 4, 5)
        folds = [l for l in lines(f.csv) if l.startswith('2026-04-05 02:00:03,')]
        check(len(folds) == 2, 'both rows in CSV')
        check(folds[0].split(',')[1] == '0.9', 'AEDT fold first')


def test_spring_forward():
    print('\n== DST spring-forward gap ==')
    # 2025-10-05: 02:00 AEST -> 03:00 AEDT. No real instant maps into 02:00-02:59.
    # A past transition, because future-dated observations are refused.
    before = '2025-10-05T01:59:00+10:00'
    after = '2025-10-05T03:00:00+11:00'
    with Tmp() as t:
        s = t.store()
        a, _, _, _, _ = s.put([obs(before), obs(after)])
        check(a == 2, 'both sides of the gap stored')
        f = DayFiles(t.logs, 2025, 10, 5)
        stamps = [l.split(',')[0] for l in lines(f.csv)[1:]]
        check(stamps == ['2025-10-05 01:59:00', '2025-10-05 03:00:00'],
              'renders across the gap without a 02:xx row')


def test_year_boundary():
    print('\n== year boundary ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2025-12-31T23:59:00+11:00'),
               obs('2026-01-01T00:01:00+11:00')])
        check(os.path.exists(DayFiles(t.logs, 2025, 12, 31).csv), 'December file')
        check(os.path.exists(DayFiles(t.logs, 2026, 1, 1).csv), 'January file')


def test_validation():
    print('\n== input validation ==')
    with Tmp() as t:
        s = t.store()
        cases = [
            ({'obs_ts': '2026-07-31 12:05:00'}, 'naive timestamp'),
            ({'obs_ts': 'not a date'}, 'garbage timestamp'),
            ({'obs_ts': '2026-07-31T12:05:00.5+10:00'}, 'fractional seconds'),
            ('a string, not an object', 'non-dict observation'),
        ]
        for payload, label in cases:
            a, d, r, det, _ = s.put([payload])
            check(r == 1 and a == 0, '%s rejected' % label)

        nan = obs('2026-07-31T10:00:03+10:00')
        nan['wind_kt'] = float('nan')
        a, _, r, _, _ = s.put([nan])
        check(r == 1, 'NaN rejected')

        import datetime as _dt
        future = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(hours=2)).replace(microsecond=0)
        a, _, r, _, _ = s.put([obs(future.isoformat())])
        check(r == 1, 'far-future observation rejected')


def test_cutover_instant():
    print('\n== cutover instant ==')
    with Tmp() as t:
        cut = utc_key(parse_obs_ts('2026-07-31T12:00:00+10:00'))
        s = t.store(min_obs_ts=cut)
        a, _, r, _, _ = s.put([obs('2026-07-31T11:59:00+10:00'),
                               obs('2026-07-31T12:00:00+10:00'),
                               obs('2026-07-31T12:01:00+10:00')])
        check((a, r) == (1, 2), 'only strictly-after-cutover accepted')


def test_legacy_csv_adoption():
    print('\n== legacy CSV is adopted, not erased (the cutover hazard) ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)
        f.ensure_dir()
        legacy = ('timestamp,wind_kt,wind_avg_kt,wind_dir,wind_gust_kt,'
                  'temp_c,humidity\r\n')
        for i in range(5):
            legacy += '2026-07-31 09:%02d:00,1.7,1.7,44,3.4,12.0,60\r\n' % i
        with open(f.csv, 'w', newline='') as fh:
            fh.write(legacy)

        s = t.store()
        a, _, _, _, _ = s.put([obs('2026-07-31T10:00:03+10:00', 9.9)])
        check(a == 1, 'new observation accepted')
        rows = lines(f.csv)[1:]
        check(len(rows) == 6, 'legacy 5 rows preserved plus the new one (%d)'
              % len(rows))
        check(rows[0].startswith('2026-07-31 09:00:00'), 'legacy first row kept')
        check(rows[-1].startswith('2026-07-31 10:00:03'), 'new row appended')
        check(os.path.exists(f.jsonl), 'legacy rows now have JSONL truth')


def test_legacy_adoption_dst_fold():
    print('\n== legacy adoption resolves the April fold by order ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 4, 5)
        f.ensure_dir()
        body = ('timestamp,wind_kt,wind_avg_kt,wind_dir,wind_gust_kt,'
                'temp_c,humidity\r\n'
                '2026-04-05 02:00:03,0.9,0.0,153,0.9,10.5,85\r\n'
                '2026-04-05 02:00:03,0.0,0.0,157,0.9,9.7,87\r\n')
        with open(f.csv, 'w', newline='') as fh:
            fh.write(body)
        s = t.store()
        n = 0
        with s._DayLock(t.state, f.key):
            n = s.adopt_legacy_csv(f)
        check(n == 2, 'both fold rows adopted')
        recs = s._read_jsonl(f)
        keys = sorted(int(r['utc']) for r in recs)
        check(len(set(keys)) == 2, 'distinct UTC instants derived')
        check(keys[1] - keys[0] == 3600, 'exactly one hour apart')


def test_torn_tail():
    print('\n== torn JSONL tail is truncated, not spliced ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T10:00:03+10:00')])
        f = DayFiles(t.logs, 2026, 7, 31)
        with open(f.jsonl, 'a') as fh:
            fh.write('{"utc": 123, "local": "partial reco')   # torn, no newline

        s2 = t.store()
        a, _, _, _, _ = s2.put([obs('2026-07-31T10:01:03+10:00')])
        check(a == 1, 'next write succeeds')
        recs = s2._read_jsonl(f)          # strict: would raise if spliced
        check(len(recs) == 2, 'fragment removed, both good records intact')
        check(len(lines(f.csv)) == 3, 'CSV has header + 2 rows')


def test_corrupt_line_is_loud():
    print('\n== corruption in the middle is not silently skipped ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T10:00:03+10:00'),
               obs('2026-07-31T10:01:03+10:00')])
        f = DayFiles(t.logs, 2026, 7, 31)
        data = read(f.jsonl).decode().split('\n')
        data[0] = '{"this": "is not a record"}'
        with open(f.jsonl, 'w') as fh:
            fh.write('\n'.join(data))
        raised = False
        try:
            t.store()._read_jsonl(f)
        except CorruptionError:
            raised = True
        check(raised, 'CorruptionError raised rather than dropping the record')


def test_failed_append_does_not_poison_index():
    print('\n== a failed durable append leaves nothing marked as stored ==')
    with Tmp() as t:
        s = t.store()
        f = DayFiles(t.logs, 2026, 7, 31)

        boom = []

        def failing_append(files, recs):
            boom.append(1)
            raise OSError(28, 'No space left on device')

        real = s._append_jsonl
        s._append_jsonl = failing_append
        try:
            s.put([obs('2026-07-31T10:00:03+10:00', 4.2)])
        except OSError:
            pass
        s._append_jsonl = real
        check(boom, 'append was attempted and failed')

        # The retry MUST store it, not dismiss it as a duplicate.
        a, d, _, _, _ = s.put([obs('2026-07-31T10:00:03+10:00', 4.2)])
        check((a, d) == (1, 0), 'retry stored the record (%d accepted, %d dup)'
              % (a, d))
        check(b'4.2' in read(f.csv), 'value reached the CSV')


def test_reconcile_detects_content_not_just_count():
    print('\n== reconcile compares content, not row count ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T10:00:03+10:00', 1.0),
               obs('2026-07-31T10:01:03+10:00', 2.0)])
        f = DayFiles(t.logs, 2026, 7, 31)

        # Same number of rows, wrong values -- a count check would pass this.
        bad = read(f.csv).replace(b',2.0,', b',7.7,')
        with open(f.csv, 'wb') as fh:
            fh.write(bad)
        with s._DayLock(t.state, f.key):
            fixed = s.reconcile(f)
        check(fixed, 'divergence detected despite equal row count')
        check(b'7.7' not in read(f.csv), 'CSV rebuilt from truth')

        # Missing header, same data rows.
        rows = read(f.csv).split(b'\r\n', 1)[1]
        with open(f.csv, 'wb') as fh:
            fh.write(rows)
        with s._DayLock(t.state, f.key):
            check(s.reconcile(f), 'missing header detected')
        check(read(f.csv).startswith(b'timestamp,'), 'header restored')


def test_reconcile_all_at_startup():
    print('\n== startup reconciles days no later request touches ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-29T10:00:03+10:00')])
        s.put([obs('2026-07-31T10:00:03+10:00')])
        old = DayFiles(t.logs, 2026, 7, 29)
        os.unlink(old.csv)                       # crash after JSONL, before CSV
        repaired = t.store().reconcile_all()
        check('2026-07-29' in repaired, 'stale day repaired at startup')
        check(os.path.exists(old.csv), 'CSV rebuilt')


def test_last_observation_after_backfill():
    print('\n== last_observation is by instant, not append order ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T12:00:00+10:00')])
        s.put([obs('2026-07-31T06:00:00+10:00')])    # backfill appended last
        last = t.store().last_observation()
        check(last is not None and last['local'] == '2026-07-31 12:00:00',
              'newest instant returned, not the last line written')


def test_stale_index_across_processes():
    print('\n== a second Store sees the first one writes ==')
    with Tmp() as t:
        a1 = t.store()
        b1 = t.store()
        a1.put([obs('2026-07-31T10:00:03+10:00')])
        b1.put([obs('2026-07-31T10:00:03+10:00')])   # b has a stale cache
        f = DayFiles(t.logs, 2026, 7, 31)
        check(len(t.store()._read_jsonl(f)) == 1,
              'no duplicate written despite the second cache being cold')


def main():
    for fn in [test_format_contract, test_replay_and_first_write_wins,
               test_out_of_order, test_dst_fold, test_spring_forward,
               test_year_boundary, test_validation, test_cutover_instant,
               test_legacy_csv_adoption, test_legacy_adoption_dst_fold,
               test_torn_tail, test_corrupt_line_is_loud,
               test_failed_append_does_not_poison_index,
               test_reconcile_detects_content_not_just_count,
               test_reconcile_all_at_startup,
               test_last_observation_after_backfill,
               test_stale_index_across_processes]:
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
