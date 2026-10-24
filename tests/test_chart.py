"""Dependency-free Helm contract tests; run python3 -m unittest discover -s tests."""
import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def render(*extra, release="docs", namespace="docs-dev"):
    return subprocess.run(
        ["helm", "template", release, "charts/private-app", "--namespace", namespace,
         "-f", "apps/docs/values.yaml", *extra],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )


class ChartContract(unittest.TestCase):
    def test_private_hardened_docs(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ["type: ClusterIP", "automountServiceAccountToken: false",
                         "runAsNonRoot: true", "readOnlyRootFilesystem: true",
                         "allowPrivilegeEscalation: false", "RuntimeDefault",
                         "kind: ResourceQuota", "TheDefiant Home Platform",
                         "checksum/config:", "@sha256:", "namespace: docs-dev",
                         "kind: NetworkPolicy", "ingress: []", "k8s-app: kube-dns"]:
            self.assertIn(expected, result.stdout)
        self.assertGreaterEqual(result.stdout.count("runAsNonRoot: true"), 2)
        for forbidden in ["hostPort:", "hostNetwork:", "type: NodePort", "type: LoadBalancer",
                          "kind: PersistentVolumeClaim", "kind: Ingress"]:
            self.assertNotIn(forbidden, result.stdout)

    def test_prod_uses_identical_content_and_image(self):
        development = render(namespace="docs-prod")
        production = render("-f", "apps/docs/values-prod.yaml", namespace="docs-prod")
        self.assertEqual(production.returncode, 0, production.stderr)
        self.assertEqual(development.stdout, production.stdout)

    def test_separate_release_and_fixture_storage(self):
        result = render("-f", "apps/docs/values-fixture.yaml", release="fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ["name: fixture", "kind: PersistentVolumeClaim", "helm.sh/resource-policy: keep",
                         "type: Recreate", "fsGroup: 65532", "containerPort: 8081",
                         'mountPath: "/data"', 'path: "/fixture.txt"']:
            self.assertIn(expected, result.stdout)
        self.assertIn("kind: ConfigMap", result.stdout)
        self.assertIn("listen 8081; root /data", result.stdout)

    def test_content_change_updates_rollout_checksum(self):
        first = render().stdout
        second = render("--set-string", "config.files.index\\.html=changed").stdout
        checksum = lambda output: next(line for line in output.splitlines() if "checksum/config:" in line)
        self.assertNotEqual(checksum(first), checksum(second))

    def test_invalid_settings_fail_schema_validation(self):
        for setting in ["image.digest=latest", "image.repository=", "containerPort=0",
                        "containerPort=65536", "resources.requests.memory=oops",
                        "securityContext.runAsUser=0", "securityContext.allowPrivilegeEscalation=true",
                        "probes.periodSeconds=0", "config.mountPath=relative"]:
            with self.subTest(setting=setting):
                result = render("--set", setting)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("schema", result.stderr.lower())

    def test_missing_image_rejected(self):
        result = subprocess.run(["helm", "template", "missing", "charts/private-app"],
                                cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image", result.stderr)


if __name__ == "__main__":
    unittest.main()
