"""Exercise public command guards without a cluster or host mutations."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PlatformGuards(unittest.TestCase):
    def test_make_environment_cannot_execute_shell_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'unexpected'
            result = subprocess.run(['make', 'render', 'ENV=dev; touch ' + str(marker)],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def run_cli(self, *args, env=None):
        return subprocess.run(["bash", str(ROOT / "scripts/platform.sh"), *args],
                              env=env, text=True, capture_output=True)

    def test_unknown_environment_rejected(self):
        self.assertEqual(self.run_cli("deploy", "default").returncode, 2)

    def test_missing_dedicated_config_rejected(self):
        env = dict(os.environ, PLATFORM_KUBECONFIG="/nonexistent/platform-kubeconfig")
        result = self.run_cli("deploy", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing dedicated kubeconfig", result.stderr)

    def test_status_lists_all_namespaces_and_docs_status_keeps_selected_environment(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / 'config'
            config.touch()
            log = path / 'calls.jsonl'
            kubectl = path / 'kubectl'
            kubectl.write_text('#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\nwith Path(' + repr(str(log)) + ').open("a") as stream:stream.write(json.dumps(sys.argv[1:])+"\\n")\nprint("https://127.0.0.1:6443" if "config" in sys.argv else "fixture")\n')
            kubectl.chmod(0o755)
            env = dict(os.environ, PLATFORM_KUBECONFIG=str(config), PATH=str(path)+os.pathsep+os.environ['PATH'])
            for action in ('status','docs-status'):
                with self.subTest(action=action):
                    log.write_text('')
                    result = self.run_cli(action, 'prod', env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls=[json.loads(line) for line in log.read_text().splitlines()]
                    resources=next(args for args in calls if any('deployments,' in arg for arg in args))
                    if action=='status':
                        self.assertIn('-A',resources)
                        self.assertTrue(any('daemonsets' in arg for arg in resources))
                        self.assertNotIn('docs-prod',resources)
                    else:
                        self.assertIn('docs-prod',resources)
                        self.assertNotIn('-A',resources)

    def test_wrong_endpoint_never_invokes_helm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            config = path / "config"
            config.touch()
            kubectl = path / "kubectl"
            kubectl.write_text("#!/bin/sh\nprintf 'https://production.example:6443'\n")
            kubectl.chmod(0o755)
            marker = path / "helm-invoked"
            helm = path / "helm"
            helm.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            helm.chmod(0o755)
            env = dict(os.environ, PLATFORM_KUBECONFIG=str(config),
                       PATH=str(path) + os.pathsep + os.environ["PATH"])
            result = self.run_cli("deploy", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing cluster", result.stderr)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
