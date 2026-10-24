#!/usr/bin/env python3
"""Build/verify a native, patched Forgejo OCI archive; never import or deploy it."""
import argparse
import datetime
import hashlib
import gzip
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
# All subprocesses invoke fixed local tools with separate argv; no shell evaluation.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CONTAINER = ROOT / 'containers/forgejo'


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def native_architecture():
    architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if platform.system() != 'Linux' or architecture is None:
        raise ValueError('Native Linux ARM64 or AMD64 required; no emulated build')
    return architecture


def fingerprint(pins, architecture):
    data = {'inputs': pins, 'architecture': architecture, 'dockerfile_sha256': digest_file(CONTAINER / 'Dockerfile'), 'builder_sha256': digest_file(Path(__file__))}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def oci_manifest(archive, architecture):
    """Verify referenced OCI blob bytes and native config, returning leaf digest."""
    with tarfile.open(archive) as bundle:
        def read(path):
            member = bundle.getmember(path)
            if not member.isfile():
                raise ValueError('OCI members must be regular files')
            with bundle.extractfile(member) as stream:
                return stream.read()
        def blob(descriptor, decode=True):
            value = descriptor.get('digest', '')
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
                raise ValueError('OCI descriptor requires SHA256')
            raw = read('blobs/sha256/' + value.removeprefix('sha256:'))
            if hashlib.sha256(raw).hexdigest() != value.removeprefix('sha256:'):
                raise ValueError('OCI blob checksum mismatch')
            return json.loads(raw) if decode else None
        def leaf(descriptor):
            data = blob(descriptor)
            if 'manifests' in data:
                if len(data['manifests']) != 1:
                    raise ValueError('Expected one native image, without attestations')
                return leaf(data['manifests'][0])
            config = blob(data['config'])
            if config.get('architecture') != architecture or config.get('os') != 'linux':
                raise ValueError('OCI image architecture does not match this host')
            if config.get('config', {}).get('User') != '1000:1000':
                raise ValueError('Patched image must retain the rootless runtime user')
            for layer in data['layers']:
                blob(layer, decode=False)
            return descriptor['digest']
        index = json.loads(read('index.json'))
        if len(index.get('manifests', [])) != 1:
            raise ValueError('Expected a single native OCI image')
        return leaf(index['manifests'][0])


def docker_to_oci(source, destination, architecture, reference):
    """Convert a single Docker-save image without extracting any paths to the host."""
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    with tarfile.open(source) as bundle, tarfile.open(destination, 'w') as output:
        def read(name):
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe Docker archive member name')
            member = bundle.getmember(name)
            if not member.isfile():
                raise ValueError('Docker archive members must be regular files')
            with bundle.extractfile(member) as stream:
                return stream.read()
        def write(name, data):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            output.addfile(member, io.BytesIO(data))
        written = set()
        def blob(data, media_type):
            digest = 'sha256:' + hashlib.sha256(data).hexdigest()
            if digest not in written:
                write('blobs/sha256/' + digest.removeprefix('sha256:'), data)
                written.add(digest)
            return {'mediaType': media_type, 'digest': digest, 'size': len(data)}
        images = json.loads(read('manifest.json'))
        if len(images) != 1 or images[0].get('RepoTags') != [reference]:
            raise ValueError('Expected exactly the newly built, uniquely tagged Docker image')
        image = images[0]
        config_bytes = read(image['Config'])
        config = json.loads(config_bytes)
        if config.get('architecture') != architecture or config.get('os') != 'linux':
            raise ValueError('Docker archive architecture does not match this host')
        if config.get('config', {}).get('User') != '1000:1000':
            raise ValueError('Docker archive must retain rootless runtime user')
        differences = config.get('rootfs', {}).get('diff_ids', [])
        if len(differences) != len(image['Layers']):
            raise ValueError('Docker layer count differs from rootfs diff_ids')
        layers = []
        for name, expected in zip(image['Layers'], differences):
            raw = read(name)
            if raw.startswith(b'\x1f\x8b'):
                raw = gzip.decompress(raw)
            elif raw.startswith(b'\x28\xb5\x2f\xfd'):
                raise ValueError('Zstandard Docker layers are unsupported; require uncompressed Docker save')
            if 'sha256:' + hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError('Docker layer bytes differ from rootfs diff_id')
            layers.append(blob(raw, 'application/vnd.oci.image.layer.v1.tar'))
        manifest = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                    'config': blob(config_bytes, 'application/vnd.oci.image.config.v1+json'), 'layers': layers}
        descriptor = blob(encoded(manifest), manifest['mediaType'])
        descriptor['platform'] = {'architecture': architecture, 'os': 'linux'}
        descriptor['annotations'] = {'org.opencontainers.image.ref.name': reference,
                                     'io.containerd.image.name': reference}
        write('oci-layout', encoded({'imageLayoutVersion': '1.0.0'}))
        write('index.json', encoded({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [descriptor]}))


