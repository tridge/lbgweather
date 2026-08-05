#!/usr/bin/env python3
"""A Davis Vantage console simulator, with fault injection.

Exists so the link layer can be exercised against silence, dropped bytes,
corrupt CRCs, refused commands and declining archive timestamps without going
near the real station. It presents the same read/write/timeout surface as
`serial.Serial`, so `davis_link` cannot tell the difference.

A warning that applies to everything here: this simulator was written from the
same reading of the spec as the decoder, so agreement between them proves the
code is self-consistent, NOT that it matches the console. The things that can
only be settled against real hardware are marked in the plan and in
`test_davis_protocol.py`: the resend byte, LOOP2 offsets 18 and 22, and the
baud rate.
"""

import struct
import time

from davis_protocol import (LOOP_FIELDS_SIZE, ARCHIVE_RECORD_SIZE,
                            ARCHIVE_PAGE_SIZE, ACK, RESEND, CANCEL,
                            append_crc, frame_loop, encode_archive_stamp)


class Fault:
    """What the console should do wrong. All default off."""

    def __init__(self, silent=False, drop_bytes=0, corrupt_crc_every=0,
                 refuse_lps=False, sleep_before_reply=0.0,
                 declining_archive=False, wrong_resend_byte=False,
                 never_wake=False, garbage_prefix=b'',
                 corrupt_resends=False, chatty_wakeup=False,
                 wakeup_echo_extra=0):
        self.silent = silent
        self.drop_bytes = drop_bytes
        self.corrupt_crc_every = corrupt_crc_every
        self.refuse_lps = refuse_lps
        self.sleep_before_reply = sleep_before_reply
        self.declining_archive = declining_archive
        self.wrong_resend_byte = wrong_resend_byte
        self.never_wake = never_wake
        self.garbage_prefix = garbage_prefix
        # A page that stays corrupt however often it is re-requested.
        self.corrupt_resends = corrupt_resends
        # THE REAL CONSOLE'S BEHAVIOUR on the first capture run: it answers
        # a wakeup with more than one LF CR, and the surplus shifts every
        # later read by a byte so each command sees b'\n' instead of its
        # ACK. The old simulator could not express this, which is why the
        # bug survived every test and only appeared against hardware.
        self.chatty_wakeup = chatty_wakeup
        self.wakeup_echo_extra = wakeup_echo_extra


