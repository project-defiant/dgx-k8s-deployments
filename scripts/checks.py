#!/usr/bin/env python3
"""Local security gates. Detailed scanner output stays in private local reports."""
import argparse
from contextlib import ExitStack
import datetime
import hashlib
import json
import os
import platform
import re
from pathlib import Path
import shutil
# Trusted local developer tools use argument arrays without a shell.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / '.dev-tools/bin'
REPORTS = ROOT / '.dev-tools/reports'
# Owner: repository maintainer. Review by 2026-11-23. Legacy GPU runtime is
# not applied by platform targets and is manual opt-in; non-root compatibility
# needs a real inference test. The large legacy image has no automated CVE coverage.
LEGACY_EXCEPTIONS = {'KSV-0014'}
EXPIRES = datetime.date(2026, 11, 23)
# Owner: repository maintainer. Reachability assessed 2026-10-24: this exact
# nginx has no Brotli build flag/loaded library and the locked config never loads
# or invokes a decoder. Raw findings remain in reports; no generic CVE ignore.
DOCS_BROTLI_IMAGE = 'docker.io/nginxinc/nginx-unprivileged@sha256:4d831b13e3a78d153ba888f43a784a519944cb8f65a0fad07219a42e5db59456'
DOCS_BROTLI_EXPIRES = datetime.date(2026, 11, 7)
DOCS_BROTLI_CONFIG = '188f4dac16d22b4e63baee59be369f7cb60e00b7bb640b41dc50aa1465bef15d'
DOCS_PROFILE_FILES = {
    'charts/private-app/templates/deployment.yaml': '45ab6f087ee5bef1268cd1c9b4ead4a5db5cadc9bb311cd09a4312a73493075f',
    'charts/private-app/templates/configmap.yaml': 'b53c26a5557facff91e28cfc23aa0865036810509e4d0d35b3fb314300c2d8fd',
    'apps/docs/values-prod.yaml': 'c4f221cfda6c3beca91de0925a51af5248bd04a790db190406a1f3c47e7965a5',
}


def tool(name):
    path = BIN / name
    if not path.is_file():
        raise RuntimeError(f'Missing {name}; run make dev to bootstrap tools')
    return str(path)


def forgejo_output():
    architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if architecture is None:
        raise RuntimeError('Native ARM64 or AMD64 required for the local Forgejo image')
    return ROOT / '.dev-tools/forgejo' / architecture


def forgejo_record():
    run('forgejo-artifact-integrity', [sys.executable, str(ROOT / 'scripts/build-forgejo.py'), '--verify'])
    return json.loads((forgejo_output() / 'record.json').read_text())


def run(name, argv, *, output=None):
    """No scanner output is echoed: even redacted reports may contain private data."""
    REPORTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    REPORTS.chmod(0o700)
    log = REPORTS / f'{name}.log'
    with log.open('wb') as stream:
        log.chmod(0o600)
        env = dict(os.environ, PATH=str(BIN) + os.pathsep + os.environ.get('PATH', ''))
        result = subprocess.run(argv, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False, timeout=600)  # nosec B603
    if result.returncode:
        raise RuntimeError(f'{name} failed (exit {result.returncode}); inspect private {log.relative_to(ROOT)}')
    if output is not None:
        shutil.copyfile(log, output)
    print(f'{name}: completed')


def candidate_files():
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT, check=True, capture_output=True)  # nosec B603, B607
    return sorted({Path(os.fsdecode(name)) for name in result.stdout.split(b'\0') if name and (ROOT / os.fsdecode(name)).is_file()})


def snapshot(destination):
    for relative in candidate_files():
        source = ROOT / relative
        if source.is_symlink():
            raise RuntimeError(f'Candidate symlink requires review: {relative}')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def secrets(staged=False):
    if staged:
        run('secrets-staged', [tool('gitleaks'), 'git', '--pre-commit', '--staged', '--redact', '--no-banner', str(ROOT)])
        return
    run('secrets-history', [tool('gitleaks'), 'git', '--log-opts=--all --full-history', '--redact', '--no-banner', str(ROOT)])
    with tempfile.TemporaryDirectory(prefix='candidate-', dir=REPORTS) as directory:
        snapshot(Path(directory))
        run('secrets-candidate', [tool('gitleaks'), 'dir', '--redact', '--no-banner', directory])


