#!/usr/bin/env python3
"""Storage for CYC weather observations pushed by the on-site collector.

Two tiers, deliberately:

  wind_logs/YYYY/MM/DD.jsonl   write-ahead truth. Full-fidelity, one JSON object
                               per line, carries the observation's real UTC
                               instant and every field the collector sent.

  wind_logs/YYYY/MM/DD.csv     a *projection* of the JSONL for the website. Same
                               seven columns, same CRLF line ending and same
                               naive-local timestamp that wind_logger.py has
                               always written, so index.html needs no change.

The CSV is rebuildable from the JSONL at any time, which is what makes the pair
crash-safe: JSONL is appended and fsynced first, and if we die before the CSV is
updated, reconcile() repairs it.

Dedup keys on the UTC instant, never the local timestamp string. Canberra's
autumn fall-back produces two distinct observations that render as the same local
string -- wind_logs/2026/04/05.csv already contains two 02:00:03 rows carrying
different readings. Keying on the string would silently delete an hour of history
every April.
"""

import csv
import fcntl
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Australia/Sydney')

# The contract with index.html and wind_stats.py. Do not reorder.
CSV_FIELDS = ['timestamp', 'wind_kt', 'wind_avg_kt', 'wind_dir',
              'wind_gust_kt', 'temp_c', 'humidity']

CSV_NEWLINE = '\r\n'


class StoreError(Exception):
    pass


def parse_obs_ts(value):
    """Parse an ISO 8601 timestamp that MUST carry an explicit UTC offset.

    Rejecting naive timestamps is the point: a bare local string is ambiguous
    for one hour every April, and accepting one would reintroduce exactly the
    bug this module exists to avoid.
    """
    if not isinstance(value, str):
        raise StoreError('obs_ts must be a string, got %r' % type(value).__name__)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise StoreError('obs_ts not ISO 8601: %s' % e)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise StoreError('obs_ts must carry an explicit UTC offset: %r' % value)
    return dt


def utc_key(dt):
    """Dedup key: whole seconds since the epoch. Unambiguous across DST folds."""
    return int(dt.timestamp())


def local_date(dt):
    """The Canberra local date an observation belongs to, as (YYYY, MM, DD)."""
    loc = dt.astimezone(TZ)
    return loc.year, loc.month, loc.day


def local_stamp(dt):
    """The naive-local string wind_logger.py has always written."""
    return dt.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DayFiles:
    """Paths for one local day."""

    def __init__(self, root, y, m, d):
        self.dir = os.path.join(root, '%04d' % y, '%02d' % m)
        self.jsonl = os.path.join(self.dir, '%02d.jsonl' % d)
        self.csv = os.path.join(self.dir, '%02d.csv' % d)
        self.key = '%04d-%02d-%02d' % (y, m, d)

    def ensure_dir(self):
        os.makedirs(self.dir, exist_ok=True)


