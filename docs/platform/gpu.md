# GPU registration on the single Spark

The existing NVIDIA RuntimeClass lets a container use the host's NVIDIA runtime;
it does not itself advertise `nvidia.com/gpu` to Kubernetes. The small NVIDIA
device-plugin DaemonSet supplies that registration. This step does not install
host drivers, change containerd/Docker configuration, restart K3s or enable an
inference workload.

## Review and deploy

```sh
python3 scripts/platform-gpu.py plan
python3 scripts/platform-gpu.py render
python3 scripts/platform-gpu.py status
python3 scripts/platform-gpu.py deploy
```

Use the ordinary platform account and the private, user-owned mode-600 dedicated
kubeconfig (`PLATFORM_KUBECONFIG`, default `~/.kube/spark-platform.yaml`). The CLI
requires the root-owned K3s client, explicit `spark-platform` context, verified
loopback API, expected node name and ARM64 architecture. No host `sudo` is needed.

The reviewed manifest pins NVIDIA device plugin **v0.20.1** to ARM64 digest:

```
nvcr.io/nvidia/k8s-device-plugin@sha256:61fb21fe1cca238426cde548aaa0e24fe0eeab497833c715e969c90bfe12910b
```

The deployment CLI binds the complete reviewed manifest bytes to a SHA256 and
checks the manifest image against its scanner target. It validates Kubernetes
schemas with strict Kubeconform, runs the current image vulnerability/coverage
gate, and applies those same captured bytes through stdin. Any manifest drift or
failed scan blocks deployment. Updating the image or manifest requires a focused
review and updating the binding together; do not bypass it with a generic ignore.

The static manifest follows NVIDIA's [official static DaemonSet](https://github.com/NVIDIA/k8s-device-plugin/blob/v0.20.1/deployments/static/nvidia-device-plugin.yml),
limited to `spark-platform`. Its environment exposes the physical NVIDIA device
and compute/utility driver libraries, and `FAIL_ON_INIT_ERROR=true` makes failed
initialization visible. No time slicing, MPS, virtual GPU replicas or other sharing
configuration is introduced. This Spark is expected to advertise **one** GPU.

## Why this addon has a host mount and UID 0

Kubelet's device-plugin protocol uses Unix sockets in the root-owned
`/var/lib/kubelet/device-plugins` directory. The plugin must create its registration
socket there and communicate with kubelet. This reviewed addon therefore has
exactly one writable hostPath, that directory with `type: Directory`, and runs as
UID/GID 0. It does not mount `/`, host credentials, Docker sockets, arbitrary
application data or driver directories through additional hostPath volumes.

The container is not privileged, cannot escalate privileges, drops all Linux
capabilities, uses RuntimeDefault seccomp and a read-only root filesystem. It has
no service-account token and no host network/PID/IPC namespaces. Resources are
bounded to 500m CPU and 512 MiB, with requests of 100m/128 MiB. GPU devices and driver
libraries are supplied through the existing NVIDIA runtime. A compromised plugin
can still damage GPU registration or other sockets in that directory: this is a
trusted node addon, not an ordinary application privilege profile.

This narrow exception must remain tied to this reviewed addon and exact socket
path. Never weaken the general app chart or globally suppress root/hostPath rules
to accommodate it. No GPU Operator, Node Feature Discovery or extra controller is
required for this single-node registration step.

## Verification and limits

Deploy waits for the DaemonSet rollout, then separately requires the node to
advertise exactly one allocatable GPU. A Ready plugin pod with zero advertised
GPUs is an error. More than one GPU is also refused pending hardware/sharing
review. Inspect private pod logs for NVML/initialization errors; there is no
silent CPU fallback.

Registration does **not** prove a CUDA inference workload works, that its image is
compatible with GB10, or that enough unified memory is free. Validate those with a
small GPU workload before enabling standby model serving. Host Ollama and other
unmanaged host processes are not counted by Kubernetes GPU scheduling; stop the
primary instance and check actual capacity before activating a GPU standby.

If rollout or registration fails, the CLI reports failure and leaves the addon
available for diagnosis. It does not silently change drivers, reset Tailscale or
remove existing workloads. The status command reports both pod readiness and GPU
allocation; it returns failure until both match the single-node expectation.

For rollback, first stop workloads using Kubernetes GPU allocations. Then delete
only this DaemonSet using the dedicated context and `kube-system` namespace:

```sh
/usr/local/bin/k3s kubectl --kubeconfig "$HOME/.kube/spark-platform.yaml" \
  --context spark-platform -n kube-system delete daemonset nvidia-device-plugin-daemonset
```

Use `PLATFORM_KUBECONFIG` instead of the example path if customized. Do not remove
the existing RuntimeClass, drivers or socket directory as part of this rollback.

## Current migration gate

The reviewed v0.20.1 image failed the repository HIGH/CRITICAL vulnerability gate
when assessed. It has not been deployed. A healthy RuntimeClass alone does not
advertise a GPU; application activation remains blocked until a scan-passing,
verified device-plugin build is available. Storage-only data copying needs no GPU.

## Patched native build and Ollama-only startup

`containers/gpu/inputs.lock.json` pins the NVIDIA source commit, Go compiler
archive and an ARM64 runtime base. `make gpu-image-build` verifies both downloaded
inputs under `.dev-tools/gpu-inputs`, compiles the plugin with patched Go and
`golang.org/x/net`, scans the final image and exports a verified OCI archive.
The runtime base shares layers with the reviewed Ollama image. The image default
UID is 1000; only the exact reviewed DaemonSet overrides it to UID0 for its single
kubelet socket directory. There is no privileged container or host driver install.

The patched artifact supersedes the blocked official plugin image described
above. The gate requires actual binary inventory with the expected patched Go
and x/net versions, plus zero unapproved HIGH/CRITICAL findings. The manifest's
exact image digest and socket configuration remain bound to the deployment CLI.

After the native build has passed, run as the ordinary user on the Spark:

```sh
make ollama-up
```

This requests sudo only for importing/tagging the verified image in K3s, installs
the reviewed device plugin, requires exactly one advertised GPU, prepares only
Ollama's Deployment/Service, disables the old host Ollama unit to prevent duplicate
servers after reboot, and starts the Kubernetes workload using the already migrated PVC.
WebUI stays stopped and its image is never deployed by this path. Existing
receipts, ownership, source-writer and image gates remain enforced.

Use `make ollama-access` for a loopback-only API forward on port 11434. NVIDIA
Sync can forward that port to a client. Ollama HTTP is not authenticated by this
chart; keep it private. The Deployment survives restarts and does not depend on
an interactive host model-server process.

The owner-run command also requires sudo for `systemctl disable --now ollama`.
For a deliberate rollback, first stop the Kubernetes workload with
`make stop-ollama`, then use `sudo systemctl enable --now ollama` to restore the
original host service. The original data store is retained and may differ from
any changes made later in the Kubernetes PVC.
