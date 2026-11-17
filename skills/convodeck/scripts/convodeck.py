#!/usr/bin/env python3
"""ConvoDeck — a launcher for your Claude Code conversations.

Cross-platform (Linux, Windows, macOS), standard library only (Python 3.9+).

Commands:
  list [--json] [--limit N] [--search TEXT]   list conversations, newest first
  show ID                                     details + condensed transcript
  open ID                                     resume it in a new terminal window
  new [DIR]                                   start a new conversation in a terminal
  rename ID [TITLE]                           set a title (no title = back to automatic)
  summary ID TEXT                             set the short summary by hand
  summarize [--all] [--id ID]                 write summaries (<=20 words) with Claude Haiku
  delete ID --yes                             move it to the trash / Recycle Bin
  hide ID | unhide ID                         hide from the lists without deleting
  ui                                          open the ConvoDeck window
  install [--no-gnome] | uninstall            launcher shortcut (+ GNOME top-bar menu)
  doctor                                      show what was detected on this machine

IDs can be shortened to any unique prefix (at least 4 characters).
Environment: CLAUDE_CONFIG_DIR (Claude Code data, default ~/.claude),
CONVODECK_TERMINAL (Linux terminal to use), CONVODECK_LANG (en | es).
"""
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = '1.0.0'
SYSTEM = platform.system()          # 'Linux' | 'Windows' | 'Darwin'
WINDOWS = SYSTEM == 'Windows'
MAC = SYSTEM == 'Darwin'
HOME = Path.home()
HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / 'assets' if (HERE.parent / 'assets').is_dir() else HERE / 'assets'

ID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SKIP_PREFIXES = ('<local-command', '<command-', '<task-notification', '<system-reminder',
                 'This session is being continued', 'Base directory for this skill',
                 '[Request interrupted', 'Your response above was stopped', 'Caveat:')

IDLE_BEFORE_SUMMARY = 15 * 60   # never summarize a conversation that may still be running
RETRY_AFTER = 24 * 3600         # a failed summary is retried the next day
REGROW = 6                      # new messages that make a summary stale


# ---------------------------------------------------------------- paths

def claude_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or HOME / '.claude')


def projects_dir():
    return claude_dir() / 'projects'


def config_dir():
    if WINDOWS:
        base = Path(os.environ.get('APPDATA') or HOME / 'AppData' / 'Roaming')
    elif MAC:
        base = HOME / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or HOME / '.config')
    return base / 'convodeck'


def cache_dir():
    if WINDOWS:
        base = Path(os.environ.get('LOCALAPPDATA') or HOME / 'AppData' / 'Local')
    elif MAC:
        base = HOME / 'Library' / 'Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME') or HOME / '.cache')
    return base / 'convodeck'


DATA = config_dir() / 'data.json'
SETTINGS = config_dir() / 'settings.json'
CACHE = cache_dir() / 'index.json'


def settings():
    try:
        return json.loads(SETTINGS.read_text(encoding='utf-8'))
    except Exception:
        return {}


# ---------------------------------------------------------------- language

def lang():
    forced = os.environ.get('CONVODECK_LANG') or settings().get('lang')
    if forced in ('en', 'es'):
        return forced
    if WINDOWS:
        try:
            import ctypes
            return 'es' if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0A else 'en'
        except Exception:
            return 'en'
    for var in ('LC_ALL', 'LC_MESSAGES', 'LANGUAGE', 'LANG'):
        v = os.environ.get(var)
        if v:
            return 'es' if v.lower().startswith('es') else 'en'
    return 'en'


T = {
    'en': {'home': 'Home', 'no_folder': 'folder missing', 'deleted': 'Moved to the trash: {}',
           'renamed': 'Renamed: {}', 'not_found': 'No conversation matches "{}"',
           'ambiguous': '"{}" matches several conversations; use more characters',
           'confirm': 'Refusing to delete without --yes (ask the user first).',
           'summary_lang': 'English', 'none': 'No conversations yet.'},
    'es': {'home': 'Inicio', 'no_folder': 'sin carpeta', 'deleted': 'Movida a la papelera: {}',
           'renamed': 'Renombrada: {}', 'not_found': 'Ninguna conversación coincide con "{}"',
           'ambiguous': '"{}" coincide con varias conversaciones; usa más caracteres',
           'confirm': 'No se elimina sin --yes (pregunta antes al usuario).',
           'summary_lang': 'Spanish', 'none': 'Aún no hay conversaciones.'},
}


