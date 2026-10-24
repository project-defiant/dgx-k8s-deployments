#!/usr/bin/env python3
"""Live smoke checks on the dedicated cluster; uses only disposable fixture data."""
import hashlib
import json
import os
from pathlib import Path
import socket
# Commands use separated argv and the explicit cluster guard, never a shell.
import subprocess  # nosec B404
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONFIG = os.environ.get("PLATFORM_KUBECONFIG", str(Path.home() / ".kube/spark-platform.yaml"))
KUBE = ["kubectl", "--kubeconfig", CONFIG, "--context", "spark-platform", "--request-timeout=15s"]
HELM = ["helm", "--kubeconfig", CONFIG, "--kube-context", "spark-platform"]
CHART = str(ROOT / "charts/private-app")
VALUES = str(ROOT / "apps/docs/values.yaml")


def run(args, *, data=None, check=True):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=240)  # nosec B603
    if check and result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr}\n{result.stdout}")
    return result


def k(*args, **kwargs):
    return run(KUBE + list(args), **kwargs)


def wait_deploy(namespace, release):
    k("-n", namespace, "rollout", "status", f"deployment/{release}", "--timeout=120s")


def get_page(namespace, release, path="/"):
    # Ask the OS for an unused localhost port, then let kubectl bind it.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(KUBE + ["-n", namespace, "port-forward", "--address", "127.0.0.1",  # nosec B603
                                          f"service/{release}", f"{port}:80"], stdout=log, stderr=log)
        try:
            for _ in range(60):
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read().decode())
                try:
                    # The scheme/host are constant and the port is allocated locally.
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:  # nosec B310
                        return response.read().decode()
                except OSError:
                    time.sleep(0.25)
            raise RuntimeError("port-forward did not become reachable")
        finally:
            process.terminate()
            process.wait(timeout=10)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    # Reuse the operational wrapper's context/endpoint/node guard.
    run([str(ROOT / "scripts/platform.sh"), "status", "dev"])
    baseline = get_page("docs-dev", "docs")
    require("TheDefiant Home Platform" in baseline, "Development docs content mismatch")
    print("PASS dev HTTP via localhost port-forward", flush=True)
    run(HELM + ["upgrade", "--install", "docs", CHART, "-n", "docs-prod", "--create-namespace",
                "-f", VALUES, "-f", str(ROOT / "apps/docs/values-prod.yaml"), "--wait", "--timeout", "180s"])
    require(get_page("docs-prod", "docs") == baseline, "Production docs content differs")
    print("PASS production has identical content", flush=True)

    dns = k("-n", "docs-dev", "exec", "deploy/docs", "--", "nslookup", "kubernetes.default.svc.cluster.local")
    require("10.85.0.1" in dns.stdout, "Cluster DNS response mismatch")
    policy = k("-n", "docs-dev", "get", "networkpolicy/docs", "-o", "name", check=False)
    if policy.returncode == 0:
        print("PASS pod DNS; restricted docs do not require API egress. Verify policy enforcement separately.", flush=True)
    else:
        api = k("-n", "docs-dev", "exec", "deploy/docs", "--", "wget", "--no-check-certificate",
                "-T", "3", "--server-response", "--spider", "https://kubernetes.default.svc/version", check=False)
        require("401" in api.stderr or "403" in api.stderr or api.returncode == 0, "Pod cannot reach cluster API")
        print("PASS pod DNS and API connectivity (no service-account token mounted)", flush=True)

    history = json.loads(run(HELM + ["history", "docs", "-n", "docs-dev", "-o", "json"]).stdout)
    revision = str(history[-1]["revision"])
    with tempfile.TemporaryDirectory() as directory:
        override = Path(directory) / "revision.json"
        override.write_text(json.dumps({"config": {"files": {"index.html": baseline + "\n<!-- rollout-proof -->\n"}}}))
        try:
            run(HELM + ["upgrade", "docs", CHART, "-n", "docs-dev", "-f", VALUES,
                        "-f", str(override), "--wait", "--timeout", "180s"])
            require("rollout-proof" in get_page("docs-dev", "docs"), "Rollout marker missing")
        finally:
            run(HELM + ["rollback", "docs", revision, "-n", "docs-dev", "--wait", "--timeout", "180s"])
    require(get_page("docs-dev", "docs") == baseline, "Rollback did not restore original content")
    print("PASS content rollout and rollback", flush=True)

    oversized = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "quota-smoke", "namespace": "docs-dev"},
                 "spec": {"containers": [{"name": "test", "image": "unused.invalid/test",
                                          "resources": {"requests": {"cpu": "2", "memory": "128Mi"},
                                                        "limits": {"cpu": "2", "memory": "256Mi"}}}]}}
    quota = k("create", "--dry-run=server", "-f", "-", data=json.dumps(oversized), check=False)
    require(quota.returncode and "exceeded quota" in quota.stderr, "Oversized request not rejected by quota")
    print("PASS oversized workload rejected by namespace quota (server dry-run)", flush=True)

    # Separate namespace and release; never modify real application data.
    run(HELM + ["upgrade", "--install", "fixture", CHART, "-n", "platform-verify", "--create-namespace",
                "-f", VALUES, "-f", str(ROOT / "apps/docs/values-fixture.yaml")])
    k("-n", "platform-verify", "wait", "--for=jsonpath={.status.phase}=Running", "pod",
      "-l", "app.kubernetes.io/instance=fixture", "--timeout=120s")
    payload = "TheDefiant persistent fixture\n"
    k("-n", "platform-verify", "exec", "deploy/fixture", "--", "sh", "-c",
      "set -eu; test ! -e /data/fixture.txt || test \"$(cat /data/fixture.txt)\" = 'TheDefiant persistent fixture'; printf '%s\\n' 'TheDefiant persistent fixture' > /data/fixture.txt")
    wait_deploy("platform-verify", "fixture")
    require(get_page("platform-verify", "fixture", "/fixture.txt") == payload, "Fixture content mismatch")
    k("-n", "platform-verify", "rollout", "restart", "deploy/fixture")
    wait_deploy("platform-verify", "fixture")
    require(get_page("platform-verify", "fixture", "/fixture.txt") == payload, "Fixture data lost after restart")
    print("PASS second release, alternative port, persistent volume and pod replacement: SHA256=" +
          hashlib.sha256(payload.encode()).hexdigest(), flush=True)
    print("Not tested: independent remote client/firewall, off-machine restore, host reboot. Fixture retained.", flush=True)


if __name__ == "__main__":
    main()
