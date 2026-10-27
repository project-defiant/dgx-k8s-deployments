#!/usr/bin/env python3
"""Operate the private development PostgreSQL service without exposing credentials."""
import argparse
import base64
import importlib.util
import json
import os
from pathlib import Path
import secrets
# Fixed tools and separated arguments; credentials use stdin or private files.
import subprocess  # nosec B404
import sys

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'postgres-dev'
KUBE = []


class Refusal(ValueError):
    """A safe operational message, never raw secret-bearing tool output."""


def run(argv, *, data=None, timeout=60):
    result = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=timeout, check=False)  # nosec B603
    if result.returncode:
        raise Refusal(f'{Path(argv[0]).name} operation failed; inspect service status or private reports')
    return result.stdout


def client():
    # Reuse the already-tested dedicated Spark client guard; no GPU operation occurs.
    spec = importlib.util.spec_from_file_location('spark_client', ROOT / 'scripts/platform-gpu.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.client()


def k(*args, data=None, timeout=60):
    return run(KUBE + ['-n', NAMESPACE, *args], data=data, timeout=timeout)


def get(kind, name):
    output = k('get', kind, name, '--ignore-not-found', '-o', 'json')
    return json.loads(output) if output.strip() else {}


def metadata(name):
    return {'name': name, 'namespace': NAMESPACE,
            'labels': {'app.kubernetes.io/managed-by': 'Helm'},
            'annotations': {'meta.helm.sh/release-name': NAMESPACE,
                            'meta.helm.sh/release-namespace': NAMESPACE}}


def owned(resource):
    expected = metadata(resource.get('metadata', {}).get('name', ''))
    actual = resource.get('metadata', {})
    return (all(actual.get('labels', {}).get(key) == value for key, value in expected['labels'].items())
            and all(actual.get('annotations', {}).get(key) == value for key, value in expected['annotations'].items())
            and not actual.get('deletionTimestamp'))


def credentials():
    resource = get('secret', 'postgres-credentials')
    if not owned(resource):
        raise Refusal('Owned PostgreSQL credentials are required')
    result = {}
    for key in ['postgres-password', 'dev-password']:
        value = base64.b64decode(resource.get('data', {}).get(key, ''), validate=True).decode()
        if len(value) < 32 or '\n' in value or '\r' in value:
            raise Refusal('Invalid stored database credentials; refusing automatic reset')
        result[key] = value
    return result


def image_record():
    spec = importlib.util.spec_from_file_location('postgres_image', ROOT / 'scripts/postgres-image.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verified_record()


def deploy():
    output, record = image_record()
    run([sys.executable, str(ROOT / 'scripts/checks.py'), 'image', '--image', record['imageRef']], timeout=1800)
    helm = [str(ROOT / '.dev-tools/bin/helm')]
    values = ['-f', str(ROOT / 'apps/postgres-dev/values.yaml'), '-f', str(output / 'values.yaml')]
    rendered = run(helm + ['template', NAMESPACE, str(ROOT / 'charts/postgres-dev'), '-n', NAMESPACE, *values])
    run([str(ROOT / '.dev-tools/bin/kubeconform'), '-strict', '-summary', '-kubernetes-version', '1.35.0', '-'], data=rendered, timeout=180)
    namespace = get('namespace', NAMESPACE)
    if namespace and not owned(namespace):
        raise Refusal('Refusing changes in an unmanaged PostgreSQL namespace')
    for kind, name in [('pvc', 'postgres-data'), ('secret', 'postgres-credentials'),
                       ('deployment', 'postgres'), ('service', 'postgres'),
                       ('configmap', 'postgres-init'), ('networkpolicy', 'postgres-private'),
                       ('resourcequota', 'postgres-budget')]:
        current = get(kind, name) if namespace else {}
        if current and not owned(current):
            raise Refusal('Refusing to overwrite an unmanaged PostgreSQL resource')
    claim = get('pvc', 'postgres-data') if namespace else {}
    secret = get('secret', 'postgres-credentials') if namespace else {}
    if claim:
        spec = claim.get('spec', {})
        if (spec.get('storageClassName') != 'local-path'
                or spec.get('resources', {}).get('requests', {}).get('storage') != '20Gi'):
            raise Refusal('Existing PostgreSQL claim differs; refusing resize or replacement')
        if not secret:
            raise Refusal('Existing data claim has no credentials; restore them instead of generating replacements')
    if secret:
        credentials()  # Never rotate initialized credentials as a side effect of deploy.
    if not namespace:
        k('create', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {key: value for key, value in metadata(NAMESPACE).items() if key != 'namespace'}}))
    if not secret:
        passwords = {key: secrets.token_urlsafe(32) for key in ['postgres-password', 'dev-password']}
        k('create', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'Secret',
          'metadata': metadata('postgres-credentials'), 'type': 'Opaque', 'stringData': passwords}))
    run(helm + ['upgrade', '--install', NAMESPACE, str(ROOT / 'charts/postgres-dev'),
                '--kubeconfig', KUBE[3], '--kube-context', 'spark-platform',
                '--namespace', NAMESPACE, *values, '--wait', '--timeout', '600s'], timeout=660)
    check = k('exec', 'deployment/postgres', '--', 'sh', '-c',
              'export PGPASSWORD; PGPASSWORD=$(cat /run/postgres-secrets/dev-password); exec psql -X -A -t -h 127.0.0.1 -U dev -d dev -v ON_ERROR_STOP=1 -c "SELECT current_user,current_database();"')
    if check.strip() != 'dev|dev':
        raise Refusal('Database pod is ready but development login failed; original credentials were retained')
    print('PostgreSQL is ready in K3s. Database: dev; user: dev; credentials retained in its Secret.')


