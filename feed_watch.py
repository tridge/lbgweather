#!/usr/bin/env python3
"""Alarm when the weather feed goes stale.

The three-week outage in July 2026 was invisible because wind_logger.py
manufactures a CSV row every minute whether or not the station is alive -- it
stamps rows with datetime.now() and discards the feed's own timestamp, so a dead
station and a healthy one produce identical-looking files.

This watches the observation's *real* age and shouts. It also refreshes the
status document's obs_age_s each run, so a dead producer shows a growing age
rather than a frozen one.

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
SKEW_WARN_S = 120
SEQ_GAP_WARN = 5
AUTH_FAIL_WARN = 20
BACKLOG_WARN = 60
REMINDER_S = 86400
RETRY_S = 900


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


def evaluate(status, now):
    """Return (severity, [problems]). severity in 'ok' | 'warn' | 'alarm'."""
    problems = []
    severity = 'ok'

    last_obs = status.get('last_obs_epoch')
    if last_obs is None:
        return 'alarm', ['no observation has ever been recorded']

    age = now - last_obs
    if age > ALARM_AGE_S:
        severity = 'alarm'
        problems.append('feed is %s old (alarm above %s)'
                        % (fmt_age(age), fmt_age(ALARM_AGE_S)))
    elif age > WARN_AGE_S:
        severity = 'warn'
        problems.append('feed is %s old (warn above %s)'
                        % (fmt_age(age), fmt_age(WARN_AGE_S)))

    skew = status.get('clock_skew_s')
    if isinstance(skew, int) and abs(skew) > SKEW_WARN_S:
        severity = 'alarm' if severity == 'alarm' else 'warn'
        problems.append('clock skew %+ds between observation and receipt '
                        '- the collector clock may be wrong, which silently '
                        'mis-stamps the historical record' % skew)

    gaps = status.get('seq_gaps')
    if isinstance(gaps, int) and gaps > SEQ_GAP_WARN:
        severity = 'alarm' if severity == 'alarm' else 'warn'
        problems.append('%d observations lost in transit (sequence gaps)' % gaps)

    auth = status.get('auth_failures_1h')
    if isinstance(auth, int) and auth > AUTH_FAIL_WARN:
        severity = 'alarm' if severity == 'alarm' else 'warn'
        problems.append('%d authentication failures in the last hour '
                        '- a key mismatch, or someone probing' % auth)

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
    p.add_argument('--no-mail', action='store_true',
                   help='evaluate and print, send nothing (for testing)')
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args(argv)

    now = time.time()
    status = load_json(args.status)
    if status is None:
        # No status file at all is itself a fault, but it is also the normal
        # state before first deployment, so stay quiet rather than crying wolf.
        if not args.quiet:
            print('feed_watch: no status document at %s' % args.status)
        return 0

    # Refresh the derived age so a dead producer shows a growing number.
    last_obs = status.get('last_obs_epoch')
    if last_obs is not None:
        status['obs_age_s'] = int(now - last_obs)
        status['age_refreshed_at_epoch'] = int(now)
        save_json_atomic(args.status, status)

    severity, problems = evaluate(status, now)
    state = load_json(args.state, {}) or {}
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
        pending = 'recovered'                        # recovery

    if pending:
        first_try = (pending != prev_pending)
        if first_try or now - last_attempt >= RETRY_S:
            state['last_attempt_epoch'] = now
            recovered = (pending == 'recovered')
            if args.no_mail:
                print('feed_watch: would mail (%s)' % pending)
                state['last_mail_epoch'] = now
                pending = None
            else:
                subject = ('lbgweather: feed recovered' if recovered
                           else 'lbgweather: weather feed stale')
                if send_email(subject,
                              body_html(status, problems, now, recovered)):
                    state['last_mail_epoch'] = now
                    pending = None

    state['pending_mail'] = pending

    state['in_episode'] = (severity == 'alarm')
    state['last_severity'] = severity
    state['last_check_epoch'] = int(now)
    save_json_atomic(args.state, state, mode=0o600)

    return 0


if __name__ == '__main__':
    sys.exit(main())