def t(key, *a):
    return T[lang()][key].format(*a)


# ---------------------------------------------------------------- locking

class FileLock:
    """Tiny cross-process lock (fcntl on Unix, msvcrt on Windows)."""

    def __init__(self, path, blocking=True):
        self.path, self.blocking, self.f = Path(path), blocking, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, 'a+')
        try:
            if WINDOWS:
                import msvcrt
                self.f.seek(0)
                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                msvcrt.locking(self.f.fileno(), mode, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except OSError:
            self.f.close()
            self.f = None
            raise
        return self

    def __exit__(self, *exc):
        try:
            if WINDOWS:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.f.close()


# ---------------------------------------------------------------- user data (titles, summaries)

def load_data():
    try:
        return json.loads(DATA.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_data(data):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    os.replace(tmp, DATA)


def update(sid, **changes):
    """Read-modify-write under a lock so the UI and a background summary never clobber each other."""
    with FileLock(cache_dir() / 'data.lock'):
        data = load_data()
        entry = data.setdefault(sid, {})
        for k, v in changes.items():
            if v is None:
                entry.pop(k, None)
            else:
                entry[k] = v
        if not entry:
            data.pop(sid, None)
        save_data(data)


# ---------------------------------------------------------------- reading Claude Code sessions

def session_files():
    out = {}
    root = projects_dir()
    if not root.is_dir():
        return out
    for folder in root.iterdir():
        if folder.is_dir():
            for f in folder.glob('*.jsonl'):
                if ID_RE.fullmatch(f.stem):
                    out[f.stem] = f
    return out


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(x.get('text', '') for x in content if isinstance(x, dict) and x.get('type') == 'text')
    return ''


def clean(s, limit=70):
    s = re.sub(r'</?pasted_content[^>]*>', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:limit] + ('…' if len(s) > limit else '')


def iso_to_unix(ts):
    try:
        return int(datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp())
    except Exception:
        return None


def read_session(path):
    """Real messages only (what the user typed + the assistant's text), without internal noise."""
    cwd, title, msgs, last = None, None, [], None
    with open(path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get('customTitle'):
                title = d['customTitle']
            kind = d.get('type')
            if kind not in ('user', 'assistant') or d.get('isMeta') or d.get('isSidechain'):
                continue
            content = (d.get('message') or {}).get('content')
            if kind == 'user':
                cwd = cwd or d.get('cwd')
                if isinstance(content, list) and any(
                        isinstance(x, dict) and x.get('type') == 'tool_result' for x in content):
                    continue
            s = text_of(content).strip()
            if not s or s.startswith(SKIP_PREFIXES):
                continue
            msgs.append((kind, s))
            last = d.get('timestamp') or last
    return cwd, title, msgs, iso_to_unix(last) if last else None


def same_path(a, b):
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return False


def conversations(include_hidden=False):
    try:
        cache = json.loads(CACHE.read_text(encoding='utf-8'))
    except Exception:
        cache = {}
    data = load_data()
    out, fresh = [], {}
    for sid, path in session_files().items():
        st = path.stat()
        key = f'{st.st_mtime_ns}:{st.st_size}'
        info = cache.get(sid)
        if not info or info.get('key') != key:
            cwd, title, msgs, last = read_session(path)
            user = [s for k, s in msgs if k == 'user']
            info = {'key': key, 'cwd': cwd, 'title': title, 'n': len(user), 'last': last,
                    'first': clean(user[0]) if user else None}
        fresh[sid] = info
        d = data.get(sid, {})
        if info['n'] == 0 or (d.get('hidden') and not include_hidden):
            continue  # empty sessions (/clear, interrupted at once)
        cwd = info['cwd'] or str(HOME)
        out.append({
            'id': sid,
            'title': d.get('title') or info['title'] or info['first'] or sid[:8],
            'summary': d.get('summary') or '',
            'cwd': cwd,
            'project': '~' if same_path(cwd, HOME) else (Path(cwd).name or cwd),
            'exists': os.path.isdir(cwd),
            'messages': info['n'],
            # time of the last real message (file mtimes change if anything rewrites the file)
            'last': info.get('last') or int(st.st_mtime),
            'hidden': bool(d.get('hidden')),
        })
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(fresh), encoding='utf-8')
    except Exception:
        pass
    out.sort(key=lambda c: -c['last'])
    return out


def resolve(ref, include_hidden=True):
    ref = ref.strip().lower()
    if len(ref) < 4:
        sys.exit(t('not_found', ref))
    matches = [c for c in conversations(include_hidden) if c['id'].startswith(ref)]
    if not matches:
        sys.exit(t('not_found', ref))
    if len(matches) > 1:
        sys.exit(t('ambiguous', ref))
    return matches[0]


# ---------------------------------------------------------------- finding programs

def claude_exe():
    found = shutil.which('claude')
    if found:
        return found
    candidates = []
    if WINDOWS:
        for base in (os.environ.get('USERPROFILE'), os.environ.get('APPDATA'), os.environ.get('LOCALAPPDATA')):
            if base:
                candidates += [Path(base) / '.local' / 'bin' / 'claude.exe', Path(base) / 'npm' / 'claude.cmd']
    else:
        candidates += [HOME / '.local' / 'bin' / 'claude', HOME / '.claude' / 'local' / 'claude',
                       Path('/usr/local/bin/claude'), Path('/opt/homebrew/bin/claude')]
        candidates += sorted((HOME / '.nvm' / 'versions' / 'node').glob('*/bin/claude'), reverse=True)
        candidates += sorted((HOME / '.volta' / 'bin').glob('claude'))
    for c in candidates:
        if c.exists():
            return str(c)
    return None


# Linux terminals: name -> argv builder (dir, title, command list). Popen also gets cwd=dir,
# so terminals without a working-directory flag still start in the right place.
TERMINALS = {
    'gnome-terminal': lambda d, t, c: ['gnome-terminal', f'--working-directory={d}', f'--title={t}', '--', *c],
    'ptyxis': lambda d, t, c: ['ptyxis', '--new-window', f'--working-directory={d}', '--', *c],
    'konsole': lambda d, t, c: ['konsole', '--workdir', d, '-p', f'tabtitle={t}', '-e', *c],
    'xfce4-terminal': lambda d, t, c: ['xfce4-terminal', f'--working-directory={d}', f'--title={t}', '-x', *c],
    'mate-terminal': lambda d, t, c: ['mate-terminal', f'--working-directory={d}', f'--title={t}', '-x', *c],
    'tilix': lambda d, t, c: ['tilix', '-w', d, '-t', t, '-e', shlex.join(c)],
    'terminator': lambda d, t, c: ['terminator', f'--working-directory={d}', '-T', t, '-x', *c],
    'lxterminal': lambda d, t, c: ['lxterminal', f'--working-directory={d}', f'--title={t}', '-e', shlex.join(c)],
    'kitty': lambda d, t, c: ['kitty', '--directory', d, '--title', t, *c],
    'alacritty': lambda d, t, c: ['alacritty', '--working-directory', d, '--title', t, '-e', *c],
    'wezterm': lambda d, t, c: ['wezterm', 'start', '--cwd', d, '--', *c],
    'foot': lambda d, t, c: ['foot', '-D', d, '-T', t, *c],
    'ghostty': lambda d, t, c: ['ghostty', f'--working-directory={d}', f'--title={t}', '-e', *c],
    'kgx': lambda d, t, c: ['kgx', f'--working-directory={d}', '--', *c],
    'x-terminal-emulator': lambda d, t, c: ['x-terminal-emulator', '-e', *c],
    'xterm': lambda d, t, c: ['xterm', '-T', t, '-e', *c],
}
DESKTOP_FIRST = {'gnome': ['gnome-terminal', 'ptyxis', 'kgx'], 'unity': ['gnome-terminal'],
                 'kde': ['konsole'], 'xfce': ['xfce4-terminal'], 'mate': ['mate-terminal'],
                 'lxqt': ['qterminal'], 'lxde': ['lxterminal'], 'x-cinnamon': ['gnome-terminal'],
                 'cinnamon': ['gnome-terminal'], 'budgie': ['tilix', 'gnome-terminal']}
TERMINALS['qterminal'] = lambda d, t, c: ['qterminal', '-w', d, '-e', shlex.join(c)]


def linux_terminal():
    wanted = os.environ.get('CONVODECK_TERMINAL') or settings().get('terminal')
    if wanted:
        return wanted if wanted in TERMINALS and shutil.which(wanted) else None
    desktop = (os.environ.get('XDG_CURRENT_DESKTOP') or '').lower().split(':')
    order = [n for d in desktop for n in DESKTOP_FIRST.get(d, [])] + list(TERMINALS)
    return next((n for n in order if shutil.which(n)), None)


def terminal_command(cwd, title, args):
    """Build (argv, popen_kwargs) for a new terminal window in cwd running `claude <args>`,
    which stays open after claude exits."""
    exe = claude_exe() or 'claude'
    if WINDOWS:
        # wt treats ';' as a command separator, so keep it out of the title
        title = title.replace(';', ',')
        wt = shutil.which('wt')
        if wt:
            return [wt, '-w', 'new', '-d', cwd, '--title', title, 'cmd', '/k', exe, *args], {}
        return ['cmd', '/k', exe, *args], {'cwd': cwd, 'creationflags': getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)}
    if MAC:
        script = f'cd {shlex.quote(cwd)} && {shlex.join([exe, *args])}'
        osa = 'tell application "Terminal"\nactivate\ndo script "{}"\nend tell'.format(
            script.replace('\\', '\\\\').replace('"', '\\"'))
        return ['osascript', '-e', osa], {}
    shell = os.environ.get('SHELL') or shutil.which('bash') or '/bin/sh'
    # interactive shell: loads nvm/asdf/PATH tweaks from the rc file, and stays open after claude exits
    inner = f'{shlex.join([exe, *args])}; exec {shlex.quote(shell)}'
    term = linux_terminal()
    if not term:
        sys.exit('No terminal emulator found. Set CONVODECK_TERMINAL (e.g. kitty).')
    return TERMINALS[term](cwd, title, [shell, '-ic', inner]), {
        'cwd': cwd, 'start_new_session': True, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}


def launch_in_terminal(cwd, title, args):
    argv, kwargs = terminal_command(cwd, title, args)
    if os.environ.get('CONVODECK_DRY_RUN'):   # tests: show what would run
        print(json.dumps(argv, ensure_ascii=False), flush=True)
        return
    subprocess.Popen(argv, **kwargs)


# ---------------------------------------------------------------- actions

def act_open(ref):
    c = resolve(ref)
    if not c['exists']:
        sys.exit(f"{t('no_folder')}: {c['cwd']}")
    launch_in_terminal(c['cwd'], f"Claude · {c['title']}", ['--resume', c['id']])


def act_new(folder=None):
    folder = os.path.abspath(os.path.expanduser(folder)) if folder else str(HOME)
    launch_in_terminal(folder, 'Claude', [])


def act_rename(ref, title):
    c = resolve(ref)
    title = re.sub(r'\s+', ' ', title or '').strip()[:80]
    update(c['id'], title=title or None)
    return title


def act_summary(ref, text):
    c = resolve(ref)
    text = re.sub(r'\s+', ' ', text).strip().rstrip('.')
    words = text.split()
    if len(words) > 20:
        text = ' '.join(words[:20]) + '…'
    update(c['id'], summary=text or None, n_summary=c['messages'] if text else None, attempt=None)


def trash(paths):
    paths = [str(p) for p in paths]
    if WINDOWS:
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              "foreach ($p in $env:CONVODECK_TRASH -split [char]10) { "
              "if (Test-Path -LiteralPath $p -PathType Container) { "
              "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($p,'OnlyErrorDialogs','SendToRecycleBin') } "
              "elseif (Test-Path -LiteralPath $p) { "
              "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p,'OnlyErrorDialogs','SendToRecycleBin') } }")
        env = dict(os.environ, CONVODECK_TRASH='\n'.join(paths))
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], check=True, env=env,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return
    if MAC:
        for p in paths:
            subprocess.run(['osascript', '-e', f'tell application "Finder" to delete POSIX file "{p}"'],
                           check=True, stdout=subprocess.DEVNULL)
        return
    for tool in (['gio', 'trash'], ['trash-put'], ['kioclient', 'move']):
        if shutil.which(tool[0]):
            argv = tool + paths + (['trash:/'] if tool[0] == 'kioclient' else [])
            if subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return
    # freedesktop.org trash spec, by hand (home trash)
    base = Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local' / 'share') / 'Trash'
    (base / 'files').mkdir(parents=True, exist_ok=True)
    (base / 'info').mkdir(parents=True, exist_ok=True)
    for p in paths:
        name, n = Path(p).name, 1
        while (base / 'files' / name).exists() or (base / 'info' / f'{name}.trashinfo').exists():
            n += 1
            name = f'{Path(p).stem}.{n}{Path(p).suffix}'
        from urllib.parse import quote
        (base / 'info' / f'{name}.trashinfo').write_text(
            f'[Trash Info]\nPath={quote(p)}\nDeletionDate={datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}\n')
        shutil.move(p, base / 'files' / name)


