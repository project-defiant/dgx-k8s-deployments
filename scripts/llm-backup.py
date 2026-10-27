#!/usr/bin/env python3
"""Manage cold LLM standby workloads and copy retained sources into empty PVCs."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import pwd
import stat
# Fixed external tools with separated arguments; secret values use stdin only.
import subprocess  # nosec B404
import sys
import tarfile
import uuid
from urllib.parse import urlsplit
import yaml

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = 'llm-backup'
COPY_IMAGE = 'local.thedefiant/llm-copy@sha256:fffc130742ffc3bf478e946dd810aa11b06ca6e614d9d36059f41be522759f93'
DEPLOYMENTS = {'ollama': 'ollama', 'webui': 'open-webui'}
CLAIMS = {'ollama': 'ollama-data', 'webui': 'webui-data'}
KUBE = []


class Refusal(ValueError):
    """A static, safe-to-display operational refusal."""


def run(argv, *, data=None, check=True, timeout=60):
    result = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=timeout, check=False)  # nosec B603
    if check and result.returncode:
        # Docker inspect, Secret validation and source-copy errors can contain secrets.
        raise Refusal(f'{Path(argv[0]).name} operation failed; no raw output was logged')
    return result


def k(*args, data=None):
    command = KUBE + (['--request-timeout=0'] if args and args[0] in ('rollout', 'wait') else [])
    return run(command + ['-n', NAMESPACE, *args], data=data, timeout=660 if args and args[0] == 'rollout' else 240 if args and args[0] == 'wait' else 60).stdout


def js(*args):
    value = k(*args, '-o', 'json')
    return json.loads(value) if value.strip() else {}


def guard():
    global KUBE
    if os.geteuid() == 0:
        raise Refusal('Run as the ordinary platform user, not with sudo')
    binary = Path('/usr/local/bin/k3s')
    info = binary.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise Refusal('Expected root-owned K3s client')
    config = Path(os.environ.get('PLATFORM_KUBECONFIG', str(Path.home() / '.kube/spark-platform.yaml')))
    info = config.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refusal('Dedicated kubeconfig must be private and owned by this user')
    KUBE = [str(binary), 'kubectl', '--kubeconfig', str(config), '--context', 'spark-platform', '--request-timeout=10s']
    view = json.loads(run(KUBE + ['config', 'view', '--minify', '-o', 'json']).stdout)
    cluster = view['clusters'][0]['cluster']
    if cluster.get('server') != 'https://127.0.0.1:6443' or cluster.get('insecure-skip-tls-verify'):
        raise Refusal('Refusing cluster: expected verified loopback endpoint')
    if len(view['contexts']) != 1 or view['contexts'][0]['name'] != 'spark-platform':
        raise Refusal('Refusing unexpected cluster context')
    node = js('get', 'node', 'spark-platform')
    if node.get('metadata', {}).get('name') != 'spark-platform':
        raise Refusal('Unexpected target node')
    return node


def cold_guard():
    resources = js('get', 'deployment', 'ollama', 'open-webui', '--ignore-not-found')
    deployments = resources.get('items', [resources] if resources else [])
    if any(item.get('spec', {}).get('replicas', 1) != 0 or item.get('status', {}).get('replicas', 0) for item in deployments):
        raise Refusal('Both standby deployments must have zero replicas before copying or redeploying')
    pods = js('get', 'pods', '-l', 'app.kubernetes.io/instance=llm-backup')
    if any(item.get('status', {}).get('phase') not in ('Succeeded', 'Failed') for item in pods.get('items', [])):
        raise Refusal('Standby workload pods must be fully stopped')


def source_stopped(component):
    if component == 'ollama':
        result = run(['/usr/bin/systemctl', 'is-active', 'ollama'], check=False)
        if result.stdout.strip() not in ('inactive', 'failed', 'unknown'):
            raise Refusal('Host Ollama must be stopped before activating its standby')
    else:
        result = run(['docker', 'inspect', '--type', 'container', '--format', '{{json .State.Running}}', 'open-webui'])
        if json.loads(result.stdout) is not False:
            raise Refusal('Original Docker Open WebUI must be stopped before activating its standby')


def local_ollama_url(value):
    parsed = urlsplit(value)
    if parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', 'host.docker.internal', '172.17.0.1', 'ollama') and parsed.port in (None, 11434) and parsed.path in ('', '/') and not parsed.username and not parsed.query and not parsed.fragment:
        return 'http://ollama:11434'
    return value


def webui_secrets():
    environment = json.loads(run(['docker', 'inspect', '--type', 'container', '--format', '{{json .Config.Env}}', 'open-webui']).stdout)
    values = {}
    for entry in environment:
        name, _, value = entry.partition('=')
        if name in ('DATABASE_TYPE', 'DB_TYPE') and value.lower() not in ('', 'sqlite', 'sqlite3'):
            raise Refusal('External WebUI database migration is unsupported; volume copy would omit its database')
        if name == 'DATABASE_URL' and value not in ('', 'sqlite:////app/backend/data/webui.db'):
            raise Refusal('External or nonstandard WebUI database URL requires a separate database backup')
        if re.search(r'TOKEN|SECRET|PASSWORD|CREDENTIAL|(?:^|_)KEYS?(?:$|_)', name) or name.startswith(('OPENAI_', 'OAUTH_', 'OPENID_', 'OLLAMA_')) or name in ('ENABLE_OPENAI_API', 'ENABLE_OLLAMA_API', 'ENABLE_PERSISTENT_CONFIG', 'WEBUI_AUTH'):
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                raise Refusal('Unsupported secret environment name; migration requires review')
            values[name] = value
    # Docker cp reads the original container filesystem even while stopped.
    command = ['docker', 'cp', 'open-webui:/app/backend/.webui_secret_key', '-']
    result = subprocess.run(command, capture_output=True, timeout=60, check=False)  # nosec B603
    if result.returncode:
        raise Refusal('Cannot read the original WebUI encryption key; refusing migration')
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        members = archive.getmembers()
        if len(members) != 1 or not members[0].isfile() or not 1 <= members[0].size <= 4096:
            raise Refusal('Unexpected original WebUI key file')
        key = archive.extractfile(members[0]).read().decode().strip()
    if not key:
        raise Refusal('Original WebUI key is empty')
    if values.get('WEBUI_SECRET_KEY') and values['WEBUI_SECRET_KEY'] != key:
        raise Refusal('Configured and on-disk WebUI keys differ; resolve before migration')
    values['WEBUI_SECRET_KEY'] = key
    for name in ('OLLAMA_BASE_URL', 'OLLAMA_BASE_URLS'):
        if name in values:
            values[name] = ';'.join(local_ollama_url(value) for value in values[name].split(';'))
    return {name: base64.b64encode(value.encode()).decode() for name, value in values.items()}


def preserve_secret(data):
    existing = js('get', 'secret', 'llm-backup-webui', '--ignore-not-found')
    if existing and existing.get('data') != data:
        raise Refusal('Existing WebUI Secret differs; refusing overwrite')
    if not existing:
        manifest = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'llm-backup-webui', 'namespace': NAMESPACE}, 'type': 'Opaque', 'data': data}
        k('apply', '-f', '-', data=json.dumps(manifest))


# This program runs ONLY inside a UID1000 pod with one PVC and no service token.
EXTRACT = r'''
import hashlib,json,os,pathlib,sqlite3,sys,tarfile
from urllib.parse import urlsplit
root=pathlib.Path('/data')
MAX_BYTES=400*1024**3
RESERVE=20*1024**3
if any(root.iterdir()): raise RuntimeError('PVC is not empty; refusing overwrite')
(root/'.llm-backup-copying').open('x').close()
count=total=0
dirs=[]
links=[]
try:
 with tarfile.open(fileobj=sys.stdin.buffer,mode='r|*') as bundle:
  for member in bundle:
   name=pathlib.PurePosixPath(member.name)
   if name.is_absolute() or '..' in name.parts: raise RuntimeError('Unsafe archive path')
   if str(name)=='.': continue
   target=root.joinpath(*name.parts)
   if member.isdir():
    target.mkdir(parents=True,exist_ok=True); dirs.append((target,member.mode & 0o777)); continue
   if member.issym() or member.islnk():
    link=pathlib.PurePosixPath(member.linkname)
    if link.is_absolute(): raise RuntimeError('Absolute archive link is forbidden')
    source=(target.parent if member.issym() else root).joinpath(*link.parts)
    resolved=source.resolve()
    if not resolved.is_relative_to(root.resolve()): raise RuntimeError('Archive link escapes the PVC')
    links.append((target,member.linkname,member.issym(),source))
    continue
   if not member.isfile(): raise RuntimeError('Archive devices and special files are forbidden')
   space=os.statvfs(root)
   if total+member.size>MAX_BYTES or space.f_bavail*space.f_frsize<member.size+RESERVE: raise RuntimeError('Insufficient space/headroom or copy budget exceeded')
   target.parent.mkdir(parents=True,exist_ok=True)
   expected=hashlib.sha256()
   with bundle.extractfile(member) as source,target.open('xb') as destination:
    while chunk:=source.read(1024*1024): destination.write(chunk); expected.update(chunk); total+=len(chunk)
   with target.open('rb') as stream:
    if hashlib.file_digest(stream,'sha256').digest()!=expected.digest(): raise RuntimeError('Copied file checksum mismatch')
   target.chmod(member.mode & 0o777); count+=1
 if not count: raise RuntimeError('Source archive contains no files')
 # Defer links until all regular files are verified: no write ever follows a link.
 for target,link,is_symlink,source in links:
  target.parent.mkdir(parents=True,exist_ok=True)
  if target.exists() or target.is_symlink(): raise RuntimeError('Duplicate archive link path')
  if is_symlink: target.symlink_to(link)
 for target,link,is_symlink,source in links:
  resolved=source.resolve(strict=True)
  if not resolved.is_relative_to(root.resolve()): raise RuntimeError('Archive link escapes the PVC')
  if not is_symlink:
   if not resolved.is_file(): raise RuntimeError('Archive hardlink target must be a regular file')
   os.link(resolved,target)
 for directory,mode in reversed(dirs): directory.chmod(mode)
 receipt={'complete':True,'files':count,'links':len(links),'bytes':total,'signup_disabled':None,'ollama_connection_reviewed':None}
 db=root/'webui.db'
 if db.is_file():
  try:
   with sqlite3.connect(str(db)) as connection:
    row=connection.execute('SELECT id,data FROM config ORDER BY id DESC LIMIT 1').fetchone()
    settings=json.loads(row[1]) if row else {}
    value=settings.get('ui',{}).get('enable_signup',False)
    receipt['signup_disabled']=value is False
    urls=settings.get('ollama',{}).get('base_urls')
    receipt['ollama_connection_reviewed']=urls is None
    if isinstance(urls,list) and all(isinstance(value,str) for value in urls):
     rewritten=[]
     configs=settings.get('ollama',{}).get('api_configs',{})
     for value in urls:
      parsed=urlsplit(value)
      local=parsed.scheme=='http' and parsed.hostname in ('localhost','127.0.0.1','host.docker.internal','172.17.0.1','ollama') and parsed.port in (None,11434) and parsed.path in ('','/') and not parsed.username and not parsed.query and not parsed.fragment
      replacement='http://ollama:11434' if local else value
      rewritten.append(replacement)
      if replacement!=value and isinstance(configs,dict) and value in configs:
       if replacement in configs and configs[replacement]!=configs[value]: raise ValueError('Conflicting backend configuration')
       configs[replacement]=configs.pop(value)
     receipt['ollama_connection_reviewed']='http://ollama:11434' in rewritten
     if rewritten!=urls:
      settings['ollama']['base_urls']=rewritten
      connection.execute('UPDATE config SET data=? WHERE id=?',(json.dumps(settings),row[0]))
      connection.commit()
      receipt['local_ollama_retargeted']=True
  except (sqlite3.Error,ValueError,TypeError):
   receipt['ollama_connection_reviewed']=False
   receipt.pop('local_ollama_retargeted',None)
 (root/'.llm-backup-complete.json').write_text(json.dumps(receipt))
 (root/'.llm-backup-copying').unlink()
 print(json.dumps(receipt))
except BaseException:
 (root/'.llm-backup-failed').touch(exist_ok=True)
 raise
'''


def helper_manifest(name, claim, image):
    if image != COPY_IMAGE:
        raise Refusal('Migration helper requires the exact reviewed Python copy image')
    return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': NAMESPACE}, 'spec': {
        'nodeSelector': {'kubernetes.io/hostname': 'spark-platform'}, 'restartPolicy': 'Never', 'activeDeadlineSeconds': 21600,
        'automountServiceAccountToken': False, 'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}},
        'containers': [{'name': 'copy', 'image': image, 'imagePullPolicy': 'Never', 'command': ['python3', '-c', 'import time; time.sleep(21600)'],
                        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                        'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '1', 'memory': '512Mi'}},
                        'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}],
        'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': claim}}]}}


def copy_stream(source, destination):
    producer = subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # nosec B603
    consumer = subprocess.Popen(destination, stdin=producer.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # nosec B603
    producer.stdout.close()
    try:
        output, error_output = consumer.communicate(timeout=21600)
        producer.communicate(timeout=30)
        if consumer.returncode or producer.returncode:
            reports = ROOT / '.dev-tools/reports'
            reports.mkdir(parents=True, exist_ok=True)
            fd = os.open(reports / 'llm-copy-last-error.log', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as log:
                os.fchmod(log.fileno(), 0o600)
                log.write(error_output[:65536])
            raise Refusal('Data copy failed; inspect private .dev-tools/reports/llm-copy-last-error.log; original retained, PVC requires review')
        return json.loads(output)
    finally:
        for process in (consumer, producer):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)


def image_gate(reference):
    result = run([sys.executable, str(ROOT / 'scripts/checks.py'), 'image', '--image', reference], check=False, timeout=1800)
    if result.returncode:
        raise Refusal('Current image vulnerability/coverage gate failed; inspect private .dev-tools/reports')


def storage_resources(rendered):
    expected = {'PersistentVolumeClaim': {'ollama-data', 'webui-data'},
                'NetworkPolicy': {'llm-backup-webui', 'llm-backup-ollama', 'llm-backup-default-deny'},
                'ResourceQuota': {'llm-backup'}}
    resources, seen = [], set()
    for resource in yaml.safe_load_all(rendered):
        if not resource or resource.get('kind') not in expected:
            continue
        kind, metadata = resource['kind'], resource.get('metadata', {})
        name = metadata.get('name')
        version = 'networking.k8s.io/v1' if kind == 'NetworkPolicy' else 'v1'
        if name not in expected[kind] or metadata.get('namespace') != NAMESPACE or resource.get('apiVersion') != version or (kind, name) in seen:
            raise Refusal('Unexpected or duplicate storage-only resource; review the chart before preparation')
        seen.add((kind, name))
        metadata.setdefault('labels', {})['app.kubernetes.io/managed-by'] = 'Helm'
        metadata.setdefault('annotations', {}).update({'meta.helm.sh/release-name': NAMESPACE, 'meta.helm.sh/release-namespace': NAMESPACE})
        resources.append(resource)
    if seen != {(kind, name) for kind, names in expected.items() for name in names}:
        raise Refusal('Storage-only render must contain exactly two claims, three policies and one quota')
    return resources


def owned(resource):
    metadata = resource.get('metadata', {})
    annotations = metadata.get('annotations', {})
    return (metadata.get('labels', {}).get('app.kubernetes.io/managed-by') == 'Helm'
            and annotations.get('meta.helm.sh/release-name') == NAMESPACE
            and annotations.get('meta.helm.sh/release-namespace') == NAMESPACE)


def prepare_storage(resources):
    namespace = js('get', 'namespace', NAMESPACE, '--ignore-not-found')
    existing, apply = {}, []
    if namespace:
        if not owned(namespace) or namespace.get('metadata', {}).get('deletionTimestamp'):
            raise Refusal('Existing standby namespace is unmanaged, foreign-owned or deleting; refusing policy changes')
        cold_guard()
        # Complete every preflight before creating/adopting/updating anything.
        for resource in resources:
            name, kind = resource['metadata']['name'], resource['kind']
            current = js('get', kind, name, '--ignore-not-found')
            existing[(kind, name)] = current
            if not current:
                continue
            if not owned(current) or current.get('metadata', {}).get('deletionTimestamp'):
                raise Refusal('Existing storage resource is unmanaged, foreign-owned or deleting; refusing adoption')
            if kind == 'PersistentVolumeClaim':
                desired, actual = resource['spec'], current.get('spec', {})
                if (desired['storageClassName'] != actual.get('storageClassName') or desired['accessModes'] != actual.get('accessModes')
                        or desired['resources']['requests']['storage'] != actual.get('resources', {}).get('requests', {}).get('storage')
                        or actual.get('volumeMode', 'Filesystem') != 'Filesystem'):
                    raise Refusal('Existing PVC class, access mode or size differs; refusing resize or replacement')
    for resource in resources:
        key = (resource['kind'], resource['metadata']['name'])
        if resource['kind'] != 'PersistentVolumeClaim' or not existing.get(key):
            apply.append(resource)
    if not namespace:
        k('create', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': NAMESPACE, 'labels': {'app.kubernetes.io/managed-by': 'Helm'}, 'annotations': {'meta.helm.sh/release-name': NAMESPACE, 'meta.helm.sh/release-namespace': NAMESPACE}}}))
    k('apply', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': apply}))
    print('Prepared standby PVCs and policies only; no application, Service or Secret was deployed.')


def refresh_network(resources):
    if not owned(js('get', 'namespace', NAMESPACE)):
        raise Refusal('Refusing policy changes in an unmanaged namespace')
    policies = [item for item in resources if item['kind'] == 'NetworkPolicy']
    if {item['metadata']['name'] for item in policies} != {'llm-backup-ollama', 'llm-backup-webui', 'llm-backup-default-deny'}:
        raise Refusal('Unexpected network policies')
    for policy in policies:
        current = js('get', 'networkpolicy', policy['metadata']['name'], '--ignore-not-found')
        if current and not owned(current):
            raise Refusal('Refusing to overwrite an unmanaged network policy')
    payload = json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': policies})
    run([str(ROOT / '.dev-tools/bin/kubeconform'), '-strict', '-summary', '-kubernetes-version', '1.35.0', '-'], data=payload, timeout=180)
    k('apply', '-f', '-', data=payload)
    print('Network policies refreshed; no workload replicas or PVCs changed.')


def scan_images(references):
    for reference in sorted(set(references)):
        if not re.fullmatch(r'(docker.io/ollama/ollama|ghcr.io/open-webui/open-webui)@sha256:[0-9a-f]{64}', reference):
            raise Refusal('Reviewed, digest-pinned Ollama/WebUI images required')
        image_gate(reference)


def deployed_images():
    references = []
    images = {}
    for name in DEPLOYMENTS.values():
        pod = js('get', 'deployment', name)['spec']['template']['spec']
        references += [item['image'] for item in pod.get('containers', []) + pod.get('initContainers', [])]
        images[name] = pod['containers'][0]['image']
    scan_images(references)
    return images


def available_gpu(node):
    capacity = int(node.get('status', {}).get('allocatable', {}).get('nvidia.com/gpu', '0'))
    used = 0
    for pod in js('get', 'pods', '--all-namespaces').get('items', []):
        spec = pod.get('spec', {})
        if spec.get('nodeName') not in (None, 'spark-platform') or pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
            continue
        def requested(container):
            resources = container.get('resources', {})
            return int(resources.get('requests', {}).get('nvidia.com/gpu', resources.get('limits', {}).get('nvidia.com/gpu', '0')))
        used += max(sum(requested(item) for item in spec.get('containers', [])), max((requested(item) for item in spec.get('initContainers', [])), default=0))
    if capacity - used < 1:
        raise Refusal('No available Kubernetes GPU: missing advertisement or GPU busy; standby remains stopped')


def ollama_source_guard():
    source = Path('/usr/share/ollama/.ollama')
    owner = pwd.getpwnam('ollama').pw_uid
    for path in [*reversed(source.parents), source]:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, owner) or info.st_mode & 0o022:
            raise Refusal('Ollama source/parents must be real directories with trusted ownership and no group/world writes')
    if source.lstat().st_uid != owner:
        raise Refusal('Ollama source must belong to the system ollama user')


def headroom(name, estimate):
    program = "import os; s=os.statvfs('/data'); print(s.f_bavail*s.f_frsize)"
    available = int(k('exec', name, '--', 'python3', '-c', program).strip())
    if available < estimate + 20 * 1024**3:
        raise Refusal('Insufficient destination free space: source estimate plus 20 GiB reserve required before stopping source')


def authenticate_source_read():
    if not sys.stdin.isatty():
        raise Refusal('Run Ollama migration in your normal interactive terminal so sudo can authenticate')
    print('Authenticate sudo for stopping and reading host Ollama; no host data will be deleted.', flush=True)
    command = ['/usr/bin/sudo', '-v']
    result = subprocess.run(command, check=False, timeout=120)  # nosec B603
    if result.returncode:
        raise Refusal('sudo authentication failed; source remains untouched')


# Narrow recovery for the observed cache-link failure, never a general PVC wipe.
PARTIAL_INVENTORY = r'''
import pathlib,hashlib,json,stat
root=pathlib.Path('/data')
def inventory():
 entries={p.name for p in root.iterdir()}
 if entries!={'.llm-backup-failed','.llm-backup-copying','cache'}: raise RuntimeError('Unexpected partial-copy layout')
 if any(not (root/n).is_file() or (root/n).is_symlink() or (root/n).stat().st_size for n in ('.llm-backup-failed','.llm-backup-copying')): raise RuntimeError('Invalid failure markers')
 result={}
 for p in root.rglob('*'):
  mode=p.lstat().st_mode
  if stat.S_ISDIR(mode): value={'type':'directory','mode':stat.S_IMODE(mode)}
  elif stat.S_ISREG(mode):
   with p.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
   value={'type':'file','mode':stat.S_IMODE(mode),'size':p.stat().st_size,'sha256':digest}
  else: raise RuntimeError('Unexpected link or special file in partial copy')
  result[p.relative_to(root).as_posix()]=value
 return result
'''


def recover_webui_partial():
    cold_guard()
    source_stopped('webui')
    image_gate(COPY_IMAGE)
    claim = js('get', 'pvc', CLAIMS['webui'])
    if not owned(claim):
        raise Refusal('Foreign WebUI PVC; refusing recovery')
    state = js('get', 'configmap', 'llm-backup-migration', '--ignore-not-found').get('data', {})
    if 'webui' in state:
        raise Refusal('A WebUI migration receipt already exists; refusing recovery')
    name = 'llm-recover-' + uuid.uuid4().hex[:12]
    try:
        k('create', '-f', '-', data=json.dumps(helper_manifest(name, CLAIMS['webui'], COPY_IMAGE)))
        k('wait', '--for=condition=Ready', 'pod/' + name, '--timeout=180s')
        before = json.loads(k('exec', name, '--', 'python3', '-c', PARTIAL_INVENTORY + "\nprint(json.dumps(inventory()))"))
        directory = ROOT / '.dev-tools/recovery'
        if directory.is_symlink():
            raise Refusal('Recovery directory must not be a symlink')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        archive = directory / ('webui-partial-' + uuid.uuid4().hex + '.tar')
        code = PARTIAL_INVENTORY + "\nimport tarfile,sys\ninventory()\nwith tarfile.open(fileobj=sys.stdout.buffer,mode='w|') as bundle:\n for p in root.iterdir(): bundle.add(p,arcname=p.name,recursive=True)"
        with archive.open('xb') as stream:
            archive.chmod(0o600)
            result = subprocess.run(KUBE + ['--request-timeout=0', '-n', NAMESPACE, 'exec', name, '--', 'python3', '-c', code], stdout=stream, stderr=subprocess.PIPE, timeout=600, check=False)  # nosec B603
            stream.flush()
            os.fsync(stream.fileno())
        if result.returncode:
            raise Refusal('Partial-copy backup failed; PVC was not changed')
        actual = {}
        with tarfile.open(archive) as bundle:
            for member in bundle:
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or member.name in actual:
                    raise Refusal('Unsafe or duplicate recovery archive member')
                if member.isdir():
                    value = {'type': 'directory', 'mode': member.mode}
                elif member.isfile():
                    with bundle.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                    value = {'type': 'file', 'mode': member.mode, 'size': member.size, 'sha256': digest}
                else:
                    raise Refusal('Unexpected recovery archive member')
                actual[member.name] = value
        if actual != before:
            raise Refusal('Recovery archive differs from the PVC; PVC was not changed')
        # Revalidate every byte immediately before clearing only the backed-up partial state.
        cleanup = PARTIAL_INVENTORY + "\nimport sys,shutil\nif inventory()!=json.load(sys.stdin): raise RuntimeError('Partial copy changed; refusing cleanup')\nfor p in root.iterdir():\n if p.is_dir(): shutil.rmtree(p)\n else: p.unlink()"
        k('exec', '-i', name, '--', 'python3', '-c', cleanup, data=json.dumps(before))
        print('Failed cache copy preserved and checksum-verified at ' + str(archive.relative_to(ROOT)))
        print('WebUI PVC is empty and ready for the corrected copy; original remains stopped and intact.')
    finally:
        k('delete', 'pod', name, '--ignore-not-found', '--wait=true', '--timeout=60s')


def migrate(component):
    cold_guard()
    image_gate(COPY_IMAGE)
    claim = js('get', 'pvc', CLAIMS[component])
    if not owned(claim):
        raise Refusal('Migration target PVC must have the expected Helm ownership')
    image = COPY_IMAGE
    name = 'llm-copy-' + uuid.uuid4().hex[:12]
    try:
        k('create', '-f', '-', data=json.dumps(helper_manifest(name, CLAIMS[component], image)))
        k('wait', '--for=condition=Ready', 'pod/' + name, '--timeout=180s')
        # Check emptiness before stopping the source. Stream program repeats atomically.
        k('exec', name, '--', 'python3', '-c', "import pathlib\nif any(pathlib.Path('/data').iterdir()): raise RuntimeError('PVC is not empty')")
        if component == 'webui':
            secret = webui_secrets()
            # Use the enforced 20-GiB copy ceiling as a conservative source estimate.
            headroom(name, 20 * 1024**3)
            preserve_secret(secret)
            run(['docker', 'stop', '--time', '30', 'open-webui'])
            source = ['docker', 'cp', 'open-webui:/app/backend/data/.', '-']
        else:
            ollama_source_guard()
            authenticate_source_read()
            size = run(['/usr/bin/sudo', '-n', '/usr/bin/du', '--summarize', '--bytes', '--one-file-system', '/usr/share/ollama/.ollama']).stdout
            headroom(name, int(size.split()[0]))
            run(['/usr/bin/sudo', '-n', '/usr/bin/systemctl', 'stop', 'ollama'])
            source = ['/usr/bin/sudo', '-n', '/usr/bin/tar', '--numeric-owner', '-C', '/usr/share/ollama/.ollama', '-cf', '-', '.']
        source_stopped(component)
        if component == 'ollama':
            ollama_source_guard()
        cold_guard()
        program = EXTRACT.replace('MAX_BYTES=400*1024**3', 'MAX_BYTES=20*1024**3') if component == 'webui' else EXTRACT
        destination = KUBE + ['--request-timeout=0', '-n', NAMESPACE, 'exec', '-i', name, '--', 'python3', '-c', program]
        receipt = copy_stream(source, destination)
        receipt['pvc_uid'] = claim['metadata']['uid']
        state = js('get', 'configmap', 'llm-backup-migration', '--ignore-not-found').get('data', {})
        state[component] = json.dumps(receipt)
        k('apply', '-f', '-', data=json.dumps({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'llm-backup-migration', 'namespace': NAMESPACE}, 'data': state}))
        print(component + ': copied and verified; original stopped and retained')
    finally:
        k('delete', 'pod', name, '--ignore-not-found', '--wait=false')


def ollama_resources(rendered):
    resources = [item for item in yaml.safe_load_all(rendered) if item and
                 (item.get('kind'), item.get('metadata', {}).get('name')) in {('Deployment', 'ollama'), ('Service', 'ollama')}]
    if len(resources) != 2 or {item['kind'] for item in resources} != {'Deployment', 'Service'}:
        raise Refusal('Ollama-only render must contain exactly its Deployment and Service')
    for resource in resources:
        metadata = resource['metadata']
        if metadata.get('namespace') != NAMESPACE:
            raise Refusal('Unexpected Ollama namespace')
        metadata.setdefault('labels', {})['app.kubernetes.io/managed-by'] = 'Helm'
        metadata.setdefault('annotations', {}).update({'meta.helm.sh/release-name': NAMESPACE, 'meta.helm.sh/release-namespace': NAMESPACE})
        if resource['kind'] == 'Deployment' and resource['spec'].get('replicas') != 0:
            raise Refusal('Deployment preparation must leave Ollama stopped')
    return resources


def ollama_receipt():
    namespace = js('get', 'namespace', NAMESPACE)
    claim = js('get', 'pvc', 'ollama-data')
    receipt = json.loads(js('get', 'configmap', 'llm-backup-migration').get('data', {}).get('ollama', '{}'))
    if not owned(namespace) or not owned(claim) or claim.get('status', {}).get('phase') != 'Bound':
        raise Refusal('Owned namespace and bound Ollama PVC required')
    if not receipt.get('complete') or receipt.get('pvc_uid') != claim['metadata']['uid']:
        raise Refusal('Verified Ollama migration receipt must match the existing PVC')


def deploy_ollama(resources):
    cold_guard()
    ollama_receipt()
    deployment = next(item for item in resources if item['kind'] == 'Deployment')
    pod = deployment['spec']['template']['spec']
    if len(pod.get('containers', [])) != 1 or pod.get('initContainers'):
        raise Refusal('Unexpected Ollama executable containers')
    reference = pod['containers'][0]['image']
    if not re.fullmatch(r'docker.io/ollama/ollama@sha256:[0-9a-f]{64}', reference):
        raise Refusal('Reviewed immutable official Ollama image required')
    for resource in resources:
        current = js('get', resource['kind'], resource['metadata']['name'], '--ignore-not-found')
        if current and not owned(current):
            raise Refusal('Refusing to overwrite an unmanaged Ollama resource')
    payload = json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': resources})
    run([str(ROOT / '.dev-tools/bin/kubeconform'), '-strict', '-summary', '-kubernetes-version', '1.35.0', '-'], data=payload, timeout=180)
    image_gate(reference)
    k('apply', '-f', '-', data=payload)
    print('Ollama-only Deployment and Service prepared at zero replicas; WebUI was not deployed.')


def start_ollama(node):
    source_stopped('ollama')
    ollama_receipt()
    deployment = js('get', 'deployment', 'ollama')
    if not owned(deployment):
        raise Refusal('Refusing unmanaged Ollama deployment')
    pod = deployment['spec']['template']['spec']
    if len(pod.get('containers', [])) != 1 or pod.get('initContainers'):
        raise Refusal('Unexpected Ollama executable containers')
    image_gate(pod['containers'][0]['image'])
    if deployment['spec'].get('replicas', 0) == 0:
        available_gpu(node)
        js('get', 'runtimeclass', 'nvidia')
        k('scale', 'deployment/ollama', '--replicas=1')
    k('rollout', 'status', 'deployment/ollama', '--timeout=600s')
    print('Ollama is ready in K3s using its migrated PVC and one NVIDIA GPU.')


def start(node):
    cold_guard()
    available_gpu(node)
    deployed_images()
    js('get', 'runtimeclass', 'nvidia')
    state = js('get', 'configmap', 'llm-backup-migration').get('data', {})
    for component in DEPLOYMENTS:
        source_stopped(component)
        receipt = json.loads(state.get(component, '{}'))
        claim = js('get', 'pvc', CLAIMS[component])
        if not receipt.get('complete') or receipt.get('pvc_uid') != claim['metadata']['uid']:
            raise Refusal('Verified migration receipt required for each existing PVC')
        if component == 'webui' and receipt.get('signup_disabled') is not True:
            raise Refusal('Effective copied WebUI signup configuration requires manual review before starting')
        if component == 'webui' and receipt.get('ollama_connection_reviewed') is not True:
            raise Refusal('Copied persistent Ollama connection requires manual review before starting')
    if not js('get', 'secret', 'llm-backup-webui').get('data', {}).get('WEBUI_SECRET_KEY'):
        raise Refusal('Preserved WebUI encryption key required')
    k('scale', 'deployment/ollama', 'deployment/open-webui', '--replicas=1')
    print('Standby startup requested; verify rollout readiness and runtime compatibility before relying on it.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'status', 'render', 'deploy', 'prepare-storage', 'refresh-network', 'recover-failed-webui', 'deploy-ollama', 'start-ollama', 'stop-ollama', 'access-ollama', 'start', 'stop', 'migrate'])
    parser.add_argument('component', nargs='?', choices=['ollama', 'webui', 'all'], default='all')
    parser.add_argument('--values', type=Path, default=ROOT / '.dev-tools/llm-backup/values.yaml')
    args = parser.parse_args()
    try:
        if args.action != 'migrate' and args.component != 'all':
            raise Refusal('Component selection is supported only for migrate; start/stop operate the paired standby')
        if args.action == 'plan':
            print('Cold standby: llm-backup namespace, ollama-data 400Gi, webui-data 20Gi; default replicas zero. Migration preserves stopped originals; no off-machine backup is created.')
            return 0
        helm = [str(ROOT / '.dev-tools/bin/helm')]
        values = ['-f', str(ROOT / 'apps/llm-backup/values.yaml')]
        if args.values.is_file():
            values += ['-f', str(args.values)]
        elif args.values != ROOT / '.dev-tools/llm-backup/values.yaml':
            raise Refusal('Requested values overlay is missing')
        values += ['--set', 'ollama.replicas=0', '--set', 'webui.replicas=0']
        storage = None
        if args.action in ('prepare-storage', 'refresh-network'):
            # Schema-validation placeholders are discarded with all executable resources.
            fixture = ['-f', str(ROOT / 'apps/llm-backup/values.yaml'), '--set', 'ollama.image.digest=sha256:' + '0'*64, '--set', 'webui.image.digest=sha256:' + '0'*64, '--set', 'ollama.replicas=0', '--set', 'webui.replicas=0']
            rendered = run(helm + ['template', NAMESPACE, str(ROOT / 'charts/llm-backup'), '-n', NAMESPACE, *fixture]).stdout
            storage = storage_resources(rendered)
        if args.action in ('render', 'deploy'):
            rendered = run(helm + ['template', NAMESPACE, str(ROOT / 'charts/llm-backup'), '-n', NAMESPACE, *values]).stdout
            if args.action == 'render':
                print(rendered, end='')
                return 0
            references = re.findall(r'^\s+image:\s*[\"\']?([^\s\"\']+)', rendered, flags=re.MULTILINE)
            if not references:
                raise Refusal('Rendered chart contains no workload images')
            scan_images(references)
        ollama = None
        if args.action == 'deploy-ollama':
            # The unused WebUI render is discarded; its image is never scanned or applied here.
            rendered = run(helm + ['template', NAMESPACE, str(ROOT / 'charts/llm-backup'), '-n', NAMESPACE, *values, '--set-string', 'webui.image.digest=sha256:' + '0'*64]).stdout
            ollama = ollama_resources(rendered)
        node = guard()
        if args.action == 'prepare-storage':
            prepare_storage(storage)
        elif args.action == 'refresh-network':
            refresh_network(storage)
        elif args.action == 'deploy-ollama':
            deploy_ollama(ollama)
        elif args.action == 'start-ollama':
            start_ollama(node)
        elif args.action == 'stop-ollama':
            k('scale', 'deployment/ollama', '--replicas=0')
            selector = 'app.kubernetes.io/instance=llm-backup,app.kubernetes.io/component=ollama'
            if js('get', 'pods', '-l', selector).get('items'):
                k('wait', '--for=delete', 'pods', '-l', selector, '--timeout=180s')
        elif args.action == 'access-ollama':
            result = subprocess.run(KUBE + ['--request-timeout=0', '-n', NAMESPACE, 'port-forward', '--address=127.0.0.1', 'service/ollama', '11434:11434'], check=False)  # nosec B603
            return result.returncode
        elif args.action == 'recover-failed-webui':
            recover_webui_partial()
        elif args.action == 'status':
            # Secret resources and environment values are deliberately absent.
            print(k('get', 'deployments,pods,pvc,services'), end='')
        elif args.action == 'stop':
            k('scale', 'deployment/ollama', 'deployment/open-webui', '--replicas=0')
            if js('get', 'pods', '-l', 'app.kubernetes.io/instance=llm-backup').get('items'):
                k('wait', '--for=delete', 'pods', '-l', 'app.kubernetes.io/instance=llm-backup', '--timeout=180s')
        elif args.action == 'start':
            start(node)
        elif args.action == 'deploy':
            if js('get', 'namespace', NAMESPACE, '--ignore-not-found'):
                cold_guard()
            print(run(helm + ['upgrade', '--install', NAMESPACE, str(ROOT / 'charts/llm-backup'), '--kubeconfig', KUBE[3], '--kube-context', 'spark-platform', '--namespace', NAMESPACE, '--create-namespace', *values]).stdout, end='')
        else:
            for component in (list(DEPLOYMENTS) if args.component == 'all' else [args.component]):
                migrate(component)
    except Refusal as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, tarfile.TarError, yaml.YAMLError):
        print('LLM standby operation failed safely. Check context, zero replicas, source state and migration prerequisites; secret values and raw source output were not logged.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
