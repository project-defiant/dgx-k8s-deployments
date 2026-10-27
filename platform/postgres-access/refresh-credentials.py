#!/usr/bin/python3
# Managed by TheDefiant postgres-service.sh
"""Root-only short-lived credential refresh; never print credential material."""
import json
import os
from pathlib import Path
# Fixed absolute executable, validated root-owned paths and list argv; no shell.
import subprocess  # nosec B404
import tempfile

BASE = Path('/etc/spark-postgres-access')
STATE = Path('/var/lib/spark-postgres-access')


def trusted(path, mode=None):
    path = Path(path)
    for item in [path, *path.parents]:
        stat = item.lstat()
        if item.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise ValueError('root helper requires root-owned, non-writable paths')
    if mode is not None and path.stat().st_mode & 0o777 != mode:
        raise ValueError('root helper requires private configuration permissions')


def refresh():
    trusted(__file__)
    trusted(BASE / 'config.json', 0o600)
    trusted('/usr/local/bin/k3s')
    trusted('/etc/rancher/k3s/spark-platform.yaml', 0o600)
    trusted(STATE)
    config = json.loads((BASE / 'config.json').read_text())
    if (config.get('managed') != 'TheDefiant postgres-service.sh' or config['environment'] != 'dev'
            or type(config['uid']) is not int or config['uid'] <= 0
            or type(config['gid']) is not int or config['gid'] < 0):
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
    response = subprocess.run(command + ['-n', 'postgres-dev', 'create', 'token',  # nosec B603
                                     'postgres-forwarder', '--duration=24h'],
                           check=True, capture_output=True, text=True)
    token = response.stdout.strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('invalid credential response')
    kubeconfig = {'apiVersion': 'v1', 'kind': 'Config',
                  'clusters': [{'name': 'spark-platform', 'cluster': {
                      'server': 'https://127.0.0.1:6443', 'certificate-authority-data': ca}}],
                  'users': [{'name': 'postgres-forwarder', 'user': {'token': token}}],
                  'contexts': [{'name': 'spark-platform', 'context': {
                      'cluster': 'spark-platform', 'user': 'postgres-forwarder',
                      'namespace': 'postgres-dev'}}],
                  'current-context': 'spark-platform'}
    fd, name = tempfile.mkstemp(dir=STATE)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(kubeconfig, output)
            output.flush()
            os.fchmod(output.fileno(), 0o640)
            os.fsync(output.fileno())
            os.fchown(output.fileno(), 0, config['gid'])
        os.replace(name, STATE / 'kubeconfig')
    finally:
        Path(name).unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        refresh()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
        raise SystemExit('Credential refresh failed; inspect trusted configuration and cluster access.') from None
