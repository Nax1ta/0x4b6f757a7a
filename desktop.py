"""Loopback-only desktop console. No cloud email credentials required."""
import argparse
import base64
import concurrent.futures
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import apply_observation, atomic_json, now_iso, observe

ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
DATA = Path(os.getenv('LOCALAPPDATA') or Path.home() / '.local/share') / '0x4b6f757a7a'
DEFAULTS = {'repository': 'Nax1ta/0x4b6f757a7a', 'rooms': []}
bootstrap = ROOT / 'client-defaults.json'
if not bootstrap.is_file():
    bootstrap = ROOT / '.local/client-defaults.json'
if bootstrap.is_file():
    DEFAULTS.update(json.loads(bootstrap.read_text(encoding='utf-8')))
LOCK = threading.RLock()
REFRESH_LOCK = threading.Lock()
CLOUD_LOCK = threading.Lock()
CLOUD_CACHE = {}
RUN_CACHE = {}
IMAGE_CACHE = {}
TOKEN = secrets.token_urlsafe(32)


def settings():
    path = DATA / 'settings.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(DEFAULTS)


def status():
    path = DATA / 'snapshot.json'
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'rooms': []}
    allowed = {r['id']: r for r in settings()['rooms']}
    known = {r['id']: r for r in state['rooms']}
    state['rooms'] = [dict(known.get(rid, {'id': rid, 'status': 'unknown', 'title': '', 'cover': '', 'error': '尚未查询'}), name=r['name']) for rid,r in allowed.items()]
    for r in state['rooms']:
        try:
            age = time.time() - datetime.fromisoformat(r.get('checked_at', '')).timestamp()
        except ValueError:
            age = float('inf')
        r['stale'] = age > 180
        if r.get('cover', '').startswith('https://'):
            r['cover'] = '/api/image?url=' + urllib.parse.quote(r['cover'], safe='')
    state['health'] = state.get('health') or {'status': 'unknown', 'last_checked_at': state.get('checked_at')}
    return state


def refresh():
    if not REFRESH_LOCK.acquire(blocking=False):
        raise ValueError('正在查询，请稍候')
    try:
        with LOCK:
            cfg = settings()
            previous = {r['id']: r for r in status()['rooms']}
        observations = observe(cfg['rooms'])
        rooms = [apply_observation(previous.get(r['id'], {}), r) for r in observations]
        # On a first failed query preserve configured identity.
        for value, obs in zip(rooms, observations):
            value.setdefault('id', obs['id'])
            value.setdefault('name', obs['name'])
            value.setdefault('url', obs.get('url'))
        snapshot = {'checked_at': now_iso(), 'rooms': rooms,
                    'health': {'status': 'degraded' if any(r['status']=='unknown' for r in observations) else 'ok', 'last_checked_at': now_iso()}}
        with LOCK:
            atomic_json(DATA / 'snapshot.json', snapshot)
        return snapshot
    finally:
        REFRESH_LOCK.release()


def health():
    cfg = settings()
    repo = cfg['repository']
    with CLOUD_LOCK:
        cached = CLOUD_CACHE.get(repo)
        if cached and time.time() - cached[0] < 180:
            return cached[1]
    try:
        def get(url, accept='application/vnd.github+json'):
            req = urllib.request.Request(url, headers={'User-Agent': 'orbit-console', 'Accept': accept})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.load(resp)
        base = 'https://api.github.com/repos/' + repo
        cached_runs = RUN_CACHE.get(repo)
        if cached_runs and time.time() - cached_runs[0] < 600:
            runs = cached_runs[1]
        else:
            runs = get(base + '/actions/workflows/poll-notify.yml/runs?per_page=15')['workflow_runs']
            RUN_CACHE[repo] = (time.time(), runs)
        heartbeat = {}
        heartbeat_error = None
        try:
            heartbeat = get(base + '/contents/health.json?ref=orbit-state', 'application/vnd.github.raw+json')
            if 'content' in heartbeat:
                heartbeat = json.loads(base64.b64decode(heartbeat['content']))
        except Exception as exc:
            heartbeat_error = '尚未取得云端心跳：' + type(exc).__name__
        latest = runs[0] if runs else None
        at = heartbeat.get('checked_at')
        age = (time.time() - datetime.fromisoformat(at).timestamp()) if at else None
        stale = age is None or age > 900
        failures = sum(r['conclusion'] == 'failure' for r in runs)
        latest_failed = latest and latest.get('conclusion') in ('failure', 'timed_out')
        result = {'status': 'stale' if stale else 'degraded' if latest_failed else heartbeat.get('status', 'unknown'), 'stale': stale,
                  'last_checked_at': at, 'last_mail_at': heartbeat.get('last_mail_at'),
                  'error': heartbeat_error, 'pending': heartbeat.get('pending'), 'failures': failures,
                  'mail_status': heartbeat.get('mail_status', 'unknown'),
                  'latest_run': {k: latest.get(k) for k in ('status', 'conclusion', 'created_at', 'html_url')} if latest else None,
                  'repository_url': 'https://github.com/' + repo, 'checked_at': now_iso(),
                  'delivery_note': 'accepted 表示发件服务器接受，不代表已进入收件箱'}
    except Exception as exc:
        result = {'status': 'unknown', 'error': '云端状态查询失败：' + type(exc).__name__, 'last_checked_at': None, 'last_mail_at': None, 'stale': True}
    with CLOUD_LOCK:
        CLOUD_CACHE[repo] = (time.time(), result)
    return result


