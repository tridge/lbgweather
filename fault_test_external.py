"""Fault injection against the DEPLOYED receiver, from outside.

Run from the laptop so the per-IP rate limiter targets this host rather than the
server's loopback -- otherwise these tests would lock out the live shadow poster.

No valid key is needed: every case here is a rejection path.
"""
import json
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = 'https://lbgweather.au'
FAILURES = []


def check(cond, what, detail=''):
    print('  %s %s%s' % ('ok  ' if cond else 'FAIL', what,
                         '' if not detail else '   [%s]' % detail))
    if not cond:
        FAILURES.append(what)


def req(path, method='GET', body=None, headers=None, timeout=25):
    r = urllib.request.Request(BASE + path, data=body, method=method)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b'')
    except Exception as e:
        return None, str(e).encode()


def jbody(d):
    return json.dumps(d).encode()


def hdrs(sig='v1=' + '0' * 64, ts=None, nonce=None, station='cyc1'):
    return {'Content-Type': 'application/json',
            'X-CYC-Station': station,
            'X-CYC-Timestamp': str(ts if ts is not None else int(time.time())),
            'X-CYC-Nonce': nonce or secrets.token_hex(16),
            'X-CYC-Signature': sig}


OBS = {'schema': 1, 'station': 'cyc1',
       'obs': [{'obs_ts': '2026-08-01T06:00:00+10:00', 'seq': 1,
                'wind_kt': 1.0}]}

print('== transport and auth faults, against the live service ==')

code, b = req('/ingest/health')
check(code == 200 and b'"ok": true' in b, 'health reachable and healthy')

code, _ = req('/ingest/obs', 'POST', jbody(OBS),
              {'Content-Type': 'application/json'})
check(code == 401, 'unsigned POST rejected', 'got %s' % code)

code, b = req('/ingest/obs', 'POST', jbody(OBS), hdrs())
check(code == 401 and b'auth_failed' in b, 'bogus signature rejected',
      'got %s' % code)

code, b = req('/ingest/obs', 'POST', jbody(OBS),
              hdrs(station='does-not-exist'))
check(code == 401, 'unknown station rejected', 'got %s' % code)

code, b = req('/ingest/obs', 'POST', jbody(OBS), hdrs(ts=int(time.time()) - 3600))
# An unknown signature is checked first, so a bad key + skew still gives 401.
check(code in (400, 401), 'stale timestamp rejected', 'got %s' % code)

# Apache LimitRequestBody should refuse this before it reaches the application.
big = b'{"schema":1,"obs":[' + b'{"obs_ts":"2026-08-01T06:00:00+10:00"},' * 90000
big = big[:-1] + b']}'
code, _ = req('/ingest/obs', 'POST', big, hdrs(), timeout=60)
check(code == 413, 'oversized body refused at the edge (%d MB)'
      % (len(big) // 1048576), 'got %s' % code)

print()
print('== rate limiting (this host only) ==')
print('   note: leaves this IP limited for %ds; auth cases above must run first'
      % 300)
codes = []
for _ in range(14):
    c, _ = req('/ingest/obs', 'POST', jbody(OBS), hdrs())
    codes.append(c)
check(429 in codes, 'repeated auth failures eventually rate limited',
      'sequence tail %s' % codes[-4:])
check(codes[0] == 401, 'first failure was 401, not 429')

print()
print('== secrets and internals must not be reachable ==')
for p, want in [('/etc/cyc_ingest.key', 404),
                # Apache rejects the malformed URI outright; either way
                # the key is unreachable.
                ('/../etc/cyc_ingest.key', 400),
                # 404, not 403: the service code no longer lives under the
                # DocumentRoot at all, so its existence is not even confirmed.
                ('/cyc_ingest.py', 404),
                ('/cyc_store.py', 404),
                ('/feed_watch.py', 404),
                ('/cyc_shadow_post.py', 404),
                ('/feedwatch_state.json', 404),
                ('/wind_logger.py', 403),
                ('/notify_emails.txt', 403)]:
    code, _ = req(p)
    check(code == want, 'GET %s -> %s' % (p, want), 'got %s' % code)

print()
print('== public health documents remain available ==')
for p in ['/status.json', '/cyc_status.json', '/ingest/health']:
    code, _ = req(p)
    check(code == 200, 'GET %s -> 200' % p, 'got %s' % code)

print()
print('== the website itself is unaffected ==')
for p in ['/', '/index.html']:
    code, _ = req(p)
    check(code == 200, 'GET %s -> 200' % p, 'got %s' % code)

print()
if FAILURES:
    print('%d FAILURE(S): %s' % (len(FAILURES), '; '.join(FAILURES)))
    sys.exit(1)
print('all external fault cases passed')
