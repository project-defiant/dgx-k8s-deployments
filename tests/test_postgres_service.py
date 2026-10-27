"""Render the boot service through its public CLI; never install a host unit."""
import getpass
import importlib.util
import json
import os
from types import SimpleNamespace
import tempfile
from unittest import mock
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ServiceCommands(unittest.TestCase):
    def test_root_owner_error_explains_make_must_not_be_run_with_sudo(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/postgres-service.sh'),
                                 'install', 'root', '/tmp/admin'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('without sudo', result.stderr)
        self.assertIn('make postgres-service-install', result.stderr)

    def render(self, *extra):
        return subprocess.run(["bash", str(ROOT / "scripts/postgres-service.sh"), "render",
                               getpass.getuser(), "/tmp/platform.yaml", *extra],
                              capture_output=True, text=True)

    def test_boot_unit_is_private_nonroot_and_independent_of_checkout(self):
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ["User=" + getpass.getuser(), "After=k3s.service", "Restart=always",
                     "WantedBy=multi-user.target", "--address 127.0.0.1", "5432:5432",
                     "--context spark-platform", "-n postgres-dev"]:
            self.assertIn(text, result.stdout)
        self.assertNotIn(str(ROOT), result.stdout)
        self.assertNotIn("@", result.stdout)

    def test_forwarder_never_receives_admin_config_and_rbac_is_scoped(self):
        result = self.render()
        forward = result.stdout.split('# Managed by TheDefiant postgres-service.sh')[1]
        self.assertNotIn('/tmp/platform.yaml', result.stdout)
        self.assertIn('--kubeconfig /var/lib/spark-postgres-access/kubeconfig', forward)
        self.assertIn('Wants=k3s.service spark-postgres-credentials.service', forward)
        self.assertNotIn('Requires=spark-postgres-credentials.service', forward)
        self.assertIn('ProtectHome=true', forward)
        self.assertIn('ExecStart=/usr/local/bin/k3s kubectl ', forward)
        self.assertIn('RuntimeMaxSec=11h', forward)
        self.assertNotIn('/usr/bin/kubectl', forward)
        self.assertIn('Persistent=true', result.stdout)
        self.assertIn('OnCalendar=*-*-* 00,12:00:00', result.stdout)
        self.assertIn('resourceNames: ["postgres"]', result.stdout)
        self.assertNotIn('resources: ["secrets"]', result.stdout)
        self.assertNotIn('ClusterRole', result.stdout)
        self.assertIn('resources: ["pods/portforward"]', result.stdout)
        self.assertIn('verbs: ["get", "create"]', result.stdout)

    def test_reject_unsafe_paths_and_root(self):
        for owner, config in [('root', '/tmp/admin'), (getpass.getuser(), '/tmp/../admin'),
                              (getpass.getuser(), '/tmp/config%bad')]:
            result = subprocess.run(['bash', str(ROOT / 'scripts/postgres-service.sh'),
                                     'render', owner, config], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def test_refresh_writes_only_restricted_config_and_no_token_arguments(self):
        spec = importlib.util.spec_from_file_location('refresh', ROOT / 'platform/postgres-access/refresh-credentials.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'config'
            state = Path(directory) / 'state'
            base.mkdir()
            state.mkdir()
            (base / 'config.json').write_text(json.dumps({'managed': 'TheDefiant postgres-service.sh', 'environment': 'dev', 'uid': 1000, 'gid': 1000}))
            outputs = [subprocess.CompletedProcess([], 0, json.dumps({'clusters': [{'cluster': {
                'certificate-authority-data': 'PUBLIC-CA', 'client-key-data': 'ADMIN-MUST-NOT-COPY'}}]})),
                       subprocess.CompletedProcess([], 0, 'fixture-short-lived-token')]
            with mock.patch.object(module, 'BASE', base), mock.patch.object(module, 'STATE', state), \
                 mock.patch.object(module, 'trusted'), mock.patch.object(module.os, 'fchown') as chown, \
                 mock.patch.object(module.subprocess, 'run', side_effect=outputs) as run:
                module.refresh()
            content = (state / 'kubeconfig').read_text()
            self.assertIn('fixture-short-lived-token', content)
            self.assertNotIn('ADMIN-MUST-NOT-COPY', content)
            self.assertEqual((state / 'kubeconfig').stat().st_mode & 0o777, 0o640)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertNotIn('fixture-short-lived-token', str(commands))
            self.assertIn('--duration=24h', commands[1])
            self.assertEqual(chown.call_args.args[1:], (0, 1000))
            self.assertIn('postgres-dev', commands[1])
            self.assertIn('postgres-forwarder', commands[1])
            self.assertEqual(commands[0][0], '/usr/local/bin/k3s')

    def test_managed_reinstall_guard(self):
        spec = importlib.util.spec_from_file_location('install_guard', ROOT / 'platform/postgres-access/check-install.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            base, state = Path(directory) / 'config', Path(directory) / 'state'
            base.mkdir(mode=0o700)
            state.mkdir(mode=0o711)
            config = base / 'config.json'
            helper = base / 'refresh-credentials.py'
            config.write_text(json.dumps({'managed': module.MARKER, 'uid': os.getuid(), 'environment': 'dev', 'gid': os.getgid()}))
            config.chmod(0o600)
            helper.write_text('#!/usr/bin/python3\n# Managed by ' + module.MARKER + '\n')
            helper.chmod(0o700)
            original = Path.lstat
            def owned(path):
                info = original(path)
                mode = info.st_mode if path.is_relative_to(Path(directory)) else info.st_mode & ~0o022
                return SimpleNamespace(st_uid=os.getuid(), st_gid=os.getgid(), st_mode=mode)
            with mock.patch.object(Path, 'lstat', owned):
                module.check(base, state, os.getuid(), 'dev', os.getgid(), root_uid=os.getuid())
                with self.assertRaisesRegex(ValueError, 'migration'):
                    module.check(base, state, os.getuid(), 'prod', os.getgid(), root_uid=os.getuid())
                config.chmod(0o644)
                with self.assertRaisesRegex(ValueError, 'permissions'):
                    module.check(base, state, os.getuid(), 'dev', os.getgid(), root_uid=os.getuid())
                config.chmod(0o600)
                (state / 'unexpected').touch()
                with self.assertRaisesRegex(ValueError, 'Unexpected'):
                    module.check(base, state, os.getuid(), 'dev', os.getgid(), root_uid=os.getuid())
                (state / 'unexpected').unlink()
                config.unlink()
                config.symlink_to(helper)
                with self.assertRaisesRegex(ValueError, 'ownership, type'):
                    module.check(base, state, os.getuid(), 'dev', os.getgid(), root_uid=os.getuid())

    def test_upgrade_stops_only_managed_units_after_validation_before_mutation(self):
        source = (ROOT / 'scripts/postgres-service.sh').read_text()
        guard = source.index('"$repo/platform/postgres-access/check-install.py"')
        stop = source.index('systemctl stop "$name"')
        mutate = source.index('--request-timeout=30s apply -f')
        self.assertLess(guard, stop)
        self.assertLess(stop, mutate)
        self.assertIn('if [[ -f /etc/systemd/system/$name ]]; then', source)
        self.assertLess(source.index('Refusing to overwrite an unmanaged unit'), stop)



if __name__ == "__main__":
    unittest.main()
