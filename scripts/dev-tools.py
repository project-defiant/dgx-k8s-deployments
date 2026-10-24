#!/usr/bin/env python3
"""Install reviewed tools locally; never invoke remote installation scripts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess  # nosec B404: fixed executable argv, never shell evaluation.
import tarfile
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / '.dev-tools' / 'bin'


def validate_pins(pins):
    sources = {
        'gitleaks': ('https://github.com/gitleaks/gitleaks/releases/download/v{v}/gitleaks_{v}_linux_{a}.tar.gz', {'arm64': 'arm64', 'amd64': 'x64'}, 'gitleaks'),
        'trivy': ('https://github.com/aquasecurity/trivy/releases/download/v{v}/trivy_{v}_Linux-{a}.tar.gz', {'arm64': 'ARM64', 'amd64': '64bit'}, 'trivy'),
        'shellcheck': ('https://github.com/koalaman/shellcheck/releases/download/v{v}/shellcheck-v{v}.linux.{a}.tar.xz', {'arm64': 'aarch64', 'amd64': 'x86_64'}, 'shellcheck-v{v}/shellcheck'),
        'kubeconform': ('https://github.com/yannh/kubeconform/releases/download/v{v}/kubeconform-linux-{a}.tar.gz', {'arm64': 'arm64', 'amd64': 'amd64'}, 'kubeconform'),
        'helm': ('https://get.helm.sh/helm-v{v}-linux-{a}.tar.gz', {'arm64': 'arm64', 'amd64': 'amd64'}, 'linux-{a}/helm'),
    }
    if set(pins) != set(sources):
        raise ValueError('Tool pins must contain only the reviewed tool names')
    for name, pin in pins.items():
        version = pin['version']
        if not re.fullmatch(r'\d+\.\d+\.\d+', version):
            raise ValueError('Tool versions must be explicit release versions')
        url, architectures, member = sources[name]
        for arch, asset_arch in architectures.items():
            artifact = pin[arch]
            if artifact['url'] != url.format(v=version, a=asset_arch) or artifact['member'] != member.format(v=version, a=asset_arch):
                raise ValueError('Tool source or archive member is not an approved release artifact')
            for field in ('sha256', 'binary_sha256'):
                if not re.fullmatch(r'[a-f0-9]{64}', artifact[field]):
                    raise ValueError('Tool artifacts require full SHA256 pins')


def run(argv):
    subprocess.run(argv, cwd=ROOT, check=True)  # nosec B603: explicit trusted developer commands.


def install_tools():
    architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if platform.system() != 'Linux' or not architecture:
        raise SystemExit('Development binaries support Linux ARM64 and x86_64 only')
    pins = json.loads((ROOT / 'scripts/dev-tools-lock.json').read_text())
    validate_pins(pins)
    DEST.mkdir(parents=True, exist_ok=True)
    for name, pin in pins.items():
        artifact = pin[architecture]
        target = DEST / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == artifact.get('binary_sha256'):
            print(f'{name}: verified')
            continue
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / 'archive'
            run(['curl', '--silent', '--show-error', '--fail', '--location', '--connect-timeout', '15', '--max-time', '180', '--proto', '=https', '--tlsv1.2', artifact['url'], '-o', str(archive)])
            if hashlib.sha256(archive.read_bytes()).hexdigest() != artifact['sha256']:
                raise SystemExit(f'{name}: archive checksum mismatch')
            with tarfile.open(archive) as bundle:
                member = bundle.getmember(artifact['member'])
                if not member.isfile():
                    raise SystemExit(f'{name}: expected regular executable')
                stream = bundle.extractfile(member)
                if stream is None:
                    raise SystemExit(f'{name}: missing executable')
                executable = stream.read()
            if hashlib.sha256(executable).hexdigest() != artifact['binary_sha256']:
                raise SystemExit(f'{name}: executable checksum mismatch')
            temporary_target = target.with_suffix('.new')
            temporary_target.write_bytes(executable)
            temporary_target.chmod(0o755)
            temporary_target.replace(target)
            print(f'{name}: installed verified {pin["version"]}')


def hooks():
    if os.environ.get('CI'):
        return
    run(['git', 'config', 'extensions.worktreeConfig', 'true'])
    run(['git', 'config', '--worktree', 'core.hooksPath', str(ROOT / '.dev-tools/hooks')])
    run([str(ROOT / '.venv/bin/prek'), 'install', '--hook-type', 'pre-commit'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hooks', action='store_true')
    args = parser.parse_args()
    prerequisites = ('uv', 'curl', 'git', 'bash', 'gpg', 'gpgconf', 'docker')
    missing = [name for name in prerequisites if not shutil.which(name)]
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12 or newer is required')
    if missing:
        raise SystemExit('Install OS prerequisites first: ' + ', '.join(missing) + '; no sudo/package-manager bootstrap is performed')
    install_tools()
    run(['uv', 'sync', '--locked', '--group', 'dev'])
    if args.hooks:
        hooks()


if __name__ == '__main__':
    main()