def static():
    files = candidate_files()
    shells = [str(path) for path in files if path.suffix == '.sh']
    if shells:
        run('shellcheck', [tool('shellcheck'), '--severity=style', *shells])
    python = [str(path) for path in files if path.suffix == '.py' and (path.parts[0] == 'scripts' or path.parts[:2] == ('platform', 'systemd'))]
    if python:
        run('bandit', [str(ROOT / '.venv/bin/bandit'), '-q', *python])


def blocking_findings(report, scope):
    blocked = []
    for result in report.get('Results', []):
        for finding in result.get('Misconfigurations', []):
            if finding.get('Severity') not in ('HIGH', 'CRITICAL'):
                continue
            # Narrow temporary exception: only the named legacy Gemma writable rootfs;
            # no other manifest, rule, secrets or severity is exempted.
            if scope == 'legacy' and finding.get('Severity') == 'HIGH' and Path(result.get('Target', '')).name == 'gemma4-31b.yaml' and finding.get('ID') in LEGACY_EXCEPTIONS and finding.get('Message', '').startswith("Container 'gemma4-31b-vllm' of Deployment 'gemma4-31b-vllm' should set 'securityContext.readOnlyRootFilesystem'") and datetime.date.today() <= EXPIRES:
                continue
            blocked.append((result.get('Target'), finding.get('ID'), finding.get('Severity')))
    return blocked


def misconfig(name, directory, scope):
    report = REPORTS / f'{name}.json'
    run(name, [tool('trivy'), 'config', '--quiet', '--cache-dir', str(ROOT / '.dev-tools/trivy-cache'), '--format', 'json', '--output', str(report), str(directory)])
    report.chmod(0o600)
    data = json.loads(report.read_text())
    coverage = [result.get('MisconfSummary', {}) for result in data.get('Results', []) if result.get('Type') == 'kubernetes']
    if not any(summary.get('Successes', 0) + summary.get('Failures', 0) > 0 for summary in coverage):
        raise RuntimeError(f'{name}: no Kubernetes policy checks ran; coverage is insufficient')
    findings = [item for result in data.get('Results', []) for item in result.get('Misconfigurations', [])]
    print(f'{name}: {len(findings)} total policy findings; detailed report is private')
    blocked = blocking_findings(data, scope)
    if blocked:
        raise RuntimeError(f'{name}: {len(blocked)} unapproved HIGH/CRITICAL findings; inspect private report')


def image_references(rendered):
    refs = set()
    for line in rendered.splitlines():
        match = re.match(r'^\s+image:\s*[\"\']?([^\s\"\']+)', line)
        if match:
            reference = match.group(1)
            if not re.fullmatch(r'[^@\s]+@sha256:[0-9a-f]{64}', reference):
                raise RuntimeError('Rendered images must use a SHA256 digest')
            refs.add(reference)
    if not refs:
        raise RuntimeError('Rendered docs contain no image references')
    return refs


def docs_brotli_profile(rendered):
    """Any executable/config/mount-template change invalidates the assessment."""
    try:
        if any(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected for path, expected in DOCS_PROFILE_FILES.items()):
            return False
        values = (ROOT / 'apps/docs/values.yaml').read_text()
        config = values.split('    nginx.conf: |\n', 1)[1].split('    index.html: |\n', 1)[0]
        if hashlib.sha256(config.encode()).hexdigest() != DOCS_BROTLI_CONFIG:
            return False
        commands = re.findall(r'^\s+command:\s*(\[.*\])\s*$', rendered, re.MULTILINE)
        arguments = re.findall(r'^\s+args:\s*(\[.*\])\s*$', rendered, re.MULTILINE)
        return (len(commands) == len(arguments) == 1 and json.loads(commands[0]) == ['nginx'] and
                json.loads(arguments[0]) == ['-g', 'daemon off;', '-c', '/www/nginx.conf'])
    except (OSError, IndexError, ValueError):
        return False


