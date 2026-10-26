"""The narrow GPU addon never applies unreviewed host mounts or skips its gate."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('platform_gpu', ROOT / 'scripts/platform-gpu.py')
gpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu)


def node(count=1, name='spark-platform', architecture='arm64'):
    return {'metadata': {'name': name}, 'status': {'nodeInfo': {'architecture': architecture}, 'allocatable': {'nvidia.com/gpu': str(count)}}}


class GPUAddon(unittest.TestCase):
    def test_manifest_has_only_reviewed_kubelet_mount_and_hardened_root(self):
        text = gpu.reviewed_manifest()
        self.assertEqual(text.count('hostPath:'), 1)
        self.assertEqual(text.count('path: /var/lib/kubelet/device-plugins'), 1)
        self.assertIn('type: Directory', text)
        self.assertIn('mountPath: /var/lib/kubelet/device-plugins', text)
        self.assertIn('runAsUser: 0', text)
        for control in ['automountServiceAccountToken: false', 'privileged: false', 'allowPrivilegeEscalation: false', 'readOnlyRootFilesystem: true', 'drop: [ALL]', 'type: RuntimeDefault', 'runtimeClassName: nvidia', 'value: "true"']:
            self.assertIn(control, text)
        for forbidden in ['hostNetwork:', 'hostPID:', 'hostIPC:', 'privileged: true', 'serviceAccountName:', 'timeSlicing:', 'mps:', 'replicas:']:
            self.assertNotIn(forbidden, text)
        self.assertIn('kubernetes.io/hostname: spark-platform', text)

    def test_changed_mount_is_rejected_before_any_external_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.yaml'
            path.write_text(gpu.reviewed_manifest().replace('path: /var/lib/kubelet/device-plugins', 'path: /etc'))
            with patch.object(gpu, 'MANIFEST', path), patch.object(gpu, 'run') as external:
                with self.assertRaisesRegex(gpu.Refusal, 'changed'):
                    gpu.reviewed_manifest()
                external.assert_not_called()
        with patch.object(gpu, 'run') as external:
            with self.assertRaises(gpu.Refusal):
                gpu.deploy(['fixed-client'], 'unreviewed bytes')
            external.assert_not_called()

    def test_fresh_image_scan_and_schema_precede_same_bytes_apply(self):
        calls = []
        manifest = gpu.reviewed_manifest()
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return json.dumps(node()) if 'node' in argv else ''
        with patch.object(gpu, 'run', side_effect=run):
            gpu.deploy(['fixed-client'], manifest)
        schema = next(index for index, (args, _) in enumerate(calls) if args[0].endswith('kubeconform'))
        scan = next(index for index, (args, _) in enumerate(calls) if str(ROOT / 'scripts/checks.py') in args)
        apply = next(index for index, (args, _) in enumerate(calls) if 'apply' in args)
        self.assertLess(schema, apply)
        self.assertLess(scan, apply)
        self.assertEqual(calls[schema][1]['data'], manifest)
        self.assertEqual(calls[apply][1]['data'], manifest)
        self.assertIn(gpu.IMAGE, calls[scan][0])
        self.assertIn('image', calls[scan][0])

    def test_scan_failure_never_applies(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            if 'image' in argv:
                raise gpu.Refusal('Image gate failed')
            return ''
        with patch.object(gpu, 'run', side_effect=run):
            with self.assertRaises(gpu.Refusal):
                gpu.deploy(['fixed-client'], gpu.reviewed_manifest())
        self.assertFalse(any('apply' in command for command in calls))

    def test_ready_daemonset_without_gpu_is_not_success(self):
        def run(argv, **kwargs):
            return json.dumps(node(0)) if 'node' in argv else ''
        with patch.object(gpu, 'run', side_effect=run), patch.object(gpu.time, 'monotonic', side_effect=[0, 1, 61]), patch.object(gpu.time, 'sleep'):
            with self.assertRaisesRegex(gpu.Refusal, 'did not advertise'):
                gpu.deploy(['fixed-client'], gpu.reviewed_manifest())

    def test_unexpected_node_architecture_or_gpu_count_fails(self):
        for item in [node(name='another-node'), node(architecture='amd64')]:
            with self.assertRaises(gpu.Refusal):
                gpu.validate_node(item)
        with patch.object(gpu, 'run', return_value=json.dumps(node(2))):
            with self.assertRaisesRegex(gpu.Refusal, 'Unexpected GPU count'):
                gpu.deploy(['fixed-client'], gpu.reviewed_manifest())

    def test_wrong_cluster_endpoint_is_refused_before_node_requests(self):
        view = {'clusters': [{'cluster': {'server': 'https://remote.invalid:6443'}}], 'contexts': [{'name': 'spark-platform'}]}
        info = [SimpleNamespace(st_uid=0, st_mode=0o100755), SimpleNamespace(st_uid=gpu.os.getuid(), st_mode=0o100600)]
        with patch.object(gpu.os, 'geteuid', return_value=1000), patch.object(gpu.Path, 'stat', side_effect=info), patch.object(gpu, 'run', return_value=json.dumps(view)) as invoke:
            with self.assertRaisesRegex(gpu.Refusal, 'loopback'):
                gpu.client()
            self.assertEqual(invoke.call_count, 1)

    def test_render_never_calls_cluster_or_network(self):
        with patch.object(gpu.sys, 'argv', ['platform-gpu.py', 'render']), patch.object(gpu, 'run', side_effect=AssertionError('No external calls')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(gpu.main(), 0)
            self.assertIn(gpu.IMAGE, output.getvalue())


if __name__ == '__main__':
    unittest.main()
