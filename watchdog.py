#!/usr/bin/env python3
"""Terminate a collector that has stopped making progress.

A wedged owner is the failure this exists for. It still holds COM4, so nothing
else can open the port; it cannot honour a stop request, because the code that
would honour it is blocked; and Task Scheduler's `IgnoreNew` plus
`ExecutionTimeLimit PT0S` means a new instance is never started while the old
one is technically alive. The result is a station that is silent until somebody
reboots the machine.

So the watchdog does not ask. It reads the heartbeat, and if the collector has
not written one within the deadline it kills the process by pid and lets the
scheduler start a fresh one.

Deliberately conservative: `down` and `resync` are ACTIVE states. A collector
retrying a dead console is working correctly and must not be killed for it --
that would turn a station fault into a restart loop and lose the backoff.
"""

import argparse
import json
import os
import subprocess
import sys
import time

STALE_S = 120           # four missed heartbeats at the 15s interval
KILL_GRACE_S = 5
ACTIVE_STATES = ('ok', 'down', 'resync', 'clock_jump', 'disk_full')


def log(msg):
    sys.stderr.write('%s watchdog: %s\n'
                     % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    sys.stderr.flush()


def read_heartbeat(path):
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, 'no heartbeat file'
    except (OSError, ValueError) as e:
        return None, 'unreadable heartbeat: %s' % e
    if not isinstance(doc, dict):
        return None, 'heartbeat is not an object'
    return doc, None


def pid_alive(pid):
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid],
                             capture_output=True, text=True, timeout=30)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                       capture_output=True, text=True, timeout=30)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def evaluate(doc, err, now, stale_s=STALE_S):
    """Decide what to do. Returns (action, reason).

    Separated from the doing so the decision can be tested without processes.
    """
    if doc is None:
        # Never having seen a heartbeat is not the same as one going stale. On
        # a fresh install the collector may simply not have started yet, and
        # killing an unknown pid is not an option anyway.
        return 'none', err

    stamp = doc.get('epoch')
    if not isinstance(stamp, (int, float)):
        return 'none', 'heartbeat has no usable timestamp'
    age = now - stamp
    state = doc.get('state')
    pid = doc.get('pid')

    if state == 'stopped':
        return 'none', 'collector exited cleanly'
    if state not in ACTIVE_STATES:
        return 'none', 'unknown state %r; leaving it alone' % state
    if age <= stale_s:
        return 'none', 'alive (%ds old, state %s)' % (age, state)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return 'alert', 'stale by %ds but no pid to kill' % age
    return 'kill', ('heartbeat %ds old (limit %ds), state %s, pid %d'
                    % (age, stale_s, state, pid))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--heartbeat', default=r'C:\cycwx\heartbeat.json')
    p.add_argument('--stale', type=float, default=STALE_S)
    p.add_argument('--task', default=None,
                   help='scheduled task to start after a kill')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args(argv)

    doc, err = read_heartbeat(args.heartbeat)
    action, reason = evaluate(doc, err, time.time(), args.stale)

    if action == 'none':
        if not args.quiet:
            log(reason)
        return 0
    if action == 'alert':
        log('PROBLEM: %s' % reason)
        return 2

    log('killing collector: %s' % reason)
    if args.dry_run:
        return 0
    pid = doc['pid']
    kill(pid)
    deadline = time.time() + KILL_GRACE_S
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.3)
    if pid_alive(pid):
        log('PROBLEM: pid %d survived the kill; COM4 is still held' % pid)
        return 2

    if args.task:
        # The scheduler will not start a second instance while the first is
        # alive, so the restart has to come after the kill, not instead of it.
        subprocess.run(['schtasks', '/Run', '/TN', args.task],
                       capture_output=True, text=True, timeout=60)
        log('restarted scheduled task %s' % args.task)
    return 1


if __name__ == '__main__':
    sys.exit(main())
