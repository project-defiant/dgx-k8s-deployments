# TheDefiant private platform

Single-machine K3s foundation for private applications on the ARM64 DGX Spark.
The first app is a small documentation site, deployed with a reusable Helm chart.
Existing Minikube/vLLM resources remain a separate legacy deployment.

For the cold-standby Ollama/Open WebUI migration, see the
[LLM backup runbook](docs/platform/llm-backup.md). Model and UI data use separate
retained PVCs; selected images and GPU readiness must pass the deployment gates.

## Start here

Before any development update, run **`make dev`**. It installs and verifies the
pinned development tools, configures prek hooks for this worktree, and runs the
security, manifest and regression checks. See [CONTRIBUTING.md](CONTRIBUTING.md)
for prerequisites and the development workflow. `make check` repeats the gate.

Development prerequisites: Linux ARM64 or x86_64, Bash, Git, Python 3.12+, uv,
curl, GnuPG and Docker with Buildx. `make dev` installs the pinned local scanners, Helm and locked
Python dev tools; it never installs system packages or uses sudo.
Host operations additionally require kubectl, OpenSSH, systemd and the
prerequisites checked by preflight. Host installation requires sudo.

Forgejo uses a locally patched LTS image because the checked official images
failed the vulnerability gate. The first `make dev` builds a native image from
verified release sources and a patched Go compiler; later runs verify and reuse
it. This needs Docker access and initial network downloads. It does not import
images into K3s or deploy anything.

To deploy the shared private docs/Git endpoint after the platform is installed:

```sh
make dev
make verify-forgejo-runtime
make forgejo-image-import  # run as your normal user; prompts for sudo
make deploy-services
make forgejo-status
```

Use NVIDIA Sync's existing 8088 mapping and the Mac hostname entries in
[the Forgejo runbook](docs/platform/forgejo.md). The private initial wizard lets
you choose your administrator credentials. SSH, package hosting and Actions are
disabled initially. No application password is stored in the repository.

1. Run `make preflight` and `make bootstrap-check`; review
   [preservation and host checks](docs/platform/preflight.md).
2. Follow [bootstrap](docs/platform/bootstrap.md) to install the pinned K3s
   release and obtain a dedicated kubeconfig. Installation changes the host;
   validation commands do not.
3. Run `make validate test`, then `make deploy-docs`.
4. Run `make access` on the Spark. From your client, forward through its existing
   Tailscale SSH connection, then visit `http://127.0.0.1:8088`:

   ```sh
   ssh -N -L 8088:127.0.0.1:8088 YOUR_USER@YOUR_SPARK_HOST
   ```

   Substitute your SSH user and Spark hostname or Tailscale address. Ordinary
   OpenSSH over Tailscale is sufficient; enabling Tailscale SSH is unnecessary.

`make status` lists workloads, GPU DaemonSets, services and storage across all
K3s namespaces, including Forgejo and Ollama. `make docs-status` retains the
focused docs view; use `make docs-status ENV=prod` for production docs.

`ENV=prod` selects a separate namespace; both use the same image and content.
`PLATFORM_KUBECONFIG` overrides the dedicated file `~/.kube/spark-platform.yaml`.
Platform commands require context `spark-platform` and the local K3s endpoint;
your default Minikube context is left untouched.

For automatic docs access after boot, stop any manual `make access` session and
run `make docs-service-install` on the Spark as your normal login user, not
`sudo make`; the target requests sudo internally. This installs a systemd service
running as your user with scoped expiring credentials, restarts failed forwards,
and keeps localhost port 8088
available without a terminal. NVIDIA Sync can then forward port 8088 directly.
Use `ENV=prod` to select production or `DOCS_PORT=8089` to choose another port.
`make docs-service-status`, `make docs-service-logs` and `make docs-service-stop`
inspect, troubleshoot, or disable it. Installing again updates the same service;
it does not create a second dev/prod listener. See [operations](docs/platform/operations.md).

See [operations](docs/platform/operations.md), [recovery](docs/platform/recovery.md)
and [verification status](docs/platform/verification.md). A same-disk archive is
not an off-machine backup. Reboot and restore checks must be completed on the
real host before calling the milestone complete.

## Legacy GPU deployment

`make minikube-start` starts the old cluster. `HF_TOKEN` must be set before
`make deploy` or `make deploy-vllm--gemma-4-31b`. These target context `minikube`
explicitly; failures are not suppressed. Never run duplicate GPU deployments
without budgeting unified memory first.

The legacy `gemma4-31b.yaml` requests one NVIDIA GPU through RuntimeClass
`nvidia`, listens on port 8000, exposes internal Service port 80, and uses a 30Gi PVC for
the Hugging Face cache. This manifest is not yet migrated or validated for K3s.
`make destroy-vllm--gemma-4-31b` also deletes its PVC: preserve data first.

The legacy image is digest-pinned but its GPU runtime, full image CVE inventory,
and non-root/read-only compatibility are not verified. See the narrowly scoped,
expiring [security exceptions](docs/platform/security-exceptions.md) before enabling it.
