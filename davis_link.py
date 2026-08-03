#!/usr/bin/env python3
"""Serial transport to a Davis Vantage console. Read-only.

Every read has a finite timeout. A collector blocked forever in a C call is
worse than one that exits: a wedged owner keeps COM4 for ever, the watchdog's
STOP sentinel cannot be honoured because the code is not running, and the port
never comes back without a reboot.

Two failure kinds are deliberately kept apart:

  * SERIAL faults -- timeouts, CRC errors, a port that vanished. These drive
    the resync ladder and are counted separately.
  * RADIO quality -- the ISS to console link, reported by RXCHECK and by
    `wind_samples` in archive records. At CYC that has been logged at 31% good.
    It is NOT a reason to reopen the port. Never reopen COM4 because the
    weather values look wrong: a LOOP packet can pass CRC while carrying
    dashed or stale sensor readings.
"""

import time

from davis_protocol import (LOOP_SIZE, ARCHIVE_PAGE_SIZE, ACK, RESEND, CANCEL,
                            append_crc, crc_ok, decode_loop, decode_gettime,
                            decode_archive_page, encode_archive_stamp)

WAKE_ATTEMPTS = 3
WAKE_TIMEOUT_S = 1.2
CMD_TIMEOUT_S = 3.0
PACKET_TIMEOUT_S = 5.0
# The ~220 LOOP limit is undocumented -- weewx says "somewhere around 220" and
# defaults to 200. 50 is prudence, not protocol.
LOOP_BATCH = 50
MAX_PAGE_RESENDS = 3

# Prefer 19200 first: it is the Vantage default. 14400 is in the list because
# the console offers it and a previous installer may have chosen it.
BAUD_CANDIDATES = (19200, 14400, 9600, 4800, 2400, 1200)


class LinkError(Exception):
    """A serial-layer fault. Retryable via the resync ladder."""


class NotAwake(LinkError):
    pass


class BadResponse(LinkError):
    pass