def act_delete(ref):
    c = resolve(ref)
    path = session_files()[c['id']]
    targets = [path]
    sidecar = path.with_suffix('')     # tool results stored next to the .jsonl
    if sidecar.is_dir():
        targets.append(sidecar)
    trash(targets)
    update(c['id'], title=None, summary=None, n_summary=None, attempt=None, hidden=None)
    return c


def needs_summary(c, d, now, force):
    if force:
        return True
    if now - c['last'] < IDLE_BEFORE_SUMMARY:
        return False
    if d.get('attempt') and now - d['attempt'] < RETRY_AFTER:
        return False
    return not d.get('summary') or c['messages'] >= d.get('n_summary', 0) + REGROW


def condensed(path, user_limit=25):
    _, _, msgs, _ = read_session(path)
    user = [clean(s, 500) for k, s in msgs if k == 'user']
    answers = [clean(s, 900) for k, s in msgs if k == 'assistant']
    return ('USER MESSAGES:\n- ' + '\n- '.join(user[:user_limit])[:6000] +
            '\n\nLAST ASSISTANT REPLY:\n' + (answers[-1] if answers else ''))


def act_summarize(all_=False, only=None):
    exe = claude_exe()
    if not exe:
        sys.exit('claude not found in PATH')
    try:
        lock = FileLock(cache_dir() / 'summarize.lock', blocking=False).__enter__()
    except OSError:
        return 0  # another summarize is already running
    done = 0
    try:
        files, now, data = session_files(), time.time(), load_data()
        for c in conversations():
            if only and c['id'] != only:
                continue
            if not needs_summary(c, data.get(c['id'], {}), now, all_ or bool(only)):
                continue
            prompt = (f"Summarize what this conversation between a user and a coding assistant was about. "
                      f"One sentence in {t('summary_lang')}, at most 20 words, no final period, no quotes, "
                      f"do not start with 'The user' or 'Conversation'. Say what was done and where it ended. "
                      f"Reply with the sentence only.\n\n{condensed(files[c['id']])}")
            env = dict(os.environ)
            env['PATH'] = os.path.dirname(exe) + os.pathsep + env.get('PATH', '')
            try:
                r = subprocess.run([exe, '-p', '--model', 'haiku', '--no-session-persistence'],
                                   input=prompt, capture_output=True, text=True, encoding='utf-8',
                                   timeout=120, cwd=str(HOME), env=env,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                sentence = ' '.join(r.stdout.split()).strip('"«» ').rstrip('.') if r.returncode == 0 else ''
            except Exception:
                sentence = ''
            if sentence and not sentence.lower().startswith(('api error', 'error', "you've hit")):
                words = sentence.split()
                if len(words) > 20:
                    sentence = ' '.join(words[:20]) + '…'
                update(c['id'], summary=sentence, n_summary=c['messages'], attempt=None)
                done += 1
            else:
                update(c['id'], attempt=int(now))
    finally:
        lock.__exit__()
    return done


# ---------------------------------------------------------------- the window (local web UI)

UI_STATE = cache_dir() / 'ui.json'


class UIServer:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.last_ping = time.time()
        self.last_summarize = 0
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}/?t={self.token}'

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _ok_origin(self):
                # Only this page may call the API: exact Host (blocks DNS rebinding) + secret token.
                host_ok = self.headers.get('Host') == f'127.0.0.1:{server.port}'
                origin = self.headers.get('Origin')
                origin_ok = origin in (None, f'http://127.0.0.1:{server.port}')
                return host_ok and origin_ok

            def _send(self, code, body, ctype='application/json'):
                raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header('Content-Type', f'{ctype}; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if not self._ok_origin():
                    return self._send(403, {'error': 'forbidden'})
                if self.path.split('?')[0] == '/' and f't={server.token}' in self.path:
                    html = (ASSETS / 'ui.html').read_bytes()
                    return self._send(200, html, 'text/html')
                if self.path == '/icon.svg':
                    return self._send(200, (ASSETS / 'icon.svg').read_bytes(), 'image/svg+xml')
                return self._send(404, {'error': 'not found'})

            def do_POST(self):
                if not self._ok_origin() or self.headers.get('X-Token') != server.token:
                    return self._send(403, {'error': 'forbidden'})
                try:
                    n = int(self.headers.get('Content-Length') or 0)
                    body = json.loads(self.rfile.read(n) or b'{}')
                except Exception:
                    return self._send(400, {'error': 'bad request'})
                server.last_ping = time.time()
                try:
                    return self._send(200, server.api(self.path, body))
                except SystemExit as e:
                    return self._send(400, {'error': str(e)})
                except Exception as e:
                    return self._send(500, {'error': str(e)})

        return Handler

    def api(self, path, body):
        sid = body.get('id', '')
        if sid and not ID_RE.fullmatch(sid):
            raise SystemExit('bad id')
        if path == '/api/list':
            self.maybe_summarize()
            return {'lang': lang(), 'home': str(HOME), 'conversations': conversations()}
        if path == '/api/ping':
            return {'ok': True}
        if path == '/api/open':
            act_open(sid)
            return {'ok': True}
        if path == '/api/new':
            act_new(body.get('dir') or None)
            return {'ok': True}
        if path == '/api/rename':
            return {'ok': True, 'title': act_rename(sid, str(body.get('title', '')))}
        if path == '/api/delete':
            act_delete(sid)
            return {'ok': True}
        raise SystemExit('unknown action')

    def maybe_summarize(self):
        if time.time() - self.last_summarize < 300 or settings().get('auto_summaries') is False:
            return
        self.last_summarize = time.time()
        threading.Thread(target=lambda: act_summarize(), daemon=True).start()

    def watchdog(self):
        # The page pings while it is open; browsers throttle hidden pages to ~1 timer/minute,
        # so wait well beyond that before deciding the window was closed.
        while True:
            time.sleep(10)
            if time.time() - self.last_ping > 180:
                self.httpd.shutdown()
                return

    def serve(self):
        UI_STATE.parent.mkdir(parents=True, exist_ok=True)
        UI_STATE.write_text(json.dumps({'url': self.url, 'pid': os.getpid()}), encoding='utf-8')
        threading.Thread(target=self.watchdog, daemon=True).start()
        try:
            self.httpd.serve_forever()
        finally:
            try:
                UI_STATE.unlink()
            except OSError:
                pass


def app_browser():
    """A Chromium-based browser can open the page as a standalone app window (--app)."""
    if WINDOWS:
        roots = [os.environ.get(v) for v in ('PROGRAMFILES(X86)', 'PROGRAMFILES', 'LOCALAPPDATA')]
        rel = [r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe',
               r'BraveSoftware\Brave-Browser\Application\brave.exe']
        for root in filter(None, roots):
            for r in rel:
                p = Path(root) / r
                if p.exists():
                    return str(p)
        return None
    if MAC:
        for app in ('Google Chrome', 'Microsoft Edge', 'Brave Browser', 'Chromium'):
            p = Path('/Applications') / f'{app}.app' / 'Contents' / 'MacOS' / app
            if p.exists():
                return str(p)
        return None
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'brave-browser',
                 'brave', 'microsoft-edge', 'microsoft-edge-stable', 'vivaldi', 'vivaldi-stable'):
        p = shutil.which(name)
        if p:
            return p
    return None


