#!/usr/bin/env python3
"""Check or import this project's verified native Forgejo image; never deploy it."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
# Fixed tools and separated argv; sudo elevates only the fixed K3s import command.
import subprocess  # nosec B404
import sys
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'local.thedefiant/forgejo'
# Container-private emptyDir, never a shared host temporary directory.
POD_TMP = '/tmp'  # nosec B108


def verified_record():
    architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if not architecture:
        raise ValueError('Native ARM64 or AMD64 required')
    command = [sys.executable, str(ROOT / 'scripts/build-forgejo.py'), '--verify']
    subprocess.run(command, check=True, capture_output=True, text=True)  # nosec B603
    output = ROOT / '.dev-tools/forgejo' / architecture
    record = json.loads((output / 'record.json').read_text())
    if record['repository'] != REPOSITORY or record['architecture'] != architecture:
        raise ValueError('Unexpected image repository or architecture')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', record['manifest_digest']):
        raise ValueError('Expected full image manifest SHA256')
    if record['imageRef'] != REPOSITORY + '@' + record['manifest_digest']:
        raise ValueError('Unexpected image reference')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}', record['immutable_tag']):
        raise ValueError('Unexpected image tag')
    return output, record


def check_node(node, record):
    if node.get('metadata', {}).get('name') != 'spark-platform':
        raise ValueError('Refusing an unexpected Kubernetes node')
    if node.get('status', {}).get('nodeInfo', {}).get('architecture') != record['architecture']:
        raise ValueError('Build architecture does not match the target node')


def import_command(output, record):
    return ['/usr/bin/sudo', '/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io', 'images', 'import',
            '--local', '--platform', 'linux/' + record['architecture'], '--digests', '--base-name', REPOSITORY,
            '--label', 'io.cri-containerd.image=managed', str(output / 'image.oci.tar')]


def root_images(arguments, output):
    command = ['/usr/bin/sudo', '/usr/local/bin/k3s', 'ctr', '--namespace', 'k8s.io', 'images', *arguments]
    result = subprocess.run(command, check=False, capture_output=True, text=True)  # nosec B603
    # Full privileged output can identify other local workloads: never print it.
    log = output / 'import.log'
    with log.open('a') as stream:
        log.chmod(0o600)
        stream.write(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError('K3s image command failed; inspect private import.log')
    return result.stdout


def own_targets(text, record):
    names = {REPOSITORY + ':' + record['immutable_tag'], record['imageRef']}
    targets = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in names:
            if len(fields) < 3 or not re.fullmatch(r'sha256:[0-9a-f]{64}', fields[2]):
                raise ValueError('Cannot determine the local Forgejo image target')
            if fields[0] in targets and targets[fields[0]] != fields[2]:
                raise ValueError('Conflicting local Forgejo image targets')
            targets[fields[0]] = fields[2]
    return targets


def ensure_import(output, record, runner=root_images):
    tag, alias, digest = REPOSITORY + ':' + record['immutable_tag'], record['imageRef'], record['manifest_digest']
    targets = own_targets(runner(['list'], output), record)
    if alias in targets and targets[alias] != digest:
        raise ValueError('Existing Forgejo digest alias targets another image; refusing overwrite')
    if targets.get(tag) != digest:
        runner(import_command(output, record)[6:], output)
        targets = own_targets(runner(['list'], output), record)
        if targets.get(tag) != digest:
            raise ValueError('Imported Forgejo tag does not target the verified manifest')
    if alias in targets and targets[alias] != digest:
        raise ValueError('Imported digest alias targets another image; refusing overwrite')
    if alias not in targets:
        runner(['tag', tag, alias], output)
    if own_targets(runner(['list'], output), record).get(alias) != digest:
        raise ValueError('Canonical Forgejo digest alias is missing; import did not complete')


def kube_command():
    if os.geteuid() == 0:
        raise ValueError('Run the image probe as the ordinary platform user, without sudo')
    binary = Path('/usr/local/bin/k3s')
    info = binary.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Expected a root-owned, non-writable K3s client')
    config = Path(os.environ.get('PLATFORM_KUBECONFIG', str(Path.home() / '.kube/spark-platform.yaml')))
    info = config.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Dedicated user kubeconfig must be private and owned by this user')
    command = [str(binary), 'kubectl', '--kubeconfig', str(config), '--context', 'spark-platform', '--request-timeout=5s']
    view = subprocess.run(command + ['config', 'view', '--minify', '-o', 'json'], check=True, capture_output=True, text=True)  # nosec B603
    data = json.loads(view.stdout)
    cluster = data['clusters'][0]['cluster']
    if len(data['clusters']) != 1 or cluster.get('server') != 'https://127.0.0.1:6443' or cluster.get('insecure-skip-tls-verify'):
        raise ValueError('Refusing cluster: expected verified dedicated loopback K3s endpoint')
    if len(data['contexts']) != 1 or data['contexts'][0]['name'] != 'spark-platform':
        raise ValueError('Refusing an unexpected Kubernetes context')
    return command


def image_ids(output, record):
    with tarfile.open(output / 'image.oci.tar') as archive:
        member = archive.getmember('blobs/sha256/' + record['manifest_digest'].removeprefix('sha256:'))
        if not member.isfile():
            raise ValueError('Expected regular verified OCI manifest')
        with archive.extractfile(member) as stream:
            config = json.load(stream)['config']['digest']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', config):
        raise ValueError('Expected full OCI config digest')
    return {record['manifest_digest'], config}


def probe_manifest(name, record):
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': 'default'},
            'spec': {'nodeName': 'spark-platform', 'restartPolicy': 'Never', 'activeDeadlineSeconds': 30,
                     'terminationGracePeriodSeconds': 1, 'automountServiceAccountToken': False,
                     'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000,
                                         'seccompProfile': {'type': 'RuntimeDefault'}},
                     'containers': [{'name': 'image-check', 'image': record['imageRef'], 'imagePullPolicy': 'Never',
                                     'command': ['/app/gitea/gitea', '--version'], 'workingDir': POD_TMP,
                                     'env': [{'name': 'TMPDIR', 'value': POD_TMP}],
                                     'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                                         'capabilities': {'drop': ['ALL']}},
                                     'resources': {'requests': {'cpu': '100m', 'memory': '64Mi'},
                                                   'limits': {'cpu': '500m', 'memory': '256Mi'}},
                                     'volumeMounts': [{'name': 'tmp', 'mountPath': POD_TMP},
                                                      {'name': 'data', 'mountPath': '/var/lib/gitea'}]}],
                     'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '32Mi'}},
                                 {'name': 'data', 'emptyDir': {'sizeLimit': '32Mi'}}]}}


def probe_image(output, record, command, runner=subprocess.run):
    name = 'forgejo-image-check-' + uuid.uuid4().hex[:12]
    expected = image_ids(output, record)
    error = 'Run make forgejo-image-import again in your normal terminal to repair the canonical digest alias, then retry deployment.'
    try:
        runner(command + ['-n', 'default', 'create', '-f', '-'], input=json.dumps(probe_manifest(name, record)), check=True, capture_output=True, text=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = runner(command + ['-n', 'default', 'get', 'pod', name, '-o', 'json'], check=True, capture_output=True, text=True)
            status = json.loads(result.stdout).get('status', {})
            containers = status.get('containerStatuses', [])
            if any(item.get('state', {}).get('waiting', {}).get('reason') in ('ErrImageNeverPull', 'ErrImagePull', 'ImagePullBackOff') for item in containers):
                raise ValueError('Exact digest image is not runnable from the K3s cache. ' + error)
            if status.get('phase') == 'Succeeded':
                if len(containers) != 1 or containers[0].get('name') != 'image-check' or containers[0].get('state', {}).get('terminated', {}).get('exitCode') != 0:
                    raise ValueError('Image probe did not prove successful command execution. ' + error)
                identifiers = {match.group(0) for item in containers for match in re.finditer(r'sha256:[0-9a-f]{64}$', item.get('imageID', ''))}
                if not identifiers or not identifiers.issubset(expected):
                    raise ValueError('Image probe returned an unexpected runtime image ID. ' + error)
                return
            if status.get('phase') == 'Failed':
                raise ValueError('Patched image execution probe failed. ' + error)
            time.sleep(1)
        raise ValueError('Patched image probe did not succeed within 30 seconds. ' + error)
    finally:
        runner(command + ['-n', 'default', 'delete', 'pod', name, '--ignore-not-found', '--wait=false'], check=True, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['import', 'check-node', 'plan'])
    args = parser.parse_args()
    try:
        output, record = verified_record()
        if args.action == 'check-node':
            check_node(json.load(sys.stdin), record)
            probe_image(output, record, kube_command())
            print('Verified exact patched Forgejo digest executes from the target node cache')
        elif args.action == 'plan':
            print(json.dumps({'imageRef': record['imageRef'], 'archive_sha256': record['archive_sha256'],
                              'command': import_command(output, record)}, indent=2))
        else:
            if os.geteuid() == 0:
                raise ValueError('Run make forgejo-image-import without sudo; the command requests sudo internally')
            # Require a current vulnerability/coverage check before privileged import.
            command = [sys.executable, str(ROOT / 'scripts/checks.py'), 'image', '--image', record['imageRef']]
            subprocess.run(command, check=True)  # nosec B603
            ensure_import(output, record)
            print('Imported verified patched Forgejo image; make deploy-services can now deploy it')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
