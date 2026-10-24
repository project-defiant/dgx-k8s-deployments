# Deployment hardening

`make dev` and `make check` validate source. They do not change a running cluster.
After reviewing the code, deploy changed charts/configuration separately and
verify the effective controls on the host. See [SECURITY.md](../../SECURITY.md).

## Secrets-at-rest encryption

New installations enable `secrets-encryption: true`. Do not overwrite an existing
K3s configuration simply to make it match the new template. The bootstrap refuses
changed managed configuration intentionally; review the migration instead.

For the pinned K3s version, enabling encryption on an existing server requires
a restart and reencryption. First verify consistent backups of the datastore,
server token, configuration and recovery keys, and arrange a maintenance window.
Use `make secrets-encryption-status` to inspect the current state without printing
secret values. Follow the single-server procedure in the
[official K3s guide](https://docs.k3s.io/cli/secrets-encrypt#enable-secrets-encryption-on-an-existing-cluster):

1. Confirm encryption is currently disabled.
2. Run `sudo /usr/local/bin/k3s secrets-encrypt enable`.
3. Add `secrets-encryption: true` to the protected host config and restart K3s.
4. Verify status is at the documented disabled/start transition with matching hashes.
5. Run `sudo /usr/local/bin/k3s secrets-encrypt rotate-keys` and wait for reencryption.
6. Restart K3s and verify encryption Enabled, `reencrypt_finished`, and matching hashes.

Do not skip checks or use force flags to hide unexpected state. Stop and use the
matching release's recovery procedure if a stage differs. Back up the resulting
encryption configuration and server token privately. Filesystem encryption and
host access controls still matter; host root can access running workloads.

## Forwarding identity

The optional systemd forwarding service uses a namespace-scoped identity rather
than giving its long-running process the administrator kubeconfig. A protected
root-only refresher obtains expiring credentials and renews them automatically;
keep its privileged credential source and installed helper private and root-owned.
The application identity must not read Secrets, modify workloads or access other
namespaces. Re-run the documented service installer after reviewing its upgrade
requirements; source changes alone do not replace installed root units.

## Network and image checks

Private service discovery is not an authentication boundary by itself. Keep
forwarders bound to localhost, verify the VPN policy and intended client mapping,
and test network isolation before introducing applications that hold private data.
Never expose inference solely on the assumption that a model-download token
authenticates API clients.

Review exact image digests and scanner package coverage. Keep GPU image-specific
permissions narrowly justified; do not blindly make a GPU container read-only or
non-root without validating startup, cache and shared-memory behavior. A legacy
manifest that is not runtime-tested must remain labeled as such.
