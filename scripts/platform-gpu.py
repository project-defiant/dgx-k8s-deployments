#!/usr/bin/env python3
"""Manage the reviewed single-Spark NVIDIA Kubernetes device-plugin addon."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
# Fixed root-owned client and local scanners; separated argv, never a shell.
import subprocess  # nosec B404
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'platform/gpu/nvidia-device-plugin.yaml'
MANIFEST_SHA256 = '9ff18ba50932b412b3f189e4b9c71404a3a22daf4f7c0bf74555d2caf88bc7ed'
IMAGE = 'local.thedefiant/nvidia-device-plugin@sha256:daee95b7632c1bd976b3442460ac92819ffd64baeaa7fdbf77d3b8bf141088bd'
DAEMONSET = 'nvidia-device-plugin-daemonset'


class Refusal(ValueError):
    """Safe operational message without raw cluster/scanner output."""


def run(argv, *, data=None, timeout=60):
    result = subprocess.run(argv, input=data, text=True, capture_output=True, check=False, timeout=timeout)  # nosec B603
    if result.returncode:
        raise Refusal(f'{Path(argv[0]).name} GPU operation failed; inspect private scanner reports or device-plugin pod logs before retrying')
    return result.stdout


def reviewed_manifest():
    if MANIFEST.is_symlink():
        raise Refusal('GPU manifest must be a reviewed regular file')
    data = MANIFEST.read_bytes()
    if hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
        raise Refusal('GPU manifest changed: re-review its image, root identity and exact kubelet socket mount before updating the binding')
    text = data.decode()
    if re.findall(r'^\s+image:\s+(\S+)$', text, flags=re.MULTILINE) != [IMAGE]:
        raise Refusal('GPU manifest image differs from the reviewed scanner target')
    return text


def client():
    if os.geteuid() == 0:
        raise Refusal('Run as the ordinary platform user; no host sudo is required')
    binary = Path('/usr/local/bin/k3s')
    info = binary.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise Refusal('Expected the root-owned, non-writable K3s client')
    config = Path(os.environ.get('PLATFORM_KUBECONFIG', str(Path.home() / '.kube/spark-platform.yaml')))
    info = config.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refusal('Dedicated kubeconfig must be private and owned by this user')
    command = [str(binary), 'kubectl', '--kubeconfig', str(config), '--context', 'spark-platform', '--request-timeout=10s']
    view = json.loads(run(command + ['config', 'view', '--minify', '-o', 'json']))
    if len(view['clusters']) != 1 or len(view['contexts']) != 1 or view['contexts'][0]['name'] != 'spark-platform':
        raise Refusal('Refusing an unexpected Kubernetes context')
    cluster = view['clusters'][0]['cluster']
    if cluster.get('server') != 'https://127.0.0.1:6443' or cluster.get('insecure-skip-tls-verify'):
        raise Refusal('Refusing cluster: expected the verified loopback K3s endpoint')
    validate_node(json.loads(run(command + ['get', 'node', 'spark-platform', '-o', 'json'])))
    return command


def validate_node(node):
    if node.get('metadata', {}).get('name') != 'spark-platform' or node.get('status', {}).get('nodeInfo', {}).get('architecture') != 'arm64':
        raise Refusal('Device-plugin manifest is reviewed only for the ARM64 spark-platform node')


def allocatable(node):
    validate_node(node)
    return int(node.get('status', {}).get('allocatable', {}).get('nvidia.com/gpu', '0'))


def deploy(command, manifest):
    if hashlib.sha256(manifest.encode()).hexdigest() != MANIFEST_SHA256:
        raise Refusal('Refusing unreviewed GPU manifest bytes before schema checks or deployment')
    # Check the same in-memory bytes that will be applied; no scan/apply file race.
    run([str(ROOT / '.dev-tools/bin/kubeconform'), '-strict', '-summary', '-kubernetes-version', '1.35.0', '-'], data=manifest, timeout=180)
    run([sys.executable, str(ROOT / 'scripts/checks.py'), 'image', '--image', IMAGE], timeout=1800)
    run(command + ['get', 'runtimeclass', 'nvidia', '-o', 'name'])
    run(command + ['apply', '-f', '-'], data=manifest)
    run(command + ['--request-timeout=0', '-n', 'kube-system', 'rollout', 'status', 'daemonset/' + DAEMONSET, '--timeout=180s'], timeout=210)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        node = json.loads(run(command + ['get', 'node', 'spark-platform', '-o', 'json']))
        count = allocatable(node)
        if count == 1:
            print('NVIDIA device plugin registered exactly one allocatable GPU. No inference workload was started.')
            return
        if count > 1:
            raise Refusal('Unexpected GPU count; review hardware and sharing configuration before using the addon')
        time.sleep(2)
    raise Refusal('DaemonSet rolled out but the node did not advertise one GPU; inspect initialization/NVML logs. CPU fallback is not enabled.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'render', 'status', 'deploy'])
    args = parser.parse_args()
    try:
        manifest = reviewed_manifest()
        if args.action == 'plan':
            print('Single ARM64 Spark addon: pinned NVIDIA v0.20.1, runtime nvidia, one kubelet device-plugin directory mount, no sharing or host driver installation.')
        elif args.action == 'render':
            print(manifest, end='')
        else:
            command = client()
            if args.action == 'deploy':
                deploy(command, manifest)
            else:
                daemonset = run(command + ['-n', 'kube-system', 'get', 'daemonset', DAEMONSET, '--ignore-not-found', '-o', 'json'])
                ready = json.loads(daemonset).get('status', {}).get('numberReady', 0) if daemonset.strip() else 0
                count = allocatable(json.loads(run(command + ['get', 'node', 'spark-platform', '-o', 'json'])))
                print(f'Device-plugin pods ready: {ready}; allocatable NVIDIA GPUs: {count}')
                if ready != 1 or count != 1:
                    return 1
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('GPU operation failed safely; verify dedicated context, tools and cluster access.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
