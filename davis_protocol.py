#!/usr/bin/env python3
"""Davis Vantage serial protocol: framing and decode only, no I/O.

Separated from the link layer so every byte-level rule can be tested without a
console, a serial port or a simulator. The station is READ ONLY -- nothing here
encodes SETTIME, EEWR, SETPER or CLRLOG, and that is what makes a rollback
instant.

Field semantics that matter for continuity with the existing history:

  * `wind_avg_kt` in the CSV has always meant the console's 10-minute average,
    and `wind_gust_kt` its 10-minute high. Those are LOOP2 offsets 18 and 22.
    Substituting a 1-minute mean or max would put a different measurement in a
    column with seven years of history behind it.
  * LOOP1 byte 15 is also a 10-minute average, but in whole mph. LOOP2 carries
    it in 0.1 mph, so LOOP2 is preferred and LOOP1 is the fallback.

Two encodings of the year appear and are deliberately kept apart: archive
date stamps use year-2000, GETTIME uses year-1900.
"""

import struct

LOOP_SIZE = 99
# 95 declared fields, then LF CR, then the 2-byte CRC. The trailer is inside
# the CRC, so a simulator that omits it produces packets that validate here
# and would not validate against the console.
LOOP_FIELDS_SIZE = 95
LOOP_TRAILER = b'\n\r'
ARCHIVE_RECORD_SIZE = 52
ARCHIVE_PAGE_SIZE = 267          # 1 seq + 5*52 + 4 unused + 2 CRC

ACK = b'\x06'
NAK = b'\x21'
# The manual contradicts itself: 0x15 in the command overview, 0x21 in the page
# download procedure. weewx uses 0x15 and so do we -- but this is a documented
# ambiguity, so it must be confirmed against the real console by forcing a
# page resend, not against a simulator built on the same assumption.
RESEND = b'\x15'
CANCEL = b'\x1b'

# Dash values. A sensor that is absent reports these rather than a reading.
DASH_U8 = 0xFF
DASH_S16 = 0x7FFF
DASH_U16 = 0xFFFF

MPH_TO_KNOTS = 0.868976


def _crc_table():
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 \
                else (crc << 1) & 0xFFFF
        table.append(crc)
    return tuple(table)


CRC_TABLE = _crc_table()


def crc16(data):
    """Davis CRC-16-CCITT, seed 0. Over a buffer INCLUDING its trailing CRC
    the result is 0, which is how received packets are checked."""
    crc = 0
    for b in data:
        crc = (CRC_TABLE[(crc >> 8) ^ b] ^ ((crc << 8) & 0xFFFF)) & 0xFFFF
    return crc


def crc_ok(data):
    return crc16(data) == 0


def append_crc(payload):
    return payload + struct.pack('>H', crc16(payload))


def frame_loop(fields):
    """Wrap 95 decoded-field bytes as the console sends them."""
    if len(fields) != LOOP_FIELDS_SIZE:
        raise ValueError('LOOP fields are %d bytes, expected %d'
                         % (len(fields), LOOP_FIELDS_SIZE))
    return append_crc(fields + LOOP_TRAILER)


def f_to_c(f):
    return None if f is None else round((f - 32) * 5.0 / 9.0, 1)


def mph_to_kt(mph):
    return None if mph is None else round(mph * MPH_TO_KNOTS, 1)


def _u8(buf, off):
    v = buf[off]
    return None if v == DASH_U8 else v


def _u16(buf, off):
    v = struct.unpack_from('<H', buf, off)[0]
    return None if v == DASH_U16 else v


def _s16(buf, off):
    v = struct.unpack_from('<h', buf, off)[0]
    return None if v == DASH_S16 else v


def _tenths(v):
    return None if v is None else v / 10.0


def _wind_dir(v):
    """0 means no reading, 360 means north. The CSV has always stored 0 for
    north, so 360 wraps rather than appearing as an eighth cardinal point."""
    if v is None or v == 0:
        return None
    return 0 if v == 360 else v


def packet_type(buf):
    """LOOP1 or LOOP2, from byte 4. Both are 99 bytes and both start 'LOO', so
    nothing else distinguishes them."""
    if len(buf) < 5 or buf[0:3] != b'LOO':
        raise ValueError('not a LOOP packet')
    return buf[4]