def open_window(url):
    if os.environ.get('CONVODECK_NO_BROWSER'):   # tests / headless: just print the address
        print(url, flush=True)
        return
    browser = settings().get('browser') or app_browser()
    if browser:
        try:
            subprocess.Popen([browser, f'--app={url}', '--window-size=520,820'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=not WINDOWS)
            return
        except OSError:
            pass
    webbrowser.open(url)


def act_ui():
    # Reuse a running instance instead of starting a second server.
    try:
        state = json.loads(UI_STATE.read_text(encoding='utf-8'))
        import urllib.request
        token = state['url'].split('t=')[1]
        req = urllib.request.Request(state['url'].split('?')[0] + 'api/ping', data=b'{}', method='POST',
                                     headers={'X-Token': token, 'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=2)
        open_window(state['url'])
        return
    except Exception:
        pass
    server = UIServer()
    open_window(server.url)
    server.serve()


# ---------------------------------------------------------------- install / uninstall

def install_root():
    if WINDOWS:
        return Path(os.environ.get('LOCALAPPDATA') or HOME / 'AppData' / 'Local') / 'convodeck'
    if MAC:
        return HOME / 'Library' / 'Application Support' / 'convodeck' / 'app'
    return Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local' / 'share') / 'convodeck'


GNOME_UUID = 'convodeck@jotnardev'


def gnome_shell_version():
    if not shutil.which('gnome-shell'):
        return None
    try:
        out = subprocess.run(['gnome-shell', '--version'], capture_output=True, text=True, timeout=10).stdout
        return int(re.search(r'(\d+)', out).group(1))
    except Exception:
        return None


def act_install(gnome=True):
    root = install_root()
    if root.exists():
        shutil.rmtree(root)
    (root / 'assets').mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve(), root / 'convodeck.py')
    for f in ('ui.html', 'icon.svg', 'icon.ico'):
        if (ASSETS / f).exists():
            shutil.copy2(ASSETS / f, root / 'assets' / f)
    report = [f'App files: {root}']

    if WINDOWS:
        py = Path(sys.executable)
        pyw = py.with_name('pythonw.exe') if py.with_name('pythonw.exe').exists() else py
        (root / 'convodeck.cmd').write_text(f'@"{py}" "{root / "convodeck.py"}" %*\r\n', encoding='utf-8')
        menu = Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'ConvoDeck.lnk'
        icon = root / 'assets' / 'icon.ico'
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CD_LNK);"
              "$s.TargetPath=$env:CD_PY;$s.Arguments='\"'+$env:CD_SCRIPT+'\" ui';"
              "$s.WorkingDirectory=$env:USERPROFILE;$s.Description='Claude Code conversations';"
              "if (Test-Path $env:CD_ICON) { $s.IconLocation=$env:CD_ICON };$s.Save()")
        env = dict(os.environ, CD_LNK=str(menu), CD_PY=str(pyw), CD_SCRIPT=str(root / 'convodeck.py'),
                   CD_ICON=str(icon))
        subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], check=True, env=env)
        report += [f'Start Menu shortcut: {menu}', f'Command line: {root / "convodeck.cmd"}']
        return report

    bindir = HOME / '.local' / 'bin'
    bindir.mkdir(parents=True, exist_ok=True)
    launcher = bindir / 'convodeck'
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{root / "convodeck.py"}" "$@"\n')
    launcher.chmod(0o755)
    report.append(f'Command: {launcher}')
    if MAC:
        return report

    apps = Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local' / 'share') / 'applications'
    apps.mkdir(parents=True, exist_ok=True)
    (apps / 'convodeck.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=ConvoDeck\n'
        'Comment=Resume, rename and tidy your Claude Code conversations\n'
        'Comment[es]=Retoma, renombra y ordena tus conversaciones de Claude Code\n'
        f'Exec="{launcher}" ui\nIcon={root / "assets" / "icon.svg"}\nTerminal=false\n'
        'Categories=Development;\nKeywords=claude;ai;chat;sessions;\nStartupNotify=false\n')
    report.append(f'App menu entry: {apps / "convodeck.desktop"}')

    version = gnome_shell_version()
    if gnome and version and version >= 45:
        ext_src = ASSETS / 'gnome' / GNOME_UUID
        ext_dst = HOME / '.local' / 'share' / 'gnome-shell' / 'extensions' / GNOME_UUID
        if ext_dst.exists():
            shutil.rmtree(ext_dst)
        shutil.copytree(ext_src, ext_dst)
        try:
            cur = subprocess.run(['gsettings', 'get', 'org.gnome.shell', 'enabled-extensions'],
                                 capture_output=True, text=True).stdout.strip()
            items = re.findall(r"'([^']+)'", cur)
            if GNOME_UUID not in items:
                items.append(GNOME_UUID)
                subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'enabled-extensions',
                                '[' + ', '.join(f"'{i}'" for i in items) + ']'], check=True)
            report.append(f'GNOME {version} top-bar menu installed and enabled '
                          '(log out and back in to see it on Wayland).')
        except Exception as e:
            report.append(f'GNOME extension copied but not enabled: {e}')
    elif gnome and version:
        report.append(f'GNOME {version} is older than 45; top-bar menu skipped (window still works).')
    return report


