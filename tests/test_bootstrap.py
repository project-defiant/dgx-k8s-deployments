"""Public CLI safety guards; never invoke the installer with authorization flags."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class BootstrapGuards(unittest.TestCase):
    def run_script(self, script, *args):
        return subprocess.run([str(ROOT / 'scripts' / script), *args], text=True, capture_output=True)

    def test_help_has_no_preflight_or_download(self):
        result = self.run_script('platform-bootstrap.sh', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--preservation-reviewed', result.stdout)

    def test_install_requires_explicit_preservation_and_network_review(self):
        for args in [('--install',), ('--install', '--preservation-reviewed'), ('--install', '--network-reviewed')]:
            with self.subTest(args=args):
                result = self.run_script('platform-bootstrap.sh', *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Installation requires', result.stderr)

    def test_unknown_options_fail_closed(self):
        for script in ['platform-bootstrap.sh', 'platform-preflight.sh']:
            result = self.run_script(script, '--not-supported')
            self.assertEqual(result.returncode, 2)

    def test_default_and_dry_run_do_not_download_or_install(self):
        # Isolate repo assets and substitute only the read-only environment inventory.
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / 'scripts').mkdir()
            shutil.copytree(ROOT / 'platform' / 'k3s', root / 'platform' / 'k3s')
            code = (ROOT / 'scripts' / 'platform-bootstrap.sh').read_text()
            # A validation test must not depend on the real host having no K3s.
            for prefix in ['/var/lib/rancher', '/etc/rancher', '/etc/systemd', '/run/systemd', '/usr/lib/systemd', '/lib/systemd', '/usr/local/bin', '/usr/local/sbin']:
                code = code.replace(prefix, str(root / prefix.lstrip('/')))
            script = root / 'scripts' / 'platform-bootstrap.sh'
            script.write_text(code)
            script.chmod(0o755)
            preflight = root / 'scripts' / 'platform-preflight.sh'
            preflight.write_text('#!/bin/sh\nexit 0\n')
            preflight.chmod(0o755)
            for args in [[], ['--dry-run'], ['--validate']]:
                result = subprocess.run([str(root / 'scripts' / 'platform-bootstrap.sh'), *args], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('Validation only; no downloads or host changes', result.stdout)

    def test_unmanaged_state_and_systemd_overrides_fail_without_mutation(self):
        for fixture_path in ['var/lib/rancher/k3s', 'etc/systemd/system/k3s.service.d', 'run/systemd/system/k3s.service']:
            with self.subTest(path=fixture_path), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                (root / 'scripts').mkdir()
                shutil.copytree(ROOT / 'platform' / 'k3s', root / 'platform' / 'k3s')
                code = (ROOT / 'scripts' / 'platform-bootstrap.sh').read_text()
                # Private fixture rewriting, never a production root/path override.
                for prefix in ['/var/lib/rancher', '/etc/rancher', '/etc/systemd', '/run/systemd', '/usr/lib/systemd', '/lib/systemd', '/usr/local/bin', '/usr/local/sbin']:
                    code = code.replace(prefix, str(root / prefix.lstrip('/')))
                script = root / 'scripts' / 'platform-bootstrap.sh'
                script.write_text(code)
                script.chmod(0o755)
                preflight = root / 'scripts' / 'platform-preflight.sh'
                preflight.write_text('#!/bin/sh\nexit 0\n')
                preflight.chmod(0o755)
                fixture = root / fixture_path
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text('preserve this unmanaged state')
                result = subprocess.run([str(script), '--validate'], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue('Unmanaged systemd override' in result.stderr or 'not a complete managed installation' in result.stderr)
                self.assertEqual(fixture.read_text(), 'preserve this unmanaged state')


class FirewallGuards(unittest.TestCase):
    def test_apply_is_idempotent_and_remove_preserves_other_rules(self):
        import json
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / 'state.json'
            initial = {tool: {'INPUT': [['-p', 'tcp', '--dport', '22', '-j', 'ACCEPT']]} for tool in ['iptables', 'ip6tables']}
            state.write_text(json.dumps(initial))
            mock = root / 'iptables'
            mock.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p=pathlib.Path(os.environ['MOCK_STATE']); all=json.loads(p.read_text())
s=all[pathlib.Path(sys.argv[0]).name]; a=sys.argv[1:]
if a[0]=='-w': a=a[1:]
op,chain,*rule=a
if op=='-S':
 if chain not in s: sys.exit(1)
 for r in s[chain]: print('-A',chain,*r)
elif op=='-N': s[chain]=[]
elif op=='-C': sys.exit(0 if rule in s.get(chain,[]) else 1)
elif op=='-A': s[chain].append(rule)
elif op=='-I': s[chain].insert(int(rule[0])-1,rule[1:])
elif op=='-D': s[chain].remove(rule)
elif op=='-F': s[chain]=[]
elif op=='-X': del s[chain]
else: sys.exit(2)
p.write_text(json.dumps(all))
''')
            mock.chmod(0o755)
            (root / 'ip6tables').symlink_to(mock)
            # Exercise a copy with only the root identity guard removed; every firewall
            # command is a stateful mock, so tests never mutate the real host.
            script = root / 'firewall.sh'
            script.write_text((ROOT / 'scripts' / 'platform-firewall.sh').read_text().replace('[[ $EUID == 0 ]]', 'true'))
            script.chmod(0o755)
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}', MOCK_STATE=str(state))
            def run(action):
                result = subprocess.run([str(script), action], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            run('--apply')
            once = state.read_text()
            run('--apply')
            self.assertEqual(once, state.read_text())
            applied = json.loads(once)
            for tool in applied:
                self.assertEqual(applied[tool]['INPUT'][0], ['-j', 'TD-SPARK-IN'])
                self.assertIn(['-p', 'tcp', '-m', 'multiport', '--dports', '6443,10250', '-j', 'DROP'], applied[tool]['TD-SPARK-IN'])
            # Foreign early RETURN must fail without mutating either family.
            poisoned = json.loads(once)
            poisoned['ip6tables']['TD-SPARK-IN'].insert(0, ['-j', 'RETURN'])
            state.write_text(json.dumps(poisoned))
            result = subprocess.run([str(script), '--apply'], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Unexpected ip6tables', result.stderr)
            self.assertEqual(json.loads(state.read_text()), poisoned)
            state.write_text(once)
            run('--remove')
            self.assertEqual(json.loads(state.read_text()), initial)
            run('--remove')
            self.assertEqual(json.loads(state.read_text()), initial)


if __name__ == '__main__':
    unittest.main()