def decode_loop1(buf):
    if len(buf) != LOOP_SIZE:
        raise ValueError('LOOP1 is %d bytes, expected %d'
                         % (len(buf), LOOP_SIZE))
    if not crc_ok(buf):
        raise ValueError('LOOP1 CRC failed')
    if packet_type(buf) != 0:
        raise ValueError('not a LOOP1 packet (type %d)' % packet_type(buf))
    return {
        'kind': 'loop1',
        'bar_trend': struct.unpack_from('<b', buf, 3)[0],
        'bar_inhg': _tenths_thousandths(_u16(buf, 7)),
        'temp_in_f': _tenths(_s16(buf, 9)),
        'hum_in': _u8(buf, 11),
        'temp_out_f': _tenths(_s16(buf, 12)),
        'wind_mph': _u8(buf, 14),
        # Whole mph. LOOP2 gives the same average in 0.1 mph.
        'wind_avg_10min_mph': _u8(buf, 15),
        'wind_dir': _wind_dir(_u16(buf, 16)),
        'hum_out': _u8(buf, 33),
        'rain_rate_clicks': _u16(buf, 41),
        'uv': _u8(buf, 43),
        'solar_rad': _u16(buf, 44),
        'rain_day_clicks': _u16(buf, 50),
        'rain_month_clicks': _u16(buf, 52),
        'rain_year_clicks': _u16(buf, 54),
        'next_archive_record': struct.unpack_from('<H', buf, 5)[0],
    }


def _tenths_thousandths(v):
    return None if v is None else v / 1000.0


def decode_loop2(buf):
    if len(buf) != LOOP_SIZE:
        raise ValueError('LOOP2 is %d bytes, expected %d'
                         % (len(buf), LOOP_SIZE))
    if not crc_ok(buf):
        raise ValueError('LOOP2 CRC failed')
    if packet_type(buf) != 1:
        raise ValueError('not a LOOP2 packet (type %d)' % packet_type(buf))
    return {
        'kind': 'loop2',
        'bar_trend': struct.unpack_from('<b', buf, 3)[0],
        'bar_inhg': _tenths_thousandths(_u16(buf, 7)),
        'temp_in_f': _tenths(_s16(buf, 9)),
        'hum_in': _u8(buf, 11),
        'temp_out_f': _tenths(_s16(buf, 12)),
        'wind_mph': _u8(buf, 14),
        'wind_dir': _wind_dir(_u16(buf, 16)),
        # THE two fields the CSV's wind_avg_kt and wind_gust_kt columns mean.
        # The averages are 0.1 mph; the GUST is whole mph. The spec reads as
        # though offset 22 were tenths too, and decoding it that way produced a
        # 10-minute gust of 2.6 mph beneath a 10-minute average of 17.4 -- which
        # is impossible, and is how the real unit was caught. Offset 24 is the
        # gust's DIRECTION, not another speed.
        'wind_avg_10min_mph': _tenths(_u16(buf, 18)),
        'wind_avg_2min_mph': _tenths(_u16(buf, 20)),
        'wind_gust_10min_mph': _u16(buf, 22),
        'wind_gust_10min_dir': _wind_dir(_u16(buf, 24)),
        'dew_point_f': _s16_plain(buf, 30),
        'hum_out': _u8(buf, 33),
        'heat_index_f': _s16_plain(buf, 35),
        'wind_chill_f': _s16_plain(buf, 37),
        'rain_rate_clicks': _u16(buf, 41),
        'uv': _u8(buf, 43),
        'solar_rad': _u16(buf, 44),
        'rain_day_clicks': _u16(buf, 50),
    }


def _s16_plain(buf, off):
    """Dew point, heat index and wind chill are whole degrees F."""
    v = struct.unpack_from('<h', buf, off)[0]
    return None if v == DASH_S16 or v == -32768 else v


def decode_loop(buf):
    t = packet_type(buf)
    if t == 0:
        return decode_loop1(buf)
    if t == 1:
        return decode_loop2(buf)
    raise ValueError('unknown LOOP packet type %d' % t)


# ------------------------------------------------------------------ archive