def act_uninstall():
    report = []
    root = install_root()
    if root.exists():
        shutil.rmtree(root)
        report.append(f'Removed {root}')
    if WINDOWS:
        lnk = Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'ConvoDeck.lnk'
        if lnk.exists():
            lnk.unlink()
            report.append(f'Removed {lnk}')
    else:
        for p in (HOME / '.local' / 'bin' / 'convodeck',
                  Path(os.environ.get('XDG_DATA_HOME') or HOME / '.local' / 'share') / 'applications' / 'convodeck.desktop'):
            if p.exists():
                p.unlink()
                report.append(f'Removed {p}')
        ext = HOME / '.local' / 'share' / 'gnome-shell' / 'extensions' / GNOME_UUID
        if ext.exists():
            subprocess.run(['gnome-extensions', 'disable', GNOME_UUID], capture_output=True)
            try:
                cur = subprocess.run(['gsettings', 'get', 'org.gnome.shell', 'enabled-extensions'],
                                     capture_output=True, text=True).stdout
                items = [i for i in re.findall(r"'([^']+)'", cur) if i != GNOME_UUID]
                subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'enabled-extensions',
                                '[' + ', '.join(f"'{i}'" for i in items) + ']'])
            except Exception:
                pass
            shutil.rmtree(ext)
            report.append(f'Removed GNOME extension {GNOME_UUID}')
    report.append(f'Kept your titles and summaries in {DATA} (delete it by hand if you want).')
    return report


