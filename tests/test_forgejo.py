"""Dependency-free Forgejo rendering and security regression contracts."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def render(*extra):
    return subprocess.run(
        ["helm", "template", "forgejo", "charts/forgejo", "--namespace", "forgejo",
         "-f", "apps/forgejo/values.yaml",
         "--set-string", "image.digest=sha256:" + "a" * 64, *extra], cwd=ROOT,
        text=True, capture_output=True, check=False,
    )


class ForgejoContract(unittest.TestCase):
    def test_private_hardened_render(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ["namespace: forgejo", "type: ClusterIP", "type: Recreate",
                         "runAsNonRoot: true", "runAsUser: 1000", "fsGroup: 1000",
                         "automountServiceAccountToken: false", "readOnlyRootFilesystem: true",
                         "allowPrivilegeEscalation: false", "RuntimeDefault", "drop: [ALL]",
                         "http://git.spark.test:8088/", "imagePullPolicy: Never",
                         "local.thedefiant/forgejo@sha256:", "FORGEJO__service__DISABLE_REGISTRATION",
                         "FORGEJO__service__REQUIRE_SIGNIN_VIEW", "FORGEJO__server__DISABLE_SSH",
                         "FORGEJO__packages__ENABLED", "FORGEJO__actions__ENABLED", "FORGEJO__database__DB_TYPE",
                         "FORGEJO__service__ENABLE_REVERSE_PROXY_AUTHENTICATION",
                         'value: "10.84.0.0/16"', "@sha256:", "startupProbe:"]:
            self.assertIn(expected, result.stdout)
        for forbidden in ["type: NodePort", "type: LoadBalancer", "hostPath:", "hostPort:",
                          "privileged: true", "name: FORGEJO__security__INSTALL_LOCK"]:
            self.assertNotIn(forbidden, result.stdout)

    def test_retained_data_claim_contains_all_persistent_state(self):
        source = render().stdout
        self.assertEqual(source.count("kind: PersistentVolumeClaim"), 1)
        self.assertEqual(source.count("helm.sh/resource-policy: keep"), 1)
        for expected in ["name: forgejo-data", "storage: 20Gi", "mountPath: /var/lib/gitea",
                         "mountPath: /tmp", "kind: ResourceQuota"]:
            self.assertIn(expected, source)

        self.assertNotIn("mountPath: /etc/gitea", source)
        self.assertNotIn("name: forgejo-config", source)

    def test_generated_digest_is_required(self):
        result = subprocess.run(
            ["helm", "template", "forgejo", "charts/forgejo", "--namespace", "forgejo",
             "-f", "apps/forgejo/values.yaml"], cwd=ROOT,
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest", result.stderr)

    def test_gateway_only_ingress_and_dns_egress(self):
        source = render().stdout
        for expected in ["kind: NetworkPolicy", "policyTypes: [Ingress, Egress]",
                         "kubernetes.io/metadata.name: docs-dev",
                         "kubernetes.io/metadata.name: docs-prod",
                         "app.kubernetes.io/instance: docs", "port: 3000", "k8s-app: kube-dns"]:
            self.assertIn(expected, source)
        self.assertNotIn("ipBlock:", source)

    def test_schema_blocks_mutable_image_and_invalid_configuration(self):
        for setting in ["image.digest=latest", "image.repository=untrusted.example/app",
                        "image.repository=codeberg.org/forgejo/forgejo", "image.pullPolicy=Always",
                        "rootURL=git.spark.test", "trustedProxyCIDR=*",
                        "resources.requests.memory=invalid"]:
            with self.subTest(setting=setting):
                result = render("--set-string", setting)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
