# Ollama and Open WebUI cold standby

This is a **same-machine standby copy**, not an off-machine backup or high
availability. A host/disk failure can lose the originals and both PVCs. Keep
independent encrypted backups and recovery keys elsewhere.

No migration is performed by preparing these files. Installed legacy images have
known high/critical findings. Candidate replacement images are not approved merely
because their vulnerability scan passes. A WebUI slim image may omit embedding,
Chroma or other capabilities used by the original standard image; validate the
standard replacement's capabilities and database compatibility before deployment.
Never silently replace the source with a reduced-feature variant.

## Storage-only migration while application images are blocked

`make llm-backup-prepare-storage` creates only the retained PVCs, isolation policies
and quota. It never deploys executable resources or application images. An existing
namespace must have this release's ownership metadata; foreign namespaces and
claims are refused before changes. Pending claims bind when the copy pod is scheduled.

The minimal copy helper uses only Python's standard library. Its unused package
installers and third-party libraries are removed completely, then the final image
is scanned and exported as a hash-verified OCI artifact. Imports and copies rescan
the exact OCI bytes; the pod uses `Never` pull policy. This does not authorize the
blocked WebUI or GPU images, whose deployment gates remain unchanged.

From the ordinary user's interactive terminal on the Spark, run:

```sh
make llm-backup-copy-data
```

This imports the already built helper (sudo for K3s), copies WebUI first, then
copies the entire Ollama state after interactive sudo authentication. Originals
are stopped and retained; failures do not overwrite PVCs or automatically restart
writers. The command does not activate either Kubernetes application. After a
successful copy, to continue using the originals while the standby remains stopped:

```sh
sudo systemctl start ollama
docker start open-webui
```

A resumed original can change after the snapshot. Do not mistake a one-time copy
for ongoing synchronization or a completed service cutover. Recopying into a
nonempty claim is deliberately refused.

## Commands and boundaries

```sh
python3 scripts/llm-backup.py plan
# Prepare PVCs/policies without running or approving application images:
python3 scripts/llm-backup.py prepare-storage
python3 scripts/llm-backup.py render --values .dev-tools/llm-backup/values.yaml
python3 scripts/llm-backup.py deploy --values .dev-tools/llm-backup/values.yaml
python3 scripts/llm-backup.py status
# Explicit maintenance operations, in your ordinary interactive terminal:
python3 scripts/llm-backup.py migrate webui
python3 scripts/llm-backup.py migrate ollama
python3 scripts/llm-backup.py start
python3 scripts/llm-backup.py stop
```

`PLATFORM_KUBECONFIG` defaults to the dedicated user-owned mode-600
`~/.kube/spark-platform.yaml`. Cluster operations require context `spark-platform`,
a verified loopback API endpoint and the expected Spark node. The CLI uses the
fixed root-owned K3s client; it never changes your default Kubernetes context.

Deploy renders/schema-validates before mutation, scans the actual selected image
digests through the current vulnerability/coverage gate, and forces both replicas
to zero. Start independently scans deployed application image references before
activation. Data migration instead scans only its exact reviewed Python copy
image, because it executes no Ollama or WebUI application image. Blank image digests and failed scans block operations;
there is no skip-security flag. Rendering is a review operation, not approval.

`ollama-data` requests 400 GiB and `webui-data` requests 20 GiB. Local-path storage
consumes physical disk as files are written. Confirm sufficient actual free space
for original data plus the additional copy. Estimate source disk use before migration; copying and reading back hundreds
of GiB can take hours.

## Migration procedure

1. Review the image digests, effective chart values, source-container environment
   and compatibility privately. Do not paste environment dumps, keys or raw scanner
   output into Git, chats or shared logs. Keep both standby deployments stopped.
2. Run `prepare-storage` to provision only the cold PVCs, policies and quota.
   It does not need candidate application images and never creates a Deployment,
   Pod, Service or Secret. Full application deployment remains a separate gated step.
3. Migrate WebUI and Ollama separately during a maintenance window. `migrate all`
   is sequential, not an atomic snapshot of both applications.
4. Inspect the resulting migration receipts and service configuration, then start
   only for an intentional standby test. Start returns **startup requested**;
   check readiness, model listings, embeddings, user login, old encrypted provider
   credentials and connection routing before relying on the copy.
5. Stop standby services after testing. Starting requires both original writers
   to be stopped and an available advertised NVIDIA GPU/runtime. An occupied or
   missing GPU blocks activation rather than silently sharing it.

Each copy requires both target deployments to have zero replicas and no remaining
workload pods. A unique UID-1000 helper mounts only its intended PVC, with bounded
resources, no service-account token, dropped capabilities, read-only root filesystem
and no host mounts. Its node selector lets the scheduler bind WaitForFirstConsumer
volumes. The helper uses only the exact digest-pinned, scanned official Python image,
independently of the blocked application images:

```
docker.io/library/python@sha256:a03d3aeb81ae3061d2e23874bf1a8ed418e919dcfe89f2ad4167bd58de639573
```