def act_doctor():
    term = 'Windows Terminal' if WINDOWS and shutil.which('wt') else ('cmd' if WINDOWS else (
        'Terminal.app' if MAC else linux_terminal()))
    convs = conversations()
    return {
        'convodeck': VERSION, 'os': f'{SYSTEM} {platform.release()}', 'python': sys.version.split()[0],
        'language': lang(), 'claude': claude_exe(), 'claude_projects': str(projects_dir()),
        'conversations': len(convs), 'with_summary': sum(1 for c in convs if c['summary']),
        'terminal': term, 'app_browser': app_browser(), 'gnome_shell': gnome_shell_version(),
        'data_file': str(DATA), 'installed_at': str(install_root()) if install_root().exists() else None,
    }


# ---------------------------------------------------------------- CLI

def when(ts):
    d, now = datetime.fromtimestamp(ts), datetime.now()
    if d.date() == now.date():
        return d.strftime('%H:%M')
    return d.strftime('%d %b %H:%M') if d.year == now.year else d.strftime('%d %b %Y')


def main(argv):
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__.strip())
        return
    cmd, rest = argv[0], argv[1:]

    def flag(name):
        if name in rest:
            rest.remove(name)
            return True
        return False

    def option(name):
        if name in rest:
            i = rest.index(name)
            val = rest[i + 1] if i + 1 < len(rest) else ''
            del rest[i:i + 2]
            return val
        return None

    if cmd == 'list':
        as_json, limit, search = flag('--json'), option('--limit'), option('--search')
        convs = conversations(include_hidden=flag('--hidden'))
        if search:
            s = search.lower()
            convs = [c for c in convs if s in f"{c['title']} {c['summary']} {c['project']}".lower()]
        if limit:
            convs = convs[:int(limit)]
        if as_json:
            print(json.dumps(convs, ensure_ascii=False, indent=1))
        elif not convs:
            print(t('none'))
        else:
            for c in convs:
                folder = t('home') if c['project'] == '~' else c['project']
                print(f"{c['id'][:8]}  {when(c['last']):>12}  {folder[:18]:<18}  {c['title']}")
                if c['summary']:
                    print(f"{'':42}{c['summary']}")
    elif cmd == 'show':
        c = resolve(rest[0])
        print(json.dumps(c, ensure_ascii=False, indent=1))
        print()
        print(condensed(session_files()[c['id']], user_limit=60))
    elif cmd == 'open':
        act_open(rest[0])
    elif cmd == 'new':
        act_new(rest[0] if rest else None)
    elif cmd == 'rename':
        title = act_rename(rest[0], ' '.join(rest[1:]))
        print(t('renamed', title or '(auto)'))
    elif cmd == 'summary':
        act_summary(rest[0], ' '.join(rest[1:]))
    elif cmd == 'summarize':
        only = option('--id')
        print(act_summarize(all_=flag('--all'), only=resolve(only)['id'] if only else None))
    elif cmd == 'delete':
        if not flag('--yes'):
            sys.exit(t('confirm'))
        print(t('deleted', act_delete(rest[0])['title']))
    elif cmd in ('hide', 'unhide'):
        update(resolve(rest[0])['id'], hidden=True if cmd == 'hide' else None)
    elif cmd == 'ui':
        act_ui()
    elif cmd == 'install':
        print('\n'.join(act_install(gnome=not flag('--no-gnome'))))
    elif cmd == 'uninstall':
        print('\n'.join(act_uninstall()))
    elif cmd == 'doctor':
        print(json.dumps(act_doctor(), ensure_ascii=False, indent=1))
    elif cmd == 'json':   # used by the GNOME extension
        print(json.dumps(conversations(), ensure_ascii=False))
    elif cmd == '--version':
        print(VERSION)
    else:
        sys.exit(f'Unknown command: {cmd}\n\n{__doc__.strip()}')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    main(sys.argv[1:])
