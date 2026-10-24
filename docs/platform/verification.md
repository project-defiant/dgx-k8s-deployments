# Verification guide

Keep actual host inventories, private endpoints, operational logs and recovery
evidence outside this public repository. This file defines checks; it does not
claim that a particular user's machine has passed them.

## Development gate

Run `make dev` before updates. It installs/verifies the pinned toolchain, configures
worktree-local prek hooks and runs `make check`. Resolve findings before proceeding.
CI repeats the checks independently without deployment credentials or root access.

## Live deployment checks

| Check | Required evidence |
|---|---|
| Preservation | Existing volumes retained and important data backed up |
| Cluster | ARM64 node Ready, core DNS/storage healthy |
| Deployment | Docs dev/prod healthy with intended image/content |
| Private access | HTTP through localhost forwarding and the intended VPN client |
| Exposure | No unintended host/public listeners; independent external-port checks |
| Delivery | Content rollout followed by verified rollback |
| Resource policy | Oversized request rejected; bounded load leaves docs responsive |
| Persistence | Known fixture content survives pod replacement |
| Restore | Encrypted off-machine backup retrieved into an isolated replacement volume |
| Reboot | Planned host reboot recovers node/apps and private access |

Record exact versions/digests, dates, outcomes, file hashes and recovery timings
in a private verification record. A local backup round trip is not an off-machine
restore. Restarting a pod, K3s or the coding agent is not a host reboot. Passing a
scanner with no package inventory is not evidence of a vulnerability-free image.

`scripts/platform-smoke.py` exercises deployments, rollout/rollback and disposable
fixture data against the explicit platform context. It changes those test releases;
inspect it and select a safe window before use. Never substitute real application
data for disposable fixtures. Root installation and live checks are not run by PR CI.
