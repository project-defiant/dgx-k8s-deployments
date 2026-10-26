# Standby Ollama and Open WebUI

The two deployments start at zero replicas. PVCs and internal services remain
present while stopped. Supply reviewed ARM64 image digests after apps/llm-backup/
values.yaml; no image tags or synthetic deployment digests are committed.

Ollama requires RuntimeClass nvidia and exactly one GPU when started. Admission
requesting a GPU does not prove the NVIDIA runtime/device plugin is installed or
that another model workload leaves enough unified memory. The platform start
command must verify capacity and stop competing inference first. Limits are
96Gi Ollama and 4Gi WebUI, with a namespace quota for both workloads. Never run a
second Ollama writer against the same ReadWriteOnce model store.

Retained claims:
- ollama-data: 400Gi mounted /data/.ollama; OLLAMA_MODELS=/data/.ollama/models and HOME=/data.
- webui-data: 20Gi mounted /app/backend/data (database, uploads and caches).

Both workloads use non-root UID/GID1000, fsGroup1000, read-only root filesystem,
RuntimeDefault seccomp and dropped capabilities. Writable temporary storage is
bounded to 1Gi per pod. Actual selected images must pass runtime tests with these
settings before deployment; the template alone does not prove compatibility.

The existing Secret llm-backup-webui must provide WEBUI_SECRET_KEY. Its other
keys are also imported via envFrom to preserve migrated provider/OAuth secrets;
explicit chart environment settings take precedence over same-named Secret keys. Preserve the
key and UI database together during migration or authentication/encrypted provider
settings may stop working. No Secret values or generated admin accounts belong
in this chart. Authentication is explicitly enabled and signup is disabled; migrate the existing administrator/database.
Check persisted application settings after migration: application database
configuration can override environment defaults in Open WebUI.

The WebUI internal backend URL is http://ollama:11434. Only docs-dev or
docs-prod pods labelled instance=docs can access WebUI; only this WebUI can access
Ollama. Both have DNS egress. Ollama internetHTTPS defaults true to preserve model
download/cloud functionality, allowing public IPv4 TCP443 while excluding private,
loopback, Tailscale and link-local destinations. Set it false for offline models.
This is IP-based filtering, not a domain allowlist, and needs an enforcing CNI.

WebUI has egress only to Ollama and DNS initially. Existing direct external API
providers, embedding/model downloads and OAuth/mail integrations need separately
reviewed egress rules before they function. No cloud-disable setting is forced on
Ollama and no existing provider credentials/settings are overwritten by this chart.
Stop both writers and back up both claims plus the independently protected secret
before migration or restore. Scaling down preserves claims; uninstall keeps them.

Official image/runtime references for selecting compatible versions:
- https://docs.ollama.com/docker
- https://docs.ollama.com/faq
- https://docs.openwebui.com/getting-started/env-configuration/

Deploy in the dedicated llm-backup namespace/release. Deployment and Service
names are ollama and open-webui; claim names are ollama-data and webui-data.
This chart intentionally supports one such service pair per namespace.

Migration copies the original .ollama directory contents to the model claim root.
Mounting that root at /data/.ollama preserves both models and id_ed25519 identity
without duplicating model blobs. Ollama parallel requests and loaded models are
both capped at one to limit memory contention. The namespace default-deny policy
also selects unlabelled migration helpers, protecting copied data from network
access; component policies add only the documented application connections.
