#!/usr/bin/env python3
"""Store-and-forward spool for the collector.

The sequence IS the filename. That is the whole design: an entry either exists
on disk complete, with its sequence, or it does not exist at all. There is no
window in which a record has been written but its sequence has not, and none in
which a sequence has been consumed but the record is missing.

That window is not hypothetical. The receiver treats the sequence as the
collector's transport cursor and trusts the collector's `seq -> instant`
assignment; if the two could be written separately, a crash between them would
silently lose an observation with no server-side detection. Writing them as one
`os.replace` removes the possibility rather than narrowing it.

`collect.py` is the only writer. `ship.py` is the only deleter. They share
nothing but the directory.
"""

import json
import os
import re
import tempfile
import time

ENTRY_RE = re.compile(r'^(\d{12})\.json$')
LOSS_MARKER = 'data_loss.json'
STATE_FILE = 'spool_state.json'

# Bounds. Worst case here is about 145 MB, which the plan budgets for on a
# machine that has run out of disk before.
MAX_BYTES = 100 * 1024 * 1024
MAX_AGE_S = 30 * 24 * 3600


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Spool:
    def __init__(self, directory, max_bytes=MAX_BYTES, max_age_s=MAX_AGE_S,
                 log=None):
        self.dir = directory
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s
        self.log = log or (lambda *a: None)
        os.makedirs(self.dir, exist_ok=True)
        self._next = self._recover_next_seq()

    # ------------------------------------------------------------ sequencing

    def _entries(self):
        out = []
        for name in os.listdir(self.dir):
            m = ENTRY_RE.match(name)
            if m:
                out.append((int(m.group(1)), os.path.join(self.dir, name)))
        out.sort()
        return out

    def _read_state(self):
        try:
            with open(os.path.join(self.dir, STATE_FILE)) as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                return {}
            return doc
        except (OSError, ValueError):
            return {}

    def _write_state(self, doc):
        path = os.path.join(self.dir, STATE_FILE)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix='.state-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            _fsync_dir(self.dir)
        except OSError as e:
            self.log('spool state write failed: %s' % e)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _recover_next_seq(self):
        """Highest sequence ever used, plus one.

        Three sources, and the maximum of all of them wins:
          * the highest entry still in the spool -- covers a crash after the
            entry was written but before the counter was updated;
          * the persisted counter -- covers a spool that has been fully shipped
            and deleted;
          * the highest sequence ship.py has had acknowledged -- covers the
            counter file being lost while shipped entries are already known to
            the receiver.
        Reusing a sequence is not merely untidy: the receiver would refuse it as
        a collector reset and stop accepting data.
        """
        state = self._read_state()
        entries = self._entries()
        highest_used = entries[-1][0] if entries else 0

        # next_seq is the NEXT sequence, so the highest USED one is one less.
        # Folding it in as though it were a used value skipped a sequence on
        # every restart, which is harmless to the receiver but makes the spool
        # numbering lie about how much was collected.
        nxt = state.get('next_seq')
        if isinstance(nxt, int) and not isinstance(nxt, bool):
            highest_used = max(highest_used, nxt - 1)

        acked = state.get('highest_acked')
        if isinstance(acked, int) and not isinstance(acked, bool):
            highest_used = max(highest_used, acked)

        return highest_used + 1

    # ---------------------------------------------------------------- write

    def append(self, observations, collector=None):
        """Write one batch as a single atomic entry. Returns its sequence.

        The sequence is allocated, embedded in the payload AND used as the
        filename in one `os.replace`.
        """
        seq = self._next
        obs = []
        for o in observations:
            rec = dict(o)
            rec['seq'] = seq
            obs.append(rec)
        doc = {
            'schema': 1,
            'seq': seq,
            'written_at': int(time.time()),
            'collector': collector or {},
            'obs': obs,
        }
        name = '%012d.json' % seq
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix='.wr-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, os.path.join(self.dir, name))
            _fsync_dir(self.dir)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._next = seq + 1
        # After the entry, never before: if this fails the entry is still on
        # disk and the scan recovers the sequence.
        state = self._read_state()
        state['next_seq'] = self._next
        self._write_state(state)
        return seq

    # ----------------------------------------------------------------- read

    def pending(self, limit=None):
        """Entries oldest-first. Shipping order is the sequence order."""
        out = []
        for seq, path in self._entries():
            try:
                with open(path) as f:
                    doc = json.load(f)
            except (OSError, ValueError) as e:
                # A torn entry cannot be shipped and cannot be interpreted.
                # Quarantine rather than delete: it may be readable by hand.
                self.log('unreadable spool entry %s: %s' % (path, e))
                self._quarantine(path)
                continue
            out.append((seq, path, doc))
            if limit and len(out) >= limit:
                break
        return out

    def _quarantine(self, path):
        try:
            os.replace(path, path + '.bad')
        except OSError:
            pass

    def ack(self, seq, path):
        """Delete an entry the receiver has acknowledged."""
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        state = self._read_state()
        prev = state.get('highest_acked')
        if not isinstance(prev, int) or isinstance(prev, bool) or seq > prev:
            state['highest_acked'] = seq
            self._write_state(state)

    # ---------------------------------------------------------------- bounds

    def usage(self):
        total = 0
        oldest = None
        for _seq, path in self._entries():
            try:
                st = os.stat(path)
            except OSError:
                continue
            total += st.st_size
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
        return total, oldest

    def prune(self):
        """Drop the oldest entries when over budget, and RECORD the loss.

        Deleting quietly would leave the receiver's sequence contiguous with a
        hole in it, which it would refuse. The declared range is what lets the
        collector resume: it is sent on the next post as `data_loss`, the
        receiver counts it in seq_gaps, and the loss is in the record rather
        than inferred from a silence nobody notices.
        """
        dropped_from = dropped_to = None
        now = time.time()
        entries = self._entries()

        def over_budget():
            total = sum(os.path.getsize(p) for _s, p in entries
                        if os.path.exists(p))
            return total > self.max_bytes

        while entries and over_budget():
            seq, path = entries.pop(0)
            try:
                os.unlink(path)
            except OSError:
                continue
            dropped_from = seq if dropped_from is None else dropped_from
            dropped_to = seq

        for seq, path in list(entries):
            try:
                if now - os.path.getmtime(path) > self.max_age_s:
                    os.unlink(path)
                    dropped_from = seq if dropped_from is None else dropped_from
                    dropped_to = seq
                    entries.remove((seq, path))
            except OSError:
                continue

        if dropped_from is not None:
            self._record_loss(dropped_from, dropped_to)
            self.log('spool pruned: dropped seq %d..%d'
                     % (dropped_from, dropped_to))
        return dropped_from, dropped_to

    def _record_loss(self, lo, hi):
        path = os.path.join(self.dir, LOSS_MARKER)
        cur = self.pending_loss()
        if cur:
            lo = min(lo, cur['from'])
            hi = max(hi, cur['to'])
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix='.loss-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump({'from': lo, 'to': hi,
                           'recorded_at': int(time.time())}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            _fsync_dir(self.dir)
        except OSError as e:
            self.log('loss marker write failed: %s' % e)

    def pending_loss(self):
        try:
            with open(os.path.join(self.dir, LOSS_MARKER)) as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        lo, hi = doc.get('from'), doc.get('to')
        if not all(isinstance(v, int) and not isinstance(v, bool)
                   for v in (lo, hi)) or hi < lo:
            return None
        return {'from': lo, 'to': hi}

    def clear_loss(self):
        """Only once the receiver has accepted the declaration."""
        try:
            os.unlink(os.path.join(self.dir, LOSS_MARKER))
            _fsync_dir(self.dir)
        except OSError:
            pass
