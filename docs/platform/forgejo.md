# Private Forgejo and shared hostname gateway

The existing docs nginx also routes `git.spark.test` to Forgejo. Port 8088 and the
systemd forwarder remain unchanged. Docs/local hosts serve the docs; unknown
hostnames return 404. Backend access is restricted by Namespace/Pod/port policy.

## Build, check and deploy

Run from the normal Docker-enabled user account on the Spark, without `sudo make`:

```sh
make dev
make verify-forgejo-runtime
make forgejo-image-import
make deploy-services
make forgejo-status
```

`make dev` builds/verifies a native patched Forgejo LTS image, scans its actual
OCI contents, and runs the code gates. The official image candidates failed the
vulnerability gate; no CVE exceptions were added. The build uses checksum-pinned
official release sources with generated frontend assets and vendored dependencies,
patched Go for both binaries, and signed Alpine security updates. See
[build provenance](../../containers/forgejo/README.md).

The runtime verification uses only disposable Docker data: a random fixture
administrator, a private Git push/clone and a restart-persistence check. It never
creates an administrator in the real Kubernetes deployment or prints passwords.

The import command verifies the archive and scans it again before requesting sudo
for the fixed K3s image-import command. It does not modify the host firewall or
restart K3s. Registry pulling is disabled (`Never`): deployment refuses an image
that is missing from the target node or built for another architecture. Rebuilding
the image requires a new scan/import before rollout.

`make deploy-services` deploys Forgejo before refreshing the shared docs gateway.
Forgejo uses SQLite and one retained 20Gi local-path PVC. The 20Gi request is scheduling metadata, not a disk quota: the local-path provisioner does not enforce volume capacity limits. Repositories totaling about 100 GB can use the backing disk without resizing this claim, provided sufficient free space remains. This space is shared with other Spark workloads and is not reserved for Forgejo. Monitor the backing filesystem before large imports; do not delete or recreate the claim just to change its displayed capacity. See the [local-path provisioner capacity limitation](https://github.com/rancher/local-path-provisioner#cons). Its configuration,
database, repositories and generated keys live under `/var/lib/gitea`; the config
file is `/var/lib/gitea/custom/conf/app.ini`. No redundant config volume is needed.

## Access from the Mac

Keep NVIDIA Sync forwarding local 8088 to Spark 8088. Add to the Mac's `/etc/hosts`:

```text
127.0.0.1 docs.spark.test
127.0.0.1 git.spark.test
```

- [Docs](http://docs.spark.test:8088/)
- [Forgejo](http://git.spark.test:8088/)
- [Docs through localhost](http://localhost:8088/) remains available.

Complete Forgejo's private installation wizard and choose your own administrator
credentials before sharing access or relying on the service. Keep the prefilled
SQLite paths and external URL. The wizard writes its install lock to the retained
volume; it is not forced off by an environment variable on restart. Self-registration
and anonymous viewing remain disabled. This is a private HTTP service transported
through the encrypted Sync/SSH connection; do not expose it through public forwarding.

Git over HTTP is enabled after setup. Git SSH, package/OCI registry, Actions and
outbound migrations/webhooks/mail are initially disabled. Those need separate
authentication, TLS and outbound-network designs before enabling.

## Large Git pushes

The private Git hostname accepts HTTP request bodies up to 40 GiB per push;
this is a transfer limit, separate from total repository storage. Docs retain
their 256 MiB limit. The gateway streams uploads with HTTP/1.1 and request
buffering disabled, so Git packs do not need to fit in its temporary volume.

An HTTP 413 means a request exceeded the gateway limit. After deploying a fix,
retry the original `git push`; increasing client `http.postBuffer` does not fix
an enforced server request limit. A trailing `Everything up-to-date` after an RPC
failure does not prove success: compare `git rev-parse HEAD` with
`git ls-remote <remote> refs/heads/<branch>` for the branch being pushed.

## Rollback and data recovery

Record `helm ... history docs -n docs-dev` before gateway updates. Rolling back the
docs release restores its previous routing/content. Forgejo can be stopped without
touching the docs: scale its Deployment to zero in the explicit platform context.
Do not delete its namespace or PVC. Helm retains the claim on uninstall, but that
is not protection against namespace deletion, disk failure or accidental writes.

Back up the whole Forgejo data volume consistently with the server stopped,
encrypt exports and keep them off-machine. A cluster datastore backup does not
contain this volume. Off-machine backup is a separate required operating task;
deploying this chart does not configure a destination or promise data recovery.