An empty target is checked **before stopping the source**, and checked again by
the streaming extractor. Existing contents, duplicate files, devices and parent-traversal paths are
refused. Relative cache links are created only after regular files are verified;
all targets must resolve inside the PVC. Absolute, escaping or broken links fail
the copy. No file extraction follows a link. File bytes are hashed while copying and read
back for comparison; key permissions are retained without preserving host UID/GID.
Files belong to the standby UID 1000.

For Ollama, interactive `sudo -v` authenticates visibly before stopping its systemd
service. Subsequent fixed `sudo -n systemctl`/`sudo -n tar` commands cannot hide a
password prompt inside the stream. The complete original `.ollama` state is read,
including identity keys and models. Runtime mounting must preserve the original
`.ollama` location relative to `HOME`; copying only `models/` is insufficient.

For WebUI, the original container is stopped gracefully before its complete data
volume is exported with `docker cp`. The original `/app/backend/.webui_secret_key`
is read from the retained container filesystem, including when stopped. The actual
key and secret/provider environment values travel in process memory and Kubernetes
Secret stdin, never command arguments or repository files. Both provider credentials
and provider URLs/enable flags are retained; the chart's explicit local settings
still take precedence. A conflicting existing Secret is refused, not overwritten.

The copied SQLite settings retain users and persistent configuration. Only known
local Ollama URLs (loopback, Docker host gateway or `ollama`, HTTP port 11434) are
retargeted to `http://ollama:11434` in the **copied** settings/environment. Other
provider endpoints, identities and keys remain unchanged. Unknown backend layouts,
conflicting configurations or an unverified effective signup policy block activation
for manual review. Do not disable persistent configuration globally to force a URL:
that can discard effective user/provider preferences. Confirm the final effective
connection and signup configuration at runtime.

## Failure and recovery

The original host directory, Docker container and volume are never removed or
modified by the copy. Writers remain stopped on success **and failure**. After
review, the owner can resume the originals with `sudo systemctl start ollama` and
`docker start open-webui`, while keeping standby replicas zero. After a successful
WebUI data-only copy, manually restarting the original Docker container keeps the
existing primary UI operational; the PVC then remains a point-in-time snapshot,
not a continuously synchronized copy. No automatic restart occurs after failure. Do not run both
Ollama instances against the single GPU.

The helper is removed in cleanup, including failures. A copying/failed marker and
partial target contents deliberately prevent automatic retry or overwrite. Inspect
and preserve partial data; a new PVC or explicit cleanup needs a separate reviewed
operator decision. The CLI never deletes PVCs or namespaces.

A completion receipt records file/byte counts and the PVC UID. Recreating a claim
with the same name invalidates its old receipt. Receipts are integrity/operational
checks, not a signed security attestation, image compatibility test or independent
backup. A newer WebUI may upgrade its copied database irreversibly; keep the source
intact until a tested rollback/recovery procedure exists.

## Disk and source-path safeguards

The importer checks destination filesystem free space before stopping a writer:
Ollama uses a bounded `sudo -n du --bytes` estimate plus a 20-GiB reserve; WebUI uses
its conservative 20-GiB copy ceiling plus the same reserve. During extraction,
each file must fit while retaining 20 GiB of free space, and total payload is
capped at 400 GiB for Ollama or 20 GiB for WebUI. A LocalPath PVC's requested size
is not a filesystem quota; these checks constrain this copy, not future runtime
growth. Copy failure deliberately leaves partial data and a failed marker.

The fixed Ollama source and every parent must be real directories, owned by root
or the system Ollama account, without group/world write permissions. The source
itself must belong to that account. Checks run before privileged reads and again
after stopping the service. These are not an atomic directory-fd security boundary:
a compromised Ollama account with another live process could race a path change.
Do not migrate from an untrusted/compromised source account. No new privileged
helper, host mount or Docker-based host-permission bypass is introduced.

Source `DATABASE_URL`, `DB_TYPE` and `DATABASE_TYPE` are checked before WebUI is
stopped. External databases and nonstandard SQLite paths are unsupported and block
migration: copying a Docker volume does not back up PostgreSQL/MySQL. Arrange a
separate consistent database export and reviewed restore procedure instead.

Producer stderr is discarded rather than buffered into a pipe or exposed to
shared logs. This prevents verbose source errors from deadlocking a large stream.
Failures report a safe operational reason; the tool never prints source tar data,
secret environment dumps or SQL contents.


## Storage-only preparation while application images are blocked

`prepare-storage` schema-validates the existing Helm chart using explicitly
synthetic image digests for rendering only. It then discards **every** workload,
Service and Secret, retaining exactly two PVCs, three NetworkPolicies and one
ResourceQuota with the expected names, namespace and API versions. Synthetic image
references never reach Kubernetes. Existing PyYAML performs safe YAML parsing;
run the repository's normal development setup first if its environment is missing.

The retained resources receive Helm's exact ownership label and release-name /
release-namespace annotations for a later real `llm-backup` release. Preparation
checks existing resource ownership before any mutation. It refuses unmanaged or
foreign-owned PVCs and changes to their storage class, access mode or requested
size. A correctly owned existing PVC is skipped entirely, preserving its volume
binding and contents without reapplying or resizing it. The fixed namespace is
created only when absent through the same dedicated context guard.

