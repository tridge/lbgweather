#!/usr/bin/env python3
"""Read the Davis console and spool observations. Owns the serial port.

Single-threaded and single-purpose. It does not ship, does not talk HTTP, and
never writes to the console. Everything it shares with `ship.py` goes through
the spool directory and `os.replace`.

Timing is on a MONOTONIC clock. The wall clock is used only to stamp an
observation, and a large jump in it quarantines readings rather than writing
them: w32time correcting a drifted PC would otherwise stamp a minute of
observations at the wrong instant, and the instant is the record's identity.

The resync ladder, in order, because each rung costs more than the last:
    flush and re-wake -> reopen the port -> re-enumerate by VID:PID -> back off
A PL2303 can come back on a different COM number, which is why enumeration is a
rung rather than a restart.
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from davis_link import Link, LinkError, NotAwake, BAUD_CANDIDATES  # noqa: E402
from davis_protocol import observation_from_loop  # noqa: E402
from spool import Spool  # noqa: E402

VERSION = 'collect-0.1'
VID_PID = '067B:2303'                    # Prolific PL2303, as fitted
HEARTBEAT_INTERVAL_S = 15
DEFAULT_PERIOD_S = 60                    # one observation a minute, as the CSV
LOOP_BATCH = 50
# A wall-clock step larger than this means something moved the clock. Readings
# either side of it cannot be stamped honestly.
CLOCK_JUMP_S = 30
QUARANTINE_S = 120
# Free space at which to degrade, and at which to stop writing entirely.
DISK_WARN_MB = 500
DISK_STOP_MB = 200
BACKOFF = (2, 5, 15, 30, 60, 120)


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


class Heartbeat:
    """What the watchdog reads to tell a working collector from a wedged one.

    A wedged owner holds COM4 for ever and cannot honour a stop request,
    because the code that would honour it is blocked. The watchdog therefore
    cannot ask -- it has to be able to see staleness and terminate.
    """

    def __init__(self, path):
        self.path = path
        self.last = 0.0

    def beat(self, state, extra=None):
        now = time.monotonic()
        if now - self.last < HEARTBEAT_INTERVAL_S and state == 'ok':
            return
        self.last = now
        doc = {'pid': os.getpid(), 'version': VERSION, 'state': state,
               'epoch': int(time.time()),
               'wall': time.strftime('%Y-%m-%dT%H:%M:%S')}
        doc.update(extra or {})
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            log('heartbeat write failed: %s' % e)


def find_port(preferred, vid_pid=VID_PID):
    """The configured port, or whatever the adapter came back as.

    Re-enumeration is a rung on the ladder because a PL2303 that re-enumerates
    frequently lands on a different COM number, and treating that as a hard
    failure would need a human for something the code can see.
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return preferred
    ports = list(list_ports.comports())
    for p in ports:
        if p.device == preferred:
            return preferred
    want = vid_pid.replace(':', '').upper()
    for p in ports:
        hwid = (p.hwid or '').upper().replace(':', '')
        if want in hwid.replace('VID_', '').replace('PID_', '').replace('&', ''):
            log('port moved: %s is gone, using %s (%s)'
                % (preferred, p.device, p.hwid))
            return p.device
    return preferred


def open_port(name, baud):
    import serial
    return serial.Serial(port=name, baudrate=baud, bytesize=8,
                         parity='N', stopbits=1, timeout=1.0)


def probe_baud(name):
    """Try each candidate until the console answers a wakeup."""
    for baud in BAUD_CANDIDATES:
        try:
            port = open_port(name, baud)
        except Exception as e:
            log('cannot open %s at %d: %s' % (name, baud, e))
            continue
        try:
            Link(port, log).wake()
            log('console answers at %d baud' % baud)
            return port, baud
        except LinkError:
            port.close()
    return None, None


