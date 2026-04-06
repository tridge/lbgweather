#!/usr/bin/env python3
"""Fetch Lake Burley Griffin water quality status from NCA website.

Extracts all location statuses and writes:
  - water_quality.json (all locations with status and comments)
  - yarralumla_bay.html (warning div if Yarralumla Bay is closed)

When data changes, sends an email notification with the differences.

Run via cron hourly.
"""

import requests
import re
import sys
import json
import os
import subprocess
from email.mime.text import MIMEText
from datetime import datetime, timezone


NCA_URL = "https://www.nca.gov.au/environment/lake-burley-griffin/water-quality"
JSON_OUTPUT = "water_quality.json"
LAST_OUTPUT = "last_water_quality.json"
YARRALUMLA_OUTPUT = "yarralumla_bay.html"
NOTIFY_EMAIL = "tridge60@gmail.com"


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


def load_previous():
    """Load previous water quality data for comparison."""
    if not os.path.exists(LAST_OUTPUT):
        return {}
    try:
        with open(LAST_OUTPUT, 'r') as f:
            data = json.load(f)
        return {loc['name']: loc for loc in data.get('locations', [])}
    except (json.JSONDecodeError, KeyError):
        return {}


def save_as_last(locations):
    """Save current data as last_water_quality.json for next comparison."""
    data = {
        'updated': datetime.now(timezone.utc).isoformat(),
        'locations': locations,
    }
    with open(LAST_OUTPUT, 'w') as f:
        json.dump(data, f, indent=2)


def find_changes(old_by_name, new_locations):
    """Compare old and new data, return list of change descriptions."""
    changes = []
    new_by_name = {loc['name']: loc for loc in new_locations}

    for loc in new_locations:
        name = loc['name']
        old = old_by_name.get(name)
        if old is None:
            changes.append(('new', name, None, loc))
        elif old['status'] != loc['status']:
            changes.append(('status', name, old, loc))
        elif old['comments'] != loc['comments']:
            changes.append(('comments', name, old, loc))

    for name in old_by_name:
        if name not in new_by_name:
            changes.append(('removed', name, old_by_name[name], None))

    return changes


def status_color(status):
    """Return HTML color for a status."""
    s = status.upper()
    if s == 'OPEN':
        return '#27ae60'
    if s == 'CLOSED':
        return '#e74c3c'
    return '#f39c12'


def format_email_html(changes, all_locations):
    """Format changes as an HTML email."""
    lines = []
    lines.append('<div style="font-family: -apple-system, Arial, sans-serif; max-width: 600px;">')
    lines.append('<h2 style="color: #2c3e50; margin-bottom: 16px;">LBG Water Quality Update</h2>')

    # Changes section
    lines.append('<div style="margin-bottom: 20px;">')
    for change_type, name, old, new in changes:
        if change_type == 'status':
            old_color = status_color(old['status'])
            new_color = status_color(new['status'])
            lines.append(
                f'<p style="margin: 8px 0;"><strong>{name}</strong>: '
                f'<span style="color:{old_color}; text-decoration: line-through;">{old["status"]}</span>'
                f' &rarr; '
                f'<span style="color:{new_color}; font-weight: bold;">{new["status"]}</span></p>')
            if new['comments'] != 'No restriction':
                lines.append(f'<p style="margin: 2px 0 8px 16px; color: #555; font-size: 14px;">{new["comments"]}</p>')
        elif change_type == 'comments':
            lines.append(
                f'<p style="margin: 8px 0;"><strong>{name}</strong> '
                f'(<span style="color:{status_color(new["status"])};">{new["status"]}</span>): '
                f'comments updated</p>')
            lines.append(f'<p style="margin: 2px 0 8px 16px; color: #555; font-size: 14px;">{new["comments"]}</p>')
        elif change_type == 'new':
            lines.append(
                f'<p style="margin: 8px 0;"><strong>{name}</strong>: '
                f'<span style="color:{status_color(new["status"])};">new area - {new["status"]}</span></p>')
        elif change_type == 'removed':
            lines.append(
                f'<p style="margin: 8px 0;"><strong>{name}</strong>: '
                f'<span style="color: #999;">removed from report</span></p>')
    lines.append('</div>')

    # Full status table
    lines.append('<table style="border-collapse: collapse; width: 100%; font-size: 14px;">')
    lines.append('<tr style="background: #f5f5f5;"><th style="padding: 8px; text-align: left; border-bottom: 2px solid #ddd;">Location</th>'
                 '<th style="padding: 8px; text-align: left; border-bottom: 2px solid #ddd;">Status</th></tr>')
    for loc in all_locations:
        color = status_color(loc['status'])
        lines.append(f'<tr><td style="padding: 6px 8px; border-bottom: 1px solid #eee;">{loc["name"]}</td>'
                     f'<td style="padding: 6px 8px; border-bottom: 1px solid #eee; color: {color}; font-weight: 600;">{loc["status"]}</td></tr>')
    lines.append('</table>')

    lines.append(f'<p style="margin-top: 16px; font-size: 12px; color: #999;">'
                 f'<a href="{NCA_URL}" style="color: #3498db;">NCA Water Quality</a> | '
                 f'<a href="https://lbgweather.au" style="color: #3498db;">lbgweather.au</a></p>')
    lines.append('</div>')
    return '\n'.join(lines)