`migrate` requires these owned claims and cold/absent application deployments. It
freshly scans and runs only the pinned Python helper; it never pulls or executes a
vulnerable provider image merely to copy files. A failed helper scan blocks the
copy before any helper is created or source writer is stopped. This is not an
exception to application security: full deploy/start still require current clean
application-image gates, GPU readiness where needed and compatibility checks.

Storage-only copying is useful progress while Chroma/embedding compatibility,
application dependencies or GPU addon images remain blocked. It does not establish
that the new applications work or that the snapshot is an independent backup.
Ollama's privileged source read still requires the owner in an interactive terminal.

## Recovering the rejected cache-symlink copy

The original importer rejected legitimate relative Hugging Face cache links.
The corrected importer defers links until after regular-file verification and
requires every link to resolve inside the volume. Copy failures now retain a
private diagnostic at `.dev-tools/reports/llm-copy-last-error.log`.

For this specific failure, with WebUI stopped and no completed migration receipt:

```sh
.venv/bin/python scripts/llm-backup.py recover-failed-webui
.venv/bin/python scripts/llm-backup.py migrate webui
```

Recovery accepts only the observed partial layout: `cache/` and the two empty
failure/copying markers. It preserves that complete partial state as a private
archive under `.dev-tools/recovery/`, compares every file hash and permission,
rechecks the live PVC and only then removes the backed-up partial contents. It
never deletes the PVC or modifies the source container. Other failure layouts
need separate review; they are refused by this recovery action.

## Start only Ollama in K3s

After the native patched GPU image is built and verified, `make ollama-up`
imports it (sudo), enables GPU scheduling and starts Ollama from its migrated
PVC without deploying or scanning the unused WebUI image. The full pair's image
gates are unchanged. `make deploy-ollama` prepares a zero-replica Deployment and
internal Service; `make start-ollama` verifies the receipt, stopped host source,
current Ollama image and GPU availability, then waits for readiness.

`make ollama-access` forwards only the internal API to Spark localhost:11434.
Keep the API behind NVIDIA Sync/Tailscale/SSH rather than exposing an unauthenticated
model-management endpoint publicly. WebUI can be migrated and activated separately
when its image gate is resolved.

## Persistent shared-port API access

`make deploy-ollama-gateway` refreshes only the owned LLM network policies and
upgrades the existing docs gateway. It never changes Ollama replicas or PVCs.
The gateway's existing systemd forwarder and NVIDIA Sync mapping on port 8088
serve `http://ollama.spark.test:8088`, including native `/api/...` and
OpenAI-compatible `/v1/...` paths. Request/response streaming is preserved and
model blob uploads are capped at 40 GiB per request. The gateway timeout is an
inactivity timeout of 600 seconds between upstream reads.

The Ollama policy permits only the named docs pods in docs-dev/docs-prod and
its local WebUI peer; gateway egress is scoped to the LLM namespace/release and
TCP11434. A separate `make ollama-access` process or port11434 mapping is optional.
Keep this unauthenticated API private through Sync/Tailscale/SSH.

On a Mac without hosts-file permission, test the existing localhost mapping:

```sh
curl -H 'Host: ollama.spark.test' http://localhost:8088/api/tags
```

For ordinary hostname access add `127.0.0.1 ollama.spark.test` to hosts, or relaunch
the dedicated Chrome instance with the additional hostname mapping shown on the
docs page. Chrome resolver mappings apply only to that browser, not curl or SDKs.

## Native CLI on the Spark

The host CLI connects to the K3s API through the existing boot-time gateway:

```sh
export OLLAMA_HOST=http://127.0.0.1:8088/ollama
ollama list
ollama run qwen3.8:latest
```

`make -s ollama-env` prints that export for use with `eval "$(make -s ollama-env)"`.
Keep this client setting in the user's shell profile; it replaces the old host
server address `0.0.0.0:11434`. Existing terminals need the export once; new
interactive shells read the updated profile. Normal local-file commands such as
`ollama create` still use the native CLI and local filesystem, with blobs uploaded
to the K3s server through the gateway. Do not start a second `ollama serve` process.

The `/ollama/` prefix strips only that leading raw path segment and preserves
query strings, request methods, streaming and authentication headers. Bare
`/ollama` redirects relatively to `/ollama/`. Static docs remain at `/`, and the
existing Ollama hostname route remains available. This path also works over the
Mac's existing Sync8088 mapping, without a hosts entry or custom Host header:

```sh
curl http://localhost:8088/ollama/api/tags
```

OpenAI-compatible clients can use `http://localhost:8088/ollama/v1`.

The public Ollama-only values pin its reviewed ARM64 image. A private overlay is
optional for that path; the unused WebUI schema placeholder is discarded with
all WebUI resources before any apply. Pair deployment still requires an explicit,
scan-passing WebUI image. A specifically requested missing overlay is refused.
