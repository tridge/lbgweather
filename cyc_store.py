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

import collections
import csv
import fcntl
import io
import json
import math
import os
import tempfile
import threading
import time
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

# Bounded wait for a day lock, so contention degrades rather than deadlocks.
LOCK_TIMEOUT_S = 10


# Per-input outcomes returned by put(). Strings so they survive JSON and read
# clearly in logs.
STORED = 'stored'
DUPLICATE = 'duplicate'
REJECTED = 'rejected'
DURABLE = (STORED, DUPLICATE)


class StoreError(Exception):
    """Bad input. Caller should reject the record, not retry it."""


class CorruptionError(Exception):
    """On-disk state is not what we wrote. Never silently repaired."""


class LockTimeout(Exception):
    """A day lock stayed busy. Retryable; the caller should shed load."""


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


def is_ambiguous(naive):
    """True if this wall-clock time occurs twice (autumn fall-back hour)."""
    return (naive.replace(tzinfo=TZ, fold=0).utcoffset()
            != naive.replace(tzinfo=TZ, fold=1).utcoffset())


def is_nonexistent(naive):
    """True if this wall-clock time never occurs (spring-forward gap)."""
    aware = naive.replace(tzinfo=TZ)
    # A time inside the gap does not survive a round trip through UTC.
    return aware.astimezone(ZoneInfo('UTC')).astimezone(TZ).replace(
        tzinfo=None) != naive


def check_value(key, v, _depth=0):
    """Reject values that JSON permits but CSV and consumers cannot carry.

    Recursive: a NaN nested inside a dict or list is just as unrepresentable as
    a top-level one, and json.dumps would happily emit the non-standard literal.
    """
    if _depth > 8:
        raise StoreError('%s nests too deeply' % key)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        raise StoreError('%s is %r, which cannot be represented' % (key, v))
    if isinstance(v, dict):
        for k2, v2 in v.items():
            check_value('%s.%s' % (key, k2), v2, _depth + 1)
    elif isinstance(v, (list, tuple)):
        for i, v2 in enumerate(v):
            check_value('%s[%d]' % (key, i), v2, _depth + 1)
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


