# Host inventory and preservation

Run `make dev` before development and `make preflight` / `make bootstrap-check`
before a host installation. The latter commands inspect prerequisites and routes;
they do not start Minikube, install K3s, or reset Tailscale.

Record the following privately, outside this public repository:

- Host OS, kernel, architecture, cgroups, RAM and free persistent disk space.
- GPU driver/toolkit compatibility and existing inference workloads.
- LAN, VPN, Docker and existing-cluster routes; listening ports and firewall rules.
- Existing Docker/Minikube services, volumes, data ownership and backup locations.
- Independent remote access and a fallback path for host/network changes.

Do not print Secrets, credentials, full container environments, or kubeconfigs in
shared logs. A stopped cluster may still hold important persistent data. Inspect
it safely and verify recovery exports before any destructive migration. Never
prune Docker, delete Minikube profiles or delete existing claims as a shortcut.
Preserving an existing volume is not proof of a usable off-machine backup.

The example pod and service networks are 10.84.0.0/16 and 10.85.0.0/16. Check all
local and remote routes for overlaps; adapt the configuration and corresponding
firewall/network policies together if different networks are needed. Do not reuse
another machine's inventory as evidence that your routes are safe.

Keep a second SSH session open during installation and verify remote access before
closing the original. Never enable/disable a global firewall or reset VPN settings
without reviewing the effect on your access path. Existing Docker listeners are
outside the new application's access policy and require their own review.
