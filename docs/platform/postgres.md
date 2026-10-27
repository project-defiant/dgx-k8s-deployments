# Private development PostgreSQL

This service is PostgreSQL 18.6 on the Spark's K3s cluster. Namespace/release:
`postgres-dev`; Deployment/Service: `postgres`; retained PVC: `postgres-data` (20Gi).
The local-path request is metadata, not a hard disk quota. Data survives pod
replacement and Helm uninstall; keep off-machine backups for disk/host failures.

## Bring up the service

Run from the PostgreSQL feature checkout as the ordinary login user:

```sh
make dev
make postgres-image-build
make verify-postgres-runtime
make postgres-up
```

The image is derived from the checksum-pinned official PostgreSQL 18 Alpine image.
Signed package updates fix its old runtime packages; its unused gosu privilege
switcher is removed entirely. The postgres OS account is mapped to UID/GID1000
and the official entrypoint runs non-root. Database binaries and initialization
behavior remain official. The final image must pass actual-package coverage and
zero unapproved HIGH/CRITICAL vulnerabilities. Native inputs currently cover ARM64.

`postgres-up` imports the verified image (sudo), deploys the database and installs
a scoped systemd forwarder (sudo) for **127.0.0.1:5432**. It does not expose a
NodePort, hostPort, LoadBalancer or public listener. The database comes back via
K3s; the forwarder starts after K3s and refreshes its namespace-scoped token.
No administrator kubeconfig is given to the long-running forwarder.

For a foreground forward instead of boot installation, use `make postgres-access`.
PostgreSQL uses its own TCP port; it cannot use the HTTP Host routing on port8088.
On a Mac, add a Sync mapping Mac5432 to Spark5432 to use the same localhost details.

## Connection details

- Host: `127.0.0.1`
- Port: `5432`
- Database: `dev`
- Username: `dev`
- Password: run `make postgres-password` explicitly in your private terminal.

`make postgres-connection` creates a mode0600 pgpass file under ignored
`.dev-tools/postgres/client/` and prints a psql command without displaying the
password. SQL GUI clients can use the same host/port/database/user and password.
No host psql package is required for `make postgres-shell`: it opens the bundled
psql client in the database pod using the development role.

The development role can create databases and owns `dev`, but is not a superuser,
cannot create roles and cannot replicate. Administrative credentials are retained
separately in the `postgres-credentials` Kubernetes Secret. Passwords are random,
not repository defaults. Initialization uses password files and SCRAM for TCP.
Local Unix-socket administrator access uses peer authentication inside the pod.

Credentials are created once and never silently rotated on redeploy. Existing data
with missing credentials blocks deployment: restore the Secret instead of creating
new passwords that would not match initialized storage. Changing an init script or
Secret does not change roles in an existing database; use an intentional SQL change.
Major upgrades require a separate reviewed data migration; the chart pins major18.

## Other K3s applications

Internal address: `postgres.postgres-dev.svc.cluster.local:5432`.
Network access requires BOTH labels:

```sh
kubectl --kubeconfig "$HOME/.kube/spark-platform.yaml" --context spark-platform \
  label namespace YOUR_NAMESPACE platform.thedefiant.io/postgres-access=true
```

Set `platform.thedefiant.io/postgres-client: "true"` in the client Deployment's
Pod-template labels. Supply the development password securely to that app; the
network label alone is not database authentication. The database has no outbound
network access. Host port-forwarding remains available without these labels.

## Operation and verification

- `make postgres-status`: pod, service, PVC and policy state.
- `make postgres-service-status`: boot-time listener and credential-renewal timer.
- `make status`: cluster-wide overview, including the database.
- `make verify-postgres-runtime`: disposable initialization, localhost SCRAM login,
  wrong-password refusal, dev-role privileges and restart-persistence tests.

The root forwarder installer refuses foreign units, drop-ins, unsafe configuration
paths and changed identities. It installs only its PostgreSQL-specific files and
RBAC. Existing docs/Forgejo/Ollama services are not modified. The database listens
on private pod networking; the external tunnel is loopback-only. PostgreSQL TLS
is not configured: keep remote access inside the existing encrypted Sync/SSH VPN.