def _crash_point(name):
    """Die instantly at a named point, if CYC_STORE_CRASH_AT says so.

    Crash-safety was previously tested by sleeping a few milliseconds and
    sending SIGKILL, which lands wherever it lands -- so a passing run proved
    nothing in particular. This makes each interleaving exact. It is inert
    unless the environment variable is set, and os._exit skips every handler
    and buffer flush, which is the point.
    """
    if os.environ.get('CYC_STORE_CRASH_AT') == name:
        os._exit(97)


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
        # day key -> (signature, message) for days known to be corrupt
        self._corrupt = {}
        self._corrupt_lock = threading.Lock()

    # ---------------------------------------------------------------- locking

    class _DayLock:
        """Exclusive per-day lock with a bounded wait.

        Blocking forever would let requests queued on one busy day consume every
        handler slot and starve every other day and station, turning contention
        into a total outage.
        """

        def __init__(self, statedir, daykey, timeout=LOCK_TIMEOUT_S):
            self.path = os.path.join(statedir, 'day-%s.lock' % daykey)
            self.timeout = timeout
            self.fd = None

        def __enter__(self):
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        os.close(self.fd)
                        self.fd = None
                        raise LockTimeout('day %s busy for more than %ds'
                                          % (os.path.basename(self.path),
                                             self.timeout))
                    time.sleep(0.05)

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
        """Parse a day's JSONL. Malformed lines are corruption, not noise.

        A known-bad day is remembered by file signature so we fail fast instead
        of re-reading and re-logging the whole file on every request. The
        degraded set is exposed so health can report it rather than silently
        serving 500s.
        """
        sig = self._sig(files.jsonl)
        with self._corrupt_lock:
            bad = self._corrupt.get(files.key)
        if bad is not None and bad[0] == sig:
            raise CorruptionError(bad[1])

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
                        msg = ('%s line %d is not a valid record: %s'
                               % (files.jsonl, n, e))
                        with self._corrupt_lock:
                            self._corrupt[files.key] = (sig, msg)
                        raise CorruptionError(msg)
                    continue
                out.append(rec)
        with self._corrupt_lock:
            self._corrupt.pop(files.key, None)
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
        foreign = self._foreign_rows(have, want)
        if foreign:
            # Same rule as the write path: never delete rows we cannot account
            # for. At startup this is the signal that the legacy writer is
            # still running against a day we have adopted.
            raise CorruptionError(
                '%s holds %d row(s) that are not in the JSONL (%s%s): another '
                'process is writing it. Refusing to reconcile over them.'
                % (files.csv, len(foreign), ', '.join(foreign[:3]),
                   ' ...' if len(foreign) > 3 else ''))
        self._write_csv_atomic(files, want)
        return True

    def reconcile_all(self, max_days=14):
        """Repair every recent day at startup.

        Without this, a crash between the JSONL and CSV writes for a day that no
        later request happens to touch -- yesterday's backfill, say -- is never
        noticed.

        Returns (repaired, errors). One unreadable day must not abort the rest,
        and the caller is expected to surface `errors` rather than serve as if
        healthy.
        """
        repaired = []
        errors = []
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
            try:
                with self._DayLock(self.statedir, files.key):
                    self._repair_torn_tail(files)
                    if self.reconcile(files):
                        repaired.append(files.key)
            except (CorruptionError, LockTimeout, OSError) as e:
                errors.append((files.key, str(e)))
        return repaired, errors

    def degraded(self):
        """Days currently known to be unreadable.

        Snapshotted under the lock: day threads add and remove entries while
        the status writer iterates, and an unlocked read raises 'dictionary
        changed size during iteration' from inside health reporting.
        """
        with self._corrupt_lock:
            return {k: v[1] for k, v in self._corrupt.items()}

    # -------------------------------------------------------- legacy adoption

    def _parse_legacy_csv(self, files, y, m, d):
        """Validate and convert an entire legacy CSV. All-or-nothing.

        Local timestamps are ambiguous for one hour each April. The file is in
        chronological order, so the fold is resolved by requiring each row to be
        strictly later than the previous one. Anything that cannot be resolved
        that way is corruption and stops the import rather than guessing --
        guessing here silently rewrites the club's history.
        """
        with open(files.csv, 'r', encoding='utf-8', newline='') as f:
            raw = f.read()
        if not raw.strip():
            raise CorruptionError('%s is empty' % files.csv)

        rdr = csv.reader(io.StringIO(raw))
        try:
            header = next(rdr)
        except StopIteration:
            raise CorruptionError('%s has no header' % files.csv)
        if [h.strip() for h in header] != CSV_FIELDS:
            raise CorruptionError('%s header is %r, expected %r'
                                  % (files.csv, header, CSV_FIELDS))

        expect_day = '%04d-%02d-%02d' % (y, m, d)

        # Pass 1: parse and validate the wall-clock stamps, nothing more.
        rows = []
        for n, row in enumerate(rdr, 2):
            if not row or not any(c.strip() for c in row):
                continue
            if len(row) != len(CSV_FIELDS):
                raise CorruptionError('%s line %d has %d columns, expected %d'
                                      % (files.csv, n, len(row),
                                         len(CSV_FIELDS)))
            stamp = row[0].strip()
            if not stamp:
                raise CorruptionError('%s line %d has a blank timestamp'
                                      % (files.csv, n))
            try:
                naive = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                raise CorruptionError('%s line %d: unparseable timestamp %r'
                                      % (files.csv, n, stamp))
            if stamp[:10] != expect_day:
                raise CorruptionError('%s line %d: %s is not on day %s'
                                      % (files.csv, n, stamp, expect_day))
            if is_nonexistent(naive):
                raise CorruptionError(
                    '%s line %d: %s never occurred (spring-forward gap)'
                    % (files.csv, n, stamp))
            rows.append((n, stamp, naive, row))

        # Pass 2: locate the wall-clock rollback, THEN assign folds.
        #
        # Resolving each row against only its predecessor is wrong when the
        # logger has a gap across the repeated hour: with 02:30 AEDT followed
        # by 02:45 AEST, both folds of 02:45 are later than 02:30, the first
        # candidate wins, and the row lands an hour early. The rollback is a
        # property of the whole file, so find it first.
        rollback = None                 # index of the first post-rollback row
        for i in range(1, len(rows)):
            # A repeated stamp inside the fold is the rollback too: the club's
            # 1-minute logger can land on the same wall-clock second either
            # side of it, so the clock need not visibly go backwards.
            back = (rows[i][2] < rows[i - 1][2]
                    or (rows[i][2] == rows[i - 1][2]
                        and is_ambiguous(rows[i][2])))
            if back:
                if rollback is not None:
                    raise CorruptionError(
                        '%s line %d: the wall clock goes backwards twice; a '
                        'day has at most one DST fold'
                        % (files.csv, rows[i][0]))
                rollback = i
        if (rollback is not None
                and not is_ambiguous(rows[rollback][2])):
            raise CorruptionError(
                '%s line %d: %s is earlier than the previous row but is not in '
                'a DST fold, so the file is simply out of order'
                % (files.csv, rows[rollback][0], rows[rollback][1]))

        ambiguous = [i for i, r in enumerate(rows) if is_ambiguous(r[2])]
        if ambiguous and rollback is None:
            # Every row in the repeated hour could be either side of it and
            # nothing in the file says which. Guessing rewrites the club's
            # history by an hour; refuse and let an operator decide.
            raise CorruptionError(
                '%s line %d: %s is in the DST fold but the file never shows '
                'the wall clock going back, so its fold cannot be determined '
                '(a logger gap across the repeated hour). Resolve by hand.'
                % (files.csv, rows[ambiguous[0]][0], rows[ambiguous[0]][1]))

        out = []
        last = None
        for i, (n, stamp, naive, row) in enumerate(rows):
            fold = 1 if (rollback is not None and i >= rollback) else 0
            cand = naive.replace(tzinfo=TZ, fold=fold)
            key = utc_key(cand)
            if last is not None and key <= last:
                raise CorruptionError(
                    '%s line %d: %s is not after the previous row; the file is '
                    'not in chronological order'
                    % (files.csv, n, stamp))
            last = key

            rec = {'utc': key, 'local': local_stamp(cand),
                   'obs_ts': cand.isoformat(timespec='seconds'),
                   'src': 'legacy', 'station': self.station}
            for col, val in zip(CSV_FIELDS[1:], row[1:]):
                val = val.strip()
                if val == '':
                    rec[col] = None
                else:
                    try:
                        rec[col] = int(val) if '.' not in val else float(val)
                    except ValueError:
                        rec[col] = val
                check_value(col, rec[col])
            out.append(rec)

        if not out:
            raise CorruptionError('%s has a header but no rows' % files.csv)
        return out

    def adopt_legacy_csv(self, files, y, m, d):
        """Import a pre-existing CSV into JSONL as one atomic transaction.

        Production days have a CSV written by wind_logger.py but no JSONL. The
        first new observation would otherwise cause a rewrite from a JSONL
        holding only that record, erasing the whole pre-cutover part of the day.

        The import is atomic: the complete JSONL is built in a temp file,
        fsynced, then installed with os.replace. An interrupted adoption leaves
        the JSONL absent, so the retry re-adopts from scratch. Appending
        incrementally would leave a partial prefix that the next run mistakes
        for a completed adoption -- and then projects over the real CSV.

        wind_logger.py does NOT take the day lock, so the source can change
        underneath us. The CSV's identity is captured before the parse and
        rechecked immediately before the JSONL is installed; if it moved, the
        import describes a stale snapshot and installing it would erase
        whatever was appended in between. Abort and let the retry re-read.

        Caller must hold the day lock.
        """
        before = self._sig(files.csv)
        if before is None:
            return 0
        if os.path.exists(files.jsonl):
            # There is already truth for this day. Only trust it if it was
            # written by a completed adoption or by us; an unexplained JSONL
            # beside a legacy CSV means the CSV is about to be projected away.
            if os.path.getsize(files.jsonl) == 0:
                raise CorruptionError(
                    '%s is empty but %s exists: refusing to project an empty '
                    'JSONL over a legacy CSV' % (files.jsonl, files.csv))
            return 0

        recs = self._parse_legacy_csv(files, y, m, d)

        fd, tmp = tempfile.mkstemp(dir=files.dir, prefix='.', suffix='.adopting')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                for rec in sorted(recs, key=lambda r: r['utc']):
                    f.write(json.dumps(rec, sort_keys=True,
                                       allow_nan=False) + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, self.jsonl_mode)
            after = self._sig(files.csv)
            if after != before:
                raise CorruptionError(
                    '%s changed during adoption (%s -> %s): another writer is '
                    'still appending to it. Stop the legacy writer first.'
                    % (files.csv, before, after))
            os.replace(tmp, files.jsonl)
            _fsync_dir(files.dir)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._index.pop(files.key, None)
        return len(recs)

    @staticmethod
    def _foreign_rows(have, want):
        """Timestamps present in the on-disk CSV but not in what we would write.

        The day lock does not bind wind_logger.py, so it can create or extend a
        CSV that adoption has already looked past. Rewriting the projection
        would then delete those rows silently. A stale CSV (a crash between the
        JSONL append and the projection) has FEWER rows, never extra ones, so
        anything extra means a second writer.
        """
        def stamps(blob):
            if not blob:
                return collections.Counter()
            rows = blob.decode('utf-8', 'replace').replace('\r\n', '\n').split('\n')
            return collections.Counter(
                r.split(',', 1)[0] for r in rows[1:] if r.strip())

        extra = stamps(have) - stamps(want)
        return sorted(extra.elements())

    # ------------------------------------------------------------------ write

    def _append_jsonl(self, files, recs):
        """Durable append. Returns only after fsync succeeds."""
        with open(files.jsonl, 'a', encoding='utf-8') as f:
            for rec in sorted(recs, key=lambda r: int(r['utc'])):
                f.write(json.dumps(rec, sort_keys=True, allow_nan=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(files.jsonl, self.jsonl_mode)
        except OSError:
            pass
        self._index.pop(files.key, None)   # force reload with the new signature

    def put(self, observations):
        """Store observations. Returns (accepted, duplicates, rejected, details,
        dispositions).

        `dispositions` has one entry per INPUT observation, in input order,
        each STORED, DUPLICATE or REJECTED. A UTC key set is not good enough:
        two inputs can share an instant, and then a rejected one is
        indistinguishable from the durable one that happens to occupy its
        slot -- which let a caller acknowledge a record that was never stored.

        Idempotent: first write wins, so replay, retry-after-lost-ACK and
        overlapping backfill are no-ops. That also means an `archive` record (an
        interval average) can never overwrite a `loop` record.
        """
        by_day = {}
        rejected = 0
        details = []
        now = datetime.now(TZ).timestamp()
        disp = [REJECTED] * len(observations)

        for i, obs in enumerate(observations):
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
            # Carry the input index so the outcome can be reported per input
            # even when several inputs share one instant.
            by_day.setdefault((y, m, d), []).append((i, rec))

        accepted = 0
        duplicates = 0

        for (y, m, d), pairs in sorted(by_day.items()):
            files = DayFiles(self.logdir, y, m, d)
            files.ensure_dir()
            with self._DayLock(self.statedir, files.key):
                a, dup, outcomes = self._put_day(files, pairs, (y, m, d))
                accepted += a
                duplicates += dup
                for i, outcome in outcomes.items():
                    disp[i] = outcome

        return accepted, duplicates, rejected, details, disp

    def _put_day(self, files, pairs, daydate):
        self._repair_torn_tail(files)
        # Adopt any pre-existing CSV before it can be rewritten from a JSONL
        # that does not yet describe it.
        self.adopt_legacy_csv(files, *daydate)

        seen = self._load_index(files)

        fresh = []
        fresh_keys = set()
        outcomes = {}
        for i, rec in pairs:
            if rec['utc'] in seen or rec['utc'] in fresh_keys:
                # Either already on disk, or an earlier input in this same
                # batch claimed the instant. Both are duplicates of something
                # durable, but only the FIRST claimant is the record on disk.
                outcomes[i] = DUPLICATE
                continue
            fresh_keys.add(rec['utc'])
            outcomes[i] = STORED
            fresh.append(rec)

        dup_count = sum(1 for v in outcomes.values() if v is DUPLICATE)

        if not fresh:
            # Nothing new. If an earlier request died between the JSONL and CSV
            # writes, this is when we notice and repair.
            self.reconcile(files)
            return 0, dup_count, outcomes

        # 1. Durable first. The index is NOT updated until this returns: if the
        #    append fails (ENOSPC is a live risk on these machines) the records
        #    must remain unknown so a retry stores them rather than being
        #    dismissed as duplicates.
        self._append_jsonl(files, fresh)
        _crash_point('after-jsonl')

        # 2. Project. Always rebuild from the JSONL so the CSV can never drift.
        existing = self._read_jsonl(files)
        want = self.render_csv(existing)
        try:
            with open(files.csv, 'rb') as f:
                have = f.read()
        except FileNotFoundError:
            have = None
        if have != want:
            foreign = self._foreign_rows(have, want)
            if foreign:
                raise CorruptionError(
                    '%s holds %d row(s) we did not write (%s%s): another '
                    'process is appending to it. Refusing to project over '
                    'them.' % (files.csv, len(foreign), ', '.join(foreign[:3]),
                               ' ...' if len(foreign) > 3 else ''))
            self._write_csv_atomic(files, want)
        _crash_point('after-csv')

        return len(fresh), dup_count, outcomes

    # ----------------------------------------------------------------- status

    def last_observation(self):
        """The newest observation by UTC instant.

        Files are selected by DATE, not mtime: an observation's instant
        determines which day file it lands in, so the newest instant is in the
        latest-dated file regardless of which file was written most recently.
        Selecting by mtime meant a backfill touching three old days could seed a
        stale observation after a restart.
        """
        dated = []
        for root, _dirs, names in os.walk(self.logdir):
            for name in names:
                if not name.endswith('.jsonl'):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, self.logdir).split(os.sep)
                if len(rel) != 3:
                    continue
                try:
                    dated.append(((int(rel[0]), int(rel[1]), int(rel[2][:2])),
                                  path))
                except ValueError:
                    continue

        newest = None
        for _key, path in sorted(dated, reverse=True)[:2]:
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
            if newest is not None:
                break        # the latest-dated file with any record wins
        return newest