class Store:
    def __init__(self, logdir, statedir, min_obs_ts=None):
        """min_obs_ts is an exact UTC epoch, not a date.

        Observations at or before it are refused, which is what stops a
        misconfigured collector -- or archive records recovered from the console
        covering the hours before cutover -- from rewriting legacy history.
        """
        self.logdir = logdir
        self.statedir = statedir
        self.min_obs_ts = min_obs_ts
        os.makedirs(self.statedir, exist_ok=True)
        self._index = {}          # day key -> set of utc keys present in JSONL

    # ---------------------------------------------------------------- locking

    class _DayLock:
        def __init__(self, statedir, daykey):
            self.path = os.path.join(statedir, 'day-%s.lock' % daykey)
            self.fd = None

        def __enter__(self):
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
            return False

    # ------------------------------------------------------------------ index

    def _load_index(self, files):
        """UTC keys already present in a day's JSONL, read once then cached."""
        if files.key in self._index:
            return self._index[files.key]
        keys = set()
        if os.path.exists(files.jsonl):
            with open(files.jsonl, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        keys.add(int(rec['utc']))
                    except (ValueError, KeyError):
                        continue        # tolerate a torn final line
        self._index[files.key] = keys
        return keys

    def _read_jsonl(self, files):
        out = []
        if not os.path.exists(files.jsonl):
            return out
        with open(files.jsonl, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    # ------------------------------------------------------------ projection

    @staticmethod
    def _csv_row(rec):
        return {
            'timestamp': rec['local'],
            'wind_kt': rec.get('wind_kt'),
            'wind_avg_kt': rec.get('wind_avg_kt'),
            'wind_dir': rec.get('wind_dir'),
            'wind_gust_kt': rec.get('wind_gust_kt'),
            'temp_c': rec.get('temp_c'),
            'humidity': rec.get('humidity'),
        }

    @staticmethod
    def _fmt(value):
        if value is None:
            return ''
        if isinstance(value, float) and value.is_integer():
            # match wind_logger.py, which writes round(x, 1) -- 4.0 not 4
            return '%.1f' % value
        return str(value)

    def _write_csv_atomic(self, files, records):
        """Rewrite the whole CSV from records, sorted by UTC instant.

        os.replace() is atomic, so a browser fetching mid-rewrite gets either the
        whole old file or the whole new one, never a truncated one. The temp file
        is created in the same directory (required for atomic rename) and is named
        so it matches neither wind_stats.py's */*/*.csv glob nor anything the
        frontend requests.
        """
        records = sorted(records, key=lambda r: int(r['utc']))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator=CSV_NEWLINE)
        w.writeheader()
        for rec in records:
            row = self._csv_row(rec)
            w.writerow({k: self._fmt(v) for k, v in row.items()})
        data = buf.getvalue()

        fd, tmp = tempfile.mkstemp(dir=files.dir, prefix='.', suffix='.csvtmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, files.csv)
            _fsync_dir(files.dir)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _append_csv(self, files, records):
        """Fast path: append rows known to sort after everything already there."""
        exists = os.path.exists(files.csv)
        with open(files.csv, 'a', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS,
                               lineterminator=CSV_NEWLINE)
            if not exists:
                w.writeheader()
            for rec in sorted(records, key=lambda r: int(r['utc'])):
                row = self._csv_row(rec)
                w.writerow({k: self._fmt(v) for k, v in row.items()})
            f.flush()
            os.fsync(f.fileno())
        if not exists:
            os.chmod(files.csv, 0o644)

    def _csv_max_utc(self, files):
        """Highest UTC instant currently represented in the CSV.

        Derived from the JSONL rather than parsed back out of the CSV, because
        the CSV's local strings are ambiguous across a fold -- which is the whole
        reason the JSONL exists.
        """
        recs = self._read_jsonl(files)
        if not recs:
            return None
        return max(int(r['utc']) for r in recs)

    def _csv_row_count(self, files):
        if not os.path.exists(files.csv):
            return 0
        n = 0
        with open(files.csv, 'r', encoding='utf-8', newline='') as f:
            for i, _line in enumerate(f):
                n = i
        return n        # excludes the header

    def reconcile(self, files):
        """Rebuild the CSV from the JSONL if they disagree on row count.

        Covers the crash window between the JSONL append and the CSV append. A
        retry of that request would otherwise be classified as all-duplicates and
        the CSV would stay permanently short.
        """
        recs = self._read_jsonl(files)
        if not recs:
            return False
        if self._csv_row_count(files) == len(recs):
            return False
        self._write_csv_atomic(files, recs)
        return True

    # ------------------------------------------------------------------ write

    def put(self, observations):
        """Store observations. Returns (accepted, duplicates, rejected, details).

        Idempotent: first write wins, so replay, retry-after-lost-ACK and
        overlapping backfill are all no-ops that write nothing. That also means
        an `archive` record (an interval average) can never overwrite a `loop`
        record (the instantaneous value the chart wants).
        """
        by_day = {}
        rejected = 0
        details = []

        for obs in observations:
            try:
                dt = parse_obs_ts(obs.get('obs_ts'))
            except StoreError as e:
                rejected += 1
                details.append(str(e))
                continue
            key = utc_key(dt)
            if self.min_obs_ts is not None and key <= self.min_obs_ts:
                rejected += 1
                details.append('before cutover: %s' % obs.get('obs_ts'))
                continue
            rec = dict(obs)
            rec['utc'] = key
            rec['local'] = local_stamp(dt)
            y, m, d = local_date(dt)
            by_day.setdefault((y, m, d), []).append(rec)

        accepted = 0
        duplicates = 0

        for (y, m, d), recs in sorted(by_day.items()):
            files = DayFiles(self.logdir, y, m, d)
            files.ensure_dir()
            with self._DayLock(self.statedir, files.key):
                a, dup = self._put_day(files, recs)
                accepted += a
                duplicates += dup

        return accepted, duplicates, rejected, details

    def _put_day(self, files, recs):
        seen = self._load_index(files)

        fresh = []
        dup = 0
        for rec in recs:
            if rec['utc'] in seen:
                dup += 1
                continue
            seen.add(rec['utc'])
            fresh.append(rec)

        if not fresh:
            # Nothing new. If a previous request died between the JSONL and CSV
            # writes, this is the moment we notice and repair.
            self.reconcile(files)
            return 0, dup

        # 1. JSONL first, and fsync it. This is the write-ahead log: once it is
        #    durable the observation is not lost even if we die on the next line.
        with open(files.jsonl, 'a', encoding='utf-8') as f:
            for rec in sorted(fresh, key=lambda r: r['utc']):
                f.write(json.dumps(rec, sort_keys=True) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(files.jsonl, 0o644)

        # 2. Project to CSV.
        prior_max = None
        existing = self._read_jsonl(files)
        prior = [r for r in existing if r['utc'] not in {x['utc'] for x in fresh}]
        if prior:
            prior_max = max(int(r['utc']) for r in prior)

        in_order = prior_max is None or min(r['utc'] for r in fresh) > prior_max
        csv_consistent = self._csv_row_count(files) == len(prior)

        if in_order and csv_consistent:
            self._append_csv(files, fresh)          # O(1) steady state
        else:
            self._write_csv_atomic(files, existing)  # backfill, or repair

        return len(fresh), dup

    # ----------------------------------------------------------------- status

    def last_observation(self):
        """Most recent stored observation across all days, or None."""
        newest = None
        for root, _dirs, names in os.walk(self.logdir):
            for name in names:
                if not name.endswith('.jsonl'):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, 'rb') as f:
                        f.seek(0, os.SEEK_END)
                        size = f.tell()
                        f.seek(max(0, size - 8192))
                        tail = f.read().decode('utf-8', 'replace')
                except OSError:
                    continue
                for line in reversed(tail.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if newest is None or int(rec['utc']) > int(newest['utc']):
                        newest = rec
                    break
        return newest
