#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-help}
if [[ $action == help ]]; then
  echo 'Usage: postgres-service.sh {render|install} USER ADMINCONFIG'
  exit 0
fi
[[ $action == render || $action == install ]] || { echo 'Unknown action' >&2; exit 2; }
[[ $# -eq 3 ]] || { echo 'USER and ADMINCONFIG are required' >&2; exit 2; }
owner=$2
kubectl=/usr/local/bin/k3s
kubeconfig=$3
environment=dev
port=5432
if [[ $owner == root ]]; then
  echo 'Run make postgres-service-install as your normal login user, without sudo. The Make target requests sudo internally.' >&2
  exit 2
fi
[[ $owner =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || { echo 'Select a valid non-root Linux username' >&2; exit 2; }
for path in "$kubectl" "$kubeconfig"; do
  [[ $path =~ ^/[A-Za-z0-9_./-]+$ && $path != *'/../'* && $path != */.. ]] || { echo 'Paths must be absolute without spaces or systemd specifiers' >&2; exit 2; }
done
group=$(id -gn "$owner")
render() {
  sed -e "s|@USER@|$owner|g" -e "s|@GROUP@|$group|g" \
    "$repo/platform/postgres-access/spark-postgres-access.service.in"
}
if [[ $action == render ]]; then
  render
  cat "$repo/platform/postgres-access/spark-postgres-credentials.service.in" "$repo/platform/postgres-access/spark-postgres-credentials.timer.in"
  cat "$repo/platform/postgres-access/rbac.yaml"
  exit 0
fi
[[ $EUID == 0 ]] || { echo 'Installation requires sudo/root' >&2; exit 1; }
[[ -x $kubectl && -f $kubeconfig ]] || { echo 'kubectl and the dedicated kubeconfig must exist' >&2; exit 1; }
server=$(runuser -u "$owner" -- "$kubectl" kubectl --kubeconfig "$kubeconfig" --context spark-platform config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
[[ $server == https://127.0.0.1:6443 ]] || { echo 'Refusing unexpected Kubernetes API endpoint' >&2; exit 1; }
runuser -u "$owner" -- "$kubectl" kubectl --kubeconfig "$kubeconfig" --context spark-platform --request-timeout=10s get node spark-platform -o name >/dev/null
# Root never executes the user-selected kubectl or reads that user's kubeconfig.
# Validate every parent as well as the fixed privileged executable/config.
/usr/bin/python3 -I - <<'CHECK'
from pathlib import Path
for value in ['/usr/local/bin/k3s', '/etc/rancher/k3s/spark-platform.yaml',
              '/etc/systemd/system', '/etc', '/var/lib']:
    path = Path(value)
    for item in [path, *path.parents]:
        info = item.lstat()
        if item.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('Refusing untrusted root executable/configuration path')
if Path('/etc/rancher/k3s/spark-platform.yaml').stat().st_mode & 0o777 != 0o600:
    raise SystemExit('Root K3s kubeconfig must have mode 0600')
CHECK
for name in spark-postgres-access.service spark-postgres-credentials.service spark-postgres-credentials.timer; do
  unit=/etc/systemd/system/$name
  if [[ -L $unit ]] || { [[ -e $unit ]] && ! head -1 "$unit" | grep -Fxq '# Managed by TheDefiant postgres-service.sh'; }; then
    echo 'Refusing to overwrite an unmanaged unit' >&2; exit 1
  fi
  for base in /etc/systemd/system /run/systemd/system; do
    [[ ! -e $base/$name.d && ! -L $base/$name.d ]] || { echo 'Existing unit overrides require review' >&2; exit 1; }
  done
done
/usr/bin/python3 -I "$repo/platform/postgres-access/check-install.py" "$(id -u "$owner")" "$(id -g "$owner")"
stage=$(mktemp -d)
update=''
trap 'rm -rf -- "$stage"; if [[ -n $update ]]; then rm -rf -- "$update"; fi' EXIT
chmod 0700 "$stage"
render > "$stage/spark-postgres-access.service"
cat "$repo/platform/postgres-access/rbac.yaml" > "$stage/rbac.yaml"
# Fail closed on upgrades: after validation, never leave the old admin forward running.
# Stop only units whose existing on-disk managed marker was checked above.
for name in spark-postgres-credentials.timer spark-postgres-credentials.service spark-postgres-access.service; do
  if [[ -f /etc/systemd/system/$name ]]; then
    systemctl stop "$name"
  fi
done
/usr/local/bin/k3s kubectl --kubeconfig /etc/rancher/k3s/spark-platform.yaml \
  --context default --server=https://127.0.0.1:6443 --request-timeout=30s apply -f "$stage/rbac.yaml"
install -d -m 0700 /etc/spark-postgres-access
install -d -m 0711 /var/lib/spark-postgres-access
update=$(mktemp -d /etc/spark-postgres-access/.update.XXXXXX)
install -m 0700 "$repo/platform/postgres-access/refresh-credentials.py" "$update/refresh-credentials.py"
printf '{"managed":"TheDefiant postgres-service.sh","environment":"%s","uid":%s,"gid":%s}\n' "$environment" "$(id -u "$owner")" "$(id -g "$owner")" > "$stage/config.json"
install -m 0600 "$stage/config.json" "$update/config.json"
mv -f -- "$update/refresh-credentials.py" /etc/spark-postgres-access/refresh-credentials.py
mv -f -- "$update/config.json" /etc/spark-postgres-access/config.json
rmdir -- "$update"
update=''
install -m 0644 "$stage/spark-postgres-access.service" /etc/systemd/system/spark-postgres-access.service
for name in spark-postgres-credentials.service spark-postgres-credentials.timer; do
  install -m 0644 "$repo/platform/postgres-access/$name.in" "/etc/systemd/system/$name"
done
systemctl daemon-reload
systemctl enable spark-postgres-access.service spark-postgres-credentials.timer
systemctl start spark-postgres-credentials.service
systemctl restart spark-postgres-access.service
systemctl start spark-postgres-credentials.timer
echo "Enabled scoped postgres-dev access on 127.0.0.1:$port with automatic credential renewal."
