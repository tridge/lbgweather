#!/usr/bin/env python3
"""Storage for CYC weather observations pushed by the on-site collector.

Two tiers, deliberately:

  wind_logs/YYYY/MM/DD.jsonl   write-ahead truth. Full-fidelity, one JSON object
                               per line, carrying the observation's real UTC
                               instant and every field the collector sent.

  wind_logs/YYYY/MM/DD.csv     a *projection* of the JSONL for the website. Same
                               seven columns, same CRLF line ending and the same
                               naive-local timestamp wind_logger.py has always
                               written, so index.html needs no change.

The CSV is always rebuildable from the JSONL, which is what makes the pair
crash-safe. Ordering matters: the JSONL is appended and fsynced first, the
in-memory dedup index is updated only *after* that succeeds, and the CSV is
projected last. reconcile() compares the CSV against a freshly rendered
projection -- byte-for-byte, not by row count, because equal counts can hide
wrong values, wrong order, a partial row or a missing header.

Dedup keys on the UTC instant, never the local timestamp string. Canberra's
autumn fall-back produces two distinct observations that render identically
(wind_logs/2026/04/05.csv already holds two 02:00:03 rows with different
readings), so a string key would delete an hour of history every April.
"""

import csv
import fcntl
import io
import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Australia/Sydney')

# The contract with index.html and wind_stats.py. Do not reorder.
CSV_FIELDS = ['timestamp', 'wind_kt', 'wind_avg_kt', 'wind_dir',
              'wind_gust_kt', 'temp_c', 'humidity']

CSV_NEWLINE = '\r\n'

# An observation dated further ahead than this is refused. A collector with a
# broken clock would otherwise write records that look perpetually fresh, which
# is exactly the blindness this whole project exists to remove.
MAX_FUTURE_S = 300


class StoreError(Exception):
    """Bad input. Caller should reject the record, not retry it."""


class CorruptionError(Exception):
    """On-disk state is not what we wrote. Never silently repaired."""


def parse_obs_ts(value):
    """Parse an ISO 8601 timestamp that MUST carry an explicit UTC offset.

    Rejecting naive timestamps is the point: a bare local string is ambiguous
    for one hour every April, and accepting one would reintroduce the bug this
    module exists to avoid. Archive records that carry console wall-clock time
    with no offset must be disambiguated by the collector, which has the
    ordering context to do it; they cannot be resolved here.
    """
    if not isinstance(value, str):
        raise StoreError('obs_ts must be a string, got %s'
                         % type(value).__name__)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise StoreError('obs_ts not ISO 8601: %s' % e)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise StoreError('obs_ts must carry an explicit UTC offset: %r' % value)
    if dt.microsecond:
        # Identity is whole seconds; sub-second precision would silently
        # collapse two distinct instants into one dedup key.
        raise StoreError('obs_ts must not carry fractional seconds: %r' % value)
    return dt


def utc_key(dt):
    """Dedup key: whole seconds since the epoch. Unambiguous across DST folds."""
    return int(dt.timestamp())


def local_date(dt):
    loc = dt.astimezone(TZ)
    return loc.year, loc.month, loc.day


def local_stamp(dt):
    """The naive-local string wind_logger.py has always written."""
    return dt.astimezone(TZ).strftime('%Y-%m-%d %H:%M:%S')


