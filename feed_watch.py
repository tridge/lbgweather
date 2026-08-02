#!/usr/bin/env python3
"""Alarm when the weather feed goes stale.

The three-week outage in July 2026 was invisible because wind_logger.py
manufactures a CSV row every minute whether or not the station is alive -- it
stamps rows with datetime.now() and discards the feed's own timestamp, so a dead
station and a healthy one produce identical-looking files.

This watches the observation's *real* age and shouts. It does NOT write to the
status document: that file belongs to whoever produces it, and a read-modify-
write from here would race the producer and could republish a stale snapshot.
Freshness is the producer's job -- cyc_ingest heartbeats its own status, and
wind_logger rewrites its own every minute. What this writes is its own verdict,
alongside the status file, for anything that wants the health view.

Run from wind.cron every minute:

    python3 feed_watch.py --status status.json

Alerting has hysteresis on purpose: one mail when an episode starts, one daily
reminder while it continues, one when it recovers. A watchdog that sends 1440
mails a day gets filtered, and then the next outage goes unnoticed too.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Australia/Sydney')
EMAIL_LIST_FILE = 'notify_emails.txt'
FROM_ADDR = 'lbgweather@wstracker.org'

WARN_AGE_S = 600
ALARM_AGE_S = 1800
LAG_WARN_S = 120
SEQ_GAP_WARN = 5
# Long enough that a one-minute drop cannot fall between two runs.
CSV_FAIL_WINDOW_S = 900
AUTH_FAIL_WARN = 20
BACKLOG_WARN = 60
REMINDER_S = 86400
RETRY_S = 900
# Small allowance for ordinary clock jitter before a future date is a fault.
SKEW_TOLERANCE_S = 120


def load_notify_emails():
    """Load notification addresses, one per line. Matches water_quality.py."""
    try:
        with open(EMAIL_LIST_FILE) as f:
            lines = [line.strip() for line in f]
            return [line for line in lines if line and not line.startswith('#')]
    except FileNotFoundError:
        print('%s not found, no email sent' % EMAIL_LIST_FILE, file=sys.stderr)
        return []


def send_email(subject, html_body):
    recipients = load_notify_emails()
    if not recipients:
        return False
    msg = MIMEText(html_body, 'html')
    msg['Subject'] = subject
    msg['From'] = FROM_ADDR
    msg['To'] = ', '.join(recipients)
    try:
        proc = subprocess.run(['/usr/sbin/sendmail', '-t'],
                              input=msg.as_string(),
                              capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            print('feed_watch: mailed %s' % msg['To'])
            return True
        print('sendmail failed: %s' % proc.stderr, file=sys.stderr)
    except Exception as e:
        print('failed to send email: %s' % e, file=sys.stderr)
    return False


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json_atomic(path, doc, mode=0o644):
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(doc, f, indent=1, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True
    except OSError as e:
        print('write %s failed: %s' % (path, e), file=sys.stderr)
        return False


def fmt_age(seconds):
    if seconds is None:
        return 'unknown'
    seconds = int(seconds)
    if seconds < 120:
        return '%ds' % seconds
    if seconds < 7200:
        return '%dm %ds' % (seconds // 60, seconds % 60)
    return '%dh %dm' % (seconds // 3600, (seconds % 3600) // 60)


def evaluate(status, now, warn_age=WARN_AGE_S, alarm_age=ALARM_AGE_S,
             lag_warn=LAG_WARN_S):
    """Return (severity, [problems]). severity in 'ok' | 'warn' | 'alarm'.

    Thresholds are arguments because the right values depend on the feed's
    cadence: the cloud path updates roughly every 15 minutes, the direct serial
    collector every minute. A threshold tighter than the cadence leaves the
    watchdog permanently in 'warn', which trains everyone to ignore it.
    """
    problems = []
    severity = 'ok'

    last_obs = status.get('last_obs_epoch')
    if last_obs is None:
        return 'alarm', ['no observation has ever been recorded']

    age = now - last_obs
    if age < -SKEW_TOLERANCE_S:
        # A future-dated record looks perpetually fresh, which is precisely the
        # blindness this watchdog exists to remove.
        return 'alarm', ['observation is dated %s in the FUTURE - the '
                         'collector clock is wrong and the historical record '
                         'is being mis-stamped' % fmt_age(-age)]
    if age > alarm_age:
        severity = 'alarm'
        problems.append('feed is %s old (alarm above %s)'
                        % (fmt_age(age), fmt_age(alarm_age)))
    elif age > warn_age:
        severity = 'warn'
        problems.append('feed is %s old (warn above %s)'
                        % (fmt_age(age), fmt_age(warn_age)))

    lag = status.get('recv_lag_s')
    if isinstance(lag, int) and lag > lag_warn:
        severity = 'alarm' if severity == 'alarm' else 'warn'
        problems.append('%ds between observation and receipt (warn above %ds) '
                        '- the collector clock may be wrong, which silently '
                        'mis-stamps the historical record, or it is backlogged'
                        % (lag, lag_warn))

    gaps = status.get('seq_gaps')
    if isinstance(gaps, int) and gaps > SEQ_GAP_WARN:
        severity = 'alarm' if severity == 'alarm' else 'warn'
        problems.append('%d observations lost in transit (sequence gaps)' % gaps)

    # Key name must track the receiver's. It was renamed on one side only
    # once, and authentication failures became invisible here.
    auth = status.get('auth_failures_recent')
    if isinstance(auth, int) and auth > AUTH_FAIL_WARN:
        severity = 'alarm' if severity == 'alarm' else 'warn'
        problems.append('%d authentication failures in the last hour '
                        '- a key mismatch, or someone probing' % auth)

    recent_fails = status.get('csv_failures_recent')
    last_fail = status.get('csv_last_failure_epoch')
    if (isinstance(recent_fails, int) and recent_fails > 0
            and isinstance(last_fail, (int, float))
            and now - last_fail < CSV_FAIL_WINDOW_S):
        severity = 'alarm'
        problems.append('%d sample(s) dropped in the last %d minutes - the '
                        'logger could not take the day lock. The feed is '
                        'healthy and nothing is being recorded'
                        % (recent_fails, CSV_FAIL_WINDOW_S // 60))
    elif status.get('csv_latch_unavailable'):
        # We cannot see whether samples are being dropped. Unknown is a fault,
        # not a pass: this is the state a broken lock directory produces.
        severity = 'alarm'
        problems.append('the logger cannot write its failure latch, so '
                        'dropped samples would be invisible - check the '
                        'lock and state directories')
    elif status.get('csv_write_failed'):
        # The feed is fine and we are dropping it on the floor. That is an
        # alarm, not a warning: every sample in this window is gone.
        severity = 'alarm'
        problems.append('the logger could not take the day lock, so samples '
                        'are being DROPPED - the feed is healthy but nothing '
                        'is being recorded')

    backlog = (status.get('collector') or {}).get('spool_backlog')
    if isinstance(backlog, int) and backlog > BACKLOG_WARN:
        severity = 'alarm' if severity == 'alarm' else 'warn'
        problems.append('collector spool backlog is %d files - it is reaching '
                        'us but falling behind' % backlog)

    return severity, problems


def body_html(status, problems, now, recovered=False):
    last_obs = status.get('last_obs_epoch')
    when = ('never' if last_obs is None else
            datetime.fromtimestamp(last_obs, TZ).strftime('%Y-%m-%d %H:%M:%S'))
    age = None if last_obs is None else now - last_obs

    if recovered:
        head = ('<h2 style="color:#27ae60;margin:0 0 12px;">'
                'Weather feed recovered</h2>')
    else:
        head = ('<h2 style="color:#c0392b;margin:0 0 12px;">'
                'Weather feed problem</h2>')

    rows = [
        ('Last observation', when),
        ('Age', fmt_age(age)),
        ('Source', status.get('source', 'unknown')),
        ('Station', status.get('station', 'cyc1')),
    ]
    if status.get('seq') is not None:
        rows.append(('Sequence', str(status['seq'])))

    out = ['<div style="font-family:sans-serif;max-width:640px;">', head]
    if problems:
        out.append('<ul style="margin:0 0 16px;padding-left:20px;">')
        for p in problems:
            out.append('<li style="margin:4px 0;">%s</li>' % p)
        out.append('</ul>')
    out.append('<table style="border-collapse:collapse;font-size:14px;">')
    for k, v in rows:
        out.append('<tr><td style="padding:4px 12px 4px 0;color:#666;">%s</td>'
                   '<td style="padding:4px 0;font-weight:600;">%s</td></tr>'
                   % (k, v))
    out.append('</table>')
    out.append('<p style="margin-top:16px;font-size:12px;color:#999;">'
               '<a href="https://lbgweather.au" style="color:#3498db;">'
               'lbgweather.au</a></p>')
    out.append('</div>')
    return '\n'.join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--status', default='status.json',
                   help='status document written by the producer')
    p.add_argument('--state', default='feedwatch_state.json',
                   help='episode state, so alerts have hysteresis')
    p.add_argument('--warn-age', type=int, default=WARN_AGE_S,
                   help='seconds of observation age before warning '
                        '(default %d; use ~1200 for the 15-min cloud feed, '
                        '~180 for the 1-min direct collector)' % WARN_AGE_S)
    p.add_argument('--alarm-age', type=int, default=ALARM_AGE_S,
                   help='seconds before alarming (default %d)' % ALARM_AGE_S)
    p.add_argument('--lag-warn', type=int, default=LAG_WARN_S,
                   help='seconds between observation and receipt before '
                        'warning (default %d; the cloud shadow path runs ~900s '
                        'so use a high value there)' % LAG_WARN_S)
    p.add_argument('--marker', default=None,
                   help='armed marker; defaults to <state>.armed. Once written, '
                        'a missing status document is an alarm even if the '
                        'state file is lost')
    p.add_argument('--armed', action='store_true',
                   help='force armed, regardless of the marker')
    p.add_argument('--health-out', default=None,
                   help='write the health verdict here (default '
                        '<status dir>/feed_health.json); the status document '
                        'itself is never modified')
    p.add_argument('--no-mail', action='store_true',
                   help='evaluate and print, send nothing (for testing)')
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args(argv)
    if args.marker is None:
        args.marker = args.state + '.armed'
    if args.health_out is None:
        args.health_out = os.path.join(os.path.dirname(args.status) or '.',
                                       'feed_health.json')

    now = time.time()
    state = load_json(args.state, {}) or {}
    status = load_json(args.status)

    # Losing the state file must not disarm the watchdog. The marker is a
    # separate, never-rewritten file: if the state JSON is deleted or corrupted
    # while the marker stands, we are still armed and a missing status document
    # is still an alarm. Otherwise the two files that prove a fault -- status
    # and state -- could vanish together and buy permanent silence.
    armed = args.armed or os.path.exists(args.marker)
    if armed:
        state['ever_seen_status'] = True

    if status is None:
        # Before first deployment this is normal and silence is right. Once we
        # have seen a status document, its disappearance is a real fault --
        # deletion, a permission change, or the producer dying -- and must not
        # be invisible.
        if not state.get('ever_seen_status'):
            if not args.quiet:
                print('feed_watch: no status document at %s (never seen one '
                      'yet, staying quiet)' % args.status)
            return 0
        status = {}
        severity, problems = 'alarm', [
            'status document %s has disappeared or is unreadable' % args.status]
    else:
        state['ever_seen_status'] = True
        if not os.path.exists(args.marker):
            try:
                fd = os.open(args.marker,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as f:
                    f.write('armed %s\n' % datetime.fromtimestamp(now, TZ)
                            .isoformat(timespec='seconds'))
                    f.flush()
                    os.fsync(f.fileno())
                # fsync the directory too: without it a power cut right here
                # loses the marker, and the watchdog silently disarms -- the
                # one failure this file exists to prevent.
                dfd = os.open(os.path.dirname(args.marker) or '.', os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except FileExistsError:
                pass          # another instance armed first; fine
            except OSError as e:
                print('could not write armed marker: %s' % e, file=sys.stderr)
        severity = None
        problems = None

    last_obs = status.get('last_obs_epoch')

    if severity is None:
        severity, problems = evaluate(status, now, args.warn_age,
                                      args.alarm_age, args.lag_warn)
    in_episode = bool(state.get('in_episode'))
    last_mail = state.get('last_mail_epoch', 0)
    last_attempt = state.get('last_attempt_epoch', 0)

    if not args.quiet:
        print('feed_watch: %s age=%s %s'
              % (severity, fmt_age(None if last_obs is None else now - last_obs),
                 '; '.join(problems) if problems else 'ok'))

    # A mail is queued as 'pending' and only cleared once actually delivered, so
    # a broken sendmail delays a notification but never loses one. State
    # transitions (episode start, recovery) send immediately; retries and the
    # daily reminder are rate-limited so a mail failure cannot itself become the
    # outage.
    prev_pending = state.get('pending_mail')
    pending = prev_pending

    if severity == 'alarm':
        if not in_episode:
            pending = 'alarm'                        # episode starts
        elif pending is None and now - last_mail > REMINDER_S:
            pending = 'alarm'                        # daily reminder
    elif severity == 'ok' and in_episode:
        # Never discard an undelivered outage alert by overwriting it with a
        # recovery notice -- that would announce the end of an incident nobody
        # was told about. Merge instead.
        pending = 'alarm_resolved' if pending == 'alarm' else 'recovered'

    if pending:
        first_try = (pending != prev_pending)
        if first_try or now - last_attempt >= RETRY_S:
            state['last_attempt_epoch'] = now
            recovered = pending in ('recovered', 'alarm_resolved')
            if args.no_mail:
                print('feed_watch: would mail (%s)' % pending)
                state['last_mail_epoch'] = now
                pending = None
            else:
                subject = {
                    'alarm': 'lbgweather: weather feed stale',
                    'recovered': 'lbgweather: feed recovered',
                    'alarm_resolved':
                        'lbgweather: feed went stale and has recovered',
                }[pending]
                if send_email(subject,
                              body_html(status, problems, now, recovered)):
                    state['last_mail_epoch'] = now
                    pending = None

    state['pending_mail'] = pending

    # An episode persists through 'warn' and only ends at 'ok'. Clearing it on
    # a transient dip to warn would make the next alarm look like a new
    # episode and re-alert, and would skip the recovery mail entirely.
    if severity == 'alarm':
        state['in_episode'] = True
    elif severity == 'ok':
        state['in_episode'] = False
    state['last_severity'] = severity
    state['last_check_epoch'] = int(now)
    save_json_atomic(args.state, state, mode=0o600)

    # Our own verdict, in our own file. Consumers that want a live age read
    # obs_age_s here; the producer's status document is left untouched.
    save_json_atomic(args.health_out, {
        'checked_at': datetime.fromtimestamp(now, TZ)
                              .isoformat(timespec='seconds'),
        'checked_at_epoch': int(now),
        'severity': severity,
        'problems': problems or [],
        'obs_age_s': None if last_obs is None else int(now - last_obs),
        'last_obs_epoch': last_obs,
        'status_source': args.status,
        'in_episode': bool(state.get('in_episode')),
    })

    return 0


if __name__ == '__main__':
    sys.exit(main())
