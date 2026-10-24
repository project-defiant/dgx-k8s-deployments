# Bootstrap the single Spark

## Review and validate

The installer supports this one ARM64 systemd host. It downloads a pinned K3s
ARM64 release and verifies both the reviewed binary hash and the official
release checksum manifest. It does not execute a downloaded shell installer,
install packages, replace Docker or modify Tailscale configuration.

```sh
make bootstrap-check
make validate test
```

Complete the [preservation review](preflight.md) first. The validation command
cannot establish whether old application data is important or backed up. The
installation flags acknowledge an actual review, not a way to bypass it.

## Install

From this checkout on the Spark, in a normal terminal with sudo access:

```sh
sudo ./scripts/platform-bootstrap.sh --install --preservation-reviewed --network-reviewed
```

This writes the K3s binary, dedicated systemd service, root-only configuration,
and a narrowly scoped firewall helper; enables K3s at boot; and creates a named
kubeconfig. Existing different K3s configuration is refused. Review exact paths
and version in the script before execution. No credentials belong in Git.

The API must be reachable by cluster pods, so it cannot listen exclusively on
localhost. The firewall helper restricts TCP 6443/10250 and UDP 8472 to local
cluster traffic; it does not flush unrelated firewall rules. The service applies
these restrictions before startup, for IPv4 and IPv6. External clients use SSH
rather than connecting directly to the Kubernetes API. Multi-node expansion
requires a deliberate networking change.

Copy the dedicated administrator kubeconfig into your account, without changing
the default kubeconfig or granting world access:

```sh
install -d -m 700 "$HOME/.kube"
sudo install -m 600 -o "$(id -u)" -g "$(id -g)" \
  /etc/rancher/k3s/spark-platform-context.yaml "$HOME/.kube/spark-platform.yaml"
kubectl --kubeconfig "$HOME/.kube/spark-platform.yaml" --context spark-platform get nodes
```

Keep that file private: it grants cluster-admin access. After K3s certificate
rotation, refresh this copy from the root-generated kubeconfig using the
bootstrap's named-context generation procedure. Do not assume a stale copy
will renew itself.

The named copy normalizes the server to `https://127.0.0.1:6443`. For a copy made
by the earlier bootstrap, repair only this dedicated client config with
`kubectl --kubeconfig "$HOME/.kube/spark-platform.yaml" config set-cluster spark-platform --server=https://127.0.0.1:6443`.

## First deployment

```sh
make deploy-docs
make deploy-docs ENV=prod
make status
make access
```

On your client, use the SSH forwarding command in the main README. Check the
site and a second independent SSH session before considering access verified.

## Recovery from a failed installation

Inspect `sudo journalctl -u k3s` and `sudo systemctl status k3s`. Stop only the
new service if required: `sudo systemctl stop k3s`. Do not run a blanket network
reset, Docker prune or K3s uninstall while data is being investigated.

Only after K3s is stopped, the project's firewall restrictions can be removed
using `sudo /usr/local/sbin/spark-platform-firewall --remove`. This does not
remove K3s-created CNI/iptables configuration or stored data; it is not a full
uninstall. Preserve the datastore, token and volumes before any later uninstall.

The exact release's supported configuration is documented at
https://docs.k3s.io/cli/server and its release is linked in the installer.
