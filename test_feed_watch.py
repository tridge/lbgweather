#!/usr/bin/env python3
"""Tests for the feed_watch alarm state machine.

The failure modes here are subtle and expensive: a watchdog that mails every
minute gets filtered, and one that drops a notification leaves an outage
invisible -- which is the exact failure this whole project exists to fix.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FAILURES = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILURES.append(what)


class Watch:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix='feedwatch-test-')
        self.status = os.path.join(self.d, 'status.json')
        self.state = os.path.join(self.d, 'state.json')
        shutil.copy(os.path.join(HERE, 'feed_watch.py'), self.d)
        with open(os.path.join(self.d, 'notify_emails.txt'), 'w') as f:
            f.write('nobody@example.invalid\n')
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.d, ignore_errors=True)
        return False

    def set_age(self, age_s, **extra):
        now = time.time()
        doc = {'station': 'cyc1', 'source': 'test',
               'last_obs_epoch': int(now - age_s),
               'written_at_epoch': int(now), 'obs_age_s': int(age_s)}
        doc.update(extra)
        with open(self.status, 'w') as f:
            json.dump(doc, f)

    def remove_status(self):
        if os.path.exists(self.status):
            os.unlink(self.status)

    def run(self, *args, expect_rc=0):
        r = subprocess.run(
            [sys.executable, 'feed_watch.py', '--status', self.status,
             '--state', self.state, '--no-mail', '--warn-age', '600',
             '--alarm-age', '1800'] + list(args),
            cwd=self.d, capture_output=True, text=True, timeout=30)
        # A crashed watchdog prints nothing and every "did it mail?" assertion
        # then passes for the wrong reason. Fail loudly on a bad exit instead.
        if r.returncode != expect_rc:
            check(False, 'feed_watch exited %d (want %d): %s'
                  % (r.returncode, expect_rc, r.stderr.strip()[:400]))
        return r.stdout

    def health(self):
        try:
            with open(os.path.join(self.d, 'feed_health.json')) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def mailed(self, out):
        return 'would mail' in out

    def st(self):
        try:
            with open(self.state) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}


def test_episode_lifecycle():
    print('\n== one alert per episode, one on recovery ==')
    with Watch() as w:
        w.set_age(60)
        check(not w.mailed(w.run()), 'healthy is silent')
        w.set_age(3600)
        check(w.mailed(w.run()), 'alarm mails once')
        check(not w.mailed(w.run()), 'still alarming is silent')
        check(not w.mailed(w.run()), 'and again')
        w.set_age(60)
        check(w.mailed(w.run()), 'recovery mails')
        check(not w.mailed(w.run()), 'steady after recovery is silent')


def test_episode_survives_a_dip_to_warn():
    print('\n== a dip to warn does not end the episode ==')
    with Watch() as w:
        w.set_age(60)
        w.run()
        w.set_age(3600)
        check(w.mailed(w.run()), 'alarm mails')
        w.set_age(900)                      # warn, not ok
        out = w.run()
        check(not w.mailed(out), 'warn does not mail')
        check(w.st().get('in_episode') is True,
              'episode still open during warn')
        w.set_age(3600)
        check(not w.mailed(w.run()),
              'returning to alarm is NOT treated as a new episode')
        w.set_age(60)
        check(w.mailed(w.run()), 'recovery still mails once at the end')


def test_undelivered_alarm_is_not_discarded():
    print('\n== an undelivered alarm is never replaced by a recovery notice ==')
    with Watch() as w:
        w.set_age(60)
        w.run()
        w.set_age(3600)
        w.run()                              # alarm delivered
        # Simulate the alarm mail having failed to send: still pending.
        st = w.st()
        st['pending_mail'] = 'alarm'
        st['last_attempt_epoch'] = 0
        with open(w.state, 'w') as f:
            json.dump(st, f)

        w.set_age(60)                        # recovers while alarm undelivered
        out = w.run()
        check(w.mailed(out), 'a mail is sent')
        check('alarm_resolved' in out,
              'merged into one went-stale-and-recovered notice, not a bare '
              'recovery for an alarm nobody saw')


def test_future_dated_is_an_alarm():
    print('\n== a future-dated observation is a fault, not freshness ==')
    with Watch() as w:
        w.set_age(-3600)                     # dated an hour ahead
        out = w.run()
        check(w.mailed(out), 'future-dated observation alarms')
        check(w.st().get('last_severity') == 'alarm', 'severity is alarm')


def test_missing_status():
    print('\n== a vanished status document ==')
    with Watch() as w:
        w.remove_status()
        out = w.run()
        check(not w.mailed(out), 'silent before we have ever seen a status')
        check('staying quiet' in out, 'and says why')

        w.set_age(60)
        w.run()
        check(w.st().get('ever_seen_status') is True, 'records having seen one')
        w.remove_status()
        out = w.run()
        check(w.mailed(out), 'disappearance after that is an alarm')


def test_lag_threshold_is_per_source():
    print('\n== receipt lag threshold ==')
    with Watch() as w:
        w.set_age(60, recv_lag_s=900)
        check(w.mailed(w.run('--lag-warn', '120')) is False,
              'lag warn does not mail (warn severity only)')
        check(w.st().get('last_severity') == 'warn', 'lag 900 > 120 -> warn')
        w.set_age(60, recv_lag_s=900)
        w.run('--lag-warn', '1800')
        check(w.st().get('last_severity') == 'ok',
              'same lag is ok when the threshold suits the source')



def test_auth_failure_key_matches_the_receiver():
    print('\n== the auth-failure field name tracks the receiver ==')
    import cyc_ingest  # noqa: F401  - just to assert the module is importable
    with Watch() as w:
        w.set_age(60, auth_failures_recent=50)
        out = w.run()
        check('authentication failures' in out,
              'auth_failures_recent is read (the receiver writes this name)')
        w.set_age(60, auth_failures_1h=50)
        out = w.run()
        check('authentication failures' not in out,
              'the old name is not silently accepted as an alias')


def test_status_document_is_never_modified():
    print('\n== the producer owns the status document ==')
    with Watch() as w:
        w.set_age(60)
        before = open(w.status, 'rb').read()
        w.run()
        after = open(w.status, 'rb').read()
        check(before == after,
              'status.json is byte-identical after a check - no read-modify-'
              'write race with the producer')
        h = w.health()
        check(h.get('obs_age_s') is not None and h.get('severity') == 'ok',
              'the live age is published in our own file instead')


def test_losing_the_state_file_does_not_disarm():
    print('\n== losing the state file must not buy silence ==')
    with Watch() as w:
        w.set_age(60)
        w.run()                                  # arms
        check(os.path.exists(w.state + '.armed'), 'armed marker written')

        # Both files that prove a fault vanish together: the producer dies and
        # takes status.json with it, and the state file is lost too.
        w.remove_status()
        os.unlink(w.state)
        out = w.run()
        check(w.mailed(out),
              'still alarms on a missing status document after the state file '
              'was lost')


def test_marker_absent_means_never_deployed():
    print('\n== a genuinely fresh install stays quiet ==')
    with Watch() as w:
        out = w.run()
        check(not w.mailed(out), 'no status, no marker -> silence')
        check(not os.path.exists(w.state + '.armed'),
              'and we do not arm ourselves without ever seeing a status')



def test_dropped_samples_alarm():
    print('\n== a healthy feed we are failing to record is an alarm ==')
    with Watch() as w:
        import time as _t
        w.set_age(60, csv_write_failed=True, csv_failures_recent=2,
                  csv_last_failure_epoch=int(_t.time()))
        out = w.run()
        check(w.mailed(out),
              'a dropped sample alarms even though the observation is fresh')
        check('dropped' in (w.health().get('problems') or [''])[0].lower(),
              'and says what is actually wrong (%s)' % w.health().get('problems'))
        check(w.health().get('severity') == 'alarm', 'severity is alarm')

        # THE CASE THAT MATTERED: the transient flag has already been cleared
        # by a good minute, but the latched count means the watchdog still
        # sees the failure it would otherwise have run straight past.
        w.set_age(60, csv_write_failed=False, csv_failures_recent=2,
                  csv_last_failure_epoch=int(_t.time()) - 60)
        out = w.run()
        check(w.health().get('severity') == 'alarm',
              'a failure that ended before this run is still caught (%s)'
              % w.health().get('severity'))

        # Stale enough, and it clears.
        w.set_age(60, csv_write_failed=False, csv_failures_recent=2,
                  csv_last_failure_epoch=int(_t.time()) - 5000)
        out = w.run()
        check(w.health().get('severity') == 'ok',
              'and clears once the failure window has passed')

        w.set_age(60, csv_write_failed=False)
        out = w.run()
        check(w.health().get('severity') == 'ok',
              'a clean status is ok')


def main():
    for fn in [test_episode_lifecycle, test_episode_survives_a_dip_to_warn,
               test_undelivered_alarm_is_not_discarded,
               test_future_dated_is_an_alarm, test_missing_status,
               test_lag_threshold_is_per_source,
               test_auth_failure_key_matches_the_receiver,
               test_status_document_is_never_modified,
               test_losing_the_state_file_does_not_disarm,
               test_marker_absent_means_never_deployed,
               test_dropped_samples_alarm]:
        fn()
    print()
    if FAILURES:
        print('%d FAILURE(S):' % len(FAILURES))
        for f in FAILURES:
            print('  - %s' % f)
        return 1
    print('all tests passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
