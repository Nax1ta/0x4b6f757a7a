"""Atomic Git data persistence on an independent branch; no plaintext watch list."""
import base64
import json
import os
import sys
import io
import zipfile
import time
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from engine import StateStore, load_env, now_iso

BRANCH = 'orbit-state'
BASE = 'https://api.github.com/repos/'


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        redirected = super().redirect_request(request, *args, **kwargs)
        if redirected and urllib.parse.urlparse(redirected.full_url).hostname != 'api.github.com':
            redirected.remove_header('Authorization')
        return redirected


def api(path, method='GET', body=None, raw=False):
    token = os.environ['GITHUB_TOKEN']
    if method == 'GET':
        path += ('&' if '?' in path else '?') + '_ts=' + str(time.time_ns())
    req = urllib.request.Request(BASE + os.environ['GITHUB_REPOSITORY'] + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'User-Agent': 'orbit', 'Cache-Control': 'no-cache'})
    with urllib.request.build_opener(SafeRedirect()).open(req, timeout=25) as response:
        return response.read() if raw else json.load(response)


def pull():
    path = Path(os.environ.get('STATE_PATH', '.local/state.enc'))
    try:
        response = api('/contents/state.enc?ref=' + BRANCH)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Confirm the entire branch is absent; a missing file on an existing
            # branch must not silently reset delivery receipts.
            try:
                api('/git/ref/heads/' + BRANCH)
            except urllib.error.HTTPError as ref_exc:
                if ref_exc.code == 404:
                    print('State: first initialization')
                    recover(path)
                    return
            raise RuntimeError('Existing state branch is missing state.enc') from exc
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(response['content']))
    recover(path)
    print('State restored')


def recover(path):
    """Compare authenticated snapshot revisions, including snapshots from the same run."""
    try:
        artifacts = api('/actions/artifacts?name=state-v2&per_page=5')['artifacts']
        current = StateStore(path, encrypted=True).read()
        seen = set(current.get('recovery_artifacts', []))
        best, best_payload = current, None
        for artifact in artifacts:
            if artifact.get('expired') or artifact['id'] in seen:
                continue
            raw = api('/actions/artifacts/' + str(artifact['id']) + '/zip?t=' + str(time.time_ns()), raw=True)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                info = archive.getinfo('state.enc')
                if info.file_size > 10_000_000:
                    raise RuntimeError('Recovery snapshot too large')
                payload = archive.read(info)
            restored = json.loads(StateStore(path, encrypted=True).cipher().decrypt(payload))
            if restored.get('version') != 2:
                raise RuntimeError('Recovery version invalid')
            if (int(restored.get('revision', 0)), restored.get('checkpoint_at', '')) > (int(best.get('revision', 0)), best.get('checkpoint_at', '')):
                best, best_payload = restored, payload
            seen.add(artifact['id'])
        if artifacts:
            best['recovery_artifacts'] = sorted(seen)[-64:]
            StateStore(path, encrypted=True).write(best)
        if best_payload:
            print('Newer encrypted recovery snapshot restored')
    except Exception as exc:
        raise RuntimeError('Recovery check unavailable') from exc


def push():
    path = Path(os.environ.get('STATE_PATH', '.local/state.enc'))
    if not path.exists():
        raise RuntimeError('State file missing; no overwrite performed')
    # Validate before publishing.
    state = StateStore(path, encrypted=True).read()
    health = {'version': 2, 'checked_at': state.get('checked_at'), 'last_mail_at': state.get('last_mail_at'),
              'status': (state.get('health') or {}).get('status', 'unknown'),
              'mail_status': (state.get('smtp') or {}).get('status', 'unknown'),
              'pending': len(state.get('outbox') or {}), 'run_url': os.environ.get('RUN_URL'),
              'saved_at': now_iso()}
    try:
        ref = api('/git/ref/heads/' + BRANCH)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        ref = None
    parent = ref['object']['sha'] if ref else None
    tree = [ {'path': 'state.enc', 'mode': '100644', 'type': 'blob',
              'sha': api('/git/blobs', 'POST', {'content': base64.b64encode(path.read_bytes()).decode(), 'encoding': 'base64'})['sha']},
             {'path': 'health.json', 'mode': '100644', 'type': 'blob', 'content': json.dumps(health)} ]
    # This branch intentionally contains only ciphertext and aggregate heartbeat.
    tree_sha = api('/git/trees', 'POST', {'tree': tree})['sha']
    commit = api('/git/commits', 'POST', {'message': 'sync', 'tree': tree_sha, 'parents': [parent] if parent else []})
    if parent:
        api('/git/refs/heads/' + BRANCH, 'PATCH', {'sha': commit['sha'], 'force': False})
    else:
        api('/git/refs', 'POST', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']})
    print('Encrypted state saved')


if __name__ == '__main__':
    load_env('.env')
    try:
        {'pull': pull, 'push': push}[sys.argv[1]]()
    except Exception as exc:
        print('Persistence failed: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
