# Backup and recovery

A single Spark accepts downtime. Recovery requires an off-machine encrypted copy,
Git-managed manifests, and access to recovery keys outside this host. Model caches
and reproducible container images need not be backed up. This tool backs up a
**consistent directory export**, not arbitrary live volumes or the entire cluster.

## Keys and destination

Install the recovery recipient's public GPG key in your normal keyring. Keep the
private key and its passphrase outside this machine; test decryption before relying
on backups. Pass the full public fingerprint with `--recipient`. Never put private
keys, passphrases or passwords in Git or command arguments. GPG uses its existing
agent for private-key access during restoration.

No remote target is configured by default. Once you have selected a separate host
and verified its SSH host key, optionally use `--destination user@host:/absolute/path`.
The remote directory must already exist. Copying to another directory on the Spark
is not an off-machine backup. This first version uses SSH/SCP, not a cloud-object
storage API; retention and scheduled exports are not implemented.

## Export application data consistently

For a database, use its native consistent export (for example `pg_dump`); copying
live database files is unsafe. Pause application writers for file-backed data and
keep them paused until the staging export is complete. Do not copy mutable PVCs
while the application is writing. Include uploads, repository data and recovery
configuration. Keep credentials separate and encrypted.

For the docs fixture, scale its deployment to zero and wait for its pods to stop.
Mount its claim read-only in a temporary exporter pod in the same namespace. Use
an approved ARM64 image containing `tar`, with the claim mounted at `/data`; avoid
assuming the serving image contains shell or archive tools. Stream the archive to
a local file with `kubectl exec EXPORTER -- tar -C /data -cf - .`, then unpack it
into a new local staging directory. Check the command exit status before proceeding.
Delete the exporter and restart the original deployment after export. The exporter
must mount the same claim, not an empty replacement. This procedure requires explicit
cluster/context selection matching the platform scripts.

```sh
python3 scripts/platform-backup.py backup /path/to/consistent-export /path/to/export.tar.gpg --recipient PUBLIC_KEY_FINGERPRINT
# Optional, only after choosing and validating a real off-machine destination:
python3 scripts/platform-backup.py backup /path/to/consistent-export /path/to/next-export.tar.gpg --recipient PUBLIC_KEY_FINGERPRINT --destination user@backup-host:/backups/
```

The output must not exist and must be outside the staging directory. Only regular
files/directories are accepted. A failed SCP leaves the encrypted local file for
manual retry. Staging exports and temporary tar files are plaintext: use private
storage; do not treat deletion as secure erasure on SSDs.

## Restore drill

```sh
python3 scripts/platform-backup.py restore /path/to/export.tar.gpg /path/to/new-restore-directory
```

Restore accepts only an absent or empty destination. It verifies GPG decryption
before reading the tar and rejects traversal, links and special files. It restores
file content, not original ownership or permissions; files are private to the
restoring user. Compare restored fixture checksums with the export, then validate
application behavior using an isolated replacement PVC before considering any
production-data replacement. This utility never imports data into a running app.

## Cluster recovery is separate

K3s's default single-server SQLite datastore needs a consistent backup: stop K3s
before copying `/var/lib/rancher/k3s/server/db`, then restart it. Back up the server
token `/var/lib/rancher/k3s/server/token` separately in encrypted recovery storage;
it is needed to decrypt confidential datastore content. Protect any configuration,
registry credentials and custom encryption configuration needed for reinstallation.
Do not tar a live SQLite datastore and assume it is valid. Restoring the datastore
does not restore application PVC contents. Record the installed K3s version and
restore into a matching version before attempting upgrades.

A successful local fixture round trip proves the encryption/restore mechanics.
An off-machine transfer, retrieval, application restore and controlled reboot
remain distinct checks requiring the actual destination and host access.
