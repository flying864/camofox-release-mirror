"""Official image validation and optional publication; no source builds."""
import base64
import json
import os
import re
import subprocess
import time
import urllib.request
import uuid

UPSTREAM = 'jo-inc/camofox-browser'
TARGET = 'flying864/camofox-browser:latest'
ACCEPT = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'


def get(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=40) as r:
        return json.load(r), r.headers


def run(*args):
    return subprocess.check_output(args, text=True, timeout=600).strip()


def stable(releases):
    valid = [r for r in releases if not r['draft'] and not r['prerelease'] and re.fullmatch(r'v\d+\.\d+\.\d+', r['tag_name'])]
    return max(valid, key=lambda r: tuple(map(int, r['tag_name'][1:].split('.'))))


def manifest(host, repo, tag, token):
    return get(f'https://{host}/v2/{repo}/manifests/{tag}', {'Authorization': 'Bearer ' + token, 'Accept': ACCEPT})


def identity(m):
    return m['config']['digest'], [x['digest'] for x in m['layers']]


def hub_manifest():
    token = get('https://auth.docker.io/token?service=registry.docker.io&scope=repository:flying864/camofox-browser:pull')[0]['token']
    m, h = manifest('registry-1.docker.io', 'flying864/camofox-browser', 'latest', token)
    if 'manifests' in m:
        child = next(x for x in m['manifests'] if x.get('platform', {}).get('architecture') == 'amd64' and x['platform']['os'] == 'linux')
        m, h = manifest('registry-1.docker.io', 'flying864/camofox-browser', child['digest'], token)
    return m, h


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
    index, ih = manifest('ghcr.io', UPSTREAM, version, token)
    child = next(x for x in index['manifests'] if x.get('platform', {}).get('architecture') == 'amd64' and x['platform']['os'] == 'linux')
    source_manifest, _ = manifest('ghcr.io', UPSTREAM, child['digest'], token)
    source = f'ghcr.io/{UPSTREAM}@{child["digest"]}'
    # Capture rollback identity before any publication; fail closed if unavailable.
    previous, previous_headers = hub_manifest()
    evidence = {'release': release['tag_name'], 'upstream_index': ih.get('Docker-Content-Digest'), 'upstream_amd64': child['digest'], 'previous_downstream': previous_headers.get('Docker-Content-Digest')}
    print(json.dumps(evidence), flush=True)
    run('docker', 'pull', '--platform', 'linux/amd64', source)
    info = json.loads(run('docker', 'image', 'inspect', source))[0]
    assert info['Architecture'] == 'amd64' and info['Os'] == 'linux'
    assert info['Id'] == source_manifest['config']['digest']
    name = 'camofox-probe-' + uuid.uuid4().hex[:12]
    try:
        run('docker', 'run', '-d', '--name', name, '--platform', 'linux/amd64', '--shm-size=2g', '-p', '127.0.0.1::9377', source)
        port = run('docker', 'port', name, '9377/tcp').split(':')[-1]
        base = 'http://127.0.0.1:' + port
        deadline = time.monotonic() + 150
        while True:
            try:
                assert get(base + '/health')[0]['ok']
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError('Health check deadline exceeded')
                time.sleep(3)
        assert get(base + '/openapi.json')[0]['info']['version'] == version
        for _ in range(3):
            user = 'probe-' + uuid.uuid4().hex
            def api(path, payload=None, method=None):
                req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None, headers={'Content-Type': 'application/json'}, method=method)
                with urllib.request.urlopen(req, timeout=70) as r:
                    return json.load(r)
            try:
                tab = api('/tabs', {'userId': user, 'sessionKey': user, 'url': 'https://example.com'})['tabId']
                snapshot = api(f'/tabs/{tab}/snapshot?userId={user}')
                assert 'Example Domain' in snapshot['snapshot']
                assert snapshot['url'].startswith('https://example.com')
            finally:
                api('/sessions/' + user, method='DELETE')
                assert api('/tabs?userId=' + user)['tabs'] == []
        evidence['smoke_tests'] = '3 independent sessions: navigation, content, cleanup passed'
    finally:
        run('docker', 'rm', '-f', name)
    if os.environ.get('PUBLISH') == 'true':
        password = os.environ.get('DOCKERHUB_TOKEN')
        if not password:
            raise RuntimeError('Smoke tests passed; publication blocked: DOCKERHUB_TOKEN secret missing')
        subprocess.run(['docker', 'login', '-u', 'flying864', '--password-stdin'], input=password, text=True, check=True, timeout=60)
        run('skopeo', 'copy', '--preserve-digests', '--authfile', os.path.join(os.environ['DOCKER_CONFIG'], 'config.json'), 'docker://' + source, 'docker://' + TARGET)
        for attempt in range(5):
            current, headers = hub_manifest()
            if identity(current) == identity(source_manifest):
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
