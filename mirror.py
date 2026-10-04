"""Official image validation and optional publication; no source builds."""
import urllib.error
import json
import os
import re
import subprocess
import shutil
import time
import urllib.request
import uuid
import tempfile
import sys
from contextlib import contextmanager

UPSTREAM = 'jo-inc/camofox-browser'
TARGET = 'flying864/camofox-browser:latest'
ACCEPT = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'


def get(url, headers=None):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=40) as r:
                return json.load(r), r.headers
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            exc.close()
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError('Network retry budget exhausted')


def run(*args):
    return subprocess.check_output(args, text=True, timeout=600).strip()


def stable(releases):
    valid = [r for r in releases if not r['draft'] and not r['prerelease'] and re.fullmatch(r'v\d+\.\d+\.\d+', r['tag_name'])]
    return max(valid, key=lambda r: tuple(map(int, r['tag_name'][1:].split('.'))))


def manifest(host, repo, tag, token):
    return get(f'https://{host}/v2/{repo}/manifests/{tag}', {'Authorization': 'Bearer ' + token, 'Accept': ACCEPT})


def missing_manifest(exc):
    if exc.code != 404:
        return False
    body = exc.read(65537)
    if len(body) > 65536:
        return False
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    errors = payload.get('errors') if isinstance(payload, dict) else None
    return isinstance(errors, list) and bool(errors) and all(
        isinstance(error, dict) and error.get('code') == 'MANIFEST_UNKNOWN'
        for error in errors)


def identity(m):
    return m['config']['digest'], [x['digest'] for x in m['layers']]


def hub_manifest():
    token = get('https://auth.docker.io/token?service=registry.docker.io&scope=repository:flying864/camofox-browser:pull')[0]['token']
    m, h = manifest('registry-1.docker.io', 'flying864/camofox-browser', 'latest', token)
    if 'manifests' in m:
        child = next(x for x in m['manifests'] if x.get('platform', {}).get('architecture') == 'amd64' and x['platform']['os'] == 'linux')
        m, h = manifest('registry-1.docker.io', 'flying864/camofox-browser', child['digest'], token)
    return m, h


# Runs only inside the disposable probe container, not in the published image.
FIXTURE_SERVER = r'''
const http = require('node:http');
http.createServer((req, res) => {
  const match = /^\/probe\/([a-f0-9]{32})$/.exec(req.url);
  if (!match) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'});
  res.end('<!doctype html><html><head><title>Camofox mirror probe</title></head>' +
    '<body><h1>Camofox mirror probe</h1><p>' + match[1] + '</p>' +
    '<a href="/probe/' + match[1] + '#verified">Probe link</a></body></html>');
}).listen(9380, '127.0.0.1');
'''


def validate_snapshot(snapshot, expected_url, nonce):
    text = snapshot.get('snapshot')
    if (snapshot.get('url') != expected_url or not isinstance(text, str)
            or not all(value in text for value in ('Camofox mirror probe', nonce, 'Probe link'))):
        raise RuntimeError('Browser smoke content/URL mismatch: ' + json.dumps(snapshot)[:4000])


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


@contextmanager
def cleanup_on_exit(action, label):
    original = sys.exception()
    try:
        yield
    except BaseException as exc:
        original = exc
        raise
    finally:
        try:
            action()
        except Exception as exc:
            if original is None:
                raise
            print(f'{label} cleanup also failed: {type(exc).__name__}', file=sys.stderr)


def publish_image(source, password):
    if not shutil.which('skopeo'):
        run('sudo', 'apt-get', 'update', '-qq')
        run('sudo', 'apt-get', 'install', '-y', '-qq', '--no-install-recommends', 'skopeo')
    # Private, per-run auth; never reuse a user's Docker config. Cleanup also
    # runs if login/copy raises. The ephemeral GitHub runner is the final backstop.
    with tempfile.TemporaryDirectory(prefix='camofox-auth-') as authdir:
        env = dict(os.environ, DOCKER_CONFIG=authdir)
        subprocess.run(['docker', 'login', '-u', 'flying864', '--password-stdin'],
                       input=password, text=True, check=True, timeout=60, env=env)
        run('skopeo', 'copy', '--preserve-digests', '--authfile',
            os.path.join(authdir, 'config.json'), 'docker://' + source, 'docker://' + TARGET)


