"""Verify stdlib copy-image gates with no Docker, sudo or network access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('llm_copy', ROOT / 'scripts/llm-copy-image.py')
copy_image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy_image)


class CopyImage(unittest.TestCase):
    def test_native_arm_and_fixed_repository_only(self):
        with mock.patch.object(copy_image.OCI, 'native_architecture', return_value='arm64'):
            self.assertEqual(copy_image.inputs()['repository'], 'local.thedefiant/llm-copy')
        with mock.patch.object(copy_image.OCI, 'native_architecture', return_value='amd64'):
            with self.assertRaisesRegex(ValueError, 'native Linux ARM64'):
                copy_image.inputs()
        self.assertEqual(copy_image.IMPORT.REPOSITORY, 'local.thedefiant/llm-copy')

    def test_inventory_and_no_high_findings_are_mandatory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.json'
            path.write_text(json.dumps({'Results': []}))
            with self.assertRaisesRegex(ValueError, 'inventory'):
                copy_image.scan_gate(path)
            result = {'Class': 'os-pkgs', 'Packages': [{'Name': 'musl'}]}
            path.write_text(json.dumps({'Results': [result]}))
            copy_image.scan_gate(path)
            result['Vulnerabilities'] = [{'Severity': 'HIGH', 'FixedVersion': ''}]
            path.write_text(json.dumps({'Results': [result]}))
            with self.assertRaisesRegex(ValueError, 'HIGH/CRITICAL'):
                copy_image.scan_gate(path)

    def test_dockerfile_removes_entire_unused_packages_and_retains_stdlib(self):
        source = (ROOT / 'containers/llm-copy/Dockerfile').read_text()
        self.assertIn('pip uninstall -y msgpack urllib3 setuptools wheel pip', source)
        self.assertIn('import tarfile, sqlite3, json, hashlib', source)
        self.assertIn('hashlib.file_digest', source)
        self.assertIn('USER 1000:1000', source)
        self.assertNotIn('apk add', source)
        self.assertNotIn('dist-info', source)

    def test_import_is_scanned_before_fixed_repository_import(self):
        source = (ROOT / 'scripts/llm-copy-image.py').read_text()
        self.assertLess(source.index("module('copy_checks', 'checks.py').image(record['imageRef'])"),
                        source.index('IMPORT.ensure_import(output, record)'))
        self.assertLess(source.index('scan_gate(report)'), source.index('OCI.docker_to_oci('))
        self.assertNotIn('--ignore-unfixed', source)
        self.assertNotIn('kubectl', source)
        self.assertNotIn('add_argument(\'--repository', source)

    def test_common_oci_conversion_is_reused(self):
        self.assertTrue(callable(copy_image.OCI.docker_to_oci))
        self.assertTrue(callable(copy_image.OCI.oci_manifest))
        self.assertTrue(callable(copy_image.IMPORT.ensure_import))


if __name__ == '__main__':
    unittest.main()
