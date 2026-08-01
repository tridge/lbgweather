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

@contextmanager
def day_lock(log_file):
    """Hold the same day lock cyc_store.py uses.

    Without this there are two unsynchronised writers on one CSV: this one
    appends, and the store rewrites the whole file from its JSONL. An append
    landing between the store reading the file and replacing it goes into the
    old inode and is lost. The store refuses to project over rows it cannot
    account for, but that check cannot close the window on its own -- only a
    shared lock can.

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
        # Never let a lock problem stop the logging this file exists to do.
        print('wind_logger: no day lock (%s); appending unlocked' % e,
              file=sys.stderr)
        yield
        return
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print('wind_logger: day lock busy for %ds; appending '
                          'unlocked' % LOCK_TIMEOUT_S, file=sys.stderr)
                    break
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def log_data(data):
    """Append data to today's CSV file, under the shared day lock."""
    log_file = get_log_file()

    with day_lock(log_file):
        file_exists = os.path.exists(log_file)
        with open(log_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)
            f.flush()
            os.fsync(f.fileno())

def write_status(weather_json):
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
    log_data(data)
    write_status(weather)
    print(f"{data['timestamp']}: {data['wind_kt']} kt, "
          f"avg {data['wind_avg_kt']} kt, "
          f"gust {data['wind_gust_kt']} kt, "
          f"dir {data['wind_dir']}°")

if __name__ == '__main__':
    main()