def main():
    releases = []
    for page in range(1, 21):
        batch, _ = get(f'https://api.github.com/repos/{UPSTREAM}/releases?per_page=100&page={page}')
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise RuntimeError('Release pagination limit reached')
    release = stable(releases)
    version = release['tag_name'][1:]
    token = get(f'https://ghcr.io/token?scope=repository:{UPSTREAM}:pull&service=ghcr.io')[0]['token']
    try:
        index, ih = manifest('ghcr.io', UPSTREAM, version, token)
    except urllib.error.HTTPError as exc:
        try:
            waiting = missing_manifest(exc)
        finally:
            exc.close()
        if not waiting:
            raise
        message = f'Official image for {release["tag_name"]} is not available yet; no tests or publication were performed.'
        print(message, flush=True)
        with open(os.environ.get('GITHUB_STEP_SUMMARY', '/dev/null'), 'a') as f:
            f.write(message + '\n')
        return
    child = next(x for x in index['manifests'] if x.get('platform', {}).get('architecture') == 'amd64' and x['platform']['os'] == 'linux')
    source_manifest, _ = manifest('ghcr.io', UPSTREAM, child['digest'], token)
    source = f'ghcr.io/{UPSTREAM}@{child["digest"]}'
    # Capture rollback identity before any publication; fail closed if unavailable.
    previous, previous_headers = hub_manifest()
    if (os.environ.get('PUBLISH') == 'true'
            and previous_headers.get('Docker-Content-Digest') == child['digest']
            and identity(previous) == identity(source_manifest)):
        with open(os.environ.get('GITHUB_STEP_SUMMARY', '/dev/null'), 'a') as f:
            f.write('unchanged: official amd64 manifest, config and layers match Docker Hub; no image smoke tests or push performed.\n')
        return  # Silent stdout; explicit outcome in Actions summary.
    evidence = {'release': release['tag_name'], 'upstream_index': ih.get('Docker-Content-Digest'), 'upstream_amd64': child['digest'], 'previous_downstream': previous_headers.get('Docker-Content-Digest')}
    print(json.dumps(evidence), flush=True)
    run('docker', 'pull', '--platform', 'linux/amd64', source)
    info = json.loads(run('docker', 'image', 'inspect', source))[0]
    require(info['Architecture'] == 'amd64' and info['Os'] == 'linux', 'Image platform mismatch')
    require(info['Id'] == source_manifest['config']['digest'], 'Image config digest mismatch')
    name = 'camofox-probe-' + uuid.uuid4().hex[:12]
    try:
        run('docker', 'run', '-d', '--name', name, '--platform', 'linux/amd64', '--shm-size=2g', '-p', '127.0.0.1::9377', source)
        port = run('docker', 'port', name, '9377/tcp').split(':')[-1]
        base = 'http://127.0.0.1:' + port
        deadline = time.monotonic() + 150
        while True:
            try:
                require(get(base + '/health')[0]['ok'] is True, 'Health check not OK')
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError('Health check deadline exceeded')
                time.sleep(3)
        require(get(base + '/openapi.json')[0]['info']['version'] == version, 'Runtime version mismatch')
        run('docker', 'exec', '-d', name, 'node', '-e', FIXTURE_SERVER)
        # Readiness is bounded; no fixed sleep and no dependency on public sites.
        run('docker', 'exec', name, 'node', '-e', r'''
const url = 'http://127.0.0.1:9380/probe/' + '0'.repeat(32);
(async () => {
  for (let i = 0; i < 30; i++) {
    try { if ((await fetch(url, {signal: AbortSignal.timeout(1000)})).ok) return; } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('Probe fixture readiness deadline exceeded');
})().catch(e => { console.error(e.message); process.exit(1); });
''')
        for _ in range(3):
            nonce = uuid.uuid4().hex
            user = 'probe-' + nonce
            probe_url = 'http://127.0.0.1:9380/probe/' + nonce
            def api(path, payload=None, method=None):
                req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None, headers={'Content-Type': 'application/json'}, method=method)
                with urllib.request.urlopen(req, timeout=70) as r:
                    return json.load(r)
            def cleanup_session():
                api('/sessions/' + user, method='DELETE')
                require(api('/tabs?userId=' + user)['tabs'] == [], 'Session cleanup left tabs')
            with cleanup_on_exit(cleanup_session, 'Session'):
                tab = api('/tabs', {'userId': user, 'sessionKey': user, 'url': probe_url})['tabId']
                snapshot = api(f'/tabs/{tab}/snapshot?userId={user}')
                validate_snapshot(snapshot, probe_url, nonce)
        evidence['smoke_tests'] = '3 independent sessions: controlled HTTP navigation, nonce content, link, exact URL and cleanup passed'
    except Exception:
        try:
            print('PROBE_CONTAINER_LOGS ' + run('docker', 'logs', '--tail', '80', name), flush=True)
        except Exception as diagnostic_error:
            print('Probe logs unavailable: ' + type(diagnostic_error).__name__, flush=True)
        raise
    finally:
        with cleanup_on_exit(lambda: run('docker', 'rm', '-f', name), 'Container'):
            pass
    if os.environ.get('PUBLISH') == 'true':
        password = os.environ.get('DOCKERHUB_TOKEN')
        if not password:
            raise RuntimeError('Smoke tests passed; publication blocked: DOCKERHUB_TOKEN secret missing')
        publish_image(source, password)
        for attempt in range(5):
            current, headers = hub_manifest()
            if headers.get('Docker-Content-Digest') == child['digest'] and identity(current) == identity(source_manifest):
                evidence['downstream_verified'] = headers.get('Docker-Content-Digest')
                break
            if attempt == 4:
                raise RuntimeError('Downstream identity mismatch after push')
            time.sleep(5)
    print(json.dumps(evidence, indent=2), flush=True)
    with open(os.environ.get('GITHUB_STEP_SUMMARY', '/dev/null'), 'a') as f:
        f.write('```json\n' + json.dumps(evidence, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()
