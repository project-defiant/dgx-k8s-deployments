"""Exercise PostgreSQL image trust gates without Docker, sudo or network access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('postgres_image', ROOT / 'scripts/postgres-image.py')
postgres = importlib.util.module_from_spec(spec)
spec.loader.exec_module(postgres)


class PostgresImage(unittest.TestCase):
    def test_native_arm_and_fixed_repository_only(self):
        with mock.patch.object(postgres.OCI, 'native_architecture', return_value='arm64'):
            pins = postgres.inputs()
            self.assertEqual(pins['repository'], 'local.thedefiant/postgres')
            self.assertEqual(pins['PG_VERSION'], '18.6')
            self.assertTrue(pins['runtime_images']['arm64'].startswith('docker.io/library/postgres@sha256:'))
        with mock.patch.object(postgres.OCI, 'native_architecture', return_value='amd64'):
            with self.assertRaisesRegex(ValueError, 'native Linux ARM64'):
                postgres.inputs()
        self.assertEqual(postgres.IMPORT.REPOSITORY, 'local.thedefiant/postgres')

    def test_inventory_no_gosu_and_no_high_findings_are_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.json'
            path.write_text(json.dumps({'Results': []}))
            with self.assertRaisesRegex(ValueError, 'inventory'):
                postgres.scan_gate(path)
            result = {'Class': 'os-pkgs', 'Packages': [{'Name': 'musl'}]}
            path.write_text(json.dumps({'Results': [result]}))
            postgres.scan_gate(path)
            for severity in ('HIGH', 'CRITICAL'):
                result['Vulnerabilities'] = [{'Severity': severity, 'FixedVersion': ''}]
                path.write_text(json.dumps({'Results': [result]}))
                with self.assertRaisesRegex(ValueError, 'HIGH/CRITICAL'):
                    postgres.scan_gate(path)
            result.pop('Vulnerabilities')
            path.write_text(json.dumps({'Results': [result, {'Target': 'usr/local/bin/gosu', 'Class': 'lang-pkgs'}]}))
            with self.assertRaisesRegex(ValueError, 'gosu'):
                postgres.scan_gate(path)

    def test_official_entrypoint_retained_with_postgres_uid_and_no_runtime_additions(self):
        source = (ROOT / 'containers/postgres/Dockerfile').read_text()
        self.assertIn('apk upgrade --no-cache', source)
        self.assertIn('test "$(command -v gosu)" = /usr/local/bin/gosu', source)
        self.assertIn('rm /usr/local/bin/gosu', source)
        self.assertIn('s/^postgres:x:70:70:/postgres:x:1000:1000:/', source)
        self.assertIn('s/^postgres:x:70:/postgres:x:1000:/', source)
        self.assertIn('chown -R 1000:1000 /var/lib/postgresql /var/run/postgresql', source)
        self.assertIn('test "$(id -u postgres)" = 1000', source)
        self.assertIn('test "$(id -g postgres)" = 1000', source)
        self.assertIn('postgres --version', source)
        self.assertTrue(source.rstrip().endswith('USER 1000:1000'))
        for forbidden in ('apk add', 'edge', 'ENTRYPOINT', 'CMD', '--allow-untrusted'):
            self.assertNotIn(forbidden, source)

    def test_import_scans_before_fixed_repository_import(self):
        calls = []
        checks = mock.Mock()
        checks.image.side_effect = lambda reference: calls.append('scan')
        with mock.patch.object(postgres, 'verified_record', return_value=(Path('/tmp/unused'), {'imageRef': 'fixed'})), \
                mock.patch.object(postgres, 'module', return_value=checks), \
                mock.patch.object(postgres.IMPORT, 'ensure_import', side_effect=lambda *_: calls.append('import')), \
                mock.patch.object(postgres.os, 'geteuid', return_value=1000), \
                mock.patch('sys.argv', ['postgres-image.py', '--import']), mock.patch('builtins.print'):
            self.assertEqual(postgres.main(), 0)
        self.assertEqual(calls, ['scan', 'import'])
        checks.image.side_effect = ValueError('Rejected image')
        with mock.patch.object(postgres, 'verified_record', return_value=(Path('/tmp/unused'), {'imageRef': 'fixed'})), \
                mock.patch.object(postgres, 'module', return_value=checks), \
                mock.patch.object(postgres.IMPORT, 'ensure_import') as importer, \
                mock.patch.object(postgres.os, 'geteuid', return_value=1000), \
                mock.patch('sys.argv', ['postgres-image.py', '--import']), mock.patch('builtins.print'):
            self.assertEqual(postgres.main(), 1)
            importer.assert_not_called()

    def test_pipeline_reuses_oci_user_architecture_and_checksum_checks(self):
        source = (ROOT / 'scripts/postgres-image.py').read_text()
        self.assertLess(source.index('scan_gate(report)'), source.index('OCI.docker_to_oci('))
        self.assertIn("OCI.oci_manifest(output / 'image.oci.tar', 'arm64')", source)
        self.assertIn("'oci_helper_sha256'", source)
        self.assertIn("'import_helper_sha256'", source)
        self.assertIn("record['sourceBasePin']", source)
        for forbidden in ('--ignore-unfixed', 'kubectl', "add_argument('--repository"):
            self.assertNotIn(forbidden, source)
        self.assertTrue(callable(postgres.OCI.docker_to_oci))
        self.assertTrue(callable(postgres.OCI.oci_manifest))


if __name__ == '__main__':
    unittest.main()
