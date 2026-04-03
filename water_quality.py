#!/usr/bin/env python3
"""Fetch Lake Burley Griffin water quality status from NCA website.

Extracts all location statuses and writes:
  - water_quality.json (all locations with status and comments)
  - yarralumla_bay.html (warning div if Yarralumla Bay is closed)

Run via cron hourly.
"""

import requests
import re
import sys
import json
from datetime import datetime, timezone


NCA_URL = "https://www.nca.gov.au/environment/lake-burley-griffin/water-quality"
JSON_OUTPUT = "water_quality.json"
YARRALUMLA_OUTPUT = "yarralumla_bay.html"


def fetch_water_quality():
    """Fetch the NCA water quality page."""
    try:
        resp = requests.get(NCA_URL, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"Error fetching NCA page: {e}", file=sys.stderr)
        return None


def clean_html(text):
    """Strip HTML tags and clean up entities."""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_all_locations(html):
    """Extract all rows from the water quality table.

    Returns list of dicts with name, status, comments.
    """
    pattern = r'<td>\s*(.*?)\s*</td>\s*<td>\s*(.*?)\s*</td>\s*<td>(.*?)</td>'
    matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)

    locations = []
    for name, status, comments in matches:
        name = clean_html(name)
        status = clean_html(status).upper()
        comments = clean_html(comments)
        # Filter to only water quality rows (skip header-like or unrelated rows)
        if status in ('OPEN', 'CLOSED') or 'OPEN' in status or 'CLOSED' in status:
            locations.append({
                'name': name,
                'status': status,
                'comments': comments,
            })

    return locations


def write_json_output(locations):
    """Write water_quality.json with all location data."""
    data = {
        'updated': datetime.now(timezone.utc).isoformat(),
        'locations': locations,
    }
    with open(JSON_OUTPUT, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {len(locations)} locations to {JSON_OUTPUT}")


def write_yarralumla_output(locations):
    """Write yarralumla_bay.html warning if Yarralumla Bay is closed."""
    for loc in locations:
        if loc['name'] == 'Yarralumla Bay':
            if loc['status'] == 'CLOSED':
                html = f'''<div class="lake-closure-warning">
    <strong>Yarralumla Bay CLOSED</strong><br>
    {loc['comments']}
    <br><small><a href="{NCA_URL}" target="_blank">NCA Water Quality</a></small>
</div>
'''
            else:
                html = ""
            with open(YARRALUMLA_OUTPUT, 'w') as f:
                f.write(html)
            return loc['status'] == 'CLOSED'

    # Yarralumla Bay not found - write empty file
    with open(YARRALUMLA_OUTPUT, 'w') as f:
        f.write("")
    return False


def main():
    html = fetch_water_quality()
    if html is None:
        print("Failed to fetch water quality data")
        return 1

    locations = extract_all_locations(html)

    if not locations:
        print("Could not find any water quality locations in page")
        with open(YARRALUMLA_OUTPUT, 'w') as f:
            f.write("")
        return 1

    write_json_output(locations)
    is_closed = write_yarralumla_output(locations)

    for loc in locations:
        marker = " **" if loc['status'] == 'CLOSED' else ""
        print(f"  {loc['name']}: {loc['status']}{marker}")

    if is_closed:
        print("Yarralumla Bay is CLOSED")

    return 0


if __name__ == '__main__':
    sys.exit(main())
