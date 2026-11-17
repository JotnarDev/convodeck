#!/usr/bin/env python3
"""End-to-end smoke test on fake data. Runs on Linux, Windows and macOS (see .github/workflows).

  python3 tools/smoke_test.py
Never touches your real Claude Code data: everything lives in a temporary folder.
"""
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / 'skills' / 'convodeck' / 'scripts' / 'convodeck.py'
SYSTEM = platform.system()
tmp = Path(tempfile.mkdtemp(prefix='convodeck-test-'))
subprocess.run([sys.executable, str(REPO / 'tools' / 'demo.py'), str(tmp), 'en'], check=True, capture_output=True)

env = dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp / 'claude'), CONVODECK_LANG='en',
           CONVODECK_DRY_RUN='1', CONVODECK_NO_BROWSER='1', PYTHONIOENCODING='utf-8')
if SYSTEM == 'Windows':
    env['APPDATA'] = str(tmp / 'config')
    env['LOCALAPPDATA'] = str(tmp / 'cache')
elif SYSTEM == 'Darwin':
    env['HOME'] = str(tmp / 'home')
    (tmp / 'home' / 'Library' / 'Application Support').mkdir(parents=True, exist_ok=True)
    (tmp / 'config' / 'convodeck').rename(tmp / 'home' / 'Library' / 'Application Support' / 'convodeck')
else:
    env.update(XDG_CONFIG_HOME=str(tmp / 'config'), XDG_CACHE_HOME=str(tmp / 'cache'),
               XDG_DATA_HOME=str(tmp / 'data'))
    # CI runners have no terminal emulator: provide a fake one so the command can be built
    fake = tmp / 'bin'
    fake.mkdir()
    (fake / 'xterm').write_text('#!/bin/sh\nexit 0\n')
    (fake / 'xterm').chmod(0o755)
    env['PATH'] = f"{fake}{os.pathsep}{env['PATH']}"
    env['CONVODECK_TERMINAL'] = 'xterm'

failures = []


def run(*args, ok=True):
    r = subprocess.run([sys.executable, str(CLI), *args], env=env, capture_output=True, text=True,
                       encoding='utf-8', timeout=60)
    if ok and r.returncode != 0:
        raise AssertionError(f'{args} failed: {r.stderr or r.stdout}')
    return r


def check(name, cond, detail=''):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f'  ({detail})' if detail and not cond else ''))
    if not cond:
        failures.append(name)


def listing(*extra):
    return json.loads(run('list', '--json', *extra).stdout)


convs = listing()
check('lists the 8 demo conversations', len(convs) == 8, len(convs))
check('newest first', all(a['last'] >= b['last'] for a, b in zip(convs, convs[1:])))
check('summaries come from data.json', all(c['summary'] for c in convs))
first = convs[0]
short = first['id'][:6]

run('rename', short, 'Renamed', 'by', 'test')
check('rename by id prefix', listing()[0]['title'] == 'Renamed by test')
run('rename', short)
check('empty rename falls back to automatic title', listing()[0]['title'] != 'Renamed by test')

run('summary', short, ' '.join(['word'] * 30))
check('summary is capped at 20 words', len(listing()[0]['summary'].split()) == 20)

run('hide', short)
check('hide removes it from the list', all(c['id'] != first['id'] for c in listing()))
check('--hidden shows it again', any(c['id'] == first['id'] for c in listing('--hidden')))
run('unhide', short)

found = json.loads(run('list', '--json', '--search', 'astro').stdout)
check('search matches title/summary', len(found) == 1 and 'Astro' in found[0]['title'])

check('ambiguous / unknown ids are rejected', run('open', 'zzzz', ok=False).returncode != 0)

argv = json.loads(run('open', short).stdout)
check('open builds a terminal command with --resume <id>',
      f"--resume {first['id']}" in ' '.join(argv) or ('--resume' in argv and first['id'] in argv), argv)
argv_new = json.loads(run('new').stdout)
check('new builds a terminal command', bool(argv_new))

check('delete refuses without --yes', run('delete', short, ok=False).returncode != 0)
if SYSTEM != 'Darwin':   # Finder automation needs a GUI permission prompt on macOS runners
    victim = convs[-1]
    run('delete', victim['id'][:8], '--yes')
    left = list((tmp / 'claude' / 'projects').rglob(f"{victim['id']}.jsonl"))
    check('delete moves the session file out of the projects folder', not left)
    check('deleted conversation is gone from the list', all(c['id'] != victim['id'] for c in listing()))

doctor = json.loads(run('doctor').stdout)
check('doctor reports the right project folder', doctor['claude_projects'] == str(tmp / 'claude' / 'projects'))

# ---- window server: start, call the API, check the security guards
proc = subprocess.Popen([sys.executable, str(CLI), 'ui'], env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, encoding='utf-8')
try:
    url = proc.stdout.readline().strip()
    check('ui prints its local address', url.startswith('http://127.0.0.1:'), url)
    base, token = url.split('?t=')

    def post(path, body=None, headers=None):
        h = {'Content-Type': 'application/json', 'X-Token': token}
        h.update(headers or {})
        req = urllib.request.Request(base + path.lstrip('/'), data=json.dumps(body or {}).encode(),
                                     headers=h, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    code, body = post('/api/list')
    check('API lists conversations', code == 200 and len(body['conversations']) >= 7, code)
    check('API refuses a wrong token', post('/api/list', headers={'X-Token': 'nope'})[0] == 403)
    check('API refuses a foreign Origin', post('/api/list', headers={'Origin': 'http://evil.test'})[0] == 403)
    check('API rejects malformed ids', post('/api/open', {'id': '../../etc'})[0] == 400)
    target = body['conversations'][0]['id']
    code, _ = post('/api/rename', {'id': target, 'title': 'From the window'})
    check('API rename', code == 200 and listing()[0]['title'] == 'From the window')
    with urllib.request.urlopen(url, timeout=10) as r:
        check('page is served with the token', r.status == 200 and b'ConvoDeck' in r.read())
finally:
    proc.terminate()
    proc.wait(timeout=10)

print(f"\n{SYSTEM}: {'all checks passed' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
