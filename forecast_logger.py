#!/usr/bin/env python3
"""Multi-Model Forecast Logger - fetches and logs forecasts from multiple weather models

Storage structure:
  forecasts/latest/model.json     - Current forecast for web UI (overwritten each update)
  forecasts/archive/YYYY/MM/DD_model.jsonl - Historical forecasts for analysis

This allows the web UI to load small, fixed-size files while preserving
historical data for long-term model accuracy comparison.
"""

import requests
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Configuration
FORECASTS_DIR = "forecasts"
LATEST_DIR = os.path.join(FORECASTS_DIR, "latest")
ARCHIVE_DIR = os.path.join(FORECASTS_DIR, "archive")
CANBERRA_LAT = -35.29
CANBERRA_LON = 149.10
CANBERRA_TZ = ZoneInfo('Australia/Sydney')
FORECAST_DAYS = 6  # Fetch up to 5 days ahead

# Open-Meteo API for GFS, ICON, GEM, JMA
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_MODELS = {
    'ecmwf': 'ecmwf_ifs025',
    'gfs': 'gfs_seamless',
    'icon': 'icon_seamless',
    'gem': 'gem_seamless',
    'jma': 'jma_seamless',
}

# BOM API (Australia)
BOM_HOURLY_URL = "https://api.weather.bom.gov.au/v1/locations/r3dp5e/forecasts/hourly"
BOM_DAILY_URL = "https://api.weather.bom.gov.au/v1/locations/r3dp5e0/forecasts/daily"

# Compass direction to degrees
COMPASS_TO_DEG = {
    'N': 0, 'NNE': 22.5, 'NE': 45, 'ENE': 67.5,
    'E': 90, 'ESE': 112.5, 'SE': 135, 'SSE': 157.5,
    'S': 180, 'SSW': 202.5, 'SW': 225, 'WSW': 247.5,
    'W': 270, 'WNW': 292.5, 'NW': 315, 'NNW': 337.5
}


def kmh_to_knots(kmh):
    """Convert km/h to knots"""
    if kmh is None:
        return None
    return round(kmh * 0.539957, 1)


