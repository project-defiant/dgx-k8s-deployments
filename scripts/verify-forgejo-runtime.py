#!/usr/bin/env python3
"""Exercise only a verified image in disposable Docker resources, never Kubernetes."""
import base64
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import re
import shutil
# Fixed tools, argv lists and captured output; credentials never enter command arguments.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
STAGE = 'artifact verification'


def run(command, **kwargs):
    # All callers build argument lists; captured diagnostics may contain fixture secrets.
    return subprocess.run(command, check=True, capture_output=True, **kwargs)  # nosec B603


class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') and attrs.get('type') not in ('submit', 'button'):
            if attrs.get('type') == 'checkbox' and 'checked' not in attrs:
                return
            self.fields[attrs['name']] = attrs.get('value', 'on' if attrs.get('type') == 'checkbox' else '')


class LocalRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(self.origin).netloc:
            raise ValueError('Fixture redirect escaped localhost endpoint')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def config_digest(archive, record):
    with tarfile.open(archive) as tar:
        member = 'blobs/sha256/' + record['manifest_digest'].split(':')[1]
        raw = tar.extractfile(member).read()
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != record['manifest_digest']:
            raise ValueError('OCI manifest integrity mismatch')
        manifest = json.loads(raw)
        digest = manifest['config']['digest']
        raw = tar.extractfile('blobs/sha256/' + digest.split(':')[1]).read()
        if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('OCI config integrity mismatch')
        config = json.loads(raw)
        image_env = dict(item.split('=', 1) for item in config.get('config', {}).get('Env', []) if '=' in item)
        if image_env.get('GITEA_APP_INI') != '/var/lib/gitea/custom/conf/app.ini':
            raise ValueError('Unexpected upstream configuration persistence path')
        return digest


def container_args(docker, name, image, volumes, envfile, port):
    return [docker, 'run', '--detach', '--name', name, '--pull=never', '--user=1000:1000',
            '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--cpus=1', '--memory=1g', '--pids-limit=256', '--publish=127.0.0.1:' + str(port) + ':3000',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=128m,mode=1777', '--env-file', str(envfile),
            '--mount', 'type=volume,src=' + volumes[0] + ',dst=/var/lib/gitea',
            image]


