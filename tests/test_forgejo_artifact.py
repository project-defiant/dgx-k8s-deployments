"""Digest aliases and a restricted Never-pull pod prove local image availability."""
import copy
import importlib.util
import json
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forgejo_artifact', ROOT / 'scripts/forgejo-artifact.py')
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)


class ArtifactPreflight(unittest.TestCase):
    def setUp(self):
        self.digest = 'sha256:' + 'a' * 64
        self.record = {'architecture': 'arm64', 'manifest_digest': self.digest,
                       'imageRef': 'local.thedefiant/forgejo@' + self.digest,
                       'immutable_tag': '15.0.10-patched1-arm64-fixture'}
        self.tag = artifact.REPOSITORY + ':' + self.record['immutable_tag']
        self.node = {'metadata': {'name': 'spark-platform'}, 'status': {'nodeInfo': {'architecture': 'arm64'}}}

    def test_node_summary_never_proves_image_availability(self):
        artifact.check_node(self.node, self.record)
        self.node['status']['images'] = []
        artifact.check_node(self.node, self.record)
        for change in ('node', 'architecture'):
            node = copy.deepcopy(self.node)
            if change == 'node':
                node['metadata']['name'] = 'another-node'
            else:
                node['status']['nodeInfo']['architecture'] = 'amd64'
            with self.assertRaises(ValueError):
                artifact.check_node(node, self.record)

    def root_fixture(self, initial):
        targets, calls = dict(initial), []
        def runner(args, output):
            calls.append(args)
            if args == ['list']:
                return 'REF TYPE DIGEST SIZE PLATFORMS LABELS\n' + '\n'.join(f'{name} application/vnd.oci.image.manifest.v1+json {digest} 1 linux/arm64 -' for name, digest in targets.items())
            if args[0] == 'import':
                targets[self.tag] = self.digest
            elif args[0] == 'tag':
                self.assertEqual(args[1], self.tag)
                targets[args[2]] = targets[args[1]]
            else:
                self.fail(f'Unexpected privileged action: {args}')
            return ''
        return runner, targets, calls

    def test_existing_tag_repairs_digest_alias_and_rerun_is_read_only(self):
        runner, targets, calls = self.root_fixture({self.tag: self.digest})
        artifact.ensure_import(Path('/tmp/fixture'), self.record, runner)
        self.assertEqual(targets[self.record['imageRef']], self.digest)
        self.assertEqual([args[0] for args in calls].count('tag'), 1)
        self.assertFalse(any(args[0] == 'import' for args in calls))
        calls.clear()
        artifact.ensure_import(Path('/tmp/fixture'), self.record, runner)
        self.assertTrue(all(args == ['list'] for args in calls))

    def test_import_uses_local_api_then_verifies_and_aliases(self):
        runner, targets, calls = self.root_fixture({})
        artifact.ensure_import(Path('/tmp/fixture'), self.record, runner)
        imported = next(args for args in calls if args[0] == 'import')
        self.assertIn('--local', imported)
        self.assertIn('linux/arm64', imported)
        self.assertNotIn('--force', imported)
        self.assertEqual(targets[self.record['imageRef']], self.digest)
        command = artifact.import_command(Path('/tmp/fixture'), self.record)
        self.assertEqual(command[:6], ['/usr/bin/sudo', '/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io', 'images'])

    def test_conflicting_existing_digest_alias_never_overwrites(self):
        runner, targets, calls = self.root_fixture({self.record['imageRef']: 'sha256:' + 'b' * 64})
        with self.assertRaises(ValueError):
            artifact.ensure_import(Path('/tmp/fixture'), self.record, runner)
        self.assertEqual(calls, [['list']])

    def test_existing_own_tag_pointing_at_index_is_reimported_as_leaf(self):
        runner, targets, calls = self.root_fixture({self.tag: 'sha256:' + 'b' * 64})
        artifact.ensure_import(Path('/tmp/fixture'), self.record, runner)
        imported = next(args for args in calls if args[0] == 'import')
        self.assertNotIn('--index-name', imported)
        self.assertNotIn('--force', imported)
        self.assertEqual(targets[self.tag], self.digest)
        self.assertEqual(targets[self.record['imageRef']], self.digest)

    def test_check_node_cli_probes_even_when_node_summary_omits_images(self):
        with patch.object(artifact.sys, 'argv', ['forgejo-artifact.py', 'check-node']), patch.object(artifact.sys, 'stdin', io.StringIO(json.dumps(self.node))), patch.object(artifact, 'verified_record', return_value=(Path('/tmp/fixture'), self.record)), patch.object(artifact, 'kube_command', return_value=['fixed-client']), patch.object(artifact, 'probe_image') as probe:
            self.assertEqual(artifact.main(), 0)
            probe.assert_called_once_with(Path('/tmp/fixture'), self.record, ['fixed-client'])

    def test_probe_client_rejects_remote_endpoint_before_pod_mutations(self):
        root_binary = SimpleNamespace(st_uid=0, st_mode=0o100755)
        user_config = SimpleNamespace(st_uid=artifact.os.getuid(), st_mode=0o100600)
        view = {'clusters': [{'cluster': {'server': 'https://remote.invalid:6443'}}], 'contexts': [{'name': 'spark-platform'}]}
        with patch.object(artifact.os, 'geteuid', return_value=1000), patch.object(artifact.Path, 'stat', side_effect=[root_binary, user_config]), patch.object(artifact.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(view))) as invoke:
            with self.assertRaisesRegex(ValueError, 'loopback'):
                artifact.kube_command()
            self.assertEqual(invoke.call_count, 1)
            command = invoke.call_args.args[0]
            self.assertEqual(command[:2], ['/usr/local/bin/k3s', 'kubectl'])
            self.assertIn('spark-platform', command)
            self.assertIn('--kubeconfig', command)

    def test_probe_uses_exact_digest_never_pull_and_cleans_up(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if 'get' in command:
                status = {'phase': 'Succeeded', 'containerStatuses': [{'name': 'image-check', 'imageID': 'containerd://' + self.digest, 'state': {'terminated': {'exitCode': 0}}}]}
                return SimpleNamespace(stdout=json.dumps({'status': status}))
            return SimpleNamespace(stdout='')
        with patch.object(artifact, 'image_ids', return_value={self.digest}):
            artifact.probe_image(Path('/tmp/fixture'), self.record, ['/usr/local/bin/k3s', 'kubectl'], runner)
        manifest = json.loads(calls[0][1]['input'])
        pod = manifest['spec']
        container = pod['containers'][0]
        self.assertEqual(container['image'], self.record['imageRef'])
        self.assertEqual(container['imagePullPolicy'], 'Never')
        self.assertEqual(container['command'], ['/app/gitea/gitea', '--version'])
        self.assertEqual(pod['activeDeadlineSeconds'], 30)
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['securityContext']['runAsUser'], 1000)
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertEqual(pod['volumes'], [{'name': 'tmp', 'emptyDir': {'sizeLimit': '32Mi'}},
                                          {'name': 'data', 'emptyDir': {'sizeLimit': '32Mi'}}])
        self.assertIn('delete', calls[-1][0])
        self.assertIn(manifest['metadata']['name'], calls[-1][0])

    def test_failed_cache_probe_and_wrong_runtime_digest_fail_and_cleanup(self):
        scenarios = [
            {'phase': 'Pending', 'containerStatuses': [{'state': {'waiting': {'reason': 'ErrImageNeverPull'}}}]},
            {'phase': 'Succeeded', 'containerStatuses': [{'name': 'image-check', 'imageID': 'sha256:' + 'b' * 64, 'state': {'terminated': {'exitCode': 0}}}]},
        ]
        for status in scenarios:
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(stdout=json.dumps({'status': status}))
            with self.subTest(status=status), patch.object(artifact, 'image_ids', return_value={self.digest}):
                with self.assertRaisesRegex(ValueError, 'make forgejo-image-import'):
                    artifact.probe_image(Path('/tmp/fixture'), self.record, ['/usr/local/bin/k3s', 'kubectl'], runner)
                self.assertIn('delete', calls[-1])


if __name__ == '__main__':
    unittest.main()