class Collector:
    def __init__(self, args):
        self.args = args
        self.spool = Spool(args.spool, log=log)
        self.hb = Heartbeat(args.heartbeat)
        self.link = None
        self.port = None
        self.baud = None
        self.port_name = args.port
        self.backoff_idx = 0
        self.last_wall = time.time()
        self.quarantine_until = 0.0
        self.stats = {'observations': 0, 'crc_errors': 0, 'resyncs': 0,
                      'quarantined': 0, 'skipped_same_instant': 0}
        self.last_emitted_ts = None

    # ------------------------------------------------------------ connection

    def connect(self):
        if self.args.sim:
            from fakeconsole import FakeConsole
            self.port = FakeConsole()
            self.link = Link(self.port, log)
            self.baud = 0
            return True
        self.port_name = find_port(self.port_name)
        if self.args.baud:
            try:
                self.port = open_port(self.port_name, self.args.baud)
                self.baud = self.args.baud
            except Exception as e:
                log('open %s failed: %s' % (self.port_name, e))
                return False
        else:
            self.port, self.baud = probe_baud(self.port_name)
            if self.port is None:
                return False
        self.link = Link(self.port, log)
        try:
            self.link.wake()
        except LinkError as e:
            log('console will not wake: %s' % e)
            self.close()
            return False
        return True

    def close(self):
        try:
            if self.link:
                self.link.stop_loop()
        except Exception:
            pass
        try:
            if self.port:
                self.port.close()
        except Exception:
            pass
        self.port = self.link = None

    def resync(self, why):
        """Climb the ladder. Each rung is more disruptive than the last."""
        self.stats['resyncs'] += 1
        log('resync (%s)' % why)
        self.hb.beat('resync', {'reason': str(why)[:120]})
        try:
            if self.link:
                self.link.stop_loop()
                self.link.wake()
                self.backoff_idx = 0
                return True
        except LinkError:
            pass
        self.close()
        if self.connect():
            self.backoff_idx = 0
            return True
        delay = BACKOFF[min(self.backoff_idx, len(BACKOFF) - 1)]
        self.backoff_idx += 1
        # "link down" is published rather than the process simply going quiet:
        # a collector that stops emitting looks identical to one that is fine
        # but becalmed, and that ambiguity is the whole reason this exists.
        self.hb.beat('down', {'reason': str(why)[:120], 'retry_in_s': delay})
        log('link down; retrying in %ds' % delay)
        time.sleep(delay)
        return False

    # ------------------------------------------------------------------ disk

    def disk_free_mb(self):
        try:
            return shutil.disk_usage(self.spool.dir).free / (1024 * 1024)
        except OSError:
            return None

    def disk_ok(self):
        free = self.disk_free_mb()
        if free is None:
            return True
        if free < DISK_STOP_MB:
            log('only %.0f MB free: NOT writing (limit %d MB)'
                % (free, DISK_STOP_MB))
            self.hb.beat('disk_full', {'free_mb': round(free)})
            return False
        if free < DISK_WARN_MB:
            log('low disk: %.0f MB free' % free)
        return True

    # ------------------------------------------------------------- collecting

    def clock_is_sane(self):
        """Detect a wall-clock step and quarantine around it.

        A jump means the stamp we would write is not the instant the reading
        was taken. The instant is the record's identity to the receiver, so a
        wrong one is worse than a missing one.
        """
        now = time.time()
        drift = abs(now - self.last_wall)
        self.last_wall = now
        if drift > CLOCK_JUMP_S:
            self.quarantine_until = time.monotonic() + QUARANTINE_S
            self.stats['quarantined'] += 1
            log('wall clock jumped %.0fs: quarantining observations for %ds'
                % (drift, QUARANTINE_S))
            self.hb.beat('clock_jump', {'jump_s': round(drift)})
            return False
        return time.monotonic() >= self.quarantine_until

    def run(self):
        period = self.args.period
        next_emit = time.monotonic()
        deadline = (time.monotonic() + self.args.seconds
                    if self.args.seconds else None)
        latest = {'loop1': None, 'loop2': None}

        while True:
            if deadline and time.monotonic() >= deadline:
                return 0
            if self.link is None and not self.connect():
                if not self.resync('no connection'):
                    continue
            try:
                for pkt in self.link.loop_packets(count=LOOP_BATCH):
                    latest[pkt['kind']] = pkt
                    self.hb.beat('ok', {
                        'observations': self.stats['observations'],
                        'crc_errors': self.link.crc_errors,
                        'port': self.port_name, 'baud': self.baud})
                    now = time.monotonic()
                    if now < next_emit:
                        continue
                    next_emit = now + period
                    self.emit(latest)
                    if deadline and time.monotonic() >= deadline:
                        return 0
            except (LinkError, NotAwake) as e:
                self.resync(e)
            except OSError as e:
                # The port vanished -- a USB reset, or the adapter moved.
                self.resync('port error: %s' % e)
            self.last_wall = self.last_wall   # keep the reference alive

    def emit(self, latest):
        if latest['loop1'] is None and latest['loop2'] is None:
            return
        # LPS interleaves the two packet types, so a LOOP2 is a second or two
        # behind the first LOOP1. Waiting for it costs one cycle and avoids
        # writing an observation with no 10-minute gust -- an empty column the
        # rest of the history does not have. If the firmware has no LOOP2 the
        # link says so and we emit from LOOP1.
        if (self.link is not None and self.link.loop2_available
                and latest['loop2'] is None):
            return
        if not self.clock_is_sane():
            return
        obs = observation_from_loop(latest['loop1'], latest['loop2'])
        obs['obs_ts'] = time.strftime('%Y-%m-%dT%H:%M:%S') + self.tz_suffix()
        # The instant is the record's identity. A second entry on the same
        # instant would store nothing and burn a sequence, so don't make one.
        if obs['obs_ts'] == self.last_emitted_ts:
            self.stats['skipped_same_instant'] += 1
            return
        self.last_emitted_ts = obs['obs_ts']
        if self.args.stdout:
            print(json.dumps(obs, sort_keys=True))
            self.stats['observations'] += 1
            return
        if not self.disk_ok():
            return
        try:
            seq = self.spool.append([obs], collector={
                'version': VERSION, 'port': self.port_name, 'baud': self.baud,
                'crc_errors': self.link.crc_errors if self.link else 0,
                'resyncs': self.stats['resyncs'],
            })
        except OSError as e:
            log('spool write failed: %s' % e)
            return
        self.stats['observations'] += 1
        self.spool.prune()
        if self.args.verbose:
            log('seq %d: %s %s kt avg %s gust %s'
                % (seq, obs['obs_ts'], obs.get('wind_kt'),
                   obs.get('wind_avg_kt'), obs.get('wind_gust_kt')))

    @staticmethod
    def tz_suffix():
        off = -(time.altzone if time.localtime().tm_isdst else time.timezone)
        sign = '+' if off >= 0 else '-'
        off = abs(off)
        return '%s%02d:%02d' % (sign, off // 3600, (off % 3600) // 60)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', default='COM4')
    p.add_argument('--baud', type=int, default=None,
                   help='skip the probe and use this rate')
    p.add_argument('--spool', default=r'C:\cycwx\send')
    p.add_argument('--heartbeat', default=r'C:\cycwx\heartbeat.json')
    p.add_argument('--period', type=float, default=DEFAULT_PERIOD_S,
                   help='seconds between spooled observations')
    p.add_argument('--seconds', type=float, default=None,
                   help='run for this long then exit (capture mode)')
    p.add_argument('--stdout', action='store_true',
                   help='print observations instead of spooling them')
    p.add_argument('--sim', action='store_true',
                   help='drive the console simulator instead of a port')
    p.add_argument('--verbose', action='store_true')
    p.add_argument('--quiet', action='store_true',
                   help='log to the file only, not to stderr')
    p.add_argument('--log', default='C:\\cycwx\\log\\collect.log')
    args = p.parse_args(argv)

    global _LOGGER
    if args.log:
        _LOGGER = setup_log(args.log,
                            quiet=getattr(args, 'quiet', False))

    c = Collector(args)
    try:
        return c.run()
    except KeyboardInterrupt:
        return 0
    finally:
        c.close()
        c.hb.beat('stopped', {'observations': c.stats['observations']})


if __name__ == '__main__':
    sys.exit(main())
