"""No Docker/network: verify full-feature preservation and strict artifact gates."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('webui_builder', ROOT / 'scripts/build-webui.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class WebUIBuild(unittest.TestCase):
    def report(self, directory, findings=None, omit=None):
        packages = ['chromadb', 'sentence-transformers', 'transformers', 'torch', 'uvicorn', 'fastapi']
        path = Path(directory) / 'scan.json'
        path.write_text(json.dumps({'Results': [
            {'Class': 'os-pkgs', 'Packages': [{'Name': 'libc6'}]},
            {'Type': 'python-pkg', 'Packages': [{'Name': name} for name in packages if name != omit],
             'Vulnerabilities': findings or []}]}))
        return path

    def test_known_unfixed_cve_is_not_exempted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.report(directory, [{'VulnerabilityID': 'CVE-2026-45829', 'Severity': 'CRITICAL'}])
            with self.assertRaisesRegex(ValueError, 'blocked by 1'):
                builder.scan_gate(path)

    def test_missing_chroma_inventory_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'inventory'):
                builder.scan_gate(self.report(directory, omit='chromadb'))

    def test_inventory_without_findings_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            builder.scan_gate(self.report(directory))

    def test_digest_pins_and_architecture_are_required(self):
        pins = builder.inputs('arm64')
        self.assertIn('@sha256:', pins['upstream_images']['arm64'])
        self.assertIn('@sha256:', pins['python_images']['arm64'])
        with self.assertRaisesRegex(ValueError, 'digest missing'):
            builder.inputs('amd64')

    def test_full_features_and_patched_interpreter_are_retained(self):
        dockerfile = (ROOT / 'containers/open-webui/Dockerfile').read_text()
        self.assertIn('COPY --from=upstream /usr/local/lib/python3.11/site-packages', dockerfile)
        self.assertIn('COPY --from=upstream --chown=1000:1000 /app /app', dockerfile)
        self.assertNotIn('COPY --from=upstream /usr/local/bin', dockerfile)
        self.assertIn('USE_SLIM_DOCKER=false', dockerfile)
        self.assertIn('sentence-transformers/all-MiniLM-L6-v2', dockerfile)
        self.assertIn('TaylorAI/bge-micro-v2', dockerfile)
        self.assertIn('CMD ["bash", "start.sh"]', dockerfile)
        self.assertIn('python3 -m pip check', dockerfile)
        self.assertIn('--only-binary=:all:', dockerfile)
        self.assertIn('USER 1000:1000', dockerfile)

    def test_shared_oci_implementation_is_reused(self):
        self.assertTrue(callable(builder.OCI.docker_to_oci))
        self.assertTrue(callable(builder.OCI.oci_manifest))
        source = (ROOT / 'scripts/build-webui.py').read_text()
        self.assertLess(source.index('scan_gate(report)  #'), source.index('OCI.docker_to_oci('))
        self.assertNotIn('--ignore-unfixed', source)
        self.assertNotIn('kubectl', source)


if __name__ == '__main__':
    unittest.main()
