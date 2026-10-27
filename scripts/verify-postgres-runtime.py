#!/usr/bin/env python3
"""Test the verified PostgreSQL image with disposable data and localhost clients."""
import importlib.util
import json
from pathlib import Path
import secrets
# Fixed Docker commands; generated passwords are files, never command arguments.
import subprocess  # nosec B404
import tempfile
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[1]


def run(argv, timeout=60):
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)  # nosec B603


def main():
    spec = importlib.util.spec_from_file_location('pgimage', ROOT / 'scripts/postgres-image.py')
    image = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(image)
    output, record = image.verified_record()
    tag = record['repository'] + ':' + record['immutable_tag']
    identity = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'])
    if identity.returncode or identity.stdout.strip() not in image.IMPORT.image_ids(output, record):
        raise RuntimeError('Docker image does not match verified OCI bytes')
    rendered = run([str(ROOT / '.dev-tools/bin/helm'), 'template', 'postgres-dev',
                    str(ROOT / 'charts/postgres-dev'), '-n', 'postgres-dev',
                    '-f', str(ROOT / 'apps/postgres-dev/values.yaml'), '-f', str(output / 'values.yaml')])
    if rendered.returncode:
        raise RuntimeError('Chart rendering failed')
    resources = list(yaml.safe_load_all(rendered.stdout))
    config = next(item for item in resources if item and item['kind'] == 'ConfigMap')
    deployment = next(item for item in resources if item and item['kind'] == 'Deployment')
    container = deployment['spec']['template']['spec']['containers'][0]
    name = 'postgres-test-' + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix='postgres-runtime-', dir=ROOT / '.dev-tools') as directory:
        root = Path(directory)
        data, credentials, init = [root / item for item in ['data', 'credentials', 'init']]
        for path in [data, credentials, init]:
            path.mkdir(mode=0o700)
        passwords = {key: secrets.token_urlsafe(32) for key in ['postgres-password', 'dev-password']}
        for key, value in passwords.items():
            (credentials / key).write_text(value)
            (credentials / key).chmod(0o400)
        for filename, value in config['data'].items():
            (init / filename).write_text(value)
            (init / filename).chmod(0o400)
        args = ['docker', 'run', '-d', '--name', name, '--user=1000:1000', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=1g', '--cpus=2',
                # This is a container-private tmpfs, not a shared host temporary path.
                '--shm-size=128m', '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,uid=1000,gid=1000',  # nosec B108
                '--tmpfs', '/var/run/postgresql:rw,nosuid,nodev,size=32m,uid=1000,gid=1000',
                '-p', '127.0.0.1::5432']
        for source, destination in [(data, '/var/lib/postgresql'), (credentials, '/run/postgres-secrets'), (init, '/docker-entrypoint-initdb.d')]:
            args += ['--mount', 'type=bind,src=' + str(source) + ',dst=' + destination + ('' if source == data else ',readonly')]
        for item in container['env']:
            args += ['-e', item['name'] + '=' + item['value']]
        args += [tag, *container['args']]
        try:
            if run(args).returncode:
                raise RuntimeError('Disposable PostgreSQL container failed to start')
            def ready():
                for _ in range(60):
                    if run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'postgres']).returncode == 0:
                        return
                    time.sleep(1)
                raise RuntimeError('PostgreSQL did not initialize; inspect the private runtime log')
            ready()
            state = run(['docker', 'inspect', name])
            port = json.loads(state.stdout)[0]['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
            def query(sql, wrong=False):
                script = ('export PGPASSWORD=deliberately-wrong; ' if wrong else
                          'export PGPASSWORD; PGPASSWORD=$(cat /run/postgres-secrets/dev-password); ')
                script += 'exec psql -X -A -t -h 127.0.0.1 -p "$1" -U dev -d dev -v ON_ERROR_STOP=1 -c "$2"'
                result = run(['docker', 'run', '--rm', '--network=host', '--user=1000:1000', '--read-only',
                              '--cap-drop=ALL', '--security-opt=no-new-privileges', '--mount',
                              'type=bind,src=' + str(credentials) + ',dst=/run/postgres-secrets,readonly',
                              '--entrypoint', 'sh', tag, '-c', script, 'sh', port, sql])
                if wrong:
                    if not result.returncode:
                        raise RuntimeError('Incorrect password was accepted')
                    return ''
                if result.returncode:
                    raise RuntimeError('Localhost SQL verification failed; original data was not used')
                return result.stdout.strip()
            if query('SELECT current_user,current_database();') != 'dev|dev':
                raise RuntimeError('Unexpected development user or database')
            if query('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user;') != 'f|t|f':
                raise RuntimeError('Development-role privileges do not match the intended policy')
            query("CREATE TABLE persistence_fixture(value text); INSERT INTO persistence_fixture VALUES ('retained');")
            query('SELECT 1;', wrong=True)
            if run(['docker', 'restart', name]).returncode:
                raise RuntimeError('Disposable database restart failed')
            ready()
            # Docker may choose a new ephemeral published port after restart.
            state = run(['docker', 'inspect', name])
            port = json.loads(state.stdout)[0]['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']
            if query('SELECT value FROM persistence_fixture;') != 'retained':
                raise RuntimeError('Restart persistence check failed')
            logs = run(['docker', 'logs', name])
            if any(password in logs.stdout + logs.stderr for password in passwords.values()):
                raise RuntimeError('A credential appeared in startup logs')
            print('PASS: non-root/read-only initialization; SCRAM localhost login; restricted dev role; wrong-password rejection; restart persistence; no credentials in startup logs.')
        finally:
            logs = run(['docker', 'logs', name])
            report = ROOT / '.dev-tools/reports/postgres-runtime.log'
            report.write_text(logs.stdout + logs.stderr)
            report.chmod(0o600)
            run(['docker', 'rm', '-f', name])


if __name__ == '__main__':
    main()
