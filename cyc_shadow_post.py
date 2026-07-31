#!/usr/bin/env python3
"""Shadow-feed the ingest receiver from the existing cloud path.

This exists to exercise the entire server half -- HMAC, replay defence, dedup,
the JSONL->CSV projection, the atomic rewrite, status.json, the Apache proxy and
the systemd unit -- against real data, while production stays untouched. It is
scaffolding for the cutover, not a permanent component: once the on-site
collector is posting directly this script is deleted.

Two differences from wind_logger.py are the entire point:

  * It sends the observation's OWN timestamp (the feed's `ts`), not
    datetime.now(). So when the station freezes, this stops producing new
    observations instead of manufacturing plausible-looking ones.

  * Because of that, the shadow tree gets roughly one row per real observation
    (~96/day at the cloud path's 15-minute cadence) where production gets 1440.
    That gap IS the bug: 1344 of production's daily rows are re-recordings of
    data that never changed.

Run from wind.cron alongside wind_logger.py.
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
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Australia/Sydney')
WEATHER_URL = 'https://canberrayc.com/admin/php/cyc/getWeather.php'
DEFAULT_URL = 'https://lbgweather.au/ingest/obs'
MPH_TO_KNOTS = 0.868976
VERSION = 'shadow-0.1'


def mph_to_kt(mph):
    return None if mph is None else round(mph * MPH_TO_KNOTS, 1)


def f_to_c(f):
    return None if f is None else round((f - 32) * 5 / 9, 1)


def load_key(path, station):
    """Return the RAW key bytes.

    The file stores hex; the HMAC is over the decoded bytes. Using the ASCII
    hex here instead silently produces a valid-looking signature that the
    server rejects -- which is exactly what happened when the server switched
    to bytes.fromhex() and this was left behind.
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


def fetch_weather(timeout=15):
    req = urllib.request.Request(WEATHER_URL,
                                 headers={'User-Agent': 'cyc-shadow/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def to_observation(weather, seq):
    """Build one observation from the feed, keyed on ITS timestamp."""
    data = weather['sensors'][0]['data'][0]
    obs_epoch = int(data['ts'])
    obs_dt = datetime.fromtimestamp(obs_epoch, TZ)
    return {
        'obs_ts': obs_dt.isoformat(timespec='seconds'),
        'seq': seq,
        'src': 'loop',
        # Same conversions and the same four source fields wind_logger.py uses,
        # so the shadow CSV is directly comparable with production.
        'wind_kt': mph_to_kt(data.get('wind_speed')),
        'wind_avg_kt': mph_to_kt(data.get('wind_speed_10_min_avg')),
        'wind_dir': data.get('wind_dir'),
        'wind_gust_kt': mph_to_kt(data.get('wind_gust_10_min')),
        'temp_c': f_to_c(data.get('temp_out')),
        'humidity': data.get('hum_out'),
        # Extra fidelity the CSV has no column for; costs nothing to carry.
        'bar_inhg': data.get('bar'),
        'dew_point_c': f_to_c(data.get('dew_point')),
        'temp_in_c': f_to_c(data.get('temp_in')),
        'hum_in': data.get('hum_in'),
        'rain_day_mm': data.get('rain_day_mm'),
        'rain_rate_mmh': data.get('rain_rate_mm'),
    }


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=1, sort_keys=True)
            f.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        print('state write failed: %s' % e, file=sys.stderr)


def post(url, key, station, doc, timeout=20):
    body = json.dumps(doc).encode('utf-8')
    # The signature covers the request PATH, not the full URL, so the same
    # signing code works whether we post via loopback or through Apache.
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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default=DEFAULT_URL)
    p.add_argument('--station', default='cyc1')
    p.add_argument('--keyfile',
                   default=os.path.expanduser('~/etc/cyc_ingest.key'))
    p.add_argument('--state',
                   default=os.path.expanduser('~/var/cyc_ingest/shadow_state.json'))
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args(argv)

    key = load_key(args.keyfile, args.station)
    state = load_state(args.state)

    try:
        weather = fetch_weather()
    except Exception as e:
        # The upstream feed being unreachable is not our failure to report
        # loudly every minute; feed_watch alarms on staleness instead.
        print('shadow: fetch failed: %s' % e, file=sys.stderr)
        return 0

    try:
        obs_epoch = int(weather['sensors'][0]['data'][0]['ts'])
    except (KeyError, IndexError, TypeError, ValueError) as e:
        print('shadow: unexpected feed shape: %s' % e, file=sys.stderr)
        return 0

    # Skip if the feed has not advanced. Posting anyway would be harmless (the
    # store dedups) but it would burn a request a minute and bury the real
    # signal in duplicates.
    if state.get('last_obs_epoch') == obs_epoch:
        if not args.quiet:
            age = int(time.time() - obs_epoch)
            print('shadow: no new observation (feed %ds old)' % age)
        return 0

    seq = int(state.get('seq', 0)) + 1
    obs = to_observation(weather, seq)
    doc = {
        'schema': 1,
        'station': args.station,
        'sent_at': datetime.now(TZ).isoformat(timespec='seconds'),
        'collector': {'version': VERSION, 'spool_backlog': 0,
                      'source': 'cloud-shadow'},
        'obs': [obs],
    }

    code, resp = post(args.url, key, args.station, doc)
    if code == 200:
        state['seq'] = seq
        state['last_obs_epoch'] = obs_epoch
        state['last_ok_epoch'] = int(time.time())
        save_state(args.state, state)
        if not args.quiet:
            print('shadow: %s accepted=%s duplicates=%s'
                  % (obs['obs_ts'], resp.get('accepted'), resp.get('duplicates')))
    else:
        # Do NOT advance the cursor: the observation must be retried.
        print('shadow: POST failed %s %s' % (code, resp), file=sys.stderr)
        state['last_error_epoch'] = int(time.time())
        state['last_error'] = '%s %s' % (code, resp.get('error'))
        save_state(args.state, state)

    return 0


if __name__ == '__main__':
    sys.exit(main())
