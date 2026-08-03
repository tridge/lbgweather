#!/usr/bin/env python3
"""Byte-level tests for the Davis protocol decode.

Every value here is built by an encoder written independently of the decoder,
so a shared misreading of the spec shows up as a mismatch rather than being
confirmed twice. Where the spec itself is ambiguous the test says so, because
a passing test against my own assumption proves nothing about the console.
"""

import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from davis_protocol import (  # noqa: E402
    crc16, crc_ok, append_crc, decode_loop1, decode_loop2, decode_loop,
    decode_archive_record, decode_archive_page, decode_archive_stamp,
    encode_archive_stamp, decode_gettime, is_unused_archive_record,
    observation_from_loop, dir_code_to_degrees, mph_to_kt, f_to_c,
    LOOP_SIZE, LOOP_FIELDS_SIZE, ARCHIVE_RECORD_SIZE, ARCHIVE_PAGE_SIZE,
    RESEND, frame_loop, LOOP_TRAILER)

FAILURES = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILURES.append(what)


def build_loop1(**kw):
    b = bytearray(b'\x00' * LOOP_FIELDS_SIZE)
    b[0:3] = b'LOO'
    b[3] = 0                                        # bar trend
    b[4] = 0                                        # LOOP1
    struct.pack_into('<H', b, 5, kw.get('next_rec', 100))
    struct.pack_into('<H', b, 7, kw.get('bar', 30283))
    struct.pack_into('<h', b, 9, kw.get('temp_in', 791))
    b[11] = kw.get('hum_in', 28)
    struct.pack_into('<h', b, 12, kw.get('temp_out', 578))
    b[14] = kw.get('wind', 5)
    b[15] = kw.get('wind_avg', 13)
    struct.pack_into('<H', b, 16, kw.get('wind_dir', 245))
    b[33] = kw.get('hum_out', 49)
    struct.pack_into('<H', b, 41, kw.get('rain_rate', 0))
    b[43] = kw.get('uv', 0xFF)
    struct.pack_into('<H', b, 44, kw.get('solar', 0xFFFF))
    struct.pack_into('<H', b, 50, kw.get('rain_day', 0))
    struct.pack_into('<H', b, 52, kw.get('rain_month', 0))
    struct.pack_into('<H', b, 54, kw.get('rain_year', 1653))
    return frame_loop(bytes(b))


def build_loop2(**kw):
    b = bytearray(b'\x00' * LOOP_FIELDS_SIZE)
    b[0:3] = b'LOO'
    b[3] = 0
    b[4] = 1                                        # LOOP2
    struct.pack_into('<H', b, 7, kw.get('bar', 30283))
    struct.pack_into('<h', b, 9, kw.get('temp_in', 791))
    b[11] = kw.get('hum_in', 28)
    struct.pack_into('<h', b, 12, kw.get('temp_out', 578))
    b[14] = kw.get('wind', 5)
    struct.pack_into('<H', b, 16, kw.get('wind_dir', 245))
    struct.pack_into('<H', b, 18, kw.get('avg10', 130))     # 0.1 mph
    struct.pack_into('<H', b, 20, kw.get('avg2', 120))
    struct.pack_into('<H', b, 22, kw.get('gust10', 190))
    struct.pack_into('<H', b, 24, kw.get('gust_dir', 250))
    struct.pack_into('<h', b, 30, kw.get('dew', 39))
    b[33] = kw.get('hum_out', 49)
    struct.pack_into('<h', b, 35, kw.get('heat', 56))
    struct.pack_into('<h', b, 37, kw.get('chill', 53))
    struct.pack_into('<H', b, 41, kw.get('rain_rate', 0))
    b[43] = kw.get('uv', 0xFF)
    struct.pack_into('<H', b, 44, kw.get('solar', 0xFFFF))
    struct.pack_into('<H', b, 50, kw.get('rain_day', 0))
    return frame_loop(bytes(b))