def connection(port):
    password = credentials()['dev-password']
    directory = ROOT / '.dev-tools/postgres/client'
    if directory.is_symlink():
        raise Refusal('Refusing a symlinked client directory')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / 'pgpass'
    escaped = password.replace('\\', '\\\\').replace(':', '\\:')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(f'127.0.0.1:{port}:*:dev:{escaped}\n')
    print(f'Host: 127.0.0.1\nPort: {port}\nDatabase: dev\nUser: dev')
    print('Password: make postgres-password (explicitly displays the password)')
    print('Private password file: ' + str(path))
    print(f'PGPASSFILE={path} psql -h 127.0.0.1 -p {port} -U dev -d dev')


def shell():
    flags = ['-it'] if sys.stdin.isatty() and sys.stdout.isatty() else ['-i']
    command = 'export PGPASSWORD; PGPASSWORD=$(cat /run/postgres-secrets/dev-password); exec psql -h 127.0.0.1 -U dev -d dev'
    return subprocess.run(KUBE + ['--request-timeout=0', '-n', NAMESPACE, 'exec', *flags,
                                  'deployment/postgres', '--', 'sh', '-c', command], check=False).returncode  # nosec B603


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'status', 'access', 'connection', 'password', 'shell'])
    parser.add_argument('--port', type=int, default=5432)
    args = parser.parse_args()
    try:
        if not 1024 <= args.port <= 65535:
            raise Refusal('Local port must be between 1024 and 65535')
        global KUBE
        KUBE = client()
        if args.action == 'deploy':
            deploy()
        elif args.action == 'status':
            print(k('get', 'deployments,pods,services,pvc,networkpolicy'), end='')
        elif args.action == 'access':
            return subprocess.run(KUBE + ['--request-timeout=0', '-n', NAMESPACE,
              'port-forward', '--address=127.0.0.1', 'service/postgres', f'{args.port}:5432'], check=False).returncode  # nosec B603
        elif args.action == 'connection':
            connection(args.port)
        elif args.action == 'password':
            print(credentials()['dev-password'])
        else:
            return shell()
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        print('PostgreSQL operation failed safely; check image verification, ownership, credentials and service status.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
