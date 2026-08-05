#!/usr/bin/env python3
"""Interrogate the console, read-only, and settle the open questions.

This is the tool to run the moment WeatherLink is stopped. It answers, against
the real hardware, the things a simulator cannot:

  * which baud rate the console is actually configured for;
  * the archive interval (EEPROM 0x2D), which sets the DMPAFT cadence and the
    backfill horizon;
  * the rain bucket type (EEPROM 0x2B), which scales every rain field;
  * whether this firmware answers LPS, and therefore whether LOOP2 offsets 18
    and 22 are available at all;
  * whether 0x15 is really the resend byte. The manual says 0x15 in one place
    and 0x21 in another; weewx uses 0x15. Nothing but the console settles it.

It sends no command that writes: no SETTIME, EEWR, SETPER or CLRLOG. The worst
it can do is hold the port, which is why it also reports how to check ownership.

`--ownership` alone is safe to run while WeatherLink is up: an access-denied
result is the proof that the port is single-owner.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from davis_link import (Link, LinkError, BAUD_CANDIDATES,  # noqa: E402
                        MAX_PAGE_RESENDS)
from davis_protocol import (observation_from_loop, mph_to_kt,  # noqa: E402
                            RESEND, ARCHIVE_PAGE_SIZE, crc_ok)

BUCKET = {0: '0.01 in', 1: '0.2 mm', 2: '0.1 mm'}


def out(msg):
    print(msg)
    sys.stdout.flush()


def check_ownership(port_name):
    """Can the port be opened at all? Safe while WeatherLink is running."""
    try:
        import serial
    except ImportError:
        out('pyserial is not installed; cannot check ownership')
        return 2
    try:
        p = serial.Serial(port=port_name, baudrate=19200, timeout=0.5)
    except Exception as e:
        out('%s is NOT available: %s' % (port_name, e))
        out('If WeatherLink is running, that is the expected answer and it '
            'proves the port is single-owner.')
        return 1
    p.close()
    out('%s opened cleanly: nothing else is holding it.' % port_name)
    return 0


def probe(args):
    import serial
    findings = {'probed_at': time.strftime('%Y-%m-%dT%H:%M:%S')}

    port = None
    for baud in ([args.baud] if args.baud else BAUD_CANDIDATES):
        try:
            cand = serial.Serial(port=args.port, baudrate=baud, bytesize=8,
                                 parity='N', stopbits=1, timeout=1.0)
        except Exception as e:
            out('cannot open %s at %d: %s' % (args.port, baud, e))
            continue
        try:
            Link(cand).wake()
            out('BAUD: console answers at %d' % baud)
            findings['baud'] = baud
            port = cand
            break
        except LinkError:
            cand.close()
    if port is None:
        out('FAILED: no baud rate produced a wakeup. If the console is sitting '
            'in a setup or numeric-entry screen it ignores serial commands -- '
            'press DONE on it and retry.')
        return None

    link = Link(port, lambda m: out('  link: %s' % m))
    try:
        try:
            y, mo, d, hh, mm, ss = link.get_time()
            console = '%04d-%02d-%02d %02d:%02d:%02d' % (y, mo, d, hh, mm, ss)
            drift = time.mktime((y, mo, d, hh, mm, ss, 0, 0, -1)) - time.time()
            out('CONSOLE TIME: %s (drift %+.0fs vs this PC)' % (console, drift))
            out('  NOT corrected. The console is read-only; drift is reported '
                'so a wrong PC clock is distinguishable from a wrong console.')
            findings['console_time'] = console
            findings['console_drift_s'] = round(drift)
        except LinkError as e:
            out('GETTIME failed: %s' % e)

        for name, addr, fmt in (('archive_interval_min', 0x2D, int),
                                ('rain_bucket_code', 0x2B, int)):
            try:
                v = link.eeprom(addr, 1)[0]
                if name == 'rain_bucket_code':
                    v &= 0x0F
                    out('RAIN BUCKET: code %d (%s)'
                        % (v, BUCKET.get(v, 'unknown')))
                else:
                    out('ARCHIVE INTERVAL: %d minutes' % v)
                    out('  This sets the DMPAFT cadence and the backfill '
                        'horizon: 2560 records is %.1f days at this interval.'
                        % (2560 * v / 1440.0))
                findings[name] = v
            except LinkError as e:
                out('EEBRD %02X failed: %s' % (addr, e))

        try:
            rx = link.rx_check()
            out('RADIO: %s%% good (%d received, %d missed, %d CRC errors)'
                % (rx['good_pct'], rx['total_received'], rx['misses'],
                   rx['crc_errors']))
            out('  This is the ISS-to-console link, NOT our serial integrity.')
            findings['radio'] = rx
        except LinkError as e:
            out('RXCHECK failed: %s' % e)

        # LOOP: does this firmware give us LOOP2, and what do 18 and 22 say?
        l1 = l2 = None
        try:
            for pkt in link.loop_packets(count=6):
                if pkt['kind'] == 'loop1':
                    l1 = pkt
                else:
                    l2 = pkt
        except LinkError as e:
            out('LOOP failed: %s' % e)
        findings['loop2_available'] = bool(l2)
        if l2:
            out('LOOP2: available. offset 18 = %.1f mph, offset 22 = %.1f mph'
                % (l2['wind_avg_10min_mph'] or 0, l2['wind_gust_10min_mph'] or 0))
            out('  CHECK THESE against the cloud feed at the same moment:')
            out('    offset 18 should equal wind_speed_10_min_avg')
            out('    offset 22 should equal wind_gust_10_min')
            out('  That comparison is the only real oracle for these two -- no '
                'local weewx has LOOP2, and the simulator shares my reading of '
                'the spec.')
        else:
            out('LOOP2: NOT available on this firmware. wind_gust_kt would be '
                'absent, which breaks continuity with the CSV history.')
        if l1 or l2:
            obs = observation_from_loop(l1, l2)
            out('OBSERVATION as the collector would store it:')
            out('  ' + json.dumps(obs, sort_keys=True))
            findings['observation'] = obs

        if args.force_resend:
            out('RESEND: forcing a page resend to settle 0x15 vs 0x21...')
            findings['resend'] = force_resend(link, args)
    finally:
        try:
            link.stop_loop()
        finally:
            port.close()
    return findings


def force_resend(link, args):
    """Ask for one archive page, deliberately request it again, see what comes.

    The point is not the data. It is whether the console honours 0x15 as
    "send that page again". If it does, the page arrives twice; if 0x15 means
    something else, the stream desynchronises and that is equally informative.
    """
    import struct
    from davis_protocol import ACK, append_crc, encode_archive_stamp
    try:
        link.wake()
        link.command('DMPAFT')
        # A cursor an hour back: enough to guarantee at least one page without
        # asking for the whole archive.
        t = time.localtime(time.time() - 3600)
        ds, ts = encode_archive_stamp(t.tm_year, t.tm_mon, t.tm_mday,
                                      t.tm_hour, t.tm_min)
        link.port.write(append_crc(struct.pack('<HH', ds, ts)))
        if link._read_exact(1, 3.0) != ACK:
            return {'ok': False, 'why': 'stamp rejected'}
        header = link._read_exact(6, 3.0)
        pages = struct.unpack('<HH', header[:4])[0] if crc_ok(header) else 0
        if not pages:
            return {'ok': False, 'why': 'console reported no pages'}
        link.port.write(ACK)
        first = link._read_exact(ARCHIVE_PAGE_SIZE, 6.0)
        link.port.write(RESEND)
        again = link._read_exact(ARCHIVE_PAGE_SIZE, 6.0)
        same = first == again
        out('  first page CRC ok: %s; resent page CRC ok: %s; identical: %s'
            % (crc_ok(first), crc_ok(again), same))
        if same and crc_ok(again):
            out('  CONFIRMED: 0x15 is the resend byte on this console.')
        else:
            out('  INCONCLUSIVE: the second read differs. 0x15 may not mean '
                'resend here -- try 0x21 before trusting the archive path.')
        link.port.write(b'\x1b')          # cancel the dump
        return {'ok': True, 'confirmed': bool(same and crc_ok(again))}
    except LinkError as e:
        return {'ok': False, 'why': str(e)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', default='COM4')
    p.add_argument('--baud', type=int, default=None)
    p.add_argument('--ownership', action='store_true',
                   help='only check whether the port can be opened')
    p.add_argument('--force-resend', action='store_true',
                   help='settle the 0x15 vs 0x21 ambiguity against the console')
    p.add_argument('--save', default=None, help='write findings as JSON')
    args = p.parse_args(argv)

    if args.ownership:
        return check_ownership(args.port)

    findings = probe(args)
    if findings is None:
        return 1
    if args.save:
        with open(args.save, 'w') as f:
            json.dump(findings, f, indent=1, sort_keys=True)
        out('findings written to %s' % args.save)
    return 0


if __name__ == '__main__':
    sys.exit(main())