def fetch_open_meteo_forecasts():
    """Fetch forecasts from Open-Meteo for all configured models"""
    models_param = ','.join(OPEN_METEO_MODELS.values())

    params = {
        'latitude': CANBERRA_LAT,
        'longitude': CANBERRA_LON,
        'hourly': 'wind_speed_10m,wind_gusts_10m,wind_direction_10m',
        'models': models_param,
        'timezone': 'Australia/Sydney',
        'forecast_days': FORECAST_DAYS,
    }

    resp = requests.get(OPEN_METEO_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    hourly = data.get('hourly', {})
    times = hourly.get('time', [])

    # Use current time rounded to nearest 6 hours as issue time
    # (models typically update every 6 hours: 00, 06, 12, 18 UTC)
    now = datetime.now(timezone.utc)
    issue_hour = (now.hour // 6) * 6
    issue_time = now.replace(hour=issue_hour, minute=0, second=0, microsecond=0)
    issue_time_str = issue_time.strftime('%Y-%m-%dT%H:%M:%SZ')

    results = {}

    for model_name, model_key in OPEN_METEO_MODELS.items():
        wind_key = f'wind_speed_10m_{model_key}'
        gust_key = f'wind_gusts_10m_{model_key}'
        dir_key = f'wind_direction_10m_{model_key}'

        winds = hourly.get(wind_key, [])
        gusts = hourly.get(gust_key, [])
        dirs = hourly.get(dir_key, [])

        if not winds:
            continue

        forecasts = []
        for i, t in enumerate(times):
            wind_kt = kmh_to_knots(winds[i]) if i < len(winds) and winds[i] is not None else None
            gust_kt = kmh_to_knots(gusts[i]) if i < len(gusts) and gusts[i] is not None else None
            wind_dir = dirs[i] if i < len(dirs) else None

            if wind_kt is not None:
                # Convert Canberra local time to UTC
                local_dt = datetime.strptime(t, '%Y-%m-%dT%H:%M').replace(tzinfo=CANBERRA_TZ)
                utc_dt = local_dt.astimezone(timezone.utc)
                valid_time_utc = utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')
                forecasts.append({
                    'valid_time': valid_time_utc,
                    'wind_kt': wind_kt,
                    'gust_kt': gust_kt,
                    'wind_dir': wind_dir,
                })

        if forecasts:
            results[model_name] = {
                'issue_time': issue_time_str,
                'forecasts': forecasts,
            }

    return results


def fetch_bom_forecast():
    """Fetch BOM hourly forecast"""
    resp = requests.get(BOM_HOURLY_URL, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if not data.get('data'):
        return None

    # Get issue time from metadata if available, otherwise use current time
    metadata = data.get('metadata', {})
    issue_time_str = metadata.get('issue_time')
    if not issue_time_str:
        now = datetime.now(timezone.utc)
        issue_hour = now.hour  # BOM updates hourly
        issue_time = now.replace(minute=0, second=0, microsecond=0)
        issue_time_str = issue_time.strftime('%Y-%m-%dT%H:%M:%SZ')

    forecasts = []
    for entry in data['data']:
        wind = entry.get('wind', {})
        direction = wind.get('direction')

        forecast_time = entry.get('time')
        if not forecast_time:
            continue

        forecasts.append({
            'valid_time': forecast_time,
            'wind_kt': wind.get('speed_knot'),
            'gust_kt': wind.get('gust_speed_knot'),
            'wind_dir': COMPASS_TO_DEG.get(direction) if direction else None,
            'temp': entry.get('temp'),
            'icon': entry.get('icon_descriptor'),
            'is_night': entry.get('is_night', False),
            'rain_chance': entry.get('rain', {}).get('chance'),
        })

    if forecasts:
        return {
            'issue_time': issue_time_str,
            'forecasts': forecasts,
        }
    return None


def get_latest_issue_time(model_name):
    """Get the issue_time of the current latest forecast for a model"""
    filepath = os.path.join(LATEST_DIR, f'{model_name}.json')
    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
            return data.get('issue_time')
    except (json.JSONDecodeError, IOError):
        return None


def get_archive_path(model_name, date=None):
    """Get archive file path for a date: archive/YYYY/MM/DD_model.jsonl"""
    if date is None:
        date = datetime.now(CANBERRA_TZ)
    archive_dir = os.path.join(ARCHIVE_DIR, date.strftime('%Y'), date.strftime('%m'))
    os.makedirs(archive_dir, exist_ok=True)
    return os.path.join(archive_dir, f"{date.strftime('%d')}_{model_name}.jsonl")


def is_already_archived(archive_path, issue_time):
    """Check if this issue_time is already in the archive file"""
    if not os.path.exists(archive_path):
        return False

    with open(archive_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    data = json.loads(line)
                    if data.get('issue_time') == issue_time:
                        return True
                except json.JSONDecodeError:
                    continue
    return False


def group_forecasts_by_date(forecasts):
    """Group forecast points by their valid date in Canberra timezone"""
    by_date = {}
    for f in forecasts:
        valid_time = f['valid_time']
        # Parse the valid_time and convert to Canberra date
        if valid_time.endswith('Z'):
            dt = datetime.fromisoformat(valid_time.replace('Z', '+00:00'))
        else:
            dt = datetime.fromisoformat(valid_time)
        canberra_dt = dt.astimezone(CANBERRA_TZ)
        date_key = canberra_dt.date()
        if date_key not in by_date:
            by_date[date_key] = []
        by_date[date_key].append(f)
    return by_date


def log_forecast(model_name, forecast_data):
    """Save forecast to latest/ and archive to archive/YYYY/MM/DD_model.jsonl for each day"""
    os.makedirs(LATEST_DIR, exist_ok=True)

    issue_time = forecast_data['issue_time']

    # Check if this is a new forecast run
    last_issue = get_latest_issue_time(model_name)
    if last_issue == issue_time:
        return False  # Already have this forecast

    # Save to latest/ (overwrite)
    latest_path = os.path.join(LATEST_DIR, f'{model_name}.json')
    with open(latest_path, 'w') as f:
        json.dump(forecast_data, f)

    # Group forecasts by date and archive to each day's file
    forecasts_by_date = group_forecasts_by_date(forecast_data['forecasts'])
    for date, day_forecasts in forecasts_by_date.items():
        day_data = {
            'issue_time': issue_time,
            'forecasts': day_forecasts,
        }
        archive_path = get_archive_path(model_name, datetime.combine(date, datetime.min.time()))
        if not is_already_archived(archive_path, issue_time):
            with open(archive_path, 'a') as f:
                f.write(json.dumps(day_data) + '\n')

    return True


WEEKLY_DAYS = 8
WEEKLY_VARS = {
    'wind': 'wind_speed_10m',
    'gust': 'wind_gusts_10m',
    'dir': 'wind_direction_10m',
    'temp': 'temperature_2m',
    'precip': 'precipitation',
    'code': 'weather_code',
    'day': 'is_day',
}


def fetch_weekly():
    """Hourly multi-variable forecast for the web UI's weekly table.
    Times are Canberra wall-clock strings; wind in knots."""
    params = {
        'latitude': CANBERRA_LAT,
        'longitude': CANBERRA_LON,
        'hourly': ','.join(WEEKLY_VARS.values()),
        'models': ','.join(OPEN_METEO_MODELS.values()),
        'timezone': 'Australia/Sydney',
        'forecast_days': WEEKLY_DAYS,
        'wind_speed_unit': 'kn',
    }
    resp = requests.get(OPEN_METEO_URL, params=params, timeout=30)
    resp.raise_for_status()
    hourly = resp.json().get('hourly', {})
    times = hourly.get('time', [])
    models = {}
    for model_name, model_key in OPEN_METEO_MODELS.items():
        m = {}
        for short, var in WEEKLY_VARS.items():
            vals = hourly.get(f'{var}_{model_key}') or []
            if short in ('dir', 'code', 'day'):
                vals = [None if v is None else int(v) for v in vals]
            else:
                vals = [None if v is None else round(v, 1) for v in vals]
            m[short] = vals
        if any(v is not None for v in m['wind']):
            models[model_name] = m
    if not times or not models:
        return None
    return {'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'time': times, 'models': models}


def write_json_atomic(path, obj):
    """Write via a unique temp file so concurrent writers can't corrupt it."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(obj, f, separators=(',', ':'))
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read_archive_lines(model_name, date):
    path = get_archive_path(model_name, datetime.combine(date, datetime.min.time()))
    if not os.path.exists(path):
        return []
    lines = []
    with open(path) as f:
        for line in f:
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return lines


def write_day_summary(date, fresh=None):
    """Write archive/YYYY/MM/DD.json holding one forecast per model for the day,
    so the web UI needs one request instead of one per model plus live APIs.

    Open-Meteo models use the fresh fetch if given, else the last archived line.
    BOM uses the archived line with most points: its rolling window means later
    issues lose the earlier hours of the day."""
    models = {}
    for model_name in list(OPEN_METEO_MODELS) + ['bom']:
        if model_name != 'bom' and fresh and model_name in fresh:
            points = group_forecasts_by_date(fresh[model_name]['forecasts']).get(date)
            if points:
                models[model_name] = points
                continue
        lines = read_archive_lines(model_name, date)
        if not lines:
            continue
        if model_name == 'bom':
            best = max(lines, key=lambda l: len(l.get('forecasts') or []))
        else:
            best = lines[-1]
        if best.get('forecasts'):
            models[model_name] = best['forecasts']
    if not models:
        return
    path = os.path.join(ARCHIVE_DIR, date.strftime('%Y'), date.strftime('%m'),
                        date.strftime('%d') + '.json')
    write_json_atomic(path, {'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                             'models': models})


def backfill_summaries():
    """Write day summaries for every date in the archive."""
    dates = set()
    for root, _, files in os.walk(ARCHIVE_DIR):
        parts = os.path.relpath(root, ARCHIVE_DIR).split(os.sep)
        if len(parts) != 2:
            continue
        for fn in files:
            if fn.endswith('.jsonl'):
                try:
                    dates.add(datetime.strptime(f"{parts[0]}-{parts[1]}-{fn[:2]}", '%Y-%m-%d').date())
                except ValueError:
                    pass
    for date in sorted(dates):
        write_day_summary(date)
    print(f"Wrote {len(dates)} day summaries")


def main():
    if '--backfill' in sys.argv:
        backfill_summaries()
        return

    print(f"Fetching forecasts at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    om_forecasts = None
    # Fetch Open-Meteo forecasts (GFS, ICON, GEM, JMA)
    try:
        om_forecasts = fetch_open_meteo_forecasts()
        for model_name, data in om_forecasts.items():
            if log_forecast(model_name, data):
                print(f"  {model_name.upper()}: logged {len(data['forecasts'])} forecast points "
                      f"(issue: {data['issue_time']})")
            else:
                print(f"  {model_name.upper()}: already logged (issue: {data['issue_time']})")
    except Exception as e:
        print(f"  Open-Meteo error: {e}")

    # Fetch BOM forecast
    try:
        bom_data = fetch_bom_forecast()
        if bom_data:
            if log_forecast('bom', bom_data):
                print(f"  BOM: logged {len(bom_data['forecasts'])} forecast points "
                      f"(issue: {bom_data['issue_time']})")
            else:
                print(f"  BOM: already logged (issue: {bom_data['issue_time']})")
        else:
            print("  BOM: no data available")
    except Exception as e:
        print(f"  BOM error: {e}")

    # BOM daily forecast, served as-is so the web UI needs no third-party fetch
    try:
        resp = requests.get(BOM_DAILY_URL, timeout=10)
        resp.raise_for_status()
        daily = resp.json()
        if daily.get('data'):
            os.makedirs(LATEST_DIR, exist_ok=True)
            write_json_atomic(os.path.join(LATEST_DIR, 'bom_daily.json'), daily)
            print(f"  BOM daily: {len(daily['data'])} days")
    except Exception as e:
        print(f"  BOM daily error: {e}")

    try:
        weekly = fetch_weekly()
        if weekly:
            os.makedirs(LATEST_DIR, exist_ok=True)
            write_json_atomic(os.path.join(LATEST_DIR, 'weekly.json'), weekly)
            print(f"  Weekly: {len(weekly['time'])} hours, {len(weekly['models'])} models")
    except Exception as e:
        print(f"  Weekly error: {e}")

    today = datetime.now(CANBERRA_TZ).date()
    for i in range(FORECAST_DAYS):
        try:
            write_day_summary(today + timedelta(days=i), om_forecasts)
        except Exception as e:
            print(f"  summary error for {today + timedelta(days=i)}: {e}")


if __name__ == '__main__':
    main()
