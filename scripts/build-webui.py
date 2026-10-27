#!/usr/bin/env python3
"""Build a full-featured WebUI candidate; publish artifacts only after a strict scan."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROOT / 'containers/open-webui'
spec = importlib.util.spec_from_file_location('forgejo_oci', ROOT / 'scripts/build-forgejo.py')
OCI = importlib.util.module_from_spec(spec)
spec.loader.exec_module(OCI)


def inputs(architecture):
    pins = json.loads((CONTAINER / 'inputs.lock.json').read_text())
    if pins['repository'] != 'local.thedefiant/open-webui':
        raise ValueError('Unexpected local WebUI repository')
    for field in ['upstream_images', 'python_images']:
        value = pins[field].get(architecture, '')
        if not re.fullmatch(r'[a-z0-9./_-]+@sha256:[0-9a-f]{64}', value):
            raise ValueError('Reviewed native digest missing: ' + field)
    for name, version in pins['python_patches'].items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', version):
            raise ValueError('Expected exact Python package release pins')
    return pins


def fingerprint(pins, architecture):
    data = {'inputs': pins, 'architecture': architecture,
            'dockerfile_sha256': OCI.digest_file(CONTAINER / 'Dockerfile'),
            'builder_sha256': OCI.digest_file(Path(__file__)),
            'oci_helper_sha256': OCI.digest_file(ROOT / 'scripts/build-forgejo.py')}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def scan_gate(report):
    results = json.loads(report.read_text()).get('Results', [])
    python = {package['Name'].lower().replace('_', '-')
              for result in results if result.get('Type') == 'python-pkg'
              for package in result.get('Packages', [])}
    required = {'chromadb', 'sentence-transformers', 'transformers', 'torch', 'uvicorn', 'fastapi'}
    if not required.issubset(python):
        raise ValueError('Full local embedding/vector/Python inventory is missing')
    if not any(result.get('Class') == 'os-pkgs' and result.get('Packages') for result in results):
        raise ValueError('Operating-system package inventory is missing')
    findings = [item for result in results for item in result.get('Vulnerabilities', [])
                if item.get('Severity') in ('HIGH', 'CRITICAL')]
    if findings:
        raise ValueError(f'Full WebUI candidate blocked by {len(findings)} HIGH/CRITICAL findings; inspect private scan report')


def verify(output, pins, architecture):
    for name in ['record.json', 'image.oci.tar', 'scan.json', 'values.yaml']:
        if (output / name).is_symlink():
            raise ValueError('Build artifacts must not be symlinks')
    record = json.loads((output / 'record.json').read_text())
    if record['source_input_fingerprint'] != fingerprint(pins, architecture):
        raise ValueError('WebUI inputs changed; rebuild required')
    if record['archive_sha256'] != OCI.digest_file(output / 'image.oci.tar'):
        raise ValueError('WebUI archive checksum mismatch')
    if record['scan_sha256'] != OCI.digest_file(output / 'scan.json'):
        raise ValueError('WebUI scan report checksum mismatch')
    scan_gate(output / 'scan.json')
    manifest = OCI.oci_manifest(output / 'image.oci.tar', architecture)
    if record['manifest_digest'] != manifest or record['imageRef'] != pins['repository'] + '@' + manifest:
        raise ValueError('WebUI image reference differs from verified OCI archive')
    values = json.loads((output / 'values.yaml').read_text())
    if values != {'image': {'repository': pins['repository'], 'digest': manifest, 'pullPolicy': 'Never'}}:
        raise ValueError('WebUI generated values differ from verified image')
    print('Verified native full WebUI image: ' + record['imageRef'])
    return record


def build(output, pins, architecture):
    if os.geteuid() == 0:
        raise ValueError('Build as the ordinary Docker-enabled user, without sudo')
    if not shutil.which('docker') or not (ROOT / '.dev-tools/bin/trivy').is_file():
        raise ValueError('Docker and the pinned repository Trivy tool are required')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    log = output / 'build.log'
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')
    tag = pins['webui_version'] + '-full-patched-' + architecture + '-' + fingerprint(pins, architecture)[:12] + '-' + stamp
    reference = pins['repository'] + ':' + tag
    print('Building full WebUI candidate; progress is private at ' + str(log), flush=True)
    with tempfile.TemporaryDirectory(prefix='build-', dir=output) as directory:
        context = Path(directory)
        shutil.copyfile(CONTAINER / 'Dockerfile', context / 'Dockerfile')
        (context / 'patches.txt').write_text(''.join(name + '==' + version + '\n' for name, version in sorted(pins['python_patches'].items())))
        metadata = context / 'metadata.json'
        OCI.run(['docker', 'buildx', 'build', '--platform', 'linux/' + architecture,
                 '--provenance=false', '--sbom=false', '--no-cache-filter', 'runtime',
                 '--build-arg', 'UPSTREAM_IMAGE=' + pins['upstream_images'][architecture],
                 '--build-arg', 'PYTHON_IMAGE=' + pins['python_images'][architecture],
                 '--tag', reference, '--load', '--metadata-file', str(metadata), str(context)], log)
        report = output / 'candidate-scan.json'
        OCI.run([str(ROOT / '.dev-tools/bin/trivy'), 'image', '--quiet', '--image-src', 'docker',
                 '--cache-dir', str(ROOT / '.dev-tools/trivy-cache'), '--scanners', 'vuln',
                 '--list-all-pkgs', '--format', 'json', '--output', str(report), '--severity', 'HIGH,CRITICAL',
                 '--exit-code', '0', reference], log)
        scan_gate(report)  # No records/Helm values are published when this gate fails.
        docker_archive, archive = context / 'image.docker.tar', context / 'image.oci.tar'
        OCI.run(['docker', 'image', 'save', '--output', str(docker_archive), reference], log)
        OCI.docker_to_oci(docker_archive, archive, architecture, reference)
        manifest = OCI.oci_manifest(archive, architecture)
        record = {'source_input_fingerprint': fingerprint(pins, architecture), 'architecture': architecture,
                  'archive_sha256': OCI.digest_file(archive), 'scan_sha256': OCI.digest_file(report),
                  'manifest_digest': manifest, 'repository': pins['repository'], 'immutable_tag': tag,
                  'imageRef': pins['repository'] + '@' + manifest, 'inputs': pins,
                  'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        (context / 'record.json').write_text(json.dumps(record, indent=2) + '\n')
        (context / 'values.yaml').write_text(json.dumps({'image': {'repository': pins['repository'], 'digest': manifest, 'pullPolicy': 'Never'}}, indent=2) + '\n')
        shutil.copyfile(report, context / 'scan.json')
        for name in ['image.oci.tar', 'metadata.json', 'record.json', 'values.yaml', 'scan.json']:
            (context / name).replace(output / name)
    verify(output, pins, architecture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--build', action='store_true')
    action.add_argument('--ensure', action='store_true')
    action.add_argument('--verify', action='store_true')
    action.add_argument('--plan', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        architecture = OCI.native_architecture()
        pins = inputs(architecture)
        output = ROOT / '.dev-tools/open-webui' / architecture
        if args.verify:
            verify(output, pins, architecture)
        elif args.build or args.ensure:
            if args.ensure and (output / 'record.json').exists():
                record = json.loads((output / 'record.json').read_text())
                if record.get('source_input_fingerprint') == fingerprint(pins, architecture):
                    verify(output, pins, architecture)
                    return 0
            build(output, pins, architecture)
        else:
            print(json.dumps({'architecture': architecture, 'output': str(output), 'inputs': pins}, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError, tarfile.TarError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
