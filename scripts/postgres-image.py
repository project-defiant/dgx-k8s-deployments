#!/usr/bin/env python3
"""Build, verify or explicitly import the fixed patched PostgreSQL development image."""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import shutil

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROOT / 'containers/postgres'
REPOSITORY = 'local.thedefiant/postgres'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


OCI = module('postgres_oci', 'build-forgejo.py')
IMPORT = module('postgres_import', 'forgejo-artifact.py')
IMPORT.REPOSITORY = REPOSITORY  # Fixed reviewed repository, never a CLI argument.


def inputs():
    if OCI.native_architecture() != 'arm64':
        raise ValueError('Only native Linux ARM64 has reviewed PostgreSQL inputs')
    pins = json.loads((CONTAINER / 'inputs.lock.json').read_text())
    if pins['PG_MAJOR'] != '18' or pins['PG_VERSION'] != '18.6':
        raise ValueError('Expected reviewed PostgreSQL 18.6 inputs')
    if pins['repository'] != REPOSITORY or not re.fullmatch(r'docker.io/library/postgres@sha256:[0-9a-f]{64}', pins['runtime_images']['arm64']):
        raise ValueError('Expected the fixed repository and digest-pinned official PostgreSQL image')
    return pins


def fingerprint(pins):
    data = {'inputs': pins, 'architecture': 'arm64',
            'dockerfile_sha256': OCI.digest_file(CONTAINER / 'Dockerfile'),
            'builder_sha256': OCI.digest_file(Path(__file__)),
            'oci_helper_sha256': OCI.digest_file(ROOT / 'scripts/build-forgejo.py'),
            'import_helper_sha256': OCI.digest_file(ROOT / 'scripts/forgejo-artifact.py')}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def scan_gate(path):
    results = json.loads(path.read_text()).get('Results', [])
    if not any(item.get('Class') == 'os-pkgs' and item.get('Packages') for item in results):
        raise ValueError('PostgreSQL scan must inventory operating-system packages')
    if any('gosu' in result.get('Target', '').lower() or any(package.get('Name') == 'gosu' for package in result.get('Packages', [])) for result in results):
        raise ValueError('PostgreSQL image must remove the unused gosu binary')
    if any(v.get('Severity') in ('HIGH', 'CRITICAL') for result in results for v in result.get('Vulnerabilities', [])):
        raise ValueError('PostgreSQL image has HIGH/CRITICAL vulnerabilities; inspect private report')


def verified_record():
    pins = inputs()
    output = ROOT / '.dev-tools/postgres/arm64'
    for name in ['record.json', 'image.oci.tar', 'scan.json', 'values.yaml']:
        if (output / name).is_symlink():
            raise ValueError('PostgreSQL artifacts must not be symlinks')
    record = json.loads((output / 'record.json').read_text())
    if record['source_input_fingerprint'] != fingerprint(pins) or record['repository'] != REPOSITORY or record['architecture'] != 'arm64' or record['sourceBasePin'] != pins['runtime_images']['arm64']:
        raise ValueError('PostgreSQL inputs changed or artifact identity is invalid')
    for filename, key in [('image.oci.tar', 'archive_sha256'), ('scan.json', 'scan_sha256')]:
        if OCI.digest_file(output / filename) != record[key]:
            raise ValueError('PostgreSQL archive/report checksum mismatch')
    scan_gate(output / 'scan.json')
    digest = OCI.oci_manifest(output / 'image.oci.tar', 'arm64')
    if record['manifest_digest'] != digest or record['imageRef'] != REPOSITORY + '@' + digest:
        raise ValueError('PostgreSQL image does not match OCI bytes')
    if not re.fullmatch(r'postgres-arm64-[a-f0-9]{12}-[0-9]{14}', record['immutable_tag']):
        raise ValueError('Unexpected PostgreSQL tag')
    if json.loads((output / 'values.yaml').read_text()) != {'image': {'repository': REPOSITORY, 'digest': digest, 'pullPolicy': 'Never'}}:
        raise ValueError('PostgreSQL values differ from verified image')
    return output, record


def build():
    pins = inputs()
    if os.geteuid() == 0:
        raise ValueError('Build as the ordinary Docker-enabled user, without sudo')
    output = ROOT / '.dev-tools/postgres/arm64'
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    log = output / 'build.log'
    tag = 'postgres-arm64-' + fingerprint(pins)[:12] + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')
    reference = REPOSITORY + ':' + tag
    with tempfile.TemporaryDirectory(prefix='build-', dir=output) as directory:
        context = Path(directory)
        shutil.copyfile(CONTAINER / 'Dockerfile', context / 'Dockerfile')
        OCI.run(['docker', 'buildx', 'build', '--platform=linux/arm64', '--provenance=false', '--sbom=false',
                 '--no-cache-filter=runtime', '--build-arg', 'RUNTIME_IMAGE=' + pins['runtime_images']['arm64'],
                 '--build-arg', 'PG_VERSION=' + pins['PG_VERSION'], '--tag', reference, '--load', str(context)], log)
        report = output / 'candidate-scan.json'
        OCI.run([str(ROOT / '.dev-tools/bin/trivy'), 'image', '--quiet', '--timeout=15m', '--image-src=docker',
                 '--cache-dir', str(ROOT / '.dev-tools/trivy-postgres-cache'), '--scanners=vuln', '--list-all-pkgs',
                 '--format=json', '--output', str(report), '--severity=HIGH,CRITICAL', '--exit-code=0', reference], log)
        scan_gate(report)
        docker_archive, archive = context / 'image.docker.tar', context / 'image.oci.tar'
        OCI.run(['docker', 'image', 'save', '--output', str(docker_archive), reference], log)
        OCI.docker_to_oci(docker_archive, archive, 'arm64', reference)
        digest = OCI.oci_manifest(archive, 'arm64')
        record = {'source_input_fingerprint': fingerprint(pins), 'architecture': 'arm64', 'repository': REPOSITORY,
                  'manifest_digest': digest, 'imageRef': REPOSITORY + '@' + digest, 'immutable_tag': tag,
                  'sourceBasePin': pins['runtime_images']['arm64'], 'archive_sha256': OCI.digest_file(archive), 'scan_sha256': OCI.digest_file(report), 'inputs': pins}
        (context / 'record.json').write_text(json.dumps(record, indent=2) + '\n')
        (context / 'values.yaml').write_text(json.dumps({'image': {'repository': REPOSITORY, 'digest': digest, 'pullPolicy': 'Never'}}, indent=2) + '\n')
        shutil.copyfile(report, context / 'scan.json')
        for name in ['image.oci.tar', 'record.json', 'values.yaml', 'scan.json']:
            (context / name).replace(output / name)
    return verified_record()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    for name in ['build', 'verify', 'import']:
        actions.add_argument('--' + name, action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        output, record = build() if args.build else verified_record()
        if getattr(args, 'import'):
            if os.geteuid() == 0:
                raise ValueError('Run --import as the ordinary user; only the fixed K3s command requests sudo')
            # The shared gate rescans verified OCI bytes using the current vulnerability DB.
            module('postgres_checks', 'checks.py').image(record['imageRef'])
            IMPORT.ensure_import(output, record)
        print('Verified PostgreSQL image: ' + record['imageRef'])
    except (OSError, ValueError, KeyError, RuntimeError, tarfile.TarError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
