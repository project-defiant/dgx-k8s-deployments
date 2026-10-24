"""Regression checks for private legacy defaults and removed host coupling."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LegacySecurity(unittest.TestCase):
    def test_legacy_private_runtime_defaults(self):
        source = (ROOT / "gemma4-31b.yaml").read_text()
        for expected in ["type: ClusterIP", "automountServiceAccountToken: false",
                         "allowPrivilegeEscalation: false", "drop: [ALL]",
                         "type: RuntimeDefault", "image: docker.io/vllm/vllm-openai@sha256:"]:
            self.assertIn(expected, source)
        for removed in ["hostPath:", "localtime", "--from-literal", "type: LoadBalancer"]:
            self.assertNotIn(removed, source)


if __name__ == "__main__":
    unittest.main()
