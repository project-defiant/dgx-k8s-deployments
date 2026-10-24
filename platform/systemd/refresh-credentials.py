#!/usr/bin/python3
# Managed by TheDefiant platform-service.sh
"""Root-only short-lived credential refresh; never print credential material."""
import json
import os
from pathlib import Path
# Fixed absolute executable, validated root-owned paths and list argv; no shell.
import subprocess  # nosec B404
import tempfile

BASE = Path('/etc/spark-docs-access')
STATE = Path('/var/lib/spark-docs-access')


def trusted(path):
    path = Path(path)
    for item in [path, *path.parents]:
        stat = item.lstat()
        if item.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise ValueError('root helper requires root-owned, non-writable paths')


def refresh():
    trusted(__file__)
    trusted(BASE / 'config.json')
    trusted('/usr/local/bin/k3s')
    trusted('/etc/rancher/k3s/spark-platform.yaml')
    trusted(STATE)
    config = json.loads((BASE / 'config.json').read_text())
    if config['environment'] not in ('dev', 'prod') or not isinstance(config['uid'], int) or config['uid'] <= 0:
        raise ValueError('invalid installed service configuration')
    command = ['/usr/local/bin/k3s', 'kubectl', '--kubeconfig',
               '/etc/rancher/k3s/spark-platform.yaml', '--context', 'default',
               '--server=https://127.0.0.1:6443', '--request-timeout=30s']
    # Root-owned executable and configuration were verified above.
    view = subprocess.run(command + ['config', 'view', '--raw', '--minify', '-o', 'json'],  # nosec B603
                          check=True, capture_output=True, text=True)
    cluster = json.loads(view.stdout)['clusters'][0]['cluster']
    ca = cluster['certificate-authority-data']
    if not ca or cluster.get('insecure-skip-tls-verify'):
        raise ValueError('verified cluster CA required')
    # Namespace is allowlisted; token is captured, never passed as an argument.
    response = subprocess.run(command + ['-n', 'docs-' + config['environment'], 'create', 'token',  # nosec B603
                                     'docs-forwarder', '--duration=24h'],
                           check=True, capture_output=True, text=True)
    token = response.stdout.strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('invalid credential response')
    kubeconfig = {'apiVersion': 'v1', 'kind': 'Config',
                  'clusters': [{'name': 'spark-platform', 'cluster': {
                      'server': 'https://127.0.0.1:6443', 'certificate-authority-data': ca}}],
                  'users': [{'name': 'docs-forwarder', 'user': {'token': token}}],
                  'contexts': [{'name': 'spark-platform', 'context': {
                      'cluster': 'spark-platform', 'user': 'docs-forwarder',
                      'namespace': 'docs-' + config['environment']}}],
                  'current-context': 'spark-platform'}
    fd, name = tempfile.mkstemp(dir=STATE)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(kubeconfig, output)
            output.flush()
            os.fsync(output.fileno())
            os.fchown(output.fileno(), config['uid'], -1)
        os.replace(name, STATE / 'kubeconfig')
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        refresh()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
        raise SystemExit('Credential refresh failed; inspect trusted configuration and cluster access.') from None