def decode_archive_stamp(datestamp, timestamp):
    """Archive stamps carry year-2000. Returns (y, m, d, hh, mm)."""
    day = datestamp & 0x1F
    month = (datestamp >> 5) & 0x0F
    year = ((datestamp >> 9) & 0x7F) + 2000
    hour, minute = divmod(timestamp, 100)
    if not (1 <= month <= 12 and 1 <= day <= 31
            and 0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError('impossible archive stamp %d/%d' % (datestamp,
                                                            timestamp))
    return year, month, day, hour, minute


def encode_archive_stamp(year, month, day, hour, minute):
    """The inverse, for DMPAFT. NOT the GETTIME encoding -- that is year-1900,
    and mixing them silently asks the console for the wrong century."""
    if not 2000 <= year <= 2127:
        raise ValueError('archive stamps carry year-2000: %d out of range'
                         % year)
    datestamp = day + (month << 5) + ((year - 2000) << 9)
    return datestamp, hour * 100 + minute


def decode_gettime(buf):
    """GETTIME: 6 bytes + CRC, and the year is year-1900."""
    if len(buf) != 8 or not crc_ok(buf):
        raise ValueError('bad GETTIME response')
    sec, minute, hour, day, month, year = buf[0:6]
    return (year + 1900, month, day, hour, minute, sec)


def is_unused_archive_record(rec):
    """An unused slot, not a terminator. There is no archive terminator: the
    end of useful data is unused records plus a declining timestamp, and both
    conditions have to be named explicitly."""
    return rec[0:2] == b'\xff\xff' or rec[0:2] == b'\x00\x00'


def decode_archive_record(rec):
    if len(rec) != ARCHIVE_RECORD_SIZE:
        raise ValueError('archive record is %d bytes, expected %d'
                         % (len(rec), ARCHIVE_RECORD_SIZE))
    rev = rec[42]
    if rev == 0xFF:
        revision = 'A'
    elif rev == 0x00:
        revision = 'B'
    else:
        # Guessing here would silently misread every field after byte 42.
        raise ValueError('unknown archive record revision byte 0x%02X' % rev)

    datestamp = struct.unpack_from('<H', rec, 0)[0]
    timestamp = struct.unpack_from('<H', rec, 2)[0]
    y, mo, d, hh, mm = decode_archive_stamp(datestamp, timestamp)
    return {
        'kind': 'archive',
        'revision': revision,
        'year': y, 'month': mo, 'day': d, 'hour': hh, 'minute': mm,
        'temp_out_f': _tenths(_s16(rec, 4)),
        'temp_out_high_f': _tenths(_s16(rec, 6)),
        'temp_out_low_f': _tenths(_s16(rec, 8)),
        'rain_clicks': _u16(rec, 10),
        'rain_rate_high_clicks': _u16(rec, 12),
        'bar_inhg': _tenths_thousandths(_u16(rec, 14)),
        'solar_rad': _u16(rec, 16),
        # Reception quality for the interval. This is the ISS->console RADIO
        # link, nothing to do with our serial integrity, and the two must never
        # be conflated: a LOOP packet can pass CRC while carrying stale values.
        'wind_samples': _u16(rec, 18),
        'temp_in_f': _tenths(_s16(rec, 20)),
        'hum_in': _u8(rec, 22),
        'hum_out': _u8(rec, 23),
        # Interval average and high, with the prevailing direction. Different
        # semantics again from LOOP -- keep src='archive' so nothing downstream
        # mistakes one for the other.
        'wind_avg_mph': _u8(rec, 24),
        'wind_high_mph': _u8(rec, 25),
        'wind_high_dir_code': _u8(rec, 26),
        'wind_prevailing_dir_code': _u8(rec, 27),
        'forecast_rule': _u8(rec, 33),
    }


def decode_archive_page(page):
    """One 267-byte page: sequence byte, five records, four unused, CRC."""
    if len(page) != ARCHIVE_PAGE_SIZE:
        raise ValueError('archive page is %d bytes, expected %d'
                         % (len(page), ARCHIVE_PAGE_SIZE))
    if not crc_ok(page):
        raise ValueError('archive page CRC failed')
    seq = page[0]
    out = []
    for i in range(5):
        off = 1 + i * ARCHIVE_RECORD_SIZE
        rec = page[off:off + ARCHIVE_RECORD_SIZE]
        if is_unused_archive_record(rec):
            out.append(None)
            continue
        out.append(decode_archive_record(rec))
    return seq, out


DIR_CODES = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
             'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']


def dir_code_to_degrees(code):
    """Archive records store a 16-point code, not degrees."""
    if code is None or not 0 <= code <= 15:
        return None
    return int(round(code * 22.5)) % 360


def observation_from_loop(loop1, loop2):
    """Merge a LOOP1/LOOP2 pair into the fields the store and CSV expect.

    LOOP2 is authoritative for the wind averages because it carries them in
    0.1 mph; LOOP1 is the fallback when LOOP2 is unavailable (older firmware
    refuses LPS). Extra fidelity rides along as additional keys -- the CSV has
    no column for it, but the JSONL keeps it.
    """
    src = loop2 or loop1
    if src is None:
        raise ValueError('need at least one LOOP packet')
    l2 = loop2 or {}
    l1 = loop1 or {}

    avg_mph = l2.get('wind_avg_10min_mph')
    if avg_mph is None:
        avg_mph = l1.get('wind_avg_10min_mph')
    gust_mph = l2.get('wind_gust_10min_mph')

    temp_f = src.get('temp_out_f')
    obs = {
        'src': 'loop',
        'wind_kt': mph_to_kt(src.get('wind_mph')),
        'wind_avg_kt': mph_to_kt(avg_mph),
        'wind_dir': src.get('wind_dir'),
        'wind_gust_kt': mph_to_kt(gust_mph),
        'temp_c': f_to_c(temp_f),
        'humidity': src.get('hum_out'),
        'bar_inhg': src.get('bar_inhg'),
        'temp_in_c': f_to_c(src.get('temp_in_f')),
        'hum_in': src.get('hum_in'),
    }
    if l2:
        obs['wind_avg_2min_kt'] = mph_to_kt(l2.get('wind_avg_2min_mph'))
        obs['wind_gust_dir'] = l2.get('wind_gust_10min_dir')
        obs['dew_point_c'] = f_to_c(l2.get('dew_point_f'))
        obs['wind_chill_c'] = f_to_c(l2.get('wind_chill_f'))
        obs['heat_index_c'] = f_to_c(l2.get('heat_index_f'))
    return obs