def send_email(subject, html_body):
    """Send an HTML email via sendmail."""
    msg = MIMEText(html_body, 'html')
    msg['Subject'] = subject
    msg['From'] = 'water-quality@wstracker.org'
    msg['To'] = NOTIFY_EMAIL

    try:
        proc = subprocess.run(
            ['/usr/sbin/sendmail', '-t'],
            input=msg.as_string(),
            capture_output=True, text=True, timeout=30
        )
        if proc.returncode == 0:
            print(f"Email sent to {NOTIFY_EMAIL}")
        else:
            print(f"sendmail failed: {proc.stderr}", file=sys.stderr)
    except Exception as e:
        print(f"Failed to send email: {e}", file=sys.stderr)


def check_and_notify(locations):
    """Compare with previous data, email if changed."""
    old_by_name = load_previous()
    if not old_by_name:
        # First run - send initial status report
        save_as_last(locations)
        subject = "LBG Water Quality: Initial Status Report"
        changes = [('new', loc['name'], None, loc) for loc in locations]
        html = format_email_html(changes, locations)
        send_email(subject, html)
        print("First run, sent initial status report")
        return

    changes = find_changes(old_by_name, locations)
    if not changes:
        print("No changes detected")
        return

    print(f"Detected {len(changes)} change(s):")
    for change_type, name, old, new in changes:
        if change_type == 'status':
            print(f"  {name}: {old['status']} -> {new['status']}")
        elif change_type == 'comments':
            print(f"  {name}: comments updated")
        elif change_type == 'new':
            print(f"  {name}: new area ({new['status']})")
        elif change_type == 'removed':
            print(f"  {name}: removed")

    # Build summary for subject line
    status_changes = [c for c in changes if c[0] == 'status']
    if status_changes:
        summaries = [f"{name} {new['status']}" for _, name, _, new in status_changes]
        subject = f"LBG Water Quality: {', '.join(summaries)}"
    else:
        subject = f"LBG Water Quality: {len(changes)} update(s)"

    html = format_email_html(changes, locations)
    send_email(subject, html)
    save_as_last(locations)


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
    check_and_notify(locations)

    for loc in locations:
        marker = " **" if loc['status'] == 'CLOSED' else ""
        print(f"  {loc['name']}: {loc['status']}{marker}")

    if is_closed:
        print("Yarralumla Bay is CLOSED")

    return 0


if __name__ == '__main__':
    sys.exit(main())
