"""Offline integrity checks for local Forgejo build artifacts and locked inputs."""
import hashlib
import gzip
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forgejo_builder', ROOT / 'scripts/build-forgejo.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def fixture_archive(path, architecture='arm64', user='1000:1000', corrupt=False):
    config = json.dumps({'architecture': architecture, 'os': 'linux', 'config': {'User': user}}).encode()
    config_hash = hashlib.sha256(config).hexdigest()
    layer = b'opaque layer bytes for hash verification'
    layer_hash = hashlib.sha256(layer).hexdigest()
    manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': 'sha256:' + config_hash}, 'layers': [{'digest': 'sha256:' + layer_hash}]}).encode()
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    index = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': 'sha256:' + manifest_hash}]}).encode()
    with tarfile.open(path, 'w') as bundle:
        for name, data in [('index.json', index), ('blobs/sha256/' + manifest_hash, manifest), ('blobs/sha256/' + config_hash, config), ('blobs/sha256/' + layer_hash, b'altered' if corrupt else layer)]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))
    return 'sha256:' + manifest_hash


def docker_fixture(path, architecture='arm64', user='1000:1000', corrupt=False, compressed=False, unsafe=False):
    layer = b'layer content never extracted to host'
    config = json.dumps({'architecture': architecture, 'os': 'linux', 'config': {'User': user},
                         'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(layer).hexdigest()]}}).encode()
    layer_name = '../unsafe' if unsafe else 'layer/layer.tar'
    manifest = [{'Config': 'config.json', 'RepoTags': ['local.thedefiant/forgejo:test'], 'Layers': [layer_name]}]
    raw_layer = b'corrupt' if corrupt else layer
    if compressed:
        raw_layer = gzip.compress(raw_layer)
    with tarfile.open(path, 'w') as bundle:
        for name, data in [('manifest.json', json.dumps(manifest).encode()), ('config.json', config), (layer_name, raw_layer)]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            bundle.addfile(member, io.BytesIO(data))


class ForgejoBuildTests(unittest.TestCase):
    def test_plan_cli_is_read_only_and_inputs_are_pinned(self):
        result = subprocess.run(['python3', str(ROOT / 'scripts/build-forgejo.py'), '--plan'], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        pins = plan['inputs']
        self.assertEqual(pins['forgejo_version'], '15.0.10')
        self.assertEqual(pins['go_version'], '1.26.10')
        self.assertIn('/releases/download/v15.0.10/forgejo-src-', pins['source']['url'])
        for architecture in ('arm64', 'amd64'):
            self.assertRegex(pins['runtime_images'][architecture], r'@sha256:[a-f0-9]{64}$')
            self.assertRegex(pins['go'][architecture]['sha256'], r'^[a-f0-9]{64}$')

    def test_oci_verifies_blob_bytes_architecture_and_rootless_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.tar'
            digest = fixture_archive(path)
            self.assertEqual(builder.oci_manifest(path, 'arm64'), digest)
            with self.assertRaises(ValueError):
                builder.oci_manifest(path, 'amd64')
            fixture_archive(path, user='0:0')
            with self.assertRaises(ValueError):
                builder.oci_manifest(path, 'arm64')
            fixture_archive(path, corrupt=True)
            with self.assertRaises(ValueError):
                builder.oci_manifest(path, 'arm64')

    def test_verify_requires_matching_archive_and_generated_values(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            archive = output / 'image.oci.tar'
            manifest = fixture_archive(archive)
            pins = json.loads((ROOT / 'containers/forgejo/inputs.lock.json').read_text())
            record = {'source_input_fingerprint': builder.fingerprint(pins, 'arm64'), 'archive_sha256': builder.digest_file(archive), 'manifest_digest': manifest, 'imageRef': pins['repository'] + '@' + manifest}
            (output / 'record.json').write_text(json.dumps(record))
            values = {'image': {'repository': pins['repository'], 'digest': manifest, 'pullPolicy': 'Never'}}
            (output / 'values.yaml').write_text(json.dumps(values))
            with patch.object(builder, 'run', side_effect=AssertionError('verify must not invoke Docker or network')):
                builder.verify(output, pins, 'arm64')
                values['image']['pullPolicy'] = 'Always'
                (output / 'values.yaml').write_text(json.dumps(values))
                with self.assertRaises(ValueError):
                    builder.verify(output, pins, 'arm64')
            archive.write_bytes(b'corrupt archive')
            with self.assertRaises(ValueError):
                builder.verify(output, pins, 'arm64')

    def test_docker_conversion_hashes_layers_and_rejects_wrong_image(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'docker.tar'
            target = Path(directory) / 'oci.tar'
            for compressed in (False, True):
                docker_fixture(source, compressed=compressed)
                builder.docker_to_oci(source, target, 'arm64', 'local.thedefiant/forgejo:test')
                self.assertRegex(builder.oci_manifest(target, 'arm64'), '^sha256:[a-f0-9]{64}$')
                with tarfile.open(target) as bundle:
                    index = json.load(bundle.extractfile('index.json'))
                    self.assertEqual(index['manifests'][0]['annotations']['io.containerd.image.name'], 'local.thedefiant/forgejo:test')
                    for member in bundle.getmembers():
                        self.assertTrue(member.isfile())
                        self.assertTrue(member.name in ('index.json', 'oci-layout') or member.name.startswith('blobs/sha256/'))
            for settings in ({'architecture': 'amd64'}, {'user': '0'}, {'corrupt': True}, {'unsafe': True}):
                with self.subTest(settings=settings):
                    docker_fixture(source, **settings)
                    with self.assertRaises(ValueError):
                        builder.docker_to_oci(source, target, 'arm64', 'local.thedefiant/forgejo:test')
            docker_fixture(source)
            with self.assertRaises(ValueError):
                builder.docker_to_oci(source, target, 'arm64', 'local.thedefiant/forgejo:another')

    def test_cached_inputs_are_checked_without_executing_download(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            source = cache / 'source.tar.gz'
            source.write_bytes(b'official bytes')
            pin = {'url': 'https://example.invalid/source.tar.gz', 'sha256': builder.digest_file(source)}
            with patch.object(builder, 'run', side_effect=AssertionError('cached input must not invoke curl')):
                builder.fetch(pin, cache / 'copy', cache, cache / 'log')
                source.write_bytes(b'corrupt bytes')
                with self.assertRaises(ValueError):
                    builder.fetch(pin, cache / 'copy', cache, cache / 'log')


if __name__ == '__main__':
    unittest.main()
