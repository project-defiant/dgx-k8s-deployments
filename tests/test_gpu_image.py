"""Patched GPU artifact gates require actual plugin and module inventory."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gpu_image',ROOT/'scripts/gpu-image.py')
image=importlib.util.module_from_spec(spec)
spec.loader.exec_module(image)

class GPUImage(unittest.TestCase):
    def test_scan_requires_patched_plugin_inventory_and_no_high_findings(self):
        report={'Results':[{'Class':'os-pkgs','Packages':[{'Name':'libc'}]}, {'Target':'usr/local/bin/nvidia-device-plugin','Type':'gobinary','Packages':[{'Name':'stdlib','Version':'v1.27.3'},{'Name':'golang.org/x/net','Version':'v0.60.0'}]}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(image,'inputs',return_value={'go_version':'1.27.3','x_net_version':'0.60.0'}):
            path=Path(directory)/'scan.json'
            path.write_text(json.dumps(report));image.scan_gate(path)
            report['Results'][1]['Packages'][0]['Version']='v1.27.1'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'patched Go'):image.scan_gate(path)
            report['Results'][1]['Packages'][0]['Version']='v1.27.3'
            report['Results'][0]['Vulnerabilities']=[{'Severity':'HIGH'}]
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'HIGH/CRITICAL'):image.scan_gate(path)

    def test_source_compiler_and_dependency_are_checked_without_host_mounts(self):
        source=(ROOT/'containers/gpu/Dockerfile').read_text()
        for expected in ['sha256sum -c','GOTOOLCHAIN=local','golang.org/x/net@v${X_NET_VERSION}','go build -trimpath','USER 1000:1000']:
            self.assertIn(expected,source)
        self.assertNotIn('privileged',source)
        builder=(ROOT/'scripts/gpu-image.py').read_text()
        self.assertIn("OCI.digest_file(source) != pins[key]['sha256']",builder)
        self.assertLess(builder.index('scan_gate(report)'),builder.index('OCI.docker_to_oci('))
        self.assertNotIn('--ignore-unfixed',builder)

if __name__=='__main__':unittest.main()
