"""Offline standby LLM deployment contracts; image digests are synthetic fixtures."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def render(*extra):
    return subprocess.run(
        ["helm", "template", "llm-backup", "charts/llm-backup", "-n", "llm-backup",
         "-f", "apps/llm-backup/values.yaml", "--set-string", "ollama.image.digest=sha256:" + "a" * 64,
         "--set-string", "webui.image.digest=sha256:" + "b" * 64, *extra],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )


class StandbyLLM(unittest.TestCase):
    def test_standby_and_retained_storage(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        source = result.stdout
        self.assertEqual(source.count("replicas: 0"), 2)
        self.assertEqual(source.count("type: Recreate"), 2)
        self.assertEqual(source.count("kind: PersistentVolumeClaim"), 2)
        self.assertEqual(source.count("helm.sh/resource-policy: keep"), 2)
        for expected in ["storage: 400Gi", "storage: 20Gi", "requests.storage: 420Gi",
                         "name: ollama-data", "name: webui-data", "kind: ResourceQuota"]:
            self.assertIn(expected, source)

    def test_security_and_existing_secret(self):
        source = render().stdout
        for expected in ["runAsUser: 1000", "fsGroup: 1000", "runAsNonRoot: true",
                         "readOnlyRootFilesystem: true", "allowPrivilegeEscalation: false",
                         "drop: [ALL]", "type: RuntimeDefault", "automountServiceAccountToken: false"]:
            self.assertEqual(source.count(expected), 2)
        for expected in ["name: WEBUI_SECRET_KEY", "secretKeyRef:", "envFrom:", "secretRef:", "name: llm-backup-webui",
                         "name: ENABLE_SIGNUP", 'value: "false"', "value: /data/.ollama/models",
                         "value: /app/backend/data", 'value: "http://ollama:11434"',
                         "runtimeClassName: nvidia", "nvidia.com/gpu: 1", "path: /api/version", "path: /health"]:
            self.assertIn(expected, source)
        for forbidden in ["hostPath:", "hostPort:", "privileged: true", "kind: Secret",
                          "type: NodePort", "type: LoadBalancer", "OLLAMA_NO_CLOUD"]:
            self.assertNotIn(forbidden, source)

    def test_identity_auth_and_capacity_defaults(self):
        source = render().stdout
        for expected in ["mountPath: /data/.ollama", "value: /data/.ollama/models",
                         "value: /data", "memory: 96Gi", "limits.memory: 104Gi",
                         "name: OLLAMA_NUM_PARALLEL", "name: OLLAMA_MAX_LOADED_MODELS"]:
            self.assertIn(expected, source)
        self.assertRegex(source, r'name: WEBUI_AUTH\s+value: "true"')
        self.assertRegex(source, r'name: OLLAMA_NUM_PARALLEL\s+value: "1"')
        self.assertRegex(source, r'name: OLLAMA_MAX_LOADED_MODELS\s+value: "1"')

    def test_network_boundary_and_optional_downloads(self):
        source = render().stdout
        self.assertEqual(source.count("kind: NetworkPolicy"), 3)
        for expected in ["kubernetes.io/metadata.name: docs-dev", "kubernetes.io/metadata.name: docs-prod",
                         "app.kubernetes.io/instance: docs", "port: 11434", "k8s-app: kube-dns",
                         "podSelector: {}", "ingress: []", "egress: []",
                         "port: 443", "169.254.0.0/16", "100.64.0.0/10", "192.168.0.0/16"]:
            self.assertIn(expected, source)
        disabled = render("--set", "ollama.internetHTTPS=false")
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertNotIn("cidr: 0.0.0.0/0", disabled.stdout)

    def test_ollama_gateway_ingress_requires_namespace_and_docs_pod_together(self):
        import yaml
        policies=[item for item in yaml.safe_load_all(render().stdout) if item and item.get('kind')=='NetworkPolicy']
        policy=next(item for item in policies if item['metadata']['name']=='llm-backup-ollama')
        rule=policy['spec']['ingress'][0]
        self.assertEqual(rule['ports'],[{'protocol':'TCP','port':11434}])
        peers=[peer for peer in rule['from'] if 'namespaceSelector' in peer]
        self.assertEqual(len(peers),2)
        self.assertEqual({peer['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'] for peer in peers},{'docs-dev','docs-prod'})
        for peer in peers:
            self.assertEqual(peer['podSelector']['matchLabels'],{'app.kubernetes.io/instance':'docs'})

    def test_activation_and_gpu_guards(self):
        active = render("--set", "ollama.replicas=1", "--set", "webui.replicas=1")
        self.assertEqual(active.returncode, 0, active.stderr)
        self.assertEqual(active.stdout.count("replicas: 1"), 2)
        for setting in ["ollama.replicas=2", "webui.replicas=-1", "ollama.runtimeClass=runc",
                        "ollama.resources.limits.nvidia\\.com/gpu=0", "webui.secretName=",
                        "webui.secretName=another-secret",
                        "webui.resources.limits.memory=invalid"]:
            with self.subTest(setting=setting):
                invalid = render("--set", setting)
                self.assertNotEqual(invalid.returncode, 0)
                self.assertIn("schema", invalid.stderr.lower())

    def test_missing_digests_fail(self):
        result = subprocess.run(["helm", "template", "llm-backup", "charts/llm-backup"],
                                cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest", result.stderr)
        mutable = render("--set-string", "ollama.image.digest=latest")
        self.assertNotEqual(mutable.returncode, 0)
        self.assertIn("digest", mutable.stderr)


if __name__ == "__main__":
    unittest.main()