def main():
    global STAGE
    docker, git = shutil.which('docker'), shutil.which('git')
    if not docker or not git:
        raise ValueError('Docker and Git are required')
    spec = importlib.util.spec_from_file_location('artifact', ROOT / 'scripts/forgejo-artifact.py')
    artifact = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(artifact)
    output, record = artifact.verified_record()
    image = record['repository'] + ':' + record['immutable_tag']
    expected = config_digest(output / 'image.oci.tar', record)
    actual = run([docker, 'image', 'inspect', '--format={{.Id}}', image], text=True).stdout.strip()
    if actual != expected:
        raise ValueError('Docker image differs from verified OCI config')
    helm = ROOT / '.dev-tools/bin/helm'
    rendered = run([str(helm), 'template', 'fixture', str(ROOT / 'charts/forgejo'), '-n', 'fixture',
                    '-f', str(ROOT / 'apps/forgejo/values.yaml'), '-f', str(output / 'values.yaml')], text=True)
    deployment = next(doc for doc in yaml.safe_load_all(rendered.stdout) if doc and doc.get('kind') == 'Deployment')
    chart_env = deployment['spec']['template']['spec']['containers'][0]['env']
    if any('INSTALL_LOCK' in item['name'] for item in chart_env):
        raise ValueError('Chart must persist installer lock, not override it in environment')
    STAGE = 'disposable container startup'
    name = 'forgejo-fixture-' + secrets.token_hex(8)
    volumes = [name + '-data']
    created = []
    started = False
    with tempfile.TemporaryDirectory(prefix='forgejo-runtime-') as directory:
        temp = Path(directory)
        try:
            for volume in volumes:
                run([docker, 'volume', 'create', volume])
                created.append(volume)
            # Reserve an OS-selected local port; Docker fails closed if another process wins it.
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            origin = 'http://127.0.0.1:' + str(port)
            envfile = temp / 'container.env'
            envfile.write_text('\n'.join(item['name'] + '=' + (origin + '/' if item['name'].endswith('__ROOT_URL') else item['value']) for item in chart_env) + '\n')
            envfile.chmod(0o600)
            started = True
            run(container_args(docker, name, expected, volumes, envfile, port))
            mapping = run([docker, 'port', name, '3000/tcp'], text=True).stdout.strip()
            if not mapping.startswith('127.0.0.1:') or not mapping.split(':')[-1].isdigit():
                raise ValueError('Expected localhost-only ephemeral port')
            origin = 'http://' + mapping
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(), LocalRedirect(origin))
            password = secrets.token_urlsafe(32)
            auth = 'Basic ' + base64.b64encode(('fixtureowner:' + password).encode()).decode()

            def request(path, data=None, authenticated=False, content_type='application/json'):
                if not path.startswith('/') or path.startswith('//'):
                    raise ValueError('Only local fixture paths are allowed')
                headers = {'Content-Type': content_type}
                if authenticated:
                    headers['Authorization'] = auth
                # URL is constructed only from Docker's validated localhost mapping and local path.
                req = urllib.request.Request(origin + path, data=data, headers=headers)  # nosec B310
                with opener.open(req, timeout=10) as response:  # nosec B310
                    return response.read()

            def wait(path, authenticated=False):
                for _ in range(180):
                    try:
                        return request(path, authenticated=authenticated)
                    except (urllib.error.URLError, TimeoutError, ConnectionError):
                        time.sleep(1)
                raise ValueError('Fixture HTTP readiness timed out')

            STAGE = 'private installer readiness'
            parser = Inputs()
            parser.feed(wait('/').decode())
            if 'db_type' not in parser.fields or 'admin_name' not in parser.fields:
                raise ValueError('Expected private first-run installer')
            STAGE = 'fixture installer submission'
            fields = parser.fields
            fields.pop('enable_update_checker', None)
            fields.update(db_type='sqlite3', db_path='/var/lib/gitea/data/forgejo.db',
                          app_name='Disposable Forgejo fixture', run_user='git', domain='127.0.0.1',
                          http_port='3000', app_url=origin + '/', repo_root_path='/var/lib/gitea/git/repositories',
                          log_root_path='/var/lib/gitea/data/log', disable_registration='on',
                          require_sign_in_view='on', offline_mode='on', disable_gravatar='on',
                          admin_name='fixtureowner', admin_email='fixture@example.invalid',
                          admin_passwd=password, admin_confirm_passwd=password)
            try:
                request('/', urllib.parse.urlencode(fields).encode(), content_type='application/x-www-form-urlencoded')
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass  # The installer may restart HTTP after persisting configuration.
            STAGE = 'fixture administrator login'
            user = json.loads(wait('/api/v1/user', authenticated=True))
            if user.get('login') != 'fixtureowner':
                raise ValueError('Fixture admin login failed')
            request('/api/v1/user/repos', json.dumps({'name': 'fixture', 'private': True}).encode(), True)
            STAGE = 'private Git push and clone'
            clean_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
            env = dict(clean_env, GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                       GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='5',
                       GIT_CONFIG_KEY_0='http.extraHeader', GIT_CONFIG_VALUE_0='Authorization: ' + auth,
                       GIT_CONFIG_KEY_1='core.hooksPath', GIT_CONFIG_VALUE_1='/dev/null',
                       GIT_CONFIG_KEY_2='commit.gpgsign', GIT_CONFIG_VALUE_2='false',
                       GIT_CONFIG_KEY_3='user.name', GIT_CONFIG_VALUE_3='Runtime Fixture',
                       GIT_CONFIG_KEY_4='user.email', GIT_CONFIG_VALUE_4='fixture@example.invalid')
            repo = temp / 'repo'
            run([git, 'init', '--initial-branch=main', str(repo)], env=env)
            (repo / 'README.md').write_text('Recover private development work reliably.\n')
            run([git, '-C', str(repo), 'add', 'README.md'], env=env)
            run([git, '-C', str(repo), 'commit', '-m', 'Prove private work survives service restarts\n\nTested: Disposable HTTP push and clone'], env=env)
            remote = origin + '/fixtureowner/fixture.git'
            run([git, '-C', str(repo), 'push', remote, 'main'], env=env)
            run([git, 'clone', '--branch=main', remote, str(temp / 'clone')], env=env)
            if (temp / 'clone/README.md').read_bytes() != (repo / 'README.md').read_bytes():
                raise ValueError('Git clone contents differ')
            STAGE = 'container restart'
            run([docker, 'restart', name])
            STAGE = 'post-restart administrator login'
            wait('/api/v1/user', authenticated=True)
            STAGE = 'persisted installer lock read'
            config = run([docker, 'exec', name, 'cat', '/var/lib/gitea/custom/conf/app.ini'], text=True).stdout
            STAGE = 'persisted installer lock assertion'
            if not re.search(r'^INSTALL_LOCK\s*=\s*true\s*$', config, re.MULTILINE | re.IGNORECASE):
                raise ValueError('Installer lock did not persist')
            STAGE = 'installer closed after restart'
            if 'name="admin_name"' in wait('/').decode():
                raise ValueError('Installer reopened after restart')
            STAGE = 'post-restart Git clone'
            run([git, 'clone', '--branch=main', remote, str(temp / 'after-restart')], env=env)
            STAGE = 'post-restart Git content comparison'
            if (temp / 'after-restart/README.md').read_bytes() != (repo / 'README.md').read_bytes():
                raise ValueError('Git data did not persist')
            print('PASS: verified image; private installer; admin login; private Git push/clone; restart persistence')
        finally:
            failures = []
            if started:
                try:
                    run([docker, 'rm', '--force', name])
                except subprocess.CalledProcessError:
                    failures.append(name)
            for volume in created:
                try:
                    run([docker, 'volume', 'rm', volume])
                except subprocess.CalledProcessError:
                    failures.append(volume)
            if failures:
                raise ValueError('Disposable resource cleanup failed')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # HTTP bodies, process diagnostics and tracebacks could contain ephemeral credentials.
        print('FAIL: isolated Forgejo runtime verification at ' + STAGE + '; no credentials printed', file=sys.stderr)
        sys.exit(1)