def verify(output, pins, architecture):
    record_path = output / 'record.json'
    archive = output / 'image.oci.tar'
    if archive.is_symlink() or record_path.is_symlink():
        raise ValueError('Build artifacts must not be symlinks')
    record = json.loads(record_path.read_text())
    if record['source_input_fingerprint'] != fingerprint(pins, architecture):
        raise ValueError('Build inputs changed; rebuild required')
    if record['archive_sha256'] != digest_file(archive):
        raise ValueError('OCI archive checksum mismatch')
    manifest = oci_manifest(archive, architecture)
    if record['manifest_digest'] != manifest or record['imageRef'] != pins['repository'] + '@' + manifest:
        raise ValueError('Image reference does not match the verified OCI manifest')
    expected = {'repository': pins['repository'], 'digest': manifest, 'pullPolicy': 'Never'}
    values = json.loads((output / 'values.yaml').read_text())
    if values.get('image') != expected:
        raise ValueError('Generated Helm values differ from the verified archive')
    print(f'Verified native {architecture} OCI archive: {record["imageRef"]}', flush=True)
    return record


def run(argv, log):
    with log.open('ab') as stream:
        log.chmod(0o600)
        result = subprocess.run(argv, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)  # nosec B603
    if result.returncode:
        raise RuntimeError(f'Build command failed (exit {result.returncode}); inspect private {log}')


def fetch(pin, destination, input_cache, log):
    filename = pin['url'].rsplit('/', 1)[1]
    if input_cache and (input_cache / filename).is_file():
        shutil.copyfile(input_cache / filename, destination)
    else:
        # URLs and expected hashes are committed inputs, not executable shell text.
        run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https', '--tlsv1.2', '--connect-timeout', '15', '--max-time', '600', pin['url'], '-o', str(destination)], log)
    if digest_file(destination) != pin['sha256']:
        raise ValueError(f'Input checksum mismatch: {filename}')


def build(output, pins, architecture, input_cache):
    if os.geteuid() == 0:
        raise ValueError('Run as your ordinary Docker-enabled user; root is unnecessary')
    if not shutil.which('docker') or not shutil.which('curl'):
        raise ValueError('Docker with Buildx and curl are prerequisites')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    log = output / 'build.log'
    run(['docker', 'buildx', 'version'], log)
    tag = '15.0.10-patched1-' + architecture + '-' + fingerprint(pins, architecture)[:12] + '-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')
    print(f'Building {pins["repository"]}:{tag}; private progress log: {log}', flush=True)
    with tempfile.TemporaryDirectory(prefix='build-', dir=output) as temporary:
        context = Path(temporary)
        shutil.copyfile(CONTAINER / 'Dockerfile', context / 'Dockerfile')
        fetch(pins['source'], context / 'source.tar.gz', input_cache, log)
        fetch(pins['go'][architecture], context / 'go.tar.gz', input_cache, log)
        archive = context / 'image.oci.tar'
        metadata = context / 'metadata.json'
        run(['docker', 'buildx', 'build', '--platform', 'linux/' + architecture,
             '--provenance=false', '--sbom=false', '--no-cache-filter', 'patched-runtime',
             '--build-arg', 'RUNTIME_IMAGE=' + pins['runtime_images'][architecture],
             '--build-arg', 'SOURCE_SHA256=' + pins['source']['sha256'],
             '--build-arg', 'GO_SHA256=' + pins['go'][architecture]['sha256'],
             '--tag', pins['repository'] + ':' + tag,
             '--load',
             '--metadata-file', str(metadata), str(context)], log)
        docker_archive = context / 'image.docker.tar'
        run(['docker', 'image', 'save', '--output', str(docker_archive), pins['repository'] + ':' + tag], log)
        docker_to_oci(docker_archive, archive, architecture, pins['repository'] + ':' + tag)
        manifest = oci_manifest(archive, architecture)
        record = {'source_input_fingerprint': fingerprint(pins, architecture), 'architecture': architecture,
                  'archive_sha256': digest_file(archive), 'manifest_digest': manifest,
                  'repository': pins['repository'], 'immutable_tag': tag,
                  'imageRef': pins['repository'] + '@' + manifest,
                  'inputs': pins, 'dockerfile_sha256': digest_file(CONTAINER / 'Dockerfile'),
                  'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        (context / 'record.json').write_text(json.dumps(record, indent=2) + '\n')
        (context / 'values.yaml').write_text(json.dumps({'image': {'repository': pins['repository'], 'digest': manifest, 'pullPolicy': 'Never'}}, indent=2) + '\n')
        for name in ['image.oci.tar', 'metadata.json', 'record.json', 'values.yaml']:
            (context / name).replace(output / name)
    verify(output, pins, architecture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--ensure', action='store_true', help='Verify cached artifact; rebuild missing/stale inputs')
    action.add_argument('--build', action='store_true', help='Explicitly rebuild with current signed Alpine updates')
    action.add_argument('--verify', action='store_true', help='Verify existing artifact without Docker or network')
    action.add_argument('--plan', action='store_true', help='Print locked inputs without downloading or building')
    parser.add_argument('--input-cache', type=Path, help='Optional read-only cache of original source/Go archives')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        architecture = native_architecture()
        pins = json.loads((CONTAINER / 'inputs.lock.json').read_text())
        output = ROOT / '.dev-tools/forgejo' / architecture
        if args.verify:
            verify(output, pins, architecture)
        elif args.ensure or args.build:
            if args.ensure and (output / 'record.json').exists():
                # Corrupt archives fail closed; only input changes trigger automatic rebuild.
                record = json.loads((output / 'record.json').read_text())
                if record.get('source_input_fingerprint') == fingerprint(pins, architecture):
                    verify(output, pins, architecture)
                    return 0
            build(output, pins, architecture, args.input_cache)
        else:
            print(json.dumps({'architecture': architecture, 'output': str(output), 'inputs': pins}, indent=2))
    except (OSError, ValueError, KeyError, RuntimeError, tarfile.TarError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