def save_settings(data):
    repo = data.get('repository', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('仓库地址格式为 用户名/仓库名')
    entries = data.get('rooms')
    if not isinstance(entries, list) or len(entries) > 30:
        raise ValueError('最多配置 30 个房间')
    clean = []
    for entry in entries:
        rid, name = str(entry.get('id', '')), str(entry.get('name', '')).strip()
        if not re.fullmatch(r'\d{1,20}', rid) or len(name) > 100:
            raise ValueError('房间号或名称格式无效')
        if rid not in [x['id'] for x in clean]:
            clean.append({'id': rid, 'name': name or rid})
    with LOCK:
        atomic_json(DATA / 'settings.json', {'repository': repo, 'rooms': clean})
    return settings()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/image':
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = query.get('url', [''])[0]
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.hdslb.com') or parsed.username or parsed.password:
                self.send_error(403)
                return
            try:
                cached = IMAGE_CACHE.get(url)
                if cached and time.time() - cached[0] < 3600:
                    data, mime = cached[1], cached[2]
                else:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=8) as response:
                        data, mime = response.read(2_000_001), response.headers.get_content_type()
                    if len(data) > 2_000_000 or mime not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
                        raise ValueError('Invalid image')
                    if len(IMAGE_CACHE) > 64:
                        IMAGE_CACHE.clear()
                    IMAGE_CACHE[url] = (time.time(), data, mime)
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Cache-Control', 'private, max-age=300')
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_error(502)
            return
        if path.startswith('/api/'):
            try:
                fn = {'/api/status': status, '/api/settings': settings, '/api/health': health}.get(path)
                self.reply(200, fn()) if fn else self.reply(404, {'error': 'Not found'})
            except Exception as exc:
                self.reply(500, {'error': str(exc)})
            return
        relative = urllib.parse.unquote(path).lstrip('/') or 'index.html'
        target = (ROOT / 'client' / relative).resolve()
        if not target.is_relative_to((ROOT / 'client').resolve()) or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        mime = {'.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml'}.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def write_request(self):
        # Refuse cross-origin writes to the loopback server.
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            self.reply(403, {'error': 'Origin refused'})
            return
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self.reply(415, {'error': 'Expected JSON'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length > 20000:
                raise ValueError('Request too large')
            body = json.loads(self.rfile.read(length) or b'{}')
            path = urllib.parse.urlparse(self.path).path
            if self.command == 'POST' and path == '/api/refresh':
                self.reply(200, refresh())
            elif self.command == 'PUT' and path == '/api/settings':
                self.reply(200, save_settings(body))
            else:
                self.reply(404, {'error': 'Not found'})
        except ValueError as exc:
            self.reply(400, {'error': str(exc)})
        except Exception as exc:
            self.reply(500, {'error': str(exc)})

    do_POST = write_request
    do_PUT = write_request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    url = 'http://127.0.0.1:' + str(server.server_address[1])
    print(url, flush=True)
    if args.no_browser:
        server.serve_forever()
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import webview
        webview.create_window('0x4b6f757a7a', url, width=1120, height=780, min_size=(680, 480), background_color='#ededeb')
        webview.start()
    except Exception:
        webbrowser.open(url)
        thread.join()
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