def check_value(key, v):
    """Reject values that JSON permits but CSV and consumers cannot carry."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        raise StoreError('%s is %r, which cannot be represented' % (key, v))
    return v


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DayFiles:
    def __init__(self, root, y, m, d):
        self.dir = os.path.join(root, '%04d' % y, '%02d' % m)
        self.jsonl = os.path.join(self.dir, '%02d.jsonl' % d)
        self.csv = os.path.join(self.dir, '%02d.csv' % d)
        self.key = '%04d-%02d-%02d' % (y, m, d)

    def ensure_dir(self):
        os.makedirs(self.dir, exist_ok=True)


class Store:
    def __init__(self, logdir, statedir, station='cyc1', min_obs_ts=None,
                 max_future_s=MAX_FUTURE_S, jsonl_mode=0o640):
        """min_obs_ts is an exact UTC epoch, not a date.

        Observations at or before it are refused, which stops a misconfigured
        collector -- or archive records recovered from the console covering the
        hours before cutover -- from rewriting legacy history.
        """
        self.logdir = logdir
        self.statedir = statedir
        self.station = station
        self.min_obs_ts = min_obs_ts
        self.max_future_s = max_future_s
        self.jsonl_mode = jsonl_mode
        os.makedirs(self.statedir, exist_ok=True)
        # day key -> (set of utc keys, jsonl stat signature). The signature lets
        # us notice another process having written and reload rather than
        # trusting a stale set.
        self._index = {}

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

    # ------------------------------------------------------------- integrity

    @staticmethod
    def _sig(path):
        try:
            st = os.stat(path)
            return (st.st_size, st.st_mtime_ns, st.st_ino)
        except FileNotFoundError:
            return None

    def _repair_torn_tail(self, files):
        """Truncate an incomplete final JSONL line. Must hold the day lock.

        A crash mid-append can leave a partial line. Appending after it would
        splice the next object onto the fragment, producing a permanently
        malformed line and losing the retried observation too -- so the
        fragment is removed before any append.
        """
        if not os.path.exists(files.jsonl):
            return False
        size = os.path.getsize(files.jsonl)
        if size == 0:
            return False
        with open(files.jsonl, 'rb+') as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) == b'\n':
                return False
            f.seek(0)
            data = f.read()
            cut = data.rfind(b'\n')
            f.truncate(cut + 1)          # cut == -1 truncates to 0
            f.flush()
            os.fsync(f.fileno())
        self._index.pop(files.key, None)
        return True

    def _read_jsonl(self, files, strict=True):
        """Parse a day's JSONL. Malformed lines are corruption, not noise."""
        out = []
        if not os.path.exists(files.jsonl):
            return out
        with open(files.jsonl, 'r', encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    int(rec['utc'])
                except (ValueError, KeyError, TypeError) as e:
                    if strict:
                        raise CorruptionError(
                            '%s line %d is not a valid record: %s'
                            % (files.jsonl, n, e))
                    continue
                out.append(rec)
        return out

    def _load_index(self, files):
        sig = self._sig(files.jsonl)
        cached = self._index.get(files.key)
        if cached is not None and cached[1] == sig:
            return cached[0]
        keys = {int(r['utc']) for r in self._read_jsonl(files)}
        self._index[files.key] = (keys, sig)
        return keys

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
            # match wind_logger.py, which writes round(x, 1): 4.0 not 4
            return '%.1f' % value
        return str(value)

    def render_csv(self, records):
        """The canonical CSV bytes for a set of records. The only renderer."""
        records = sorted(records, key=lambda r: int(r['utc']))
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_FIELDS,
                           lineterminator=CSV_NEWLINE)
        w.writeheader()
        for rec in records:
            row = self._csv_row(rec)
            w.writerow({k: self._fmt(v) for k, v in row.items()})
        return buf.getvalue().encode('utf-8')

    def _write_csv_atomic(self, files, data):
        """os.replace is atomic, so a browser fetching mid-rewrite gets either
        the whole old file or the whole new one, never a truncated one."""
        fd, tmp = tempfile.mkstemp(dir=files.dir, prefix='.', suffix='.csvtmp')
        try:
            with os.fdopen(fd, 'wb') as f:
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

    def reconcile(self, files):
        """Rebuild the CSV if it differs from the projection of the JSONL.

        Compares the whole rendered file, not the row count. Equal counts can
        hide wrong values, wrong order, a partial final row, a missing header,
        or a missing row cancelled out by a duplicate. Caller must hold the day
        lock.
        """
        recs = self._read_jsonl(files)
        if not recs:
            return False
        want = self.render_csv(recs)
        try:
            with open(files.csv, 'rb') as f:
                have = f.read()
        except FileNotFoundError:
            have = None
        if have == want:
            return False
        self._write_csv_atomic(files, want)
        return True

    def reconcile_all(self, max_days=14):
        """Repair every recent day at startup.

        Without this, a crash between the JSONL and CSV writes for a day that no
        later request happens to touch -- yesterday's backfill, say -- is never
        noticed.
        """
        repaired = []
        days = []
        for root, _dirs, names in os.walk(self.logdir):
            for name in names:
                if name.endswith('.jsonl'):
                    p = os.path.join(root, name)
                    try:
                        days.append((os.path.getmtime(p), p))
                    except OSError:
                        pass
        for _mtime, path in sorted(days, reverse=True)[:max_days]:
            rel = os.path.relpath(path, self.logdir)
            parts = rel.split(os.sep)
            if len(parts) != 3:
                continue
            try:
                y, m, d = int(parts[0]), int(parts[1]), int(parts[2][:2])
            except ValueError:
                continue
            files = DayFiles(self.logdir, y, m, d)
            with self._DayLock(self.statedir, files.key):
                self._repair_torn_tail(files)
                if self.reconcile(files):
                    repaired.append(files.key)
        return repaired

    # -------------------------------------------------------- legacy adoption

    def adopt_legacy_csv(self, files):
        """Import pre-existing CSV rows into the JSONL before we ever rewrite it.

        Production days have a CSV written by wind_logger.py but no JSONL. The
        first new observation would otherwise cause a rewrite from a JSONL
        holding only that one record -- erasing the whole pre-cutover part of
        the day. Caller must hold the day lock.

        Local timestamps are ambiguous for one hour each April. The file is in
        chronological order, so the fold is resolved by requiring each row to be
        strictly later than the previous one: the first 02:00:03 takes fold 0
        (AEDT), the second fold 1 (AEST).
        """
        if not os.path.exists(files.csv):
            return 0
        if self._read_jsonl(files):
            return 0                      # already has truth; nothing to adopt

        with open(files.csv, 'r', encoding='utf-8', newline='') as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return 0

        adopted = []
        last = None
        for row in rows:
            stamp = (row.get('timestamp') or '').strip()
            if not stamp:
                continue
            try:
                naive = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                raise CorruptionError('%s: unparseable legacy timestamp %r'
                                      % (files.csv, stamp))
            chosen = None
            for fold in (0, 1):
                cand = naive.replace(tzinfo=TZ, fold=fold)
                key = utc_key(cand)
                if last is None or key > last:
                    chosen = (cand, key)
                    break
            if chosen is None:
                # Non-monotonic legacy data; keep it rather than drop it, but do
                # not let it corrupt ordering.
                cand = naive.replace(tzinfo=TZ, fold=1)
                chosen = (cand, utc_key(cand))
            cand, key = chosen
            last = key

            rec = {'utc': key, 'local': local_stamp(cand),
                   'obs_ts': cand.isoformat(timespec='seconds'),
                   'src': 'legacy', 'station': self.station}
            for col in CSV_FIELDS[1:]:
                raw = (row.get(col) or '').strip()
                if raw == '':
                    rec[col] = None
                else:
                    try:
                        rec[col] = int(raw) if '.' not in raw else float(raw)
                    except ValueError:
                        rec[col] = raw
            adopted.append(rec)

        if not adopted:
            return 0
        self._append_jsonl(files, adopted)
        return len(adopted)

    # ------------------------------------------------------------------ write

    def _append_jsonl(self, files, recs):
        """Durable append. Returns only after fsync succeeds."""
        with open(files.jsonl, 'a', encoding='utf-8') as f:
            for rec in sorted(recs, key=lambda r: int(r['utc'])):
                f.write(json.dumps(rec, sort_keys=True) + '\n')
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(files.jsonl, self.jsonl_mode)
        except OSError:
            pass
        self._index.pop(files.key, None)   # force reload with the new signature

    def put(self, observations):
        """Store observations. Returns (accepted, duplicates, rejected, details,
        durable_keys) where durable_keys are the UTC keys now known to be on
        disk -- either newly written or already present.

        Idempotent: first write wins, so replay, retry-after-lost-ACK and
        overlapping backfill are no-ops. That also means an `archive` record (an
        interval average) can never overwrite a `loop` record.
        """
        by_day = {}
        rejected = 0
        details = []
        now = datetime.now(TZ).timestamp()

        for obs in observations:
            if not isinstance(obs, dict):
                rejected += 1
                details.append('observation is %s, not an object'
                               % type(obs).__name__)
                continue
            try:
                dt = parse_obs_ts(obs.get('obs_ts'))
                for k, v in obs.items():
                    check_value(k, v)
            except StoreError as e:
                rejected += 1
                details.append(str(e))
                continue
            key = utc_key(dt)
            if self.min_obs_ts is not None and key <= self.min_obs_ts:
                rejected += 1
                details.append('before cutover: %s' % obs.get('obs_ts'))
                continue
            if key > now + self.max_future_s:
                rejected += 1
                details.append('dated %ds in the future: %s'
                               % (key - now, obs.get('obs_ts')))
                continue
            rec = dict(obs)
            rec['utc'] = key
            rec['local'] = local_stamp(dt)
            rec['station'] = self.station
            y, m, d = local_date(dt)
            by_day.setdefault((y, m, d), []).append(rec)

        accepted = 0
        duplicates = 0
        durable = set()

        for (y, m, d), recs in sorted(by_day.items()):
            files = DayFiles(self.logdir, y, m, d)
            files.ensure_dir()
            with self._DayLock(self.statedir, files.key):
                a, dup, keys = self._put_day(files, recs)
                accepted += a
                duplicates += dup
                durable |= keys

        return accepted, duplicates, rejected, details, durable

    def _put_day(self, files, recs):
        self._repair_torn_tail(files)
        # Adopt any pre-existing CSV before it can be rewritten from a JSONL
        # that does not yet describe it.
        self.adopt_legacy_csv(files)

        seen = self._load_index(files)

        fresh = []
        fresh_keys = set()
        dup_keys = set()
        for rec in recs:
            if rec['utc'] in seen or rec['utc'] in fresh_keys:
                dup_keys.add(rec['utc'])
                continue
            fresh_keys.add(rec['utc'])
            fresh.append(rec)

        if not fresh:
            # Nothing new. If an earlier request died between the JSONL and CSV
            # writes, this is when we notice and repair.
            self.reconcile(files)
            return 0, len(dup_keys), dup_keys

        # 1. Durable first. The index is NOT updated until this returns: if the
        #    append fails (ENOSPC is a live risk on these machines) the records
        #    must remain unknown so a retry stores them rather than being
        #    dismissed as duplicates.
        self._append_jsonl(files, fresh)

        # 2. Project. Always rebuild from the JSONL so the CSV can never drift.
        existing = self._read_jsonl(files)
        want = self.render_csv(existing)
        try:
            with open(files.csv, 'rb') as f:
                have = f.read()
        except FileNotFoundError:
            have = None
        if have != want:
            self._write_csv_atomic(files, want)

        return len(fresh), len(dup_keys), fresh_keys | dup_keys

    # ----------------------------------------------------------------- status

    def last_observation(self):
        """The newest observation by UTC instant across all days.

        Scans rather than taking the last line: the JSONL is append-ordered, not
        UTC-ordered, so after an out-of-order backfill the final line is an old
        record and seeding from it would understate freshness.
        """
        newest = None
        days = []
        for root, _dirs, names in os.walk(self.logdir):
            for name in names:
                if name.endswith('.jsonl'):
                    p = os.path.join(root, name)
                    try:
                        days.append((os.path.getmtime(p), p))
                    except OSError:
                        pass
        for _mtime, path in sorted(days, reverse=True)[:3]:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            u = int(rec['utc'])
                        except (ValueError, KeyError, TypeError):
                            continue
                        if newest is None or u > int(newest['utc']):
                            newest = rec
            except OSError:
                continue
        return newest
