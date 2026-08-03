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
import time
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cyc_store import (Store, DayFiles, StoreError, CorruptionError,  # noqa: E402
                       LockTimeout, parse_obs_ts, utc_key, CSV_FIELDS,
                       LOCK_TIMEOUT_S)

HERE = os.path.dirname(os.path.abspath(__file__))
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
        # Like the real file, this starts well before the fold, so the fold
        # IS resolvable by order. A file that *begins* inside the repeated
        # hour is refused instead -- see the impossible-local-times test.
        body = ('timestamp,wind_kt,wind_avg_kt,wind_dir,wind_gust_kt,'
                'temp_c,humidity\r\n'
                '2026-04-05 01:59:03,1.0,0.0,150,1.0,10.7,84\r\n'
                '2026-04-05 02:00:03,0.9,0.0,153,0.9,10.5,85\r\n'
                '2026-04-05 02:00:03,0.0,0.0,157,0.9,9.7,87\r\n')
        with open(f.csv, 'w', newline='') as fh:
            fh.write(body)
        s = t.store()
        n = 0
        with s._DayLock(t.state, f.key):
            n = s.adopt_legacy_csv(f, 2026, 4, 5)
        check(n == 3, 'all rows adopted including both folds')
        recs = s._read_jsonl(f)
        keys = sorted(int(r['utc']) for r in recs)
        check(len(set(keys)) == 3, 'distinct UTC instants derived')
        fold_keys = [k for k, r in zip(keys, sorted(recs, key=lambda x: x['utc']))
                     if r['local'].endswith('02:00:03')]
        check(len(fold_keys) == 2 and fold_keys[1] - fold_keys[0] == 3600,
              'the two 02:00:03 rows are exactly one hour apart')


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
        repaired, errors = t.store().reconcile_all()
        check('2026-07-29' in repaired, 'stale day repaired at startup')
        check(not errors, 'no errors reported')
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
    print('\n== a second Store sees the first one\'s writes ==')
    with Tmp() as t:
        a1 = t.store()
        b1 = t.store()
        f = DayFiles(t.logs, 2026, 7, 31)

        # WARM b's cache first. With a cold cache b would read the file anyway
        # and the test would pass without the signature check existing at all.
        b1.put([obs('2026-07-31T09:00:00+10:00')])
        check(len(b1._load_index(f)) == 1, 'b holds a real cached index')

        a1.put([obs('2026-07-31T10:00:03+10:00')])   # b's cache is now STALE
        b1.put([obs('2026-07-31T10:00:03+10:00')])
        recs = t.store()._read_jsonl(f)
        check(len(recs) == 2,
              'no duplicate written despite b holding a stale cached index '
              '(got %d records)' % len(recs))

        # And the reverse direction: a must see b's earlier write too.
        a1.put([obs('2026-07-31T09:00:00+10:00')])
        check(len(t.store()._read_jsonl(f)) == 2,
              'a does not re-add the record b wrote before it')



def write_csv(f, rows, header=True):
    f.ensure_dir()
    body = ''
    if header:
        body += ','.join(CSV_FIELDS) + '\r\n'
    body += ''.join(rows)
    with open(f.csv, 'w', newline='') as fh:
        fh.write(body)


def legacy_row(stamp, wind='1.7'):
    return '%s,%s,1.7,44,3.4,12.0,60\r\n' % (stamp, wind)


