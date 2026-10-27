"""Offline refusal, secret handling and actual streaming-copy regression tests."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('llm_backup', ROOT / 'scripts/llm-backup.py')
llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm)
IMAGE = llm.COPY_IMAGE


def archive_files(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


class LLMBackupCLI(unittest.TestCase):
    def test_ollama_only_render_excludes_webui_and_retains_migrated_claim(self):
        command = [str(ROOT / '.dev-tools/bin/helm'), 'template', 'llm-backup', 'charts/llm-backup', '-n', 'llm-backup', '-f', 'apps/llm-backup/values.yaml', '--set', 'ollama.image.digest=sha256:' + 'a'*64, '--set', 'webui.image.digest=sha256:' + 'b'*64]
        source = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True).stdout
        resources = llm.ollama_resources(source)
        self.assertEqual({(item['kind'],item['metadata']['name']) for item in resources}, {('Deployment','ollama'),('Service','ollama')})
        self.assertNotIn('open-webui', json.dumps(resources))
        deployment = next(item for item in resources if item['kind']=='Deployment')
        self.assertEqual(deployment['spec']['replicas'], 0)
        self.assertEqual(deployment['spec']['template']['spec']['volumes'][0]['persistentVolumeClaim']['claimName'], 'ollama-data')
        self.assertTrue(all(llm.owned(item) for item in resources))

    def test_ollama_only_deploy_works_without_private_overlay_or_webui_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'apps/llm-backup').mkdir(parents=True)
            (root/'apps/llm-backup/values.yaml').write_text((ROOT/'apps/llm-backup/values.yaml').read_text())
            (root/'charts').symlink_to(ROOT/'charts',target_is_directory=True)
            (root/'.dev-tools/bin').mkdir(parents=True)
            (root/'.dev-tools/bin/helm').symlink_to(ROOT/'.dev-tools/bin/helm')
            with patch.object(llm,'ROOT',root), patch.object(llm.sys,'argv',['llm-backup.py','deploy-ollama']), patch.object(llm,'guard',return_value={}), patch.object(llm,'deploy_ollama') as deploy:
                self.assertEqual(llm.main(),0)
                resources=deploy.call_args.args[0]
                self.assertEqual({item['metadata']['name'] for item in resources},{'ollama'})
                rendered=json.dumps(resources)
                self.assertIn('064204ed2149',rendered)
                self.assertNotIn('open-webui',rendered)
                self.assertNotIn('sha256:'+'0'*64,rendered)

    def test_ollama_only_scan_failure_never_applies_a_resource(self):
        resources = [{'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'ollama'},'spec':{'template':{'spec':{'containers':[{'image':'docker.io/ollama/ollama@sha256:'+'a'*64}]}}}}]
        with patch.object(llm, 'cold_guard'), patch.object(llm, 'ollama_receipt'), patch.object(llm, 'js', return_value={}), patch.object(llm, 'run'), patch.object(llm, 'image_gate', side_effect=llm.Refusal('Image blocked')), patch.object(llm, 'k') as mutate:
            with self.assertRaisesRegex(llm.Refusal, 'Image blocked'):
                llm.deploy_ollama(resources)
            mutate.assert_not_called()

    def test_start_ollama_does_not_query_or_scale_webui(self):
        deployment={'metadata':{},'spec':{'replicas':0,'template':{'spec':{'containers':[{'image':'docker.io/ollama/ollama@sha256:'+'a'*64}]}}}}
        with patch.object(llm, 'source_stopped') as source, patch.object(llm, 'ollama_receipt'), patch.object(llm, 'owned', return_value=True), patch.object(llm, 'js', return_value=deployment) as query, patch.object(llm, 'image_gate') as gate, patch.object(llm, 'available_gpu') as capacity, patch.object(llm, 'k') as mutate:
            llm.start_ollama({})
            source.assert_called_once_with('ollama')
            gate.assert_called_once_with(deployment['spec']['template']['spec']['containers'][0]['image'])
            capacity.assert_called_once()
            mutate.assert_any_call('scale','deployment/ollama','--replicas=1')
            self.assertNotIn('webui', str(query.call_args_list)+str(mutate.call_args_list))

    def test_ollama_only_start_fails_without_gpu_before_scaling(self):
        deployment={'spec':{'replicas':0,'template':{'spec':{'containers':[{'image':'docker.io/ollama/ollama@sha256:'+'a'*64}]}}}}
        with patch.object(llm, 'source_stopped'), patch.object(llm, 'ollama_receipt'), patch.object(llm, 'owned', return_value=True), patch.object(llm, 'js', return_value=deployment), patch.object(llm, 'image_gate'), patch.object(llm, 'available_gpu', side_effect=llm.Refusal('No GPU')), patch.object(llm, 'k') as mutate:
            with self.assertRaisesRegex(llm.Refusal, 'No GPU'):
                llm.start_ollama({})
            mutate.assert_not_called()

    def test_plan_never_calls_host_or_cluster(self):
        with patch.object(llm.sys, 'argv', ['llm-backup.py', 'plan']), patch.object(llm, 'run', side_effect=AssertionError('No external commands allowed')):
            self.assertEqual(llm.main(), 0)

    def test_wrong_cluster_refuses_before_node_or_mutation(self):
        view = {'clusters': [{'cluster': {'server': 'https://remote.invalid:6443'}}], 'contexts': [{'name': 'spark-platform'}]}
        info = [SimpleNamespace(st_uid=0, st_mode=0o100755), SimpleNamespace(st_uid=llm.os.getuid(), st_mode=0o100600)]
        with patch.object(llm.os, 'geteuid', return_value=1000), patch.object(llm.Path, 'stat', side_effect=info), patch.object(llm, 'run', return_value=SimpleNamespace(stdout=json.dumps(view))), patch.object(llm, 'js') as kube:
            with self.assertRaisesRegex(llm.Refusal, 'loopback'):
                llm.guard()
            kube.assert_not_called()

    def test_no_gpu_or_busy_gpu_blocks_start_without_scaling(self):
        for node in [{'status': {'allocatable': {}}}, {'status': {'allocatable': {'nvidia.com/gpu': '1'}}}]:
            pods = {'items': [{'spec': {'nodeName': 'spark-platform', 'containers': [{'resources': {'requests': {'nvidia.com/gpu': 1}}}]}, 'status': {'phase': 'Running'}}]}
            with patch.object(llm, 'cold_guard'), patch.object(llm, 'js', return_value=pods), patch.object(llm, 'k') as mutate:
                with self.assertRaisesRegex(llm.Refusal, 'GPU'):
                    llm.start(node)
                mutate.assert_not_called()

    def test_active_replicas_refuse_copy_before_helper_or_source_commands(self):
        with patch.object(llm, 'js', return_value={'items': [{'spec': {'replicas': 1}}]}), patch.object(llm, 'run') as external:
            with self.assertRaisesRegex(llm.Refusal, 'zero replicas'):
                llm.migrate('ollama')
            external.assert_not_called()

    def test_helper_has_no_host_access_and_allows_pvc_scheduling(self):
        pod = llm.helper_manifest('copy-fixture', 'ollama-data', IMAGE)['spec']
        self.assertNotIn('nodeName', pod)
        self.assertEqual(pod['nodeSelector'], {'kubernetes.io/hostname': 'spark-platform'})
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['securityContext']['runAsUser'], 1000)
        self.assertEqual(pod['volumes'], [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'ollama-data'}}])
        self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        with self.assertRaises(llm.Refusal):
            llm.helper_manifest('copy-fixture', 'ollama-data', IMAGE.split('@')[0] + ':latest')

    def test_secret_and_provider_settings_never_print_or_enter_argv(self):
        sensitive = 'fixture-encryption-material'
        environment = ['OPENAI_API_KEY=fixture-provider-value', 'OPENAI_API_BASE_URLS=https://provider.invalid/v1', 'ENABLE_OPENAI_API=false']
        command_result = SimpleNamespace(stdout=archive_files({'.webui_secret_key': sensitive.encode()}), returncode=0)
        stream = io.StringIO()
        with patch.object(llm, 'run', return_value=SimpleNamespace(stdout=json.dumps(environment))), patch.object(llm.subprocess, 'run', return_value=command_result) as invoke, contextlib.redirect_stdout(stream):
            data = llm.webui_secrets()
        self.assertIn('WEBUI_SECRET_KEY', data)
        self.assertIn('OPENAI_API_KEY', data)
        self.assertIn('OPENAI_API_BASE_URLS', data)
        self.assertIn('ENABLE_OPENAI_API', data)
        self.assertNotIn(sensitive, str(invoke.call_args.args))
        self.assertNotIn(sensitive, stream.getvalue())
        with patch.object(llm, 'js', return_value={'data': {'WEBUI_SECRET_KEY': 'different'}}), patch.object(llm, 'k') as mutate:
            with self.assertRaisesRegex(llm.Refusal, 'refusing overwrite'):
                llm.preserve_secret(data)
            mutate.assert_not_called()

    def test_image_gate_fails_before_helper_or_source_changes(self):
        with patch.object(llm, 'cold_guard'), patch.object(llm, 'image_gate', side_effect=llm.Refusal('Image gate failed')), patch.object(llm, 'k') as mutate:
            with self.assertRaises(llm.Refusal):
                llm.migrate('webui')
            mutate.assert_not_called()

    def test_external_database_refused_without_key_capture_or_source_stop(self):
        environment = ['DATABASE_URL=postgresql://fixture.invalid/webui']
        with patch.object(llm, 'run', return_value=SimpleNamespace(stdout=json.dumps(environment))), patch.object(llm.subprocess, 'run') as external:
            with self.assertRaisesRegex(llm.Refusal, 'database'):
                llm.webui_secrets()
            external.assert_not_called()

    def test_low_headroom_refuses_before_copy(self):
        with patch.object(llm, 'k', return_value='1'):
            with self.assertRaisesRegex(llm.Refusal, 'free space'):
                llm.headroom('copy-fixture', 1024)

    def test_source_directory_symlink_is_refused_without_root(self):
        import stat
        info = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0), SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)]
        with patch.object(llm.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=996)), patch.object(llm.Path, 'lstat', side_effect=info), patch.object(llm, 'run') as external:
            with self.assertRaisesRegex(llm.Refusal, 'real directories'):
                llm.ollama_source_guard()
            external.assert_not_called()

    def test_large_producer_stderr_cannot_deadlock_stream(self):
        code = "import importlib.util,sys; s=importlib.util.spec_from_file_location('llm',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.copy_stream([sys.executable,'-c',\"import sys; sys.stderr.write('x'*262144); sys.stdout.write('payload')\"],[sys.executable,'-c',\"import sys; sys.stdin.read(); print('{\\\"complete\\\":true}')\"]))"
        result = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts/llm-backup.py')], capture_output=True, timeout=10, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('True', result.stdout)

    def test_copied_db_retargets_only_local_backend_and_retains_cloud_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / 'source.db'
            settings = {'ui': {'enable_signup': False}, 'ollama': {'base_urls': ['http://host.docker.internal:11434', 'https://cloud.invalid'], 'api_configs': {'0': {'key': 'fixture'}}}, 'openai': {'api_keys': ['fixture-provider'], 'base_urls': ['https://provider.invalid']}}
            with sqlite3.connect(source) as connection:
                connection.execute('CREATE TABLE config(id INTEGER PRIMARY KEY, data TEXT)')
                connection.execute('INSERT INTO config VALUES(1,?)', (json.dumps(settings),))
            root = directory / 'pvc'
            root.mkdir()
            program = llm.EXTRACT.replace("root=pathlib.Path('/data')", 'root=pathlib.Path(' + repr(str(root)) + ')')
            result = subprocess.run([sys.executable, '-c', program], input=archive_files({'webui.db': source.read_bytes()}), capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertTrue(receipt['signup_disabled'])
            self.assertTrue(receipt['ollama_connection_reviewed'])
            self.assertTrue(receipt['local_ollama_retargeted'])
            with sqlite3.connect(root / 'webui.db') as connection:
                copied = json.loads(connection.execute('SELECT data FROM config').fetchone()[0])
            self.assertEqual(copied['ollama']['base_urls'], ['http://ollama:11434', 'https://cloud.invalid'])
            self.assertEqual(copied['openai'], settings['openai'])
            self.assertEqual(copied['ollama']['api_configs'], settings['ollama']['api_configs'])
            with sqlite3.connect(source) as connection:
                self.assertEqual(json.loads(connection.execute('SELECT data FROM config').fetchone()[0]), settings)
            failed = directory / 'failed-commit'
            failed.mkdir()
            broken = llm.EXTRACT.replace("root=pathlib.Path('/data')", 'root=pathlib.Path(' + repr(str(failed)) + ')').replace('connection.commit()', "raise sqlite3.OperationalError('fixture commit failure')")
            failure = subprocess.run([sys.executable, '-c', broken], input=archive_files({'webui.db': source.read_bytes()}), capture_output=True, check=False)
            self.assertEqual(failure.returncode, 0, failure.stderr)
            failed_receipt = json.loads(failure.stdout)
            self.assertFalse(failed_receipt['ollama_connection_reviewed'])
            self.assertNotIn('local_ollama_retargeted', failed_receipt)
            with sqlite3.connect(failed / 'webui.db') as connection:
                self.assertEqual(json.loads(connection.execute('SELECT data FROM config').fetchone()[0]), settings)
            blocked = directory / 'blocked'
            blocked.mkdir()
            program = llm.EXTRACT.replace("root=pathlib.Path('/data')", 'root=pathlib.Path(' + repr(str(blocked)) + ')').replace('RESERVE=20*1024**3', 'RESERVE=2**100')
            result = subprocess.run([sys.executable, '-c', program], input=archive_files({'webui.db': source.read_bytes()}), capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((blocked / 'webui.db').exists())

    def storage_fixture(self):
        command = [str(ROOT / '.dev-tools/bin/helm'), 'template', 'llm-backup', 'charts/llm-backup', '-n', 'llm-backup', '-f', 'apps/llm-backup/values.yaml', '--set', 'ollama.image.digest=sha256:' + '0'*64, '--set', 'webui.image.digest=sha256:' + '0'*64]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True)
        return llm.storage_resources(result.stdout)

    def test_prepare_applies_only_storage_policy_kinds_with_helm_ownership(self):
        resources = self.storage_fixture()
        self.assertEqual({item['kind'] for item in resources}, {'PersistentVolumeClaim', 'NetworkPolicy', 'ResourceQuota'})
        self.assertNotIn('sha256:', json.dumps(resources))
        self.assertEqual(len(resources), 6)
        for item in resources:
            self.assertTrue(llm.owned(item))
            metadata = item['metadata']
            self.assertEqual(metadata['namespace'], 'llm-backup')
            self.assertEqual(metadata['annotations']['meta.helm.sh/release-name'], 'llm-backup')
            self.assertEqual(metadata['annotations']['meta.helm.sh/release-namespace'], 'llm-backup')
        calls = []
        with patch.object(llm, 'js', return_value={}), patch.object(llm, 'k', side_effect=lambda *args, **kwargs: calls.append((args, kwargs))):
            llm.prepare_storage(resources)
        manifests = [json.loads(kwargs['data']) for _, kwargs in calls]
        self.assertEqual(manifests[0]['kind'], 'Namespace')
        self.assertEqual(manifests[1]['kind'], 'List')
        self.assertEqual({item['kind'] for item in manifests[1]['items']}, {'PersistentVolumeClaim', 'NetworkPolicy', 'ResourceQuota'})

    def test_network_refresh_updates_only_owned_policies_without_stopping_workloads(self):
        resources = self.storage_fixture()
        owned_resource={'metadata':{'labels':{'app.kubernetes.io/managed-by':'Helm'},'annotations':{'meta.helm.sh/release-name':'llm-backup','meta.helm.sh/release-namespace':'llm-backup'}}}
        with patch.object(llm, 'js', return_value=owned_resource), patch.object(llm, 'run'), patch.object(llm, 'k') as mutate, patch.object(llm, 'cold_guard', side_effect=AssertionError('Running Ollama must not be stopped')):
            llm.refresh_network(resources)
            payload=json.loads(mutate.call_args.kwargs['data'])
            self.assertEqual(len(payload['items']),3)
            self.assertEqual({item['kind'] for item in payload['items']},{'NetworkPolicy'})
        with patch.object(llm, 'js', return_value={'metadata':{}}), patch.object(llm, 'k') as mutate:
            with self.assertRaisesRegex(llm.Refusal,'unmanaged namespace'):
                llm.refresh_network(resources)
            mutate.assert_not_called()

    def test_foreign_namespace_is_not_modified(self):
        with patch.object(llm, 'js', return_value={'metadata': {'name': 'llm-backup'}}), patch.object(llm, 'k') as mutate, patch.object(llm, 'cold_guard') as cold:
            with self.assertRaisesRegex(llm.Refusal, 'namespace is unmanaged'):
                llm.prepare_storage(self.storage_fixture())
            mutate.assert_not_called()
            cold.assert_not_called()

    def test_existing_claims_never_adopt_resize_or_reapply(self):
        resources = self.storage_fixture()
        claims = {item['metadata']['name']: item for item in resources if item['kind'] == 'PersistentVolumeClaim'}
        for mismatch in ('owner', 'size', 'class'):
            current = copy.deepcopy(claims['ollama-data'])
            if mismatch == 'owner':
                current['metadata']['labels'] = {}
            elif mismatch == 'size':
                current['spec']['resources']['requests']['storage'] = '401Gi'
            else:
                current['spec']['storageClassName'] = 'another-class'
            def lookup(*args):
                if args[1] == 'namespace': return {'metadata': {'name': 'llm-backup', 'labels': {'app.kubernetes.io/managed-by': 'Helm'}, 'annotations': {'meta.helm.sh/release-name': 'llm-backup', 'meta.helm.sh/release-namespace': 'llm-backup'}}}
                return current if args[1] == 'PersistentVolumeClaim' and args[2] == 'ollama-data' else {}
            with self.subTest(mismatch=mismatch), patch.object(llm, 'cold_guard'), patch.object(llm, 'js', side_effect=lookup), patch.object(llm, 'k') as mutate:
                with self.assertRaises(llm.Refusal):
                    llm.prepare_storage(resources)
                mutate.assert_not_called()
        def lookup(*args):
            if args[1] == 'namespace': return {'metadata': {'name': 'llm-backup', 'labels': {'app.kubernetes.io/managed-by': 'Helm'}, 'annotations': {'meta.helm.sh/release-name': 'llm-backup', 'meta.helm.sh/release-namespace': 'llm-backup'}}}
            return claims.get(args[2], {}) if args[1] == 'PersistentVolumeClaim' else {}
        with patch.object(llm, 'cold_guard'), patch.object(llm, 'js', side_effect=lookup), patch.object(llm, 'k') as mutate:
            llm.prepare_storage(resources)
            applied = json.loads(mutate.call_args.kwargs['data'])['items']
            self.assertFalse(any(item['kind'] == 'PersistentVolumeClaim' for item in applied))

    def test_storage_only_migration_scans_and_executes_only_copy_image(self):
        claim = {'metadata': {'uid': 'fixture-claim', 'labels': {'app.kubernetes.io/managed-by': 'Helm'}, 'annotations': {'meta.helm.sh/release-name': 'llm-backup', 'meta.helm.sh/release-namespace': 'llm-backup'}}}
        def lookup(*args):
            return claim if args[1] == 'pvc' else {}
        with patch.object(llm, 'cold_guard'), patch.object(llm, 'image_gate') as gate, patch.object(llm, 'deployed_images', side_effect=AssertionError('No application image needed for copying')), patch.object(llm, 'js', side_effect=lookup), patch.object(llm, 'k') as kube, patch.object(llm, 'webui_secrets', return_value={'WEBUI_SECRET_KEY': 'fixture'}), patch.object(llm, 'headroom'), patch.object(llm, 'preserve_secret'), patch.object(llm, 'source_stopped'), patch.object(llm, 'run'), patch.object(llm, 'copy_stream', return_value={'complete': True, 'files': 1, 'bytes': 1}):
            llm.migrate('webui')
            gate.assert_called_once_with(llm.COPY_IMAGE)
            creation = next(call for call in kube.call_args_list if call.args[0] == 'create')
            helper = json.loads(creation.kwargs['data'])
            self.assertEqual(helper['spec']['containers'][0]['image'], llm.COPY_IMAGE)
            self.assertEqual(helper['spec']['securityContext']['runAsUser'], 1000)

    def test_failed_copy_inventory_requires_exact_markers_and_preserves_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'cache').mkdir()
            (root / 'cache/fixture').write_bytes(b'partial cache bytes')
            (root / '.llm-backup-failed').touch()
            (root / '.llm-backup-copying').touch()
            code = llm.PARTIAL_INVENTORY.replace("root=pathlib.Path('/data')", 'root=pathlib.Path(' + repr(str(root)) + ')') + '\nprint(json.dumps(inventory()))'
            result = subprocess.run([sys.executable, '-c', code], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            files = json.loads(result.stdout)
            self.assertEqual(files['cache/fixture']['size'], len(b'partial cache bytes'))
            (root / 'webui.db').touch()
            result = subprocess.run([sys.executable, '-c', code], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((root / 'cache/fixture').read_bytes(), b'partial cache bytes')

    def test_recovery_refuses_a_completed_migration_before_pod_creation(self):
        with patch.object(llm, 'cold_guard'), patch.object(llm, 'source_stopped'), patch.object(llm, 'image_gate'), patch.object(llm, 'owned', return_value=True), patch.object(llm, 'js', side_effect=[{'metadata': {'uid': 'fixture'}}, {'data': {'webui': 'completed'}}]), patch.object(llm, 'k') as mutate:
            with self.assertRaisesRegex(llm.Refusal, 'receipt already exists'):
                llm.recover_webui_partial()
            mutate.assert_not_called()

    def test_internal_cache_links_survive_and_escaping_links_are_rejected(self):
        for linkname, allowed in [('blobs/model', True), ('../../outside', False), ('/etc/passwd', False)]:
            with self.subTest(target=linkname), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / 'pvc'
                root.mkdir()
                buffer = io.BytesIO()
                with tarfile.open(fileobj=buffer, mode='w') as bundle:
                    link = tarfile.TarInfo('cache/model-link')
                    link.type = tarfile.SYMTYPE
                    link.linkname = linkname
                    bundle.addfile(link)
                    payload = b'fixture weights'
                    member = tarfile.TarInfo('cache/blobs/model')
                    member.size = len(payload)
                    bundle.addfile(member, io.BytesIO(payload))
                program = llm.EXTRACT.replace("root=pathlib.Path('/data')", 'root=pathlib.Path(' + repr(str(root)) + ')')
                result = subprocess.run([sys.executable, '-c', program], input=buffer.getvalue(), capture_output=True)
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                if allowed:
                    self.assertTrue((root / 'cache/model-link').is_symlink())
                    self.assertEqual((root / 'cache/model-link').read_bytes(), payload)
                    self.assertEqual(json.loads(result.stdout)['links'], 1)
                else:
                    self.assertFalse((root / '.llm-backup-complete.json').exists())

    def test_stream_program_preserves_identity_and_refuses_nonempty_or_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'pvc'
            root.mkdir()
            program = llm.EXTRACT.replace("root=pathlib.Path('/data')", 'root=pathlib.Path(' + repr(str(root)) + ')')
            payload = archive_files({'id_ed25519': b'fixture identity bytes', 'models/blob': b'fixture model bytes'})
            result = subprocess.run(['python3', '-c', program], input=payload, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'id_ed25519').read_bytes(), b'fixture identity bytes')
            self.assertEqual((root / 'id_ed25519').stat().st_mode & 0o777, 0o600)
            self.assertTrue(json.loads(result.stdout)['complete'])
            again = subprocess.run(['python3', '-c', program], input=payload, capture_output=True, check=False)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual((root / 'id_ed25519').read_bytes(), b'fixture identity bytes')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'pvc'
            root.mkdir()
            program = llm.EXTRACT.replace("root=pathlib.Path('/data')", 'root=pathlib.Path(' + repr(str(root)) + ')')
            result = subprocess.run(['python3', '-c', program], input=archive_files({'../escape': b'fixture'}), capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / 'escape').exists())
            self.assertTrue((root / '.llm-backup-failed').exists())


if __name__ == '__main__':
    unittest.main()
