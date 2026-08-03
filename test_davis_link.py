#!/usr/bin/env python3
"""Link-layer tests against the console simulator, including fault injection.

What these DO prove: the link recovers from silence, dropped bytes, corrupt
CRCs, a refused LPS and a console that will not wake, and that every read is
bounded so a wedged owner cannot hold COM4 for ever.

What they do NOT prove: that the simulator behaves like the real console.
Simulator and decoder share one reading of the spec. The resend byte, the LOOP2
offsets and the baud rate can only be settled against the hardware.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from davis_link import Link, LinkError, NotAwake, BAUD_CANDIDATES  # noqa: E402
from fakeconsole import FakeConsole, Fault, make_archive_record  # noqa: E402
from davis_protocol import mph_to_kt, observation_from_loop  # noqa: E402

FAILURES = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILURES.append(what)


def test_wakeup():
    print('\n== wakeup ==')
    c = FakeConsole()
    link = Link(c)
    check(link.wake() is True, 'a sleeping console wakes on a bare LF')
    check(link.wakeups == 1, 'and the wakeup is counted')

    c2 = FakeConsole(fault=Fault(never_wake=True))
    link2 = Link(c2)
    t0 = time.time()
    raised = ''
    try:
        link2.wake()
    except NotAwake as e:
        raised = str(e)
    took = time.time() - t0
    check('did not answer' in raised, 'a console that never answers raises')
    check('setup or numeric-entry screen' in raised,
          'and names the non-USB cause, which reads identically otherwise')
    check(took < 8, 'bounded: gave up in %.1fs rather than blocking' % took)


def test_loop_stream():
    print('\n== LOOP streaming ==')
    c = FakeConsole()
    link = Link(c)
    packets = list(link.loop_packets(count=6))
    check(len(packets) == 6, 'six packets read (%d)' % len(packets))
    kinds = [p['kind'] for p in packets]
    check(kinds.count('loop1') == 3 and kinds.count('loop2') == 3,
          'LPS 3 n interleaves LOOP1 and LOOP2, and n is the TOTAL count (%s)'
          % kinds)
    check(c.commands and c.commands[0].startswith('LPS 3'),
          'the LPS form is used (%s)' % c.commands[:1])

    l1 = next(p for p in packets if p['kind'] == 'loop1')
    l2 = next(p for p in packets if p['kind'] == 'loop2')
    obs = observation_from_loop(l1, l2)
    check(obs['wind_avg_kt'] == mph_to_kt(13.0),
          'the merged observation takes its 10-min average from LOOP2')
    check(obs['wind_gust_kt'] == mph_to_kt(19.0),
          'and its 10-min gust from LOOP2 offset 22')


def test_corrupt_packets_are_skipped_not_fatal():
    print('\n== corrupt LOOP packets ==')
    c = FakeConsole(fault=Fault(corrupt_crc_every=3))
    link = Link(c)
    packets = list(link.loop_packets(count=9))
    check(len(packets) == 6,
          'the three corrupt packets are dropped, the rest survive (%d)'
          % len(packets))
    check(link.crc_errors == 3, 'and are counted as SERIAL errors (%d)'
          % link.crc_errors)
    check(all(p.get('wind_dir') == 245 for p in packets),
          'the surviving packets decoded correctly')


def test_silence_is_bounded():
    print('\n== a console that goes silent mid-stream ==')
    c = FakeConsole(fault=Fault(silent=True))
    link = Link(c)
    t0 = time.time()
    raised = False
    try:
        list(link.loop_packets(count=4))
    except (LinkError, NotAwake):
        raised = True
    took = time.time() - t0
    check(raised, 'silence raises rather than hanging')
    check(took < 10,
          'and does so in %.1fs -- a blocked read here holds COM4 for ever '
          'and the watchdog cannot take it back' % took)


def test_dropped_bytes():
    print('\n== a short read ==')
    c = FakeConsole(fault=Fault(drop_bytes=5))
    link = Link(c)
    raised = False
    try:
        list(link.loop_packets(count=2))
    except LinkError:
        raised = True
    check(raised, 'a truncated packet is a link error, not a bad decode')


def test_lps_refused_falls_back():
    print('\n== firmware without LOOP2 ==')
    c = FakeConsole(fault=Fault(refuse_lps=True))
    link = Link(c)
    packets = list(link.loop_packets(count=3))
    check(len(packets) == 3, 'the LOOP fallback still yields packets')
    check(all(p['kind'] == 'loop1' for p in packets),
          'all LOOP1, since this firmware has no LOOP2')
    check(any(cmd.startswith('LOOP') for cmd in c.commands),
          'the plain LOOP command was issued after the NAK (%s)' % c.commands)

    obs = observation_from_loop(packets[0], None)
    check(obs['wind_avg_kt'] == mph_to_kt(13),
          'the average falls back to LOOP1 whole mph')
    check(obs['wind_gust_kt'] is None,
          'and the 10-minute gust is ABSENT rather than substituted from '
          'another field, which would corrupt the CSV column')


def test_eeprom_reads():
    print('\n== EEPROM: archive interval and rain bucket ==')
    c = FakeConsole(eeprom={0x2B: 0x01, 0x2D: 10})
    link = Link(c)
    check(link.archive_interval_minutes() == 10,
          'archive interval from 0x2D -- this sets the DMPAFT cadence and the '
          'backfill horizon')
    check(link.rain_bucket_type() == 1,
          'rain bucket type from 0x2B low nibble (1 = 0.2mm, as installed)')


def test_rxcheck_is_radio_not_serial():
    print('\n== RXCHECK reports the RADIO link ==')
    c = FakeConsole()
    link = Link(c)
    stats = link.rx_check()
    check(stats['total_received'] == 7311 and stats['misses'] == 16612,
          'counters parsed (%s)' % stats)
    check(stats['good_pct'] == 30.6,
          'reception percentage computed (%s%%)' % stats['good_pct'])
    check(link.crc_errors == 0,
          'and NONE of it touched the serial CRC counter -- these measure '
          'different links and conflating them would make us reopen COM4 '
          'because the weather looks wrong')


def test_archive_download():
    print('\n== DMPAFT archive download ==')
    recs = [make_archive_record(2026, 8, 3, 10, m) for m in (0, 10, 20, 30, 40)]
    c = FakeConsole(archive_records=recs)
    link = Link(c)

    got = link.dump_after(2026, 8, 3, 9, 50)
    check(len(got) == 5, 'five records returned (%d)' % len(got))
    check([r['minute'] for r in got] == [0, 10, 20, 30, 40],
          'in order (%s)' % [r['minute'] for r in got])
    check(all(r['revision'] == 'B' for r in got), 'decoded as Rev B')
    check(got[0]['wind_avg_mph'] == 12 and got[0]['wind_high_mph'] == 19,
          'interval average and high, which are NOT the LOOP semantics')


def test_archive_stops_on_declining_stamps():
    print('\n== there is no archive terminator ==')
    recs = [make_archive_record(2026, 8, 3, 10, 0),
            make_archive_record(2026, 8, 3, 10, 10),
            # An old record left in the ring buffer: the stamp goes backwards.
            make_archive_record(2026, 7, 1, 3, 0),
            make_archive_record(2026, 7, 1, 3, 10)]
    c = FakeConsole(archive_records=recs)
    link = Link(c)

    got = link.dump_after(2026, 8, 3, 9, 50)
    check(len(got) == 2,
          'the download stops where the stamps decline, rather than looking '
          'for a terminator that does not exist (%d records)' % len(got))
    check(got[-1]['minute'] == 10, 'and keeps only the current data')


def test_page_resend_recovers():
    print('\n== a corrupt archive page is re-requested and recovers ==')
    recs = [make_archive_record(2026, 8, 3, 10, m) for m in (0, 10)]
    c = FakeConsole(archive_records=recs, fault=Fault(corrupt_crc_every=1))
    link = Link(c)
    got = link.dump_after(2026, 8, 3, 9, 50)
    check(c.resend_requests > 0,
          'a resend was requested (%d)' % c.resend_requests)
    check(len(got) == 2,
          'and the retry succeeded, so no data was lost to a line error (%d)'
          % len(got))
    check(link.crc_errors == 1, 'the CRC error is counted (%d)'
          % link.crc_errors)


def test_page_resend_gives_up_eventually():
    print('\n== a page that stays corrupt does not retry for ever ==')
    recs = [make_archive_record(2026, 8, 3, 10, 0)]
    c = FakeConsole(archive_records=recs,
                    fault=Fault(corrupt_crc_every=1, corrupt_resends=True))
    link = Link(c)
    raised = ''
    try:
        link.dump_after(2026, 8, 3, 9, 50)
    except LinkError as e:
        raised = str(e)
    check('resend' in raised,
          'it gives up with a clear reason rather than looping (%r)'
          % raised[:60])
    check(c.resend_requests == 3,
          'after a bounded number of attempts (%d)' % c.resend_requests)
    print('       NOTE: the resend byte is 0x15 here on weewx\'s authority. '
          'The manual also says 0x21.')
    print('       Only forcing a resend on the REAL console settles it.')


def test_baud_candidates():
    print('\n== baud probe order ==')
    check(BAUD_CANDIDATES[0] == 19200, '19200 first: the Vantage default')
    check(14400 in BAUD_CANDIDATES,
          '14400 is included -- the console offers it and an installer may '
          'have chosen it')
    check(list(BAUD_CANDIDATES) == sorted(BAUD_CANDIDATES, reverse=True),
          'descending, so the fastest working rate wins')


def main():
    for fn in [test_wakeup, test_loop_stream,
               test_corrupt_packets_are_skipped_not_fatal,
               test_silence_is_bounded, test_dropped_bytes,
               test_lps_refused_falls_back, test_eeprom_reads,
               test_rxcheck_is_radio_not_serial, test_archive_download,
               test_archive_stops_on_declining_stamps,
               test_page_resend_recovers,
               test_page_resend_gives_up_eventually,
               test_baud_candidates]:
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