class FakeConsole:
    """Speaks the console half of the protocol over an in-memory buffer."""

    def __init__(self, fault=None, awake=False, archive_records=None,
                 eeprom=None, loop_values=None):
        self.fault = fault or Fault()
        self.awake = awake
        self.out = bytearray()          # bytes the console has produced
        self.inbuf = bytearray()        # bytes the host has sent
        self.timeout = 2.0
        self.closed = False
        self.packets_sent = 0
        self.pages_sent = 0
        self.resend_requests = 0
        self.commands = []
        self.archive_records = archive_records or []
        self.eeprom = eeprom or {0x2B: 1, 0x2D: 30}   # bucket type, interval
        self.loop_values = loop_values or {}
        self._crc_counter = 0
        self._pending_pages = []
        self._last_page = None

    # ------------------------------------------------------- serial surface

    def read(self, n=1):
        if self.closed:
            raise OSError('port closed')
        if self.fault.silent:
            time.sleep(min(self.timeout, 0.01))
            return b''
        data = bytes(self.out[:n])
        del self.out[:n]
        return data

    def write(self, data):
        if self.closed:
            raise OSError('port closed')
        self.inbuf.extend(data)
        self._process()
        return len(data)

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        self.out.clear()

    def reset_output_buffer(self):
        self.inbuf.clear()

    # ------------------------------------------------------------- internals

    def _emit(self, data):
        if self.fault.sleep_before_reply:
            time.sleep(self.fault.sleep_before_reply)
        if self.fault.garbage_prefix:
            self.out.extend(self.fault.garbage_prefix)
            self.fault.garbage_prefix = b''
        if self.fault.drop_bytes and len(data) > self.fault.drop_bytes:
            data = data[self.fault.drop_bytes:]
        self.out.extend(data)

    def _maybe_corrupt(self, packet):
        """Flip a byte every Nth packet, leaving the CRC wrong."""
        if not self.fault.corrupt_crc_every:
            return packet
        self._crc_counter += 1
        if self._crc_counter % self.fault.corrupt_crc_every:
            return packet
        bad = bytearray(packet)
        bad[10] ^= 0xFF
        return bytes(bad)

    def _process(self):
        while True:
            if not self.inbuf:
                return
            # DMPAFT is a byte-level exchange, not a line protocol: once the
            # command is ACKed the console waits for a raw stamp, then for a
            # single ACK per page. Driving it from the same byte stream as
            # everything else is what the real console does.
            if self._dmpaft_stage == 'want_stamp':
                if len(self.inbuf) < 6:
                    return
                blob = bytes(self.inbuf[:6])
                del self.inbuf[:6]
                self._recv_stamp(blob)
                continue
            if self._dmpaft_stage == 'sending':
                b = self.inbuf[0:1]
                del self.inbuf[0:1]
                if b == ACK:
                    if not self._send_next_page():
                        self._dmpaft_stage = None
                elif b == RESEND:
                    self._resend_last_page()
                elif b == CANCEL:
                    self._dmpaft_stage = None
                continue
            # A bare LF is the wakeup.
            if self.inbuf[0:1] == b'\n':
                del self.inbuf[0:1]
                if self.fault.never_wake:
                    continue
                self.awake = True
                self._emit(b'\n\r')
                if self.fault.chatty_wakeup:
                    # Surplus the host must discard before commanding.
                    self._emit(b'\n\r' * max(1, self.fault.wakeup_echo_extra))
                continue
            if b'\n' not in self.inbuf:
                return
            idx = self.inbuf.index(b'\n')
            line = bytes(self.inbuf[:idx])
            del self.inbuf[:idx + 1]
            if line:
                self._command(line.decode('ascii', 'replace').strip())

    def _command(self, cmd):
        self.commands.append(cmd)
        parts = cmd.split()
        if not parts:
            return
        name = parts[0].upper()

        if name == 'TEST':
            self._emit(b'\n\rTEST\n\r')
        elif name == 'VER':
            self._emit(b'\n\rOK\n\rApr 24 2010\n\r')
        elif name == 'NVER':
            self._emit(b'\n\rOK\n\r3.83\n\r')
        elif name == 'RXCHECK':
            self._emit(b'\n\rOK\n\r7311 13 16612 33 2715\n\r')
        elif name == 'LPS':
            if self.fault.refuse_lps:
                self._emit(b'\x21')          # NAK: no LOOP2 on this firmware
                return
            self._emit(ACK)
            count = int(parts[2]) if len(parts) > 2 else 1
            for i in range(count):
                self._emit(self._maybe_corrupt(
                    self._loop_packet(1 if i % 2 else 0)))
                self.packets_sent += 1
        elif name == 'LOOP':
            self._emit(ACK)
            for _ in range(int(parts[1]) if len(parts) > 1 else 1):
                self._emit(self._maybe_corrupt(self._loop_packet(0)))
                self.packets_sent += 1
        elif name == 'GETTIME':
            self._emit(ACK)
            t = time.localtime()
            self._emit(append_crc(bytes([t.tm_sec, t.tm_min, t.tm_hour,
                                         t.tm_mday, t.tm_mon,
                                         t.tm_year - 1900])))
        elif name == 'EEBRD':
            addr = int(parts[1], 16)
            n = int(parts[2], 16)
            self._emit(ACK)
            vals = bytes((self.eeprom.get(addr + i, 0) & 0xFF)
                         for i in range(n))
            self._emit(append_crc(vals))
        elif name == 'DMPAFT':
            self._emit(ACK)
            self._dmpaft_stage = 'want_stamp'
        else:
            self._emit(b'\n\rOK\n\r')

    def _loop_packet(self, ptype):
        b = bytearray(b'\x00' * LOOP_FIELDS_SIZE)
        b[0:3] = b'LOO'
        b[3] = 0
        b[4] = ptype
        v = self.loop_values
        struct.pack_into('<H', b, 5, v.get('next_rec', 100))
        struct.pack_into('<H', b, 7, v.get('bar', 30283))
        struct.pack_into('<h', b, 9, v.get('temp_in', 791))
        b[11] = v.get('hum_in', 28)
        struct.pack_into('<h', b, 12, v.get('temp_out', 578))
        b[14] = v.get('wind', 5)
        struct.pack_into('<H', b, 16, v.get('wind_dir', 245))
        b[33] = v.get('hum_out', 49)
        b[43] = 0xFF                                   # no UV sensor
        struct.pack_into('<H', b, 44, 0xFFFF)          # no solar sensor
        if ptype == 0:
            b[15] = v.get('avg10_whole', 13)
        else:
            struct.pack_into('<H', b, 18, v.get('avg10_tenths', 130))
            struct.pack_into('<H', b, 20, v.get('avg2_tenths', 120))
            struct.pack_into('<H', b, 22, v.get('gust10_tenths', 190))
            struct.pack_into('<H', b, 24, v.get('gust_dir', 250))
            struct.pack_into('<h', b, 30, v.get('dew', 39))
            struct.pack_into('<h', b, 35, v.get('heat', 56))
            struct.pack_into('<h', b, 37, v.get('chill', 53))
        return frame_loop(bytes(b))

    # DMPAFT is a multi-step exchange, so it is driven by raw byte writes
    # rather than by lines. The host sends the stamp, then ACKs each page.
    _dmpaft_stage = None

    def _recv_stamp(self, blob):
        self._pending_pages = self._build_pages()
        self._emit(ACK)
        self._emit(append_crc(struct.pack('<HH', len(self._pending_pages), 0)))
        self._dmpaft_stage = 'sending'

    def _build_pages(self):
        pages = []
        recs = list(self.archive_records)
        for pnum in range(0, max(1, (len(recs) + 4) // 5)):
            chunk = recs[pnum * 5:pnum * 5 + 5]
            body = bytearray()
            for i in range(5):
                if i < len(chunk):
                    body.extend(chunk[i])
                else:
                    body.extend(b'\xff' * ARCHIVE_RECORD_SIZE)
            pages.append(append_crc(bytes([pnum & 0xFF]) + bytes(body)
                                    + b'\x00' * 4))
        return pages

    def _send_next_page(self):
        if not self._pending_pages:
            return False
        page = self._pending_pages.pop(0)
        self._last_page = page
        self._emit(self._maybe_corrupt(page))
        self.pages_sent += 1
        return True

    def _resend_last_page(self):
        self.resend_requests += 1
        if self._last_page is None:
            return
        # By default a resend is honest -- the usual case is a one-off line
        # error and the retry succeeds. corrupt_resends models the other case:
        # a link bad enough that re-requesting never helps.
        if self.fault.corrupt_resends:
            bad = bytearray(self._last_page)
            bad[10] ^= 0xFF
            self._emit(bytes(bad))
        else:
            self._emit(self._last_page)


def make_archive_record(year, month, day, hour, minute, rev='B', **kw):
    r = bytearray(b'\x00' * ARCHIVE_RECORD_SIZE)
    ds, ts = encode_archive_stamp(year, month, day, hour, minute)
    struct.pack_into('<H', r, 0, ds)
    struct.pack_into('<H', r, 2, ts)
    struct.pack_into('<h', r, 4, kw.get('temp_out', 578))
    struct.pack_into('<h', r, 6, kw.get('temp_high', 600))
    struct.pack_into('<h', r, 8, kw.get('temp_low', 550))
    struct.pack_into('<H', r, 14, kw.get('bar', 30283))
    struct.pack_into('<H', r, 16, 0xFFFF)
    struct.pack_into('<H', r, 18, kw.get('samples', 200))
    struct.pack_into('<h', r, 20, kw.get('temp_in', 791))
    r[22] = kw.get('hum_in', 28)
    r[23] = kw.get('hum_out', 49)
    r[24] = kw.get('wind_avg', 12)
    r[25] = kw.get('wind_high', 19)
    r[26] = kw.get('high_dir', 11)
    r[27] = kw.get('prev_dir', 12)
    r[33] = 12
    r[42] = 0x00 if rev == 'B' else 0xFF
    return bytes(r)
