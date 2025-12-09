#!/usr/bin/env python3
"""CYC Wind Speed Logger - fetches and logs wind data to CSV"""

import requests
import csv
import os
from datetime import datetime

WEATHER_URL = "https://canberrayc.com/admin/php/cyc/getWeather.php"
LOG_BASE_DIR = "wind_logs"

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

def log_data(data):
    """Append data to today's CSV file"""
    log_file = get_log_file()
    file_exists = os.path.exists(log_file)

    with open(log_file, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(data)

def main():
    weather = fetch_weather()
    data = extract_wind_data(weather)
    log_data(data)
    print(f"{data['timestamp']}: {data['wind_kt']} kt, "
          f"avg {data['wind_avg_kt']} kt, "
          f"gust {data['wind_gust_kt']} kt, "
          f"dir {data['wind_dir']}°")

if __name__ == '__main__':
    main()