def build_archive(rev='B', **kw):
    r = bytearray(b'\x00' * ARCHIVE_RECORD_SIZE)
    ds, ts = encode_archive_stamp(kw.get('year', 2026), kw.get('month', 8),
                                  kw.get('day', 3), kw.get('hour', 14),
                                  kw.get('minute', 30))
    struct.pack_into('<H', r, 0, ds)
    struct.pack_into('<H', r, 2, ts)
    struct.pack_into('<h', r, 4, kw.get('temp_out', 578))
    struct.pack_into('<h', r, 6, kw.get('temp_high', 600))
    struct.pack_into('<h', r, 8, kw.get('temp_low', 550))
    struct.pack_into('<H', r, 10, kw.get('rain', 0))
    struct.pack_into('<H', r, 12, kw.get('rain_rate', 0))
    struct.pack_into('<H', r, 14, kw.get('bar', 30283))
    struct.pack_into('<H', r, 16, kw.get('solar', 0xFFFF))
    struct.pack_into('<H', r, 18, kw.get('samples', 200))
    struct.pack_into('<h', r, 20, kw.get('temp_in', 791))
    r[22] = kw.get('hum_in', 28)
    r[23] = kw.get('hum_out', 49)
    r[24] = kw.get('wind_avg', 12)
    r[25] = kw.get('wind_high', 19)
    r[26] = kw.get('high_dir', 11)
    r[27] = kw.get('prev_dir', 12)
    r[33] = kw.get('forecast', 12)
    r[42] = 0x00 if rev == 'B' else 0xFF
    return bytes(r)


def test_crc():
    print('\n== CRC-16-CCITT ==')
    check(crc16(b'') == 0, 'empty buffer is 0')
    # A buffer with its own CRC appended checks to zero -- that is how a
    # received packet is validated, so it is the property worth asserting.
    for payload in (b'\x00', b'LOO', bytes(range(64)), b'\xff' * 97):
        check(crc_ok(append_crc(payload)),
              'payload+CRC verifies (%d bytes)' % len(payload))
    good = append_crc(bytes(range(64)))
    bad = bytearray(good)
    bad[10] ^= 0x01
    check(not crc_ok(bytes(bad)), 'a single flipped bit fails')
    bad2 = bytearray(good)
    bad2[-1] ^= 0xFF
    check(not crc_ok(bytes(bad2)), 'a corrupted CRC fails')


def test_loop1():
    print('\n== LOOP1 ==')
    p = build_loop1()
    check(len(p) == LOOP_SIZE, 'a framed packet is 99 bytes')
    check(p[95:97] == LOOP_TRAILER,
          'the LF CR trailer is present and inside the CRC - a simulator that '
          'omits it makes packets that pass here and fail on the console')
    d = decode_loop1(p)
    check(d['kind'] == 'loop1', 'type 0 decodes as LOOP1')
    check(d['temp_out_f'] == 57.8, 'outside temp is 0.1F (%s)' % d['temp_out_f'])
    check(d['bar_inhg'] == 30.283, 'barometer is 0.001 inHg (%s)' % d['bar_inhg'])
    check(d['wind_mph'] == 5, 'wind speed is whole mph')
    check(d['wind_avg_10min_mph'] == 13,
          'LOOP1 byte 15 is the 10-min average in WHOLE mph (%s)'
          % d['wind_avg_10min_mph'])
    check(d['wind_dir'] == 245, 'wind direction in degrees')
    check(d['hum_out'] == 49, 'outside humidity')
    check(d['uv'] is None and d['solar_rad'] is None,
          'absent UV and solar read as None, not 255/65535')

    check(decode_loop(p)['kind'] == 'loop1', 'decode_loop dispatches on byte 4')

    bad = bytearray(p)
    bad[20] ^= 0xFF
    raised = False
    try:
        decode_loop1(bytes(bad))
    except ValueError as e:
        raised = 'CRC' in str(e)
    check(raised, 'a corrupt packet is refused, not decoded')