class Link:
    def __init__(self, port, log=None):
        self.port = port
        self.log = log or (lambda *a: None)
        self.crc_errors = 0
        self.timeouts = 0
        self.wakeups = 0

    # ----------------------------------------------------------------- io

    def _read_exact(self, n, timeout):
        """Read n bytes or raise. Never blocks past `timeout` in total."""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                self.timeouts += 1
                raise LinkError('timed out after %d of %d bytes'
                                % (len(buf), n))
            self.port.timeout = min(left, 0.5)
            chunk = self.port.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def _drain(self):
        try:
            self.port.reset_input_buffer()
        except Exception:
            pass

    # -------------------------------------------------------------- wakeup

    def wake(self):
        """The console sleeps after ~2 minutes idle. A bare LF wakes it and it
        answers LF CR."""
        for attempt in range(WAKE_ATTEMPTS):
            self._drain()
            self.port.write(b'\n')
            try:
                resp = self._read_exact(2, WAKE_TIMEOUT_S)
            except LinkError:
                continue
            if resp == b'\n\r':
                self.wakeups += 1
                return True
            # Something else is in the stream; flush and try again.
        raise NotAwake('console did not answer %d wakeups. If it is sitting in '
                       'a setup or numeric-entry screen it ignores serial '
                       'commands -- that is not a USB fault'
                       % WAKE_ATTEMPTS)

    def command(self, text, expect_ack=True, timeout=CMD_TIMEOUT_S):
        self.port.write(text.encode('ascii') + b'\n')
        if not expect_ack:
            return
        resp = self._read_exact(1, timeout)
        if resp != ACK:
            raise BadResponse('%s: expected ACK, got %r' % (text, resp))

    # ---------------------------------------------------------------- loop

    def loop_packets(self, count=LOOP_BATCH, want_loop2=True):
        """Yield decoded LOOP packets. LPS 3 n interleaves LOOP1 and LOOP2 and
        `n` is the TOTAL packet count, not n of each."""
        self.wake()
        if want_loop2:
            try:
                self.command('LPS 3 %d' % count)
            except BadResponse:
                # Older firmware has no LOOP2. Fall back rather than fail: a
                # 10-minute average in whole mph beats no data at all.
                self.log('LPS refused; falling back to LOOP')
                self.wake()
                self.command('LOOP %d' % count)
                want_loop2 = False
        else:
            self.command('LOOP %d' % count)

        for _ in range(count):
            raw = self._read_exact(LOOP_SIZE, PACKET_TIMEOUT_S)
            if not crc_ok(raw):
                self.crc_errors += 1
                self.log('LOOP CRC error (%d so far)' % self.crc_errors)
                continue
            try:
                yield decode_loop(raw)
            except ValueError as e:
                self.crc_errors += 1
                self.log('undecodable LOOP: %s' % e)

    def stop_loop(self):
        """Cancel a LOOP stream so the console is ready for a command."""
        try:
            self.port.write(CANCEL)
            time.sleep(0.2)
            self._drain()
        except Exception:
            pass

    # ------------------------------------------------------------- console

    def get_time(self):
        self.wake()
        self.command('GETTIME')
        return decode_gettime(self._read_exact(8, CMD_TIMEOUT_S))

    def eeprom(self, addr, count=1):
        self.wake()
        self.command('EEBRD %02X %02X' % (addr, count))
        raw = self._read_exact(count + 2, CMD_TIMEOUT_S)
        if not crc_ok(raw):
            self.crc_errors += 1
            raise LinkError('EEBRD %02X CRC failed' % addr)
        return raw[:count]

    def archive_interval_minutes(self):
        """EEPROM 0x2D. Sets the DMPAFT cadence and the backfill horizon."""
        return self.eeprom(0x2D, 1)[0]

    def rain_bucket_type(self):
        """EEPROM 0x2B, low nibble: 0 = 0.01in, 1 = 0.2mm, 2 = 0.1mm."""
        return self.eeprom(0x2B, 1)[0] & 0x0F

    def rx_check(self):
        """Radio statistics. Reported alongside, never mixed with, the serial
        counters -- they measure different links."""
        self.wake()
        self.port.write(b'RXCHECK\n')
        deadline = time.monotonic() + CMD_TIMEOUT_S
        buf = bytearray()
        while time.monotonic() < deadline and b'\n\r' not in bytes(buf[4:]):
            self.port.timeout = 0.3
            chunk = self.port.read(64)
            if chunk:
                buf.extend(chunk)
            elif buf:
                break
        parts = bytes(buf).decode('ascii', 'replace').split()
        nums = [int(p) for p in parts if p.lstrip('-').isdigit()]
        if len(nums) < 5:
            raise BadResponse('RXCHECK returned %r' % bytes(buf)[:40])
        total, resyncs, misses, max_run, crc = nums[:5]
        received = total + misses
        return {
            'total_received': total, 'resyncs': resyncs, 'misses': misses,
            'max_in_a_row': max_run, 'crc_errors': crc,
            'good_pct': round(100.0 * total / received, 1) if received else None,
        }

    # ------------------------------------------------------------- archive

    def dump_after(self, year, month, day, hour, minute, max_pages=None):
        """DMPAFT from a cursor. Returns a list of decoded archive records.

        Two hazards make this dangerous to run casually:

        * While a download runs the console does NOT receive ISS packets. On a
          31%-good link, dumping on every reconnect would actively make
          reception worse.
        * The timestamp must match a REAL archived record. Feed it an arbitrary
          LOOP timestamp and the console returns the entire archive.
        """
        self.wake()
        self.command('DMPAFT')
        ds, ts = encode_archive_stamp(year, month, day, hour, minute)
        import struct as _s
        self.port.write(append_crc(_s.pack('<HH', ds, ts)))
        resp = self._read_exact(1, CMD_TIMEOUT_S)
        if resp != ACK:
            raise BadResponse('DMPAFT stamp rejected: %r' % resp)

        header = self._read_exact(6, CMD_TIMEOUT_S)
        if not crc_ok(header):
            raise LinkError('DMPAFT header CRC failed')
        pages, first_record = _s.unpack('<HH', header[:4])
        if max_pages is not None:
            pages = min(pages, max_pages)

        out = []
        last_stamp = None
        self.port.write(ACK)                 # begin the download
        for page_no in range(pages):
            page = self._read_page()
            seq, records = decode_archive_page(page)
            for i, rec in enumerate(records):
                if rec is None:
                    continue
                if page_no == 0 and i < first_record:
                    continue
                stamp = (rec['year'], rec['month'], rec['day'],
                         rec['hour'], rec['minute'])
                # There is no archive terminator. The end of useful data is
                # unused records plus a stamp that goes backwards, and both
                # conditions have to be named rather than assumed.
                if last_stamp is not None and stamp < last_stamp:
                    self.log('archive stamps declined at page %d: end of data'
                             % page_no)
                    self._cancel_dump()
                    return out
                last_stamp = stamp
                out.append(rec)
            self.port.write(ACK)             # request the next page
        return out

    def _read_page(self):
        for attempt in range(MAX_PAGE_RESENDS + 1):
            page = self._read_exact(ARCHIVE_PAGE_SIZE, PACKET_TIMEOUT_S)
            if crc_ok(page):
                return page
            self.crc_errors += 1
            if attempt == MAX_PAGE_RESENDS:
                break
            self.log('archive page CRC failed; requesting resend')
            self.port.write(RESEND)
        raise LinkError('archive page still corrupt after %d resends'
                        % MAX_PAGE_RESENDS)

    def _cancel_dump(self):
        try:
            self.port.write(CANCEL)
        except Exception:
            pass

