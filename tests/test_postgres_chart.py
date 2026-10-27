"""PostgreSQL persistence and isolation contracts; no running cluster required."""
import pathlib
import subprocess
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def render(*extra):
    return subprocess.run(
        ["helm", "template", "postgres-dev", "charts/postgres-dev", "--namespace",
         "postgres-dev", "-f", "apps/postgres-dev/values.yaml",
         "--set", "image.digest=sha256:" + "a" * 64, *extra],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )


class PostgresChart(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = render()
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.raw = result.stdout
        cls.docs = {doc["kind"]: doc for doc in yaml.safe_load_all(result.stdout)}
        cls.pod = cls.docs["Deployment"]["spec"]["template"]["spec"]
        cls.container = cls.pod["containers"][0]

    def test_persistent_singleton_and_bounded_resources(self):
        deployment = self.docs["Deployment"]["spec"]
        self.assertEqual(deployment["replicas"], 1)
        self.assertEqual(deployment["strategy"], {"type": "Recreate"})
        pvc = self.docs["PersistentVolumeClaim"]
        self.assertEqual(pvc["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertEqual(pvc["spec"]["resources"]["requests"]["storage"], "20Gi")
        mounts = {mount["name"]: mount["mountPath"] for mount in self.container["volumeMounts"]}
        self.assertEqual(mounts["data"], "/var/lib/postgresql")
        env = {item["name"]: item["value"] for item in self.container["env"]}
        self.assertEqual(env["PGDATA"], "/var/lib/postgresql/18/docker")
        self.assertEqual(self.container["resources"]["limits"]["memory"], "1Gi")
        self.assertEqual(self.docs["ResourceQuota"]["spec"]["hard"]["requests.storage"], "20Gi")

    def test_nonroot_readonly_with_only_bounded_writable_mounts(self):
        self.assertFalse(self.pod["automountServiceAccountToken"])
        security = self.pod["securityContext"]
        self.assertEqual((security["runAsUser"], security["runAsGroup"], security["fsGroup"]), (1000, 1000, 1000))
        self.assertEqual(security["fsGroupChangePolicy"], "OnRootMismatch")
        self.assertEqual(self.container["imagePullPolicy"], "Never")
        self.assertEqual(security["seccompProfile"]["type"], "RuntimeDefault")
        container_security = self.container["securityContext"]
        self.assertTrue(container_security["readOnlyRootFilesystem"])
        self.assertFalse(container_security["allowPrivilegeEscalation"])
        self.assertEqual(container_security["capabilities"]["drop"], ["ALL"])
        volumes = {volume["name"]: volume for volume in self.pod["volumes"]}
        self.assertEqual(volumes["tmp"]["emptyDir"]["sizeLimit"], "128Mi")
        self.assertEqual(volumes["shm"]["emptyDir"], {"medium": "Memory", "sizeLimit": "128Mi"})
        self.assertEqual(volumes["credentials"]["secret"]["defaultMode"], 0o440)
        for forbidden in ["hostPath:", "hostNetwork:", "privileged:", "kind: Secret", "nodePort:"]:
            self.assertNotIn(forbidden, self.raw)

    def test_password_files_and_unprivileged_development_role(self):
        env = {item["name"]: item["value"] for item in self.container["env"]}
        self.assertNotIn("POSTGRES_PASSWORD", env)
        self.assertEqual(env["POSTGRES_PASSWORD_FILE"], "/run/postgres-secrets/postgres-password")
        self.assertEqual(env["PGHOST"], "/var/run/postgresql")
        self.assertEqual(env["POSTGRES_INITDB_ARGS"], "--auth-host=scram-sha-256 --auth-local=peer")
        self.assertIn("unix_socket_directories=/var/run/postgresql", self.container["args"])
        sql = self.docs["ConfigMap"]["data"]["10-dev.sql"]
        self.assertIn("LOGIN CREATEDB NOSUPERUSER NOCREATEROLE NOREPLICATION PASSWORD %L", sql)
        self.assertIn("pg_read_file('/run/postgres-secrets/dev-password')", sql)
        self.assertIn("\\gexec", sql)
        self.assertIn("CREATE DATABASE dev OWNER dev;", sql)
        for probe in ["startupProbe", "readinessProbe"]:
            self.assertEqual(self.container[probe]["exec"]["command"],
                             ["pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "postgres"])

    def test_only_explicitly_labelled_clients_can_connect(self):
        service = self.docs["Service"]["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertEqual(service["ports"][0]["port"], 5432)
        policy = self.docs["NetworkPolicy"]["spec"]
        self.assertEqual(policy["podSelector"], {})
        self.assertEqual(set(policy["policyTypes"]), {"Ingress", "Egress"})
        self.assertEqual(policy["egress"], [])
        self.assertEqual(len(policy["ingress"]), 1)
        peers = policy["ingress"][0]["from"]
        self.assertEqual(peers, [{
            "namespaceSelector": {"matchLabels": {"platform.thedefiant.io/postgres-access": "true"}},
            "podSelector": {"matchLabels": {"platform.thedefiant.io/postgres-client": "true"}},
        }])

    def test_generated_image_overlay_required(self):
        result = subprocess.run(
            ["helm", "template", "postgres-dev", "charts/postgres-dev", "-f",
             "apps/postgres-dev/values.yaml"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest", result.stderr)

    def test_schema_rejects_major_upgrade_unpinning_and_expansion(self):
        for setting in ["majorVersion=19", "replicas=2", "port=5433", "storage=40Gi",
                        "storageClass=other", "secretName=other", "image.digest=latest",
                        "image.repository=other", "image.pullPolicy=Always",
                        "unexpected=true"]:
            with self.subTest(setting=setting):
                result = render("--set", setting)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("schema", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
