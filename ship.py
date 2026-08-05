#!/usr/bin/env python3
"""Ship spooled observations to the receiver. Never touches the serial port.

Separate process from `collect.py` on purpose: an HTTP call that hangs must not
be able to stall the thing holding COM4, and a collector that dies must not
take the backlog with it.

Oldest first, and it STOPS on the first failure rather than skipping ahead. The
receiver's cursor is contiguous: shipping seq 7 while seq 6 is still pending
would be refused, and pressing on past a refusal turns one stuck entry into a
stuck stream that looks like many separate faults.
"""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spool import Spool  # noqa: E402

VERSION = 'ship-0.1'
DEFAULT_URL = 'https://lbgweather.au/ingest/obs'
BATCH_LIMIT = 20


def _stderr(line):
    """pythonw.exe has no console: sys.stderr is None, and writing to it raises
    AttributeError. The scheduled tasks all run under pythonw, so an unguarded
    write here would kill the process on its first log line."""
    if sys.stderr is None:
        return
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


_LOGGER = None


def log(msg):
    if _LOGGER is not None:
        _LOGGER.info(msg)
        return
    _stderr('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))

def setup_log(path, quiet=False):
    """Bounded logging. 1 MB x 4 keeps the worst case at 5 MB on a machine
    that has run out of disk before."""
    import logging
    from logging.handlers import RotatingFileHandler
    logger = logging.getLogger(os.path.basename(path))
    logger.setLevel(logging.INFO)
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            h = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=4)
            h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            logger.addHandler(h)
        except OSError as e:
            sys.stderr.write('cannot open log %s: %s\n' % (path, e))
    if not quiet and sys.stderr is not None:
        s = logging.StreamHandler(sys.stderr)
        s.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(s)
    return logger


def load_key(path, station):
    """The file stores hex; the HMAC is over the decoded bytes.

    Signing the ASCII hex produces a valid-looking signature the server rejects
    with a bare 401, which reads as a key mismatch rather than a type error.
    That has caught two callers already.
    """
    with open(path) as f:
        for n, line in enumerate(f, 1):
            parts = line.split()
            if len(parts) == 2 and parts[0] == station:
                try:
                    raw = bytes.fromhex(parts[1])
                except ValueError:
                    raise SystemExit('%s line %d: key is not hex' % (path, n))
                if len(raw) != 32:
                    raise SystemExit('%s line %d: key is %d bytes, want 32'
                                     % (path, n, len(raw)))
                return raw
    raise SystemExit('no key for station %r in %s' % (station, path))


def sign(key, method, path, station, ts, nonce, body):
    s = '\n'.join(['v1', method, path, station, str(ts), nonce,
                   hashlib.sha256(body).hexdigest()])
    return 'v1=' + hmac.new(key, s.encode('utf-8'), hashlib.sha256).hexdigest()


def post(url, key, station, doc, timeout=30):
    body = json.dumps(doc).encode('utf-8')
    path = urllib.parse.urlparse(url).path
    ts = int(time.time())
    nonce = secrets.token_hex(16)
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-CYC-Station', station)
    req.add_header('X-CYC-Timestamp', str(ts))
    req.add_header('X-CYC-Nonce', nonce)
    req.add_header('X-CYC-Signature',
                   sign(key, 'POST', path, station, ts, nonce, body))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read() or b'{}'
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {'error': raw[:200].decode('utf-8', 'replace')}


def acknowledged(code, resp, seq):
    """Only a confirmation that THIS sequence is durable counts.

    A 200 is not enough: the receiver can accept a request and still refuse the
    record inside it, and deleting a spool entry on the status line alone loses
    the observation for ever.
    """
    if code != 200 or not isinstance(resp, dict):
        return False
    return (resp.get('ok') is not False
            and resp.get('rejected') in (0, None)
            and resp.get('ack_seq') == seq)


def resume_cursor(code, resp):
    """A structured resume point from a 409, or None.

    Without this a collector whose spool reaches back further than the
    receiver's ledger retries the same refused entry for ever.
    """
    if code != 409 or not isinstance(resp, dict):
        return None
    cursor = resp.get('server_cursor')
    resume = resp.get('resume_seq')
    if (not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0
            or not isinstance(resume, int) or isinstance(resume, bool)
            or resume != cursor + 1):
        return None
    return cursor


def ship_one(spool, url, key, station, seq, path, doc, loss, quiet):
    payload = {
        'schema': 1,
        'station': station,
        'sent_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'collector': dict(doc.get('collector') or {}, shipper=VERSION),
        'obs': doc.get('obs') or [],
    }
    if loss:
        # Declared exactly once, alongside the entry that follows the hole.
        payload['data_loss'] = {'from': loss['from'], 'to': loss['to']}

    code, resp = post(url, key, station, payload)
    if acknowledged(code, resp, seq):
        spool.ack(seq, path)
        if loss:
            spool.clear_loss()
            log('data_loss %d..%d accepted' % (loss['from'], loss['to']))
        if not quiet:
            log('seq %d shipped (accepted=%s duplicates=%s)'
                % (seq, resp.get('accepted'), resp.get('duplicates')))
        return True, None

    cursor = resume_cursor(code, resp)
    if cursor is not None and seq <= cursor:
        # The receiver has moved past this entry and can no longer verify it.
        # Dropping it is correct: it is either already stored or unreachable,
        # and holding it blocks everything behind it for ever.
        log('seq %d is behind the server cursor %d; dropping it and resuming '
            'from %d' % (seq, cursor, cursor + 1))
        spool.ack(seq, path)
        return True, None

    return False, '%s %s' % (code, resp.get('error') or resp.get('detail'))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default=DEFAULT_URL)
    p.add_argument('--station', default='cyc1')
    p.add_argument('--keyfile', default=r'C:\cycwx\etc\cyc_ingest.key')
    p.add_argument('--spool', default=r'C:\cycwx\send')
    p.add_argument('--limit', type=int, default=BATCH_LIMIT,
                   help='entries per run; the rest wait for the next one')
    p.add_argument('--quiet', action='store_true')
    p.add_argument('--log', default='C:\\cycwx\\log\\ship.log')
    args = p.parse_args(argv)

    global _LOGGER
    if args.log:
        _LOGGER = setup_log(args.log,
                            quiet=getattr(args, 'quiet', False))

    key = load_key(args.keyfile, args.station)
    spool = Spool(args.spool, log=log)
    loss = spool.pending_loss()

    pending = spool.pending(limit=args.limit)
    if not pending:
        if not args.quiet:
            log('nothing to ship')
        return 0

    sent = 0
    for seq, path, doc in pending:
        ok, why = ship_one(spool, args.url, key, args.station, seq, path, doc,
                           loss, args.quiet)
        if not ok:
            # Stop here. Everything behind this entry has a higher sequence and
            # the receiver would refuse it anyway; carrying on would turn one
            # stuck entry into a stream of failures that hides the real cause.
            log('stopping at seq %d: %s (%d shipped, %d still queued)'
                % (seq, why, sent, len(pending) - sent))
            return 1
        sent += 1
        loss = None                 # declared once, with the first entry sent
    if not args.quiet:
        log('%d shipped, %d still queued' % (sent, len(spool.pending())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
