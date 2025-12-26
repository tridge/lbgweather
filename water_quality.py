#!/usr/bin/env python3
"""Fetch Lake Burley Griffin water quality status from NCA website.

Extracts Yarralumla Bay status and writes to yarralumla_bay.html.
If the bay is CLOSED, writes a warning div. If OPEN, writes empty file.

Run via cron hourly.
"""

import requests
import re
import sys

NCA_URL = "https://www.nca.gov.au/environment/lake-burley-griffin/water-quality"
OUTPUT_FILE = "yarralumla_bay.html"


def fetch_water_quality():
    """Fetch the NCA water quality page."""
    try:
        resp = requests.get(NCA_URL, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"Error fetching NCA page: {e}", file=sys.stderr)
        return None


def extract_yarralumla_bay_status(html):
    """Extract Yarralumla Bay row from the water quality table.

    Returns tuple of (status, description) or (None, None) if not found.
    """
    # Look for the Yarralumla Bay row in the table
    # Pattern: <td>Yarralumla Bay</td>\n<td>STATUS</td>\n<td>...description...</td>
    pattern = r'<td>\s*Yarralumla Bay\s*</td>\s*<td>\s*(OPEN|CLOSED)\s*</td>\s*<td>(.*?)</td>'
    match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)

    if match:
        status = match.group(1).upper()
        description = match.group(2).strip()
        # Clean up HTML tags and entities in description
        description = re.sub(r'<[^>]+>', '', description)
        description = description.replace('&nbsp;', ' ').strip()
        return status, description

    return None, None


def write_output(status, description):
    """Write the output HTML file.

    If CLOSED, write a warning div. If OPEN or unknown, write empty file.
    """
    if status == "CLOSED":
        html = f'''<div class="lake-closure-warning">
    <strong>Yarralumla Bay CLOSED</strong><br>
    {description}
    <br><small><a href="{NCA_URL}" target="_blank">NCA Water Quality</a></small>
</div>
'''
    else:
        html = ""

    with open(OUTPUT_FILE, 'w') as f:
        f.write(html)

    return status == "CLOSED"


def main():
    html = fetch_water_quality()
    if html is None:
        # On error, don't change existing file
        print("Failed to fetch water quality data")
        return 1

    status, description = extract_yarralumla_bay_status(html)

    if status is None:
        print("Could not find Yarralumla Bay status in page")
        # Write empty file on parse error
        with open(OUTPUT_FILE, 'w') as f:
            f.write("")
        return 1

    is_closed = write_output(status, description)
    print(f"Yarralumla Bay: {status}")
    if is_closed:
        print(f"  {description}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
