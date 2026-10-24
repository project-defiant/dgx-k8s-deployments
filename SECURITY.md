# Security policy

This is a personal infrastructure project. Public source is not permission to
access a deployed host. Examples default to private access; applications requiring
broader exposure need explicit authentication and an exposure review.

## Report a vulnerability

Use the repository's private vulnerability-reporting/advisory mechanism when
available. Do not publish tokens, kubeconfigs, private addresses or exploit output
containing credentials in public issues. If private reporting is not enabled,
request a private reporting channel without disclosing sensitive details.

## Boundaries

- Root installation and credential-refresh commands are trusted administrative
  operations, reviewed separately from ordinary app deployment.
- Kubernetes namespace and credential separation do not alone prove network
  isolation. Apply and test the intended NetworkPolicies and client access paths.
- Secrets in Kubernetes are not secured merely by base64 encoding. Enable and
  verify encryption at rest and protect its recovery keys before valuable data
  is introduced. Existing installations require a deliberate migration/restart.
- Local volumes and local Git history are not off-machine backups. Verify a
  restore and keep recovery credentials independently available.
- Digest-pinned images and checksummed binaries still require updates and review.
  Scanning cannot prove the absence of malicious behavior or host compromise.

## Before making a release public

Run `make dev` / `make check` on the actual release candidate, scan all available
history and staged/current files, inspect the publication file list and commit
metadata, and confirm GitHub secret scanning/push protection are enabled. Keep
local inventory and scanner reports private. CI should run on hosted disposable
runners, use pinned actions and read-only permissions, and have no tailnet access
or deployment credentials. Enable required status checks on the default branch
through repository administration before treating the workflow as enforced.

GitHub settings and repository visibility are separate administrative actions;
running the local gate does not change them or rewrite history.