def test_adoption_is_atomic():
    print('\n== an interrupted adoption does not lose the legacy CSV ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)
        write_csv(f, [legacy_row('2026-07-31 09:%02d:00' % i) for i in range(5)])
        original = read(f.csv)

        s = t.store()
        real = s._parse_legacy_csv

        def boom(files, y, m, d):
            recs = real(files, y, m, d)
            raise OSError(28, 'No space left on device')

        s._parse_legacy_csv = boom
        try:
            s.put([obs('2026-07-31T10:00:03+10:00')])
        except OSError:
            pass
        check(not os.path.exists(f.jsonl),
              'no partial JSONL left behind by the failed adoption')
        check(read(f.csv) == original, 'legacy CSV untouched')

        # The retry must adopt the WHOLE file, not a prefix.
        s2 = t.store()
        a, _, _, _, _ = s2.put([obs('2026-07-31T10:00:03+10:00')])
        check(a == 1, 'retry accepted the new observation')
        rows = lines(f.csv)[1:]
        check(len(rows) == 6, 'all 5 legacy rows survived plus the new one (%d)'
              % len(rows))


def test_adoption_rejects_damaged_input():
    print('\n== damaged legacy CSV is refused, never partially imported ==')
    cases = [
        ([], True, 'header only, no rows'),
        ([legacy_row('2026-07-31 09:00:00')], False, 'missing header'),
        (['2026-07-31 09:00:00,1.7,1.7\r\n'], True, 'short row'),
        ([',1.7,1.7,44,3.4,12.0,60\r\n'], True, 'blank timestamp'),
        ([legacy_row('not a time')], True, 'unparseable timestamp'),
        ([legacy_row('2026-07-30 09:00:00')], True, 'row from another day'),
        ([legacy_row('2026-07-31 09:05:00'),
          legacy_row('2026-07-31 09:00:00')], True, 'non-monotonic rows'),
    ]
    for rows, header, label in cases:
        with Tmp() as t:
            f = DayFiles(t.logs, 2026, 7, 31)
            write_csv(f, rows, header=header)
            before = read(f.csv)
            s = t.store()
            raised = False
            try:
                s.put([obs('2026-07-31T10:00:03+10:00')])
            except CorruptionError:
                raised = True
            check(raised, '%s -> CorruptionError' % label)
            check(read(f.csv) == before, '  %s: CSV untouched' % label)
            check(not os.path.exists(f.jsonl), '  %s: no JSONL written' % label)


def test_adoption_rejects_impossible_local_times():
    print('\n== spring-forward gap and an unresolvable leading fold ==')
    with Tmp() as t:
        # 2025-10-05 02:30 never happened.
        f = DayFiles(t.logs, 2025, 10, 5)
        write_csv(f, [legacy_row('2025-10-05 02:30:00')])
        raised = False
        try:
            with t.store()._DayLock(t.state, f.key):
                t.store().adopt_legacy_csv(f, 2025, 10, 5)
        except CorruptionError as e:
            raised = 'never occurred' in str(e)
        check(raised, 'a spring-forward-gap timestamp is refused')

    with Tmp() as t:
        # A file that BEGINS inside the repeated hour never shows the clock
        # going back, so its fold is unknowable.
        f = DayFiles(t.logs, 2026, 4, 5)
        write_csv(f, [legacy_row('2026-04-05 02:30:00'),
                      legacy_row('2026-04-05 03:00:00')])
        raised = False
        try:
            with t.store()._DayLock(t.state, f.key):
                t.store().adopt_legacy_csv(f, 2026, 4, 5)
        except CorruptionError as e:
            raised = 'fold cannot be determined' in str(e)
        check(raised, 'an ambiguous FIRST row is refused rather than guessed')

    with Tmp() as t:
        # THE GAP CASE. The logger stops at 02:30 AEDT and resumes at 02:45
        # AEST. Both folds of 02:45 are later than 02:30, so resolving each row
        # against only its predecessor picks fold 0 and dates the row an hour
        # early. Nothing in the file distinguishes the two, so it must refuse.
        f = DayFiles(t.logs, 2026, 4, 5)
        write_csv(f, [legacy_row('2026-04-05 01:59:03'),
                      legacy_row('2026-04-05 02:30:00'),
                      legacy_row('2026-04-05 02:45:00'),
                      legacy_row('2026-04-05 03:10:00')])
        raised = ''
        try:
            with t.store()._DayLock(t.state, f.key):
                t.store().adopt_legacy_csv(f, 2026, 4, 5)
        except CorruptionError as e:
            raised = str(e)
        check('fold cannot be determined' in raised,
              'a logger gap across the fold is refused, not silently dated an '
              'hour early (got %r)' % raised[:90])

    with Tmp() as t:
        # And the same shape WITH the rollback visible resolves correctly:
        # everything from the repeat onward is the second pass of the hour.
        f = DayFiles(t.logs, 2026, 4, 5)
        write_csv(f, [legacy_row('2026-04-05 01:59:03'),
                      legacy_row('2026-04-05 02:30:00'),
                      legacy_row('2026-04-05 02:10:00'),
                      legacy_row('2026-04-05 02:45:00')])
        with t.store()._DayLock(t.state, f.key):
            n = t.store().adopt_legacy_csv(f, 2026, 4, 5)
        recs = t.store()._read_jsonl(f)
        keys = [int(r['utc']) for r in recs]
        check(n == 4 and len(set(keys)) == 4, 'all four rows distinct')
        check(keys == sorted(keys), 'and in increasing UTC order')
        # 02:10 and 02:45 are after the rollback, so both are AEST (+10:00).
        after = {r['local']: r['obs_ts'] for r in recs}
        check(after['2026-04-05 02:45:00'].endswith('+10:00'),
              'post-rollback row is AEST, not AEDT (got %s)'
              % after['2026-04-05 02:45:00'])
        check(after['2026-04-05 02:30:00'].endswith('+11:00'),
              'pre-rollback row is still AEDT (got %s)'
              % after['2026-04-05 02:30:00'])


def test_nested_nan_rejected():
    print('\n== NaN nested inside a sub-object is rejected ==')
    with Tmp() as t:
        s = t.store()
        rec = obs('2026-07-31T10:00:03+10:00')
        rec['extra'] = {'inner': [1.0, float('inf')]}
        a, _, r, _, _ = s.put([rec])
        check(r == 1 and a == 0, 'nested Infinity rejected')


def test_corruption_is_memoised():
    print('\n== a known-bad day fails fast and is reported ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T10:00:03+10:00')])
        f = DayFiles(t.logs, 2026, 7, 31)
        with open(f.jsonl, 'w') as fh:
            fh.write('{"not": "a record"}\n')
        for _ in range(2):
            try:
                s._read_jsonl(f)
            except CorruptionError:
                pass
        check(f.key in s.degraded(), 'day reported as degraded')
        check('not a valid record' in s.degraded()[f.key], 'with the reason')


