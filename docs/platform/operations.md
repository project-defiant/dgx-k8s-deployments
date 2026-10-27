# Operate the first application

Run commands on the Spark from the implementation checkout. No default kubectl
context is changed. `PLATFORM_KUBECONFIG` can select a protected dedicated file.

| Task | Command |
|---|---|
| Validate manifests | `make validate` |
| CLI/chart checks | `make test` |
| Deploy development | `make deploy-docs` |
| Enable Ollama on shared gateway | `make deploy-ollama-gateway` |
| Deploy production | `make deploy-docs ENV=prod` |
| Inspect resources | `make status` |
| Private local listener | `make access` |
| Install/start listener at boot | `make docs-service-install` |
| Inspect boot listener | `make docs-service-status` |
| Listener logs | `make docs-service-logs` |
| Stop/disable boot listener | `make docs-service-stop` |
| Inspect release history | `helm --kubeconfig "$HOME/.kube/spark-platform.yaml" --kube-context spark-platform -n docs-dev history docs` |
| Roll back | `PLATFORM_REVISION=1 make rollback` |

The site is deliberately small and static. Modify the content in
`apps/docs/values.yaml`, validate, then deploy development. Promote the same
file/image digest to production. Config checksums trigger a pod rollout; Helm
history retains prior content for rollback. Keep ConfigMaps below Kubernetes'
1MiB limit. A larger docs site should be built into an image in a later phase.

The reusable chart exposes only ClusterIP. Quotas are namespace-wide; don't
enable duplicate quota objects per app when reusing a shared namespace. The
optional retained PVC is intentionally not deleted on uninstall. Confirm volume
ownership and backups before removing retained resources manually.

The port-forward listens on 127.0.0.1:8088 only. Use `make access` for a manual
session or `make docs-service-install` to keep it running through systemd.
Stop an existing manual forward before installing the service to avoid a port
conflict. Installation requires sudo, validates the dedicated cluster, copies
the unit to `/etc/systemd/system/spark-docs-access.service`, enables boot startup
and starts it immediately. It does not redeploy the Kubernetes app.
Run `make docs-service-install` from your normal login shell, **without `sudo`
before `make`**. The target requests sudo internally while preserving your
non-root username and home-directory kubeconfig. From a root shell, return to
your normal account first; the forwarding process must not run as root.

The service uses your non-root account and a namespace-scoped kubeconfig at
`/var/lib/spark-docs-access/kubeconfig`, starts after K3s and its credential job,
and retries every five seconds if Kubernetes is unavailable or a pod is replaced.
It invokes the fixed root-owned K3s binary, hides home directories and cannot read
your administrator kubeconfig. The checkout is not required at runtime.
`/run/spark-docs-access` holds its cache. The restricted identity can discover docs
pods and forward their ports, but cannot read Secrets or modify workloads.

A root-only oneshot uses the protected root K3s administrator config to obtain a
24-hour token. A persistent timer renews it every 12 hours; the forwarder also
restarts at most every 11 hours to reopen current credentials. Tokens never go in
command arguments or logs. The privileged helper/config stay in
`/etc/spark-docs-access`; preserve root ownership and permissions. The human admin
kubeconfig is used only under your account for installation validation, not by the
forwarding process. Stopping the service through Make also disables its timer.
The refresher reads K3s's protected generated config directly, so it picks up
certificate renewals instead of relying on a stale administrator-config copy.

`ENV=prod make docs-service-install` selects production; the default is dev.
`DOCS_PORT=8089 make docs-service-install` changes the listening port. Both update
the same unit; managed reinstalls for the same account/environment are supported.
Changing that identity/environment requires an explicit credential/RBAC migration,
so the installer refuses it instead of silently reusing old state.
`make docs-service-render` previews units and RBAC without host changes.
Status reports the forwarding process; verify HTTP as well before considering
the app accessible. Automatic service recovery has not yet been reboot-tested.

Remote clients reach it using NVIDIA Sync port mapping (8088 to 8088) or an
ordinary SSH tunnel over the existing tailnet. Reconnect Sync/the tunnel after
client disconnection; the systemd service manages only the Spark-side listener.
This does not configure public ingress, automatic HTTPS or Funnel.

For another app, use a separate release and namespace, provide an ARM64 image
digest, command/args, port and correct HTTP probe path. Supply CPU/memory budgets.
Do not change the chart's ClusterIP type to expose apps directly on the host.

## Reboot verification

Schedule the reboot with the owner; confirm a fallback path and no important
Docker/GPU jobs are running. Record site content and fixture hashes, run a backup
and confirm it is recoverable, then reboot from the host terminal. After host
networking returns, measure time until node/docs become healthy (initial target:
under 10 minutes). Reconnect SSH/tunnel and verify fixture hashes.

Do not mark this verified from merely restarting the docs pod or K3s service.
The planned full-host reboot has not happened until recorded in verification.md.

## Private service links

The docs landing page keeps these links for the shared-port gateway:

| Link | Destination |
|---|---|
| [docs.spark.test:8088](http://docs.spark.test:8088/) | Platform docs |
| [git.spark.test:8088](http://git.spark.test:8088/) | Forgejo |

After `make deploy-services`, keep one NVIDIA Sync mapping from
Mac port 8088 to Spark port 8088. On the Mac, add these entries to `/etc/hosts`:

```text
127.0.0.1 docs.spark.test
127.0.0.1 git.spark.test
```

Both names resolve to the Mac's local tunnel endpoint; the future Spark gateway
routes by the HTTP hostname. These entries do not require public DNS.
`http://localhost:8088` continues to serve docs. Complete Forgejo's private
installation wizard and administrator setup on first use; see [the Forgejo guide](forgejo.md).

## Future nodes

The NanoPi is not required yet. Its arrival needs backup/restore and a reviewed
control-plane migration; adding a node does not transfer local PV data. Optional
workstation jobs must tolerate that machine being off. GPU inference and its
memory budget are outside this milestone; the old vLLM manifest is not applied
to the new cluster automatically.

Ollama uses the same persistent gateway as docs and Forgejo:
`http://ollama.spark.test:8088/api/tags` lists available models, and
`http://ollama.spark.test:8088/v1` is the OpenAI-compatible API base. The service
has no web chat UI. Use the existing NVIDIA Sync8088 mapping and add the Ollama
hostname to the client resolver. The docs page includes a Chrome-only alternative
for Macs without hosts-file permissions and a curl example using the Host header.