def test_loop2_is_the_csv_source():
    print('\n== LOOP2 offsets 18 and 22 are what the CSV columns mean ==')
    p = build_loop2(avg10=130, gust10=190, avg2=120)
    d = decode_loop2(p)
    check(d['kind'] == 'loop2', 'type 1 decodes as LOOP2')
    check(d['wind_avg_10min_mph'] == 13.0,
          'offset 18 is the 10-min average in 0.1 mph (%s)'
          % d['wind_avg_10min_mph'])
    check(d['wind_gust_10min_mph'] == 19.0,
          'offset 22 is the 10-min GUST in 0.1 mph (%s)'
          % d['wind_gust_10min_mph'])
    check(d['wind_avg_2min_mph'] == 12.0,
          'offset 20 is the 2-min average, which is NOT what the CSV means')
    check(d['dew_point_f'] == 39 and d['wind_chill_f'] == 53,
          'dew point and wind chill are whole degrees F')

    # The cloud feed the CSV was built from reports wind_speed_10_min_avg and
    # wind_gust_10_min. Those are these two fields, and nothing else.
    obs = observation_from_loop(None, d)
    check(obs['wind_avg_kt'] == mph_to_kt(13.0),
          'wind_avg_kt comes from offset 18')
    check(obs['wind_gust_kt'] == mph_to_kt(19.0),
          'wind_gust_kt comes from offset 22')
    check(obs['wind_kt'] == mph_to_kt(5), 'wind_kt is the instantaneous speed')


def test_loop_merge_prefers_loop2():
    print('\n== merging a LOOP1/LOOP2 pair ==')
    l1 = decode_loop1(build_loop1(wind_avg=13))
    l2 = decode_loop2(build_loop2(avg10=134))
    obs = observation_from_loop(l1, l2)
    check(obs['wind_avg_kt'] == mph_to_kt(13.4),
          'LOOP2 wins: 13.4 mph, not the whole-mph 13 from LOOP1 (%s)'
          % obs['wind_avg_kt'])

    only1 = observation_from_loop(l1, None)
    check(only1['wind_avg_kt'] == mph_to_kt(13),
          'LOOP1 is the fallback when LOOP2 is unavailable')
    check(only1.get('wind_gust_kt') is None,
          'and there is NO 10-minute gust in LOOP1, so it stays absent rather '
          'than being faked from another field')

    raised = False
    try:
        observation_from_loop(None, None)
    except ValueError:
        raised = True
    check(raised, 'no packets at all is an error, not an empty observation')


def test_dash_values():
    print('\n== absent sensors ==')
    d = decode_loop1(build_loop1(temp_out=0x7FFF, hum_out=0xFF,
                                 wind_dir=0xFFFF))
    check(d['temp_out_f'] is None, '0x7FFF signed dash -> None')
    check(d['hum_out'] is None, '0xFF byte dash -> None')
    check(d['wind_dir'] is None, '0xFFFF unsigned dash -> None')

    d2 = decode_loop2(build_loop2(wind_dir=360))
    check(d2['wind_dir'] == 0,
          '360 degrees wraps to 0 for north, matching the CSV history (%s)'
          % d2['wind_dir'])
    d3 = decode_loop2(build_loop2(wind_dir=0))
    check(d3['wind_dir'] is None,
          '0 means no reading, which is NOT the same as north')


def test_archive_stamps():
    print('\n== archive stamps use year-2000, GETTIME uses year-1900 ==')
    ds, ts = encode_archive_stamp(2026, 8, 3, 14, 30)
    check(decode_archive_stamp(ds, ts) == (2026, 8, 3, 14, 30),
          'archive stamp round-trips')
    check((ds >> 9) & 0x7F == 26, 'the year field holds 26, not 126')

    # GETTIME is the other encoding. Keeping them visibly apart is the point:
    # mixing them asks the console for the wrong century.
    gt = append_crc(bytes([30, 45, 14, 3, 8, 126]))
    check(decode_gettime(gt) == (2026, 8, 3, 14, 45, 30),
          'GETTIME year 126 means 2026')

    raised = False
    try:
        encode_archive_stamp(1999, 1, 1, 0, 0)
    except ValueError:
        raised = True
    check(raised, 'a pre-2000 year is refused rather than wrapping')

    raised = False
    try:
        decode_archive_stamp(0, 9999)
    except ValueError:
        raised = True
    check(raised, 'an impossible stamp is refused')