def blocking_image_findings(report, reference, docs_profile=False):
    blocked = []
    for result in report.get('Results', []):
        for finding in result.get('Vulnerabilities', []):
            if finding.get('Severity') not in ('HIGH', 'CRITICAL'):
                continue
            excepted = (docs_profile and reference == DOCS_BROTLI_IMAGE and
                        datetime.date.today() <= DOCS_BROTLI_EXPIRES and finding.get('Severity') == 'HIGH' and
                        finding.get('VulnerabilityID') == 'CVE-2026-111497' and
                        finding.get('PkgName') == 'brotli-libs' and finding.get('InstalledVersion') == '1.2.0-r1')
            if not excepted:
                blocked.append((finding.get('VulnerabilityID'), finding.get('PkgName'), finding.get('Severity')))
    return blocked


def manifests():
    references = set()
    contexts = {}
    with tempfile.TemporaryDirectory(prefix='manifests-', dir=REPORTS) as temporary:
        root = Path(temporary)
        for environment in ('dev', 'prod'):
            values = ['-f', 'apps/docs/values.yaml']
            if environment == 'prod':
                values += ['-f', 'apps/docs/values-prod.yaml']
            args = [tool('helm'), 'lint', 'charts/private-app', *values, '--strict']
            run(f'helm-lint-{environment}', args)
            directory = root / environment
            directory.mkdir()
            rendered = directory / 'docs.yaml'
            run(f'helm-render-{environment}', [tool('helm'), 'template', f'docs-{environment}', 'charts/private-app', '--namespace', f'docs-{environment}', *values], output=rendered)
            text = rendered.read_text()
            selected = image_references(text)
            references.update(selected)
            for reference in selected:
                contexts.setdefault(reference, []).append(docs_brotli_profile(text))
            run(f'kubeconform-{environment}', [tool('kubeconform'), '-strict', '-summary', '-kubernetes-version', '1.35.0', str(rendered)])
            misconfig(f'trivy-docs-{environment}', directory, 'docs')
            rbac = root / f'rbac-{environment}'
            rbac.mkdir()
            rolefile = rbac / 'docs-forwarder.yaml'
            rolefile.write_text((ROOT / 'platform/rbac/docs-forwarder.yaml.in').read_text().replace('@ENV@', environment))
            run(f'kubeconform-rbac-{environment}', [tool('kubeconform'), '-strict', '-summary', '-kubernetes-version', '1.35.0', str(rolefile)])
            misconfig(f'trivy-rbac-{environment}', rbac, 'rbac')
        legacy = root / 'legacy'
        legacy.mkdir()
        shutil.copyfile(ROOT / 'gemma4-31b.yaml', legacy / 'gemma4-31b.yaml')
        misconfig('trivy-legacy', legacy, 'legacy')
        forgejo = root / 'forgejo'
        forgejo.mkdir()
        rendered = forgejo / 'forgejo.yaml'
        forgejo_record()
        overlay = str(forgejo_output() / 'values.yaml')
        run('helm-lint-forgejo', [tool('helm'), 'lint', 'charts/forgejo', '-f', 'apps/forgejo/values.yaml', '-f', overlay, '--strict'])
        run('helm-render-forgejo', [tool('helm'), 'template', 'forgejo', 'charts/forgejo', '--namespace', 'forgejo',
                                    '-f', 'apps/forgejo/values.yaml', '-f', overlay], output=rendered)
        selected = image_references(rendered.read_text())
        references.update(selected)
        for reference in selected:
            contexts.setdefault(reference, []).append(False)
        run('kubeconform-forgejo', [tool('kubeconform'), '-strict', '-summary', '-kubernetes-version', '1.35.0', str(rendered)])
        misconfig('trivy-forgejo', forgejo, 'forgejo')
    for reference in sorted(references):
        if contexts.get(reference) and all(contexts[reference]):
            image(reference, docs_profile=True)
        else:
            image(reference)


