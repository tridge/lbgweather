#!/usr/bin/env python3
"""Convert Open-Meteo forecast archive valid_times from naive Canberra local to UTC.

BOM archives already use UTC (valid_times end with 'Z') and are skipped.
Handles DST correctly via zoneinfo.ZoneInfo('Australia/Sydney').

Run once to migrate existing archives. Safe to run multiple times -
already-converted entries (ending with 'Z') are skipped.
"""

import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CANBERRA_TZ = ZoneInfo('Australia/Sydney')
ARCHIVE_DIR = os.path.join('forecasts', 'archive')

# Only convert Open-Meteo model files, not BOM (which already uses UTC)
OPEN_METEO_MODELS = ['ecmwf', 'gfs', 'icon', 'gem', 'jma']


def convert_valid_time(vt):
    """Convert a naive local time string to UTC. Skip if already UTC."""
    if vt.endswith('Z'):
        return vt  # Already UTC
    try:
        local_dt = datetime.strptime(vt, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=CANBERRA_TZ)
    except ValueError:
        try:
            local_dt = datetime.strptime(vt, '%Y-%m-%dT%H:%M').replace(tzinfo=CANBERRA_TZ)
        except ValueError:
            print(f"  WARNING: Could not parse valid_time: {vt}")
            return vt
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def convert_file(filepath):
    """Convert all valid_times in a JSONL forecast file to UTC."""
    with open(filepath, 'r') as f:
        lines = f.readlines()

    converted = 0
    skipped = 0
    new_lines = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue

        forecasts = data.get('forecasts', [])
        for entry in forecasts:
            vt = entry.get('valid_time', '')
            if vt.endswith('Z'):
                skipped += 1
            else:
                entry['valid_time'] = convert_valid_time(vt)
                converted += 1

        new_lines.append(json.dumps(data))

    if converted > 0:
        with open(filepath, 'w') as f:
            f.write('\n'.join(new_lines) + '\n')

    return converted, skipped


def main():
    if not os.path.isdir(ARCHIVE_DIR):
        print(f"Archive directory not found: {ARCHIVE_DIR}")
        return 1

    total_converted = 0
    total_skipped = 0
    files_modified = 0

    for root, dirs, files in sorted(os.walk(ARCHIVE_DIR)):
        dirs.sort()
        for filename in sorted(files):
            if not filename.endswith('.jsonl'):
                continue
            # Extract model name from filename like "06_gfs.jsonl"
            model = filename.rsplit('_', 1)[-1].replace('.jsonl', '')
            if model not in OPEN_METEO_MODELS:
                continue

            filepath = os.path.join(root, filename)
            converted, skipped = convert_file(filepath)
            total_converted += converted
            total_skipped += skipped
            if converted > 0:
                files_modified += 1
                print(f"  {filepath}: converted {converted} times, {skipped} already UTC")

    print(f"\nDone: {total_converted} times converted in {files_modified} files, "
          f"{total_skipped} already UTC")
    return 0


if __name__ == '__main__':
    sys.exit(main())