def test_last_observation_prefers_latest_date():
    print('\n== last_observation picks the latest DATE, not the latest mtime ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T12:00:00+10:00')])
        # Touch three older days afterwards, so mtime order is misleading.
        for day in (28, 29, 30):
            s.put([obs('2026-07-%dT06:00:00+10:00' % day)])
        last = t.store().last_observation()
        check(last is not None and last['local'] == '2026-07-31 12:00:00',
              'newest instant found despite older days written later (%s)'
              % (last or {}).get('local'))


def test_day_lock_times_out():
    print('\n== a contended day lock fails bounded, not forever ==')
    with Tmp() as t:
        s = t.store()
        f = DayFiles(t.logs, 2026, 7, 31)
        held = Store._DayLock(t.state, f.key)
        held.__enter__()
        try:
            start = __import__('time').monotonic()
            raised = False
            try:
                with Store._DayLock(t.state, f.key, timeout=1):
                    pass
            except LockTimeout:
                raised = True
            waited = __import__('time').monotonic() - start
            check(raised, 'LockTimeout raised')
            check(waited < 3, 'gave up promptly (%.1fs)' % waited)
        finally:
            held.__exit__()



CRASH_CHILD = """
import os, sys
sys.path.insert(0, %r)
from cyc_store import Store
s = Store(sys.argv[1], sys.argv[2], station='cyc1')
s.put([{'obs_ts': '2026-07-31T10:%s:03+10:00', 'seq': int(sys.argv[3]),
        'src': 'loop', 'wind_kt': 1.0, 'wind_avg_kt': 0.9, 'wind_dir': 270,
        'wind_gust_kt': 2.0, 'temp_c': 13.0, 'humidity': 50}
       for _ in [0]])
print('completed')
"""


