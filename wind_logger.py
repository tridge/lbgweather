#!/usr/bin/env python3
"""CYC Wind Speed Logger - fetches and logs wind data to CSV"""

import requests
import csv
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime

WEATHER_URL = "https://canberrayc.com/admin/php/cyc/getWeather.php"
LOG_BASE_DIR = "wind_logs"
STATUS_FILE = "status.json"
# Shared with cyc_store.py. Both writers must name the SAME directory and the
# same lock file, or the lock protects nothing at all. The store puts its day
# locks in <statedir>/<station>, so this must be the statedir of whichever
# station owns LOG_BASE_DIR -- cyc1 once the collector writes production.
# Override with CYC_LOCK_DIR if that ever changes.
LOCK_DIR = os.environ.get(
    'CYC_LOCK_DIR', os.path.expanduser("~/var/cyc_ingest/cyc1"))
LOCK_TIMEOUT_S = 10
FAIL_WINDOW_S = 900
# Latched failure record. A transient boolean cleared by the next good minute
# can fall entirely between two watchdog runs.
FAIL_FILE = os.path.join(LOCK_DIR, "logger_failures.json")

# Conversion factors
MPH_TO_KNOTS = 0.868976

def mph_to_kt(mph):
    """Convert mph to knots"""
    if mph is None:
        return None
    return round(mph * MPH_TO_KNOTS, 1)

def f_to_c(f):
    """Convert Fahrenheit to Celsius"""
    if f is None:
        return None
    return round((f - 32) * 5/9, 1)

CSV_FIELDS = [
    'timestamp', 'wind_kt', 'wind_avg_kt',
    'wind_dir', 'wind_gust_kt', 'temp_c', 'humidity'
]

def fetch_weather():
    """Fetch current weather from CYC API"""
    resp = requests.get(WEATHER_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()

def extract_wind_data(weather_json):
    """Extract wind data from API response, convert to metric/knots"""
    data = weather_json['sensors'][0]['data'][0]
    return {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'wind_kt': mph_to_kt(data.get('wind_speed')),
        'wind_avg_kt': mph_to_kt(data.get('wind_speed_10_min_avg')),
        'wind_dir': data.get('wind_dir'),
        'wind_gust_kt': mph_to_kt(data.get('wind_gust_10_min')),
        'temp_c': f_to_c(data.get('temp_out')),
        'humidity': data.get('hum_out'),
    }

def get_log_file():
    """Get today's CSV file path with YYYY/MM directory structure"""
    now = datetime.now()
    log_dir = os.path.join(LOG_BASE_DIR, now.strftime('%Y'), now.strftime('%m'))
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, now.strftime('%d') + '.csv')

class LockUnavailable(Exception):
    """The shared day lock could not be taken. Do not write the CSV."""


@contextmanager
def day_lock(log_file):
    """Hold the same day lock cyc_store.py uses.

    Without this there are two unsynchronised writers on one CSV: this one
    appends, and the store rewrites the whole file from its JSONL. An append
    landing between the store reading the file and replacing it goes into the
    old inode and is lost.

    There is deliberately NO unlocked fallback. Contention here is most likely
    while the store is doing the adoption or projection that must not be raced,
    so an unlocked append would fire exactly when it is most dangerous. Missing
    one sample is recoverable; silently erasing one, or corrupting the cutover,
    is not.

    The key must match cyc_store.DayFiles.key: "YYYY-MM-DD".
    """
    stem = os.path.basename(log_file)[:-4]
    parts = log_file.split(os.sep)
    key = '%s-%s-%s' % (parts[-3], parts[-2], stem)
    try:
        os.makedirs(LOCK_DIR, exist_ok=True)
        fd = os.open(os.path.join(LOCK_DIR, 'day-%s.lock' % key),
                     os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise LockUnavailable('cannot open the day lock in %s: %s'
                              % (LOCK_DIR, e))
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockUnavailable('day lock %s busy for %ds'
                                          % (key, LOCK_TIMEOUT_S))
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def log_data(data):
    """Append data to today's CSV file, under the shared day lock.

    Returns False and logs loudly if the lock was unavailable. The caller keeps
    going -- status.json still gets written, so the watchdog sees the feed's
    real age rather than a silence it cannot explain.
    """
    log_file = get_log_file()

    # The contextmanager raises on __enter__, not on construction, so the
    # `with` has to be inside the try.
    try:
        with day_lock(log_file):
            file_exists = os.path.exists(log_file)
            with open(log_file, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(data)
                f.flush()
                os.fsync(f.fileno())
    except LockUnavailable as e:
        print('wind_logger: NOT logging this sample: %s' % e, file=sys.stderr)
        return False
    return True

def note_failure(logged):
    """Latch a dropped sample so a 2-minute watchdog cannot miss it.

    Returns (count, last_epoch) for the recent failure window. The
    counter is cleared only after FAIL_WINDOW_S of clean running, not
    by the very next successful minute.
    """
    now = time.time()
    try:
        with open(FAIL_FILE) as f:
            doc = json.load(f)
        count = int(doc.get('count', 0))
        last = float(doc.get('last_epoch', 0))
    except (OSError, ValueError, TypeError):
        count, last = 0, 0
    if logged:
        if not count or now - last > FAIL_WINDOW_S:
            count, last = 0, 0
    else:
        count += 1
        last = now
    try:
        os.makedirs(os.path.dirname(FAIL_FILE), exist_ok=True)
        tmp = FAIL_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'count': count, 'last_epoch': last}, f)
        os.replace(tmp, FAIL_FILE)
    except OSError:
        pass                # best effort; status still carries it
    return count, last


