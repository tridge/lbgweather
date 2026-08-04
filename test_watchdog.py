#!/usr/bin/env python3
"""Watchdog decision tests.

The decision is separated from the killing so it can be tested exhaustively
without spawning processes. The one real-process test proves a genuinely
wedged collector is terminated and its port released.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import watchdog  # noqa: E402
from watchdog import evaluate, read_heartbeat  # noqa: E402

FAILURES = []


def check(cond, what):
    print('  %s %s' % ('ok  ' if cond else 'FAIL', what))
    if not cond:
        FAILURES.append(what)


def hb(state='ok', age=0, pid=1234, now=None):
    now = now or time.time()
    return {'state': state, 'epoch': now - age, 'pid': pid,
            'version': 'collect-0.1'}


def test_healthy_is_left_alone():
    print('\n== a working collector ==')
    now = time.time()
    action, why = evaluate(hb('ok', age=5, now=now), None, now)
    check(action == 'none', 'a fresh heartbeat is left alone (%s)' % why)
    action, why = evaluate(hb('ok', age=119, now=now), None, now, stale_s=120)
    check(action == 'none', 'and so is one just inside the limit')


def test_stale_is_killed():
    print('\n== a wedged collector ==')
    now = time.time()
    action, why = evaluate(hb('ok', age=300, now=now), None, now, stale_s=120)
    check(action == 'kill',
          'a heartbeat past the limit means kill (%s)' % why)
    check('pid 1234' in why, 'and names the pid to kill')

    action, why = evaluate(hb('ok', age=300, pid=None, now=now), None, now,
                           stale_s=120)
    check(action == 'alert',
          'stale with no pid is reported rather than guessed at (%s)' % why)


def test_retrying_is_not_wedged():
    print('\n== retrying a dead console is WORK, not a hang ==')
    now = time.time()
    for state in ('down', 'resync', 'clock_jump', 'disk_full'):
        action, why = evaluate(hb(state, age=5, now=now), None, now)
        check(action == 'none',
              '%s with a fresh heartbeat is active, not killed' % state)
    # ...but a collector stuck in any of them still stops beating eventually.
    action, _ = evaluate(hb('down', age=600, now=now), None, now, stale_s=120)
    check(action == 'kill',
          'a state that stops updating is still killed, whatever it claims')


def test_clean_exit_is_not_killed():
    print('\n== a collector that exited on purpose ==')
    now = time.time()
    action, why = evaluate(hb('stopped', age=9999, now=now), None, now)
    check(action == 'none',
          'an old "stopped" heartbeat is not a hang (%s)' % why)


def test_missing_and_malformed():
    print('\n== no heartbeat, or a broken one ==')
    now = time.time()
    action, why = evaluate(None, 'no heartbeat file', now)
    check(action == 'none',
          'never having seen one is not the same as one going stale, and '
          'there is no pid to kill anyway (%s)' % why)

    action, why = evaluate({'state': 'ok', 'pid': 5}, None, now)
    check(action == 'none', 'a heartbeat with no timestamp is not actionable')

    action, why = evaluate(hb('mystery', age=999, now=now), None, now)
    check(action == 'none',
          'an unrecognised state is left alone rather than killed on a guess')

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 'hb.json')
        with open(p, 'w') as f:
            f.write('{"truncated"')
        doc, err = read_heartbeat(p)
        check(doc is None and 'unreadable' in err,
              'a torn heartbeat reads as unreadable, not as healthy')


def test_kills_a_really_wedged_process():
    print('\n== killing a process that is genuinely stuck ==')
    d = tempfile.mkdtemp(prefix='watchdog-test-')
    try:
        hbpath = os.path.join(d, 'hb.json')
        # A process that writes one heartbeat and then blocks for ever, which
        # is exactly what a collector stuck in a serial read looks like.
        src = ('import json, os, time, sys\n'
               'json.dump({"state": "ok", "pid": os.getpid(),\n'
               '           "epoch": time.time() - 9999}, open(%r, "w"))\n'
               'sys.stdout.write("ready\\n"); sys.stdout.flush()\n'
               'time.sleep(3600)\n' % hbpath)
        p = subprocess.Popen([sys.executable, '-c', src],
                             stdout=subprocess.PIPE, text=True)
        check(p.stdout.readline().strip() == 'ready', 'the stuck process ran')
        check(watchdog.pid_alive(p.pid), 'and is alive')

        doc, err = read_heartbeat(hbpath)
        doc['pid'] = p.pid
        action, why = evaluate(doc, err, time.time())
        check(action == 'kill', 'the watchdog decides to kill it (%s)' % why)

        watchdog.kill(p.pid)
        rc = p.wait(timeout=10)
        # On POSIX a killed child stays a zombie until it is reaped, and
        # os.kill(pid, 0) still succeeds on a zombie. Reap first, then ask --
        # otherwise the check is about bookkeeping rather than about the
        # process actually being gone. Windows taskkill has no such state.
        check(rc != 0,
              'the process was terminated rather than exiting normally (%s)'
              % rc)
        check(not watchdog.pid_alive(p.pid),
              'and it is gone, so COM4 is released for the next instance')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dry_run_changes_nothing():
    print('\n== --dry-run ==')
    d = tempfile.mkdtemp(prefix='watchdog-test-')
    try:
        hbpath = os.path.join(d, 'hb.json')
        with open(hbpath, 'w') as f:
            json.dump(hb('ok', age=9999, pid=999999), f)
        rc = watchdog.main(['--heartbeat', hbpath, '--dry-run', '--quiet'])
        check(rc == 0, 'it reports what it would do and stops (rc=%s)' % rc)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    for fn in [test_healthy_is_left_alone, test_stale_is_killed,
               test_retrying_is_not_wedged, test_clean_exit_is_not_killed,
               test_missing_and_malformed, test_kills_a_really_wedged_process,
               test_dry_run_changes_nothing]:
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