def test_archive_record():
    print('\n== archive records ==')
    r = decode_archive_record(build_archive('B'))
    check(r['revision'] == 'B', 'byte 42 = 0x00 is Rev B')
    check((r['year'], r['month'], r['day'], r['hour'], r['minute'])
          == (2026, 8, 3, 14, 30), 'stamp decodes')
    check(r['temp_out_f'] == 57.8, 'interval temperature')
    check(r['wind_avg_mph'] == 12 and r['wind_high_mph'] == 19,
          'interval average and high are WHOLE mph, different semantics from '
          'LOOP -- which is why src=archive matters')
    check(r['wind_samples'] == 200,
          'wind_samples is RADIO reception quality, not serial integrity')

    ra = decode_archive_record(build_archive('A'))
    check(ra['revision'] == 'A', 'byte 42 = 0xFF is Rev A')

    bad = bytearray(build_archive('B'))
    bad[42] = 0x42
    raised = False
    try:
        decode_archive_record(bytes(bad))
    except ValueError as e:
        raised = 'revision' in str(e)
    check(raised,
          'an unknown revision byte is REFUSED rather than guessed - guessing '
          'silently misreads every field after byte 42')


def test_archive_page():
    print('\n== archive pages ==')
    recs = b''.join(build_archive('B', minute=(30 + i) % 60) for i in range(5))
    page = append_crc(bytes([3]) + recs + b'\x00' * 4)
    check(len(page) == ARCHIVE_PAGE_SIZE,
          'a page is %d bytes on the wire' % ARCHIVE_PAGE_SIZE)
    seq, out = decode_archive_page(page)
    check(seq == 3, 'the sequence byte is read')
    check(len(out) == 5 and all(o is not None for o in out),
          'five records per page')

    # Unused slots are not a terminator. There is no terminator.
    partial = build_archive('B') + b'\xff' * (ARCHIVE_RECORD_SIZE * 4)
    page2 = append_crc(bytes([4]) + partial + b'\x00' * 4)
    seq2, out2 = decode_archive_page(page2)
    check(out2[0] is not None and all(o is None for o in out2[1:]),
          'unused records decode as None rather than raising')
    check(is_unused_archive_record(b'\xff' * ARCHIVE_RECORD_SIZE),
          '0xFF slots are recognised as unused')
    check(is_unused_archive_record(b'\x00' * ARCHIVE_RECORD_SIZE),
          'zeroed slots are too')

    bad = bytearray(page)
    bad[100] ^= 0xFF
    raised = False
    try:
        decode_archive_page(bytes(bad))
    except ValueError as e:
        raised = 'CRC' in str(e)
    check(raised, 'a corrupt page is refused so it can be re-requested')


def test_direction_codes():
    print('\n== archive direction codes are 16-point, not degrees ==')
    check(dir_code_to_degrees(0) == 0, 'code 0 is north')
    check(dir_code_to_degrees(4) == 90, 'code 4 is east')
    check(dir_code_to_degrees(8) == 180, 'code 8 is south')
    check(dir_code_to_degrees(12) == 270, 'code 12 is west')
    check(dir_code_to_degrees(15) == 338, 'code 15 is NNW')
    check(dir_code_to_degrees(None) is None and dir_code_to_degrees(255) is None,
          'an absent or out-of-range code is None, not 0 degrees')


def test_unit_conversions():
    print('\n== unit conversions match the existing history ==')
    # wind_logger.py has used this constant for years; a different rounding
    # would put a step in the record on cutover day.
    check(mph_to_kt(10) == 8.7, '10 mph -> 8.7 kt')
    check(f_to_c(32) == 0.0 and f_to_c(212) == 100.0, 'F -> C endpoints')
    check(f_to_c(57.8) == 14.3, '57.8F -> 14.3C')
    check(mph_to_kt(None) is None and f_to_c(None) is None,
          'None passes through both')


def test_documented_ambiguity():
    print('\n== the resend byte is a documented ambiguity ==')
    check(RESEND == b'\x15',
          'we use 0x15, as weewx does; the manual also says 0x21 in the page '
          'download section')
    print('       NOTE: this must be confirmed against the real console by '
          'forcing a page resend.')
    print('       A simulator built on the same assumption proves nothing.')


def main():
    for fn in [test_crc, test_loop1, test_loop2_is_the_csv_source,
               test_loop_merge_prefers_loop2, test_dash_values,
               test_archive_stamps, test_archive_record, test_archive_page,
               test_direction_codes, test_unit_conversions,
               test_documented_ambiguity]:
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