def write_status(weather_json, logged=True):
    """Record the feed's OWN timestamp so staleness is detectable.

    The CSV deliberately keeps its datetime.now() timestamp - changing it would
    put a discontinuity in years of history. But that is also why a dead station
    is invisible here: this logger appends a plausible-looking row every minute
    regardless, so frozen data and live data produce identical files. The July
    2026 outage went unnoticed for three weeks for exactly that reason.

    The feed carries its own 'ts' (when the station actually reported) alongside
    'generated_at' (when the PHP proxy built the response). The gap between them
    is the real staleness. Written here for feed_watch.py to alarm on.
    """
    try:
        data = weather_json['sensors'][0]['data'][0]
        obs_ts = int(data['ts'])
    except (KeyError, IndexError, TypeError, ValueError):
        return
    generated = weather_json.get('generated_at')
    now = time.time()
    fail_count, fail_last = note_failure(logged)

    doc = {
        'station': 'cyc1',
        'source': 'cloud',
        'last_obs_epoch': obs_ts,
        'last_obs_ts': datetime.fromtimestamp(obs_ts).isoformat(timespec='seconds'),
        'last_fetch_epoch': int(now),
        'written_at_epoch': int(now),
        'written_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
        'obs_age_s': int(now - obs_ts),
        # How stale the feed was when the proxy generated it - non-zero here
        # means the station stopped reporting, independent of our own clock.
        'feed_lag_s': (int(generated - obs_ts)
                       if isinstance(generated, (int, float)) else None),
        # The feed can be perfectly healthy while we are failing to record it.
        # Without this the watchdog sees a fresh observation age and stays
        # green while every sample is dropped -- the same silent failure, one
        # step further down the pipe.
        'csv_write_failed': not logged,
        # Latched, so a failure lasting one minute is still visible to a
        # watchdog that only runs every two.
        'csv_failures_recent': fail_count,
        'csv_last_failure_epoch': int(fail_last) or None,
    }
    tmp = STATUS_FILE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(doc, f, indent=1, sort_keys=True)
            f.write('\n')
        os.chmod(tmp, 0o644)
        os.replace(tmp, STATUS_FILE)
    except OSError as e:
        print(f"status write failed: {e}", file=sys.stderr)


def main():
    weather = fetch_weather()
    data = extract_wind_data(weather)
    logged = log_data(data)
    # Status is written either way -- the feed's age is still true and useful.
    # But it carries the failure, and we exit non-zero, so a dropped sample is
    # visible to the watchdog and to cron rather than only in wind.log.
    write_status(weather, logged=logged)
    print(f"{data['timestamp']}: {data['wind_kt']} kt, "
          f"avg {data['wind_avg_kt']} kt, "
          f"gust {data['wind_gust_kt']} kt, "
          f"dir {data['wind_dir']}°"
          + ("" if logged else "  [NOT LOGGED - lock unavailable]"))
    return 0 if logged else 1

if __name__ == '__main__':
    sys.exit(main())