def image(reference, docs_profile=False):
    if not re.fullmatch(r'[^@\s]+@sha256:[0-9a-f]{64}', reference):
        raise RuntimeError('Image checks require a digest-pinned image reference')
    identifier = reference.split('@sha256:', 1)[1][:12]
    report = REPORTS / f'image-{identifier}.json'
    local = reference.startswith('local.thedefiant/forgejo@')
    with ExitStack() as temporary:
        if local:
            record = forgejo_record()
            if reference != record['imageRef']:
                raise RuntimeError('Forgejo reference does not match the verified native build')
            # This Trivy version accepts an OCI layout directory, not an OCI tar.
            layout = Path(temporary.enter_context(tempfile.TemporaryDirectory(prefix='oci-scan-', dir=REPORTS)))
            prepare_oci_layout(forgejo_output() / 'image.oci.tar', layout)
            source = ['--input', str(layout)]
        else:
            source = ['--image-src', 'remote', reference]
        run(f'trivy-image-{identifier}', [tool('trivy'), 'image', '--quiet', '--cache-dir', str(ROOT / '.dev-tools/trivy-cache'), '--scanners', 'vuln', '--list-all-pkgs', '--format', 'json', '--output', str(report), '--exit-code', '0', '--severity', 'HIGH,CRITICAL', *source])
    report.chmod(0o600)
    data = json.loads(report.read_text())
    results = data.get('Results', [])
    blocked = blocking_image_findings(data, reference, docs_profile)
    if blocked:
        raise RuntimeError(f'Image has {len(blocked)} unapproved HIGH/CRITICAL findings; inspect private {report.relative_to(ROOT)}')
    if any(result.get('Vulnerabilities') for result in results):
        print(f'Docs-only Brotli non-reachability exception applied; expires {DOCS_BROTLI_EXPIRES}; raw finding retained')
    if not any(result.get('Packages') for result in results):
        raise RuntimeError('Image scanner returned no package inventory; coverage is insufficient')
    if local:
        required = {'app/gitea/gitea', 'usr/local/bin/environment-to-ini'}
        covered = set()
        expected = record['inputs']['go_version']
        for result in results:
            target = result.get('Target', '').lstrip('/')
            if target in required and result.get('Type') == 'gobinary':
                versions = {item.get('Version', '').lstrip('v') for item in result.get('Packages', []) if item.get('Name') == 'stdlib'}
                if versions != {expected}:
                    raise RuntimeError('Rebuilt Go binary is missing the expected patched compiler inventory')
                covered.add(target)
        if covered != required:
            raise RuntimeError('Both rebuilt Forgejo Go binaries must be inventoried by the scanner')


def prepare_oci_layout(archive, destination):
    """Copy only canonical OCI members, never arbitrary archive paths or links."""
    with tarfile.open(archive) as bundle:
        for member in bundle:
            name = member.name.rstrip('/')
            if member.isdir() and name in ('blobs', 'blobs/sha256'):
                continue
            allowed = name in ('oci-layout', 'index.json') or re.fullmatch(r'blobs/sha256/[a-f0-9]{64}', name)
            if not allowed or not member.isfile():
                raise RuntimeError('Unexpected path or link in OCI archive')
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with bundle.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)


def dependencies():
    """Audit the exact uv-synchronized environment, including development groups."""
    report = REPORTS / 'dependencies.json'
    # Trivy currently omits uv development groups; a successful empty audit is
    # insufficient. pip-audit runs with the locked venv interpreter, not system Python.
    run('dependencies', [str(ROOT / '.venv/bin/python'), '-m', 'pip_audit', '--local', '--strict',
                         '--format', 'json', '--output', str(report), '--progress-spinner', 'off',
                         '--cache-dir', str(ROOT / '.dev-tools/pip-audit-cache')])
    report.chmod(0o600)
    results = json.loads(report.read_text()).get('dependencies', [])
    packages = {package.get('name') for package in results}
    if not {'bandit', 'prek', 'pip-audit'}.issubset(packages) or any(package.get('skip_reason') for package in results):
        raise RuntimeError('Dependency scanner did not inventory the locked dev tools; coverage is insufficient')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['all', 'static', 'staged', 'image'])
    parser.add_argument('--image')
    args = parser.parse_args()
    REPORTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if args.mode == 'image':
            if not args.image:
                parser.error('image mode requires --image')
            image(args.image)
        elif args.mode == 'staged':
            secrets(staged=True)
            static()
        else:
            static()
            if args.mode == 'all':
                secrets()
                dependencies()
                manifests()
                run('tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f'{args.mode}: all required checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