def _crash_run(t, at, minute, seq):
    """Run a put in a child process that dies at an exact point."""
    import subprocess
    src = CRASH_CHILD % (HERE, minute)
    env = dict(os.environ)
    if at:
        env['CYC_STORE_CRASH_AT'] = at
    else:
        env.pop('CYC_STORE_CRASH_AT', None)
    return subprocess.run([sys.executable, '-c', src, t.logs, t.state,
                           str(seq)], env=env, capture_output=True, text=True)


def test_crash_between_jsonl_and_csv_is_repaired():
    print('\n== crash at an EXACT point, not wherever a sleep landed ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)

        r = _crash_run(t, None, '00', 1)
        check(r.returncode == 0, 'baseline put completed')
        good_csv = open(f.csv, 'rb').read()

        # Crash AFTER the JSONL fsync, BEFORE the CSV is rendered. The record
        # is durable but the projection is a row short -- the interleaving the
        # old sleep-then-SIGKILL test could only hit by luck.
        r = _crash_run(t, 'after-jsonl', '01', 2)
        check(r.returncode == -9 or r.returncode == 97,
              'child died at the injected point (rc=%s)' % r.returncode)
        check(len(t.store()._read_jsonl(f)) == 2,
              'the observation IS durable in the JSONL')
        check(open(f.csv, 'rb').read() == good_csv,
              'and the CSV is provably stale - one row short')

        # A retry of the same observation is a pure duplicate. It must still
        # repair the projection rather than short-circuit on "nothing new".
        s = t.store()
        a, dup, rej, _, _ = s.put([obs('2026-07-31T10:01:03+10:00')])
        check((a, dup, rej) == (0, 1, 0), 'the retry is a duplicate')
        rendered = s.render_csv(s._read_jsonl(f))
        check(open(f.csv, 'rb').read() == rendered,
              'the duplicate retry reconciled the CSV back to the JSONL')
        check(len(open(f.csv, 'rb').read().rstrip(b'\r\n').split(b'\r\n'))
              == 3, 'CSV now has header + 2 rows')


def test_crash_after_csv_leaves_nothing_to_do():
    print('\n== crash after the CSV write is already consistent ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)
        _crash_run(t, None, '00', 1)
        r = _crash_run(t, 'after-csv', '01', 2)
        check(r.returncode in (-9, 97), 'child died after the CSV write')
        s = t.store()
        check(open(f.csv, 'rb').read() == s.render_csv(s._read_jsonl(f)),
              'JSONL and CSV already agree - no repair needed')
        check(s.reconcile(f) is False,
              'reconcile reports nothing to repair')



def test_adoption_aborts_if_the_csv_moves_under_it():
    print('\n== adoption refuses a source that changed mid-import ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)
        write_csv(f, [legacy_row('2026-07-31 09:00:00'),
                      legacy_row('2026-07-31 09:01:00')])
        s = t.store()

        # Simulate wind_logger appending WHILE we build the temp JSONL: the
        # append lands after the parse and before the rename. The day lock does
        # not bind wind_logger, so this really can happen.
        real_parse = s._parse_legacy_csv

        def parse_then_append(files, y, m, d):
            recs = real_parse(files, y, m, d)
            with open(files.csv, 'a', newline='') as fh:
                fh.write(legacy_row('2026-07-31 09:02:00'))
            return recs
        s._parse_legacy_csv = parse_then_append

        raised = ''
        try:
            with s._DayLock(t.state, f.key):
                s.adopt_legacy_csv(f, 2026, 7, 31)
        except CorruptionError as e:
            raised = str(e)
        check('changed during adoption' in raised,
              'the import is abandoned, not installed over the new row (%r)'
              % raised[:80])
        check(not os.path.exists(f.jsonl),
              'and no JSONL was installed, so a retry re-reads the full CSV')
        check(len(open(f.csv).read().strip().splitlines()) == 4,
              'the CSV still holds all three rows plus the header')

        # The retry, with nothing else writing, succeeds and covers all three.
        s2 = t.store()
        with s2._DayLock(t.state, f.key):
            n = s2.adopt_legacy_csv(f, 2026, 7, 31)
        check(n == 3, 'retry adopts all three rows (got %d)' % n)


def test_foreign_csv_rows_are_never_projected_over():
    print('\n== rows we did not write are not silently deleted ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)
        s = t.store()
        s.put([obs('2026-07-31T09:00:00+10:00')])

        # A second writer appends to OUR csv, as wind_logger would if it were
        # still running after cutover.
        with open(f.csv, 'a', newline='') as fh:
            fh.write(legacy_row('2026-07-31 09:30:00'))

        raised = ''
        try:
            s.put([obs('2026-07-31T10:00:00+10:00')])
        except CorruptionError as e:
            raised = str(e)
        check('another process is appending' in raised,
              'the projection refuses rather than erasing the foreign row '
              '(%r)' % raised[:70])
        body = open(f.csv).read()
        check('09:30:00' in body, 'the foreign row is still there')

        # reconcile() must refuse for the same reason.
        raised = ''
        try:
            with s._DayLock(t.state, f.key):
                s.reconcile(f)
        except CorruptionError as e:
            raised = str(e)
        check('not in the JSONL' in raised, 'reconcile refuses too')


def test_empty_jsonl_beside_a_legacy_csv():
    print('\n== a zero-byte JSONL must not authorise erasing the CSV ==')
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)
        write_csv(f, [legacy_row('2026-07-31 09:00:00'),
                      legacy_row('2026-07-31 09:01:00')])
        open(f.jsonl, 'w').close()          # zero bytes
        raised = ''
        try:
            with t.store()._DayLock(t.state, f.key):
                t.store().adopt_legacy_csv(f, 2026, 7, 31)
        except CorruptionError as e:
            raised = str(e)
        check('refusing to project an empty JSONL' in raised,
              'an empty JSONL beside a legacy CSV is refused (%r)'
              % raised[:70])


def test_dispositions_are_per_input():
    print('\n== put reports an outcome for every input ==')
    from cyc_store import STORED, DUPLICATE, REJECTED
    with Tmp() as t:
        s = t.store()
        ts = '2026-07-31T10:00:03+10:00'
        a, d, r, _, disp = s.put([
            obs(ts),                                   # stored
            obs(ts),                                   # same instant -> dup
            {'obs_ts': 'not a timestamp'},             # rejected
            obs('2026-07-31T10:01:03+10:00'),          # stored
        ])
        check(disp == [STORED, DUPLICATE, REJECTED, STORED],
              'one outcome per input, in input order (got %s)' % disp)
        check((a, d, r) == (2, 1, 1), 'and the totals agree (%s)' % ((a, d, r),))
        check(len(disp) == 4, 'length matches the input list')



def test_wind_logger_shares_the_day_lock():
    print('\n== the legacy writer and the store use ONE lock ==')
    import subprocess
    with Tmp() as t:
        f = DayFiles(t.logs, 2026, 7, 31)
        f.ensure_dir()
        state_for_station = os.path.join(t.state, 'cyc1')
        os.makedirs(state_for_station, exist_ok=True)

        # wind_logger lives in the web tree, not beside the service code, so
        # find it rather than assuming it is importable from here. Not finding
        # it is a failure, not a skip: the whole point is that these two files
        # agree, and an unrun check proves nothing.
        wl_dir = None
        for cand in (HERE, os.path.expanduser('~/wind')):
            if os.path.exists(os.path.join(cand, 'wind_logger.py')):
                wl_dir = cand
                break
        check(wl_dir is not None,
              'wind_logger.py found, so the shared-lock invariant can be '
              'checked')
        if wl_dir is None:
            return

        # wind_logger derives its lock path from the CSV path and CYC_LOCK_DIR.
        # If that ever stops matching DayFiles.key, the two writers hold
        # different locks and neither notices.
        src = ('import os, sys; sys.path.insert(0, %r); '
               'os.environ["CYC_LOCK_DIR"] = %r; '
               'import wind_logger as wl; '
               'import fcntl, time; '
               'lf = %r\n'
               'with wl.day_lock(lf):\n'
               '    print("HELD", flush=True)\n'
               '    time.sleep(3)\n'
               % (wl_dir, state_for_station,
                  os.path.join(t.logs, '2026', '07', '31.csv')))
        p = subprocess.Popen([sys.executable, '-c', src],
                             stdout=subprocess.PIPE, text=True)
        try:
            check(p.stdout.readline().strip() == 'HELD',
                  'wind_logger acquired the day lock')

            # The store must now be unable to take it. If the paths did not
            # match, this would succeed instantly and prove nothing.
            s = Store(t.logs, state_for_station, station='cyc1')
            t0 = time.time()
            blocked = False
            try:
                with s._DayLock(state_for_station, f.key, timeout=1):
                    pass
            except LockTimeout:
                blocked = True
            check(blocked,
                  'the store is BLOCKED by the legacy writer - one lock, not '
                  'two (waited %.1fs)' % (time.time() - t0))
        finally:
            p.kill()
            p.wait(timeout=10)

        # And once released, the store proceeds.
        s = Store(t.logs, state_for_station, station='cyc1')
        with s._DayLock(state_for_station, f.key, timeout=5):
            pass
        check(True, 'and proceeds once the writer releases it')



def test_truncated_jsonl_does_not_read_as_repaired():
    print('\n== truncating the JSONL is not a repair ==')
    with Tmp() as t:
        s = t.store()
        s.put([obs('2026-07-31T10:00:03+10:00'),
               obs('2026-07-31T10:01:03+10:00')])
        f = DayFiles(t.logs, 2026, 7, 31)

        # Corrupt it, then "fix" it by emptying it -- which is what a panicked
        # hand-repair looks like. _read_jsonl clears its cached corruption for
        # an empty file and reconcile() returns early with no records, so this
        # used to read as a healthy empty day and clear health, with the CSV
        # still holding two rows nothing accounted for.
        with open(f.jsonl, 'w') as fh:
            fh.write('')
        raised = ''
        try:
            s.recheck_day('2026-07-31')
        except CorruptionError as e:
            raised = str(e)
        check('truncated' in raised,
              'an empty JSONL beside a populated CSV is reported, not '
              'declared healthy (%r)' % raised[:80])
        check(len(open(f.csv).read().strip().splitlines()) == 3,
              'and the CSV rows are still there')


def test_wind_logger_reports_a_dropped_sample():
    print('\n== a dropped sample is visible, not just absent ==')
    import subprocess
    with Tmp() as t:
        wl_dir = None
        for cand in (HERE, os.path.expanduser('~/wind')):
            if os.path.exists(os.path.join(cand, 'wind_logger.py')):
                wl_dir = cand
                break
        check(wl_dir is not None, 'wind_logger.py found')
        if wl_dir is None:
            return
        src = ('import os, sys; sys.path.insert(0, %r); '
               'os.environ["CYC_LOCK_DIR"] = "/proc/cannot-create-this"; '
               'import wind_logger as wl; '
               'wl.LOG_BASE_DIR = %r; '
               'print("RESULT", wl.log_data({"timestamp": "2026-07-31 10:00:00",'
               ' "wind_kt": 1.0, "wind_avg_kt": 1.0, "wind_dir": 90,'
               ' "wind_gust_kt": 2.0, "temp_c": 10.0, "humidity": 50}))'
               % (wl_dir, t.logs))
        r = subprocess.run([sys.executable, '-c', src], capture_output=True,
                           text=True, timeout=60)
        check('RESULT False' in r.stdout,
              'log_data reports failure rather than returning silently (%r)'
              % r.stdout.strip()[:60])
        check('NOT logging' in r.stderr, 'and says why on stderr')
        import glob
        check(not glob.glob(os.path.join(t.logs, '*', '*', '*.csv')),
              'and wrote NOTHING unlocked')

        # main() is what cron runs, and it is main() that has to exit non-zero
        # and publish the failure. Testing log_data alone proved neither.
        # The latch file is INDEPENDENT of the lock directory on purpose:
        # the failure it reports is that the lock directory is unusable. This
        # test previously pointed it at a writable temp dir, which is what
        # hid the fact that production had it inside the failing directory.
        fail_dir = os.path.join(t.d, 'latch')
        os.makedirs(fail_dir, exist_ok=True)
        status = os.path.join(t.d, 'status.json')
        main_src = (
            'import os, sys, json; sys.path.insert(0, %r); '
            'os.environ["CYC_LOCK_DIR"] = "/proc/cannot-create-this"; '
            'import wind_logger as wl; '
            'wl.LOG_BASE_DIR = %r; wl.STATUS_FILE = %r; '
            'wl.FAIL_FILE = %r; '
            'wl.fetch_weather = lambda: {"sensors": [{"data": [{'
            '"ts": 1785500000, "wind_speed": 1.0, '
            '"wind_speed_10_min_avg": 1.0, "wind_dir": 90, '
            '"wind_gust_10_min": 2.0, "temp_out": 50.0, "hum_out": 50}]}], '
            '"generated_at": 1785500001}; '
            'sys.exit(wl.main())'
            % (wl_dir, t.logs, status, os.path.join(fail_dir, 'f.json')))
        r = subprocess.run([sys.executable, '-c', main_src],
                           capture_output=True, text=True, timeout=60)
        check(r.returncode != 0,
              'main() exits non-zero so cron can see it (rc=%s, %s)'
              % (r.returncode, r.stderr.strip()[:120]))
        doc = json.load(open(status))
        check(doc.get('csv_write_failed') is True,
              'status records the failure (%s)' % doc.get('csv_write_failed'))
        check(doc.get('csv_failures_recent', 0) >= 1,
              'and latches a count so a 2-minute watchdog cannot miss a '
              '1-minute failure (%s)' % doc.get('csv_failures_recent'))
        check(doc.get('csv_last_failure_epoch'), 'with a timestamp')
        check(doc.get('csv_latch_unavailable') is False,
              'and the latch itself was writable, so the count is trustworthy')

        # Now the case that matters: the latch is ALSO unwritable. We must not
        # publish a clean-looking zero we cannot stand behind.
        main_src2 = main_src.replace(
            repr(os.path.join(fail_dir, 'f.json')),
            repr('/proc/cannot-write-here/f.json'))
        r = subprocess.run([sys.executable, '-c', main_src2],
                           capture_output=True, text=True, timeout=60)
        doc = json.load(open(status))
        check(doc.get('csv_latch_unavailable') is True,
              'an unpersistable latch is declared, not silently zeroed (%s)'
              % doc.get('csv_latch_unavailable'))

        # A corrupt latch during a FAILING minute: we have a fact worth
        # recording and the old contents are unreadable anyway, so start a
        # fresh record rather than lose the failure. Losing it is precisely
        # what the latch exists to prevent.
        latch = os.path.join(fail_dir, 'f.json')
        with open(latch, 'w') as fh:
            fh.write('[]')                      # valid JSON, not an object
        r = subprocess.run([sys.executable, '-c', main_src],
                           capture_output=True, text=True, timeout=60)
        doc = json.load(open(status))
        check(doc.get('csv_failures_recent') == 1,
              'the failing minute is recorded despite the corrupt latch (%s)'
              % doc.get('csv_failures_recent'))
        check(json.load(open(latch)).get('count') == 1,
              'and the latch now holds a usable record')

        # A corrupt latch during a CLEAN minute is the opposite: writing zeros
        # would turn "cannot tell" into a confident clean state.
        with open(latch, 'w') as fh:
            fh.write('[]')
        clean_src = main_src.replace('"/proc/cannot-create-this"',
                                     repr(os.path.join(t.d, 'goodlock')))
        r = subprocess.run([sys.executable, '-c', clean_src],
                           capture_output=True, text=True, timeout=60)
        check(r.returncode == 0, 'the clean run logged successfully (rc=%s)'
              % r.returncode)
        check(open(latch).read() == '[]',
              'the unreadable latch is left alone, not clobbered (%r)'
              % open(latch).read()[:40])
        doc = json.load(open(status))
        check(doc.get('csv_latch_unavailable') is True,
              'and the status says we cannot tell (%s)'
              % doc.get('csv_latch_unavailable'))



def test_lock_budget_is_bounded_across_days():
    print('\n== a many-day request cannot hold locks for ever ==')
    with Tmp() as t:
        s = t.store()
        # More days than a single request may lock at once.
        many = [obs('2026-0%d-1%dT10:00:00+10:00' % (1 + (i // 9), i % 9),
                    i + 1)
                for i in range(40)]
        raised = ''
        try:
            s.put(many)
        except StoreError as e:
            raised = str(e)
        check('the limit is' in raised,
              'a request spanning too many days is refused with an actionable '
              'message (%r)' % raised[:70])

        # And the acquisition budget is ONE deadline, not per day: with a day
        # already locked, the whole call gives up in about LOCK_TIMEOUT_S, not
        # LOCK_TIMEOUT_S x days.
        # THREE contended locks, not one. With a single contended day the old
        # per-day implementation aborts after one wait and passes too, so that
        # version of this test proved nothing about the shared budget.
        keys = ['2026-07-29', '2026-07-30', '2026-07-31']
        held = []
        released = []
        releasers = []
        try:
            for k in keys:
                DayFiles(t.logs, *[int(x) for x in k.split('-')]).ensure_dir()
                lk = s._DayLock(t.state, k)
                lk.__enter__()
                held.append(lk)
                released.append(threading.Event())

            # Release the locks at 3, 6 and 12 seconds. A shared 10-second
            # budget times out waiting for the third. Giving each day a fresh
            # 10 seconds acquires all three at 12 seconds and therefore fails
            # the timed_out assertion below.
            def release_later(i, delay):
                time.sleep(delay)
                held[i].__exit__(None, None, None)
                released[i].set()

            for i, delay in enumerate((3, 6, 12)):
                th = threading.Thread(target=release_later,
                                      args=(i, delay), daemon=True)
                th.start()
                releasers.append(th)

            batch = [obs('2026-07-29T10:00:00+10:00', 1),
                     obs('2026-07-30T10:00:00+10:00', 2),
                     obs('2026-07-31T10:00:00+10:00', 3)]
            t0 = time.time()
            try:
                t.store().put(batch)
                took, timed_out = time.time() - t0, False
            except LockTimeout:
                took, timed_out = time.time() - t0, True
            check(timed_out, 'the contended request gives up')
            check(LOCK_TIMEOUT_S * 0.8 < took < LOCK_TIMEOUT_S * 1.3,
                  'ONE budget expires near %ds across staged contention '
                  '(%.1fs)' % (LOCK_TIMEOUT_S, took))
        finally:
            for th in releasers:
                th.join(timeout=13)
            for i, lk in enumerate(held):
                if not released[i].is_set():
                    lk.__exit__(None, None, None)


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
               test_stale_index_across_processes,
               test_crash_between_jsonl_and_csv_is_repaired,
               test_crash_after_csv_leaves_nothing_to_do,
               test_adoption_aborts_if_the_csv_moves_under_it,
               test_foreign_csv_rows_are_never_projected_over,
               test_empty_jsonl_beside_a_legacy_csv,
               test_dispositions_are_per_input,
               test_wind_logger_shares_the_day_lock,
               test_truncated_jsonl_does_not_read_as_repaired,
               test_wind_logger_reports_a_dropped_sample,
               test_lock_budget_is_bounded_across_days,
               test_adoption_is_atomic,
               test_adoption_rejects_damaged_input,
               test_adoption_rejects_impossible_local_times,
               test_nested_nan_rejected, test_corruption_is_memoised,
               test_last_observation_prefers_latest_date,
               test_day_lock_times_out]:
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
